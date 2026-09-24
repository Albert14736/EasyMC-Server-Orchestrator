import os
import re
import sys
import json
import hashlib
import zipfile

import requests

try:  # Python 3.11+；老版本没有 tomllib 时只跳过 mods.toml 的 MC 版本范围检查
    import tomllib as _toml
except ImportError:  # pragma: no cover
    _toml = None


_UA = "HMSL/0.1 mod-downloader"

# 服务端 loader → Modrinth 上能在它上面跑的 loader 标签。必须精确匹配，不能做子串
# 比较（"forge" in "neoforge" 为 True，会把 NeoForge 专属 jar 装进 Forge 服）。
# Paper 能跑 Spigot/Bukkit 插件；只标了 purpur/folia 的不保证能在 Paper 上跑。
_ACCEPTED_LOADERS = {
    "fabric": ["fabric"],
    "quilt": ["quilt", "fabric"],
    "forge": ["forge"],
    "neoforge": ["neoforge"],
    "paper": ["paper", "spigot", "bukkit"],
    "purpur": ["purpur", "paper", "spigot", "bukkit"],
    "spigot": ["spigot", "bukkit"],
    "bukkit": ["bukkit"],
    "folia": ["folia"],
}

# MOD_DATABASE.md「适用平台」列里，哪些标记算是给这种服务端用的
_ROW_TAGS = {
    "fabric": {"fabric"},
    "quilt": {"quilt", "fabric"},
    "forge": {"forge"},
    "neoforge": {"neoforge"},
    "paper": {"paper", "spigot", "bukkit"},
    "purpur": {"purpur", "paper", "spigot", "bukkit"},
    "spigot": {"spigot", "bukkit"},
    "bukkit": {"bukkit"},
    "folia": {"folia"},
}

# 一个条目最多尝试几个候选版本（Modrinth 按发布时间新→旧返回）
_MAX_CANDIDATES = 6
# 连续几次连不上网络就放弃剩余条目，免得每条都干等超时
_MAX_NET_ERRORS = 2

_VERSION_NUM_RE = re.compile(r"^\d+(?:\.\d+)*$")


def _say(msg, end="\n"):
    """print 的安全版：GBK 控制台 / 管道遇到 emoji 等字符时不让整个同步崩掉。"""
    try:
        print(msg, end=end, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(msg.encode(enc, "replace").decode(enc, "replace"), end=end, flush=True)
    except (OSError, ValueError, AttributeError):
        pass


def _required_java(mc_version):
    """复用 server_factory 的 Java 映射；它万一导入失败就用一份保守的兜底。"""
    try:
        from core.server_factory import required_java_version
        return int(required_java_version(mc_version))
    except Exception:
        nums = _ver_tuple(mc_version) or (1, 21)
        if nums[0] >= 26:   # 2026 起的新版本号（26.1 …）要求 Java 25
            return 25
        if nums[0] != 1:
            return 21
        minor = nums[1] if len(nums) > 1 else 0
        patch = nums[2] if len(nums) > 2 else 0
        if minor > 20 or (minor == 20 and patch >= 5):
            return 21
        if minor >= 18:
            return 17
        return 16 if minor == 17 else 8


def _spec_java_floor(target_dir):
    """已有服务器（mods/ 或 plugins/ 的上一级）里 hmsl_launch.json 记录的最低 Java；
    新建服务器同步时它还没写出来，返回 None。"""
    spec = os.path.join(os.path.dirname(os.path.abspath(target_dir)), "hmsl_launch.json")
    try:
        with open(spec, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        v = data.get("java_min") if isinstance(data, dict) else None
        return int(v) if isinstance(v, (int, str)) and str(v).isdigit() else None
    except (OSError, ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# 版本号 / 版本范围工具（只覆盖 fabric.mod.json / quilt.mod.json / mods.toml 里
# 常见的写法；解析不了一律返回 None = "不确定"，调用方按兼容处理）
# ---------------------------------------------------------------------------

def _ver_tuple(v):
    """'1.20.1' -> (1, 20, 1)；不是纯数字版本返回 None。"""
    if v is None:
        return None
    v = str(v).strip()
    if not _VERSION_NUM_RE.match(v):
        return None
    return tuple(int(x) for x in v.split("."))


def _cmp(a, b):
    n = max(len(a), len(b))
    a = tuple(a) + (0,) * (n - len(a))
    b = tuple(b) + (0,) * (n - len(b))
    return (a > b) - (a < b)


def _split_semver(s):
    """'1.20.1-rc.1+build' -> ([1,20,1], True, None)；'1.20.x' -> ([1,20], False, 2)。
    返回 (数字列表, 是否带预发布标签, 通配符位置 or None)；解析不了返回 None。"""
    s = s.split("+", 1)[0]
    pre = False
    if "-" in s:
        s, _rest = s.split("-", 1)
        pre = True
    nums = []
    for i, part in enumerate(s.split(".")):
        if part in ("x", "X", "*"):
            return nums, pre, i
        if not part.isdigit():
            return None
        nums.append(int(part))
    if not nums:
        return None
    return nums, pre, None


def _semver_term_ok(term, target):
    """单个 Fabric 版本谓词项，例如 '>=1.20'、'~1.20.1'、'1.20.x'、'*'。"""
    term = term.strip()
    if term in ("", "*"):
        return True
    m = re.match(r"^(>=|<=|>|<|=|~|\^)?\s*(.+)$", term)
    if not m:
        return None
    op, ver = m.group(1) or "", m.group(2)
    parsed = _split_semver(ver)
    if parsed is None:
        return None
    nums, pre, wild = parsed
    if wild is not None:
        if op in ("", "="):
            return list(target[:len(nums)]) == nums if nums else True
        # 带运算符的通配符很少见，按 .0 处理
    c = _cmp(target, nums)
    if op in ("", "="):
        return c == 0 and not pre
    if op == ">=":
        return c >= 0
    if op == ">":
        return c >= 0 if pre else c > 0
    if op == "<=":
        return c < 0 if pre else c <= 0
    if op == "<":
        return c < 0
    if op == "~":
        upper = [nums[0] + 1] if len(nums) == 1 else [nums[0], nums[1] + 1]
        return c >= 0 and _cmp(target, upper) < 0
    if op == "^":
        return c >= 0 and _cmp(target, [nums[0] + 1]) < 0
    return None


def _semver_pred_ok(pred, target):
    """Fabric 风格谓词：字符串里空格分隔 = 且；列表 = 或。True/False/None(不确定)。"""
    if pred is None:
        return True
    if isinstance(pred, list):
        if not pred:
            return True
        results = [_semver_pred_ok(p, target) for p in pred]
        if any(r is True for r in results):
            return True
        return None if any(r is None for r in results) else False
    if isinstance(pred, dict):  # quilt: {"any": [...]} / {"all": [...]}
        if "any" in pred:
            return _semver_pred_ok(list(pred["any"]), target)
        if "all" in pred:
            results = [_semver_pred_ok(p, target) for p in pred["all"]]
            if any(r is False for r in results):
                return False
            return None if any(r is None for r in results) else True
        return None
    if not isinstance(pred, str):
        return None
    ok = True
    for term in pred.split():
        r = _semver_term_ok(term, target)
        if r is False:
            return False
        if r is None:
            ok = None
    return ok


def _maven_range_ok(spec, target):
    """mods.toml 的 versionRange，例如 '[1.20.1,1.21)'、'[1.20,)'、'[1.20.1]'。"""
    spec = (spec or "").strip()
    if not spec or spec == "*":
        return True
    if spec[0] not in "[(":
        return True  # 裸版本号在 Maven 里只是"推荐"，不是硬限制
    ranges = re.findall(r"[\[\(][^\]\)]*[\]\)]", spec)
    if not ranges:
        return None
    unsure = False
    for r in ranges:
        lo_inc, hi_inc, inner = r[0] == "[", r[-1] == "]", r[1:-1]
        if "," not in inner:
            v = _split_semver(inner.strip())
            if v is None or v[2] is not None:
                unsure = True
                continue
            if _cmp(target, v[0]) == 0 and not v[1]:
                return True
            continue
        lo, hi = (x.strip() for x in inner.split(",", 1))
        ok = True
        if lo:
            v = _split_semver(lo)
            if v is None:
                unsure = True
                continue
            c = _cmp(target, v[0])
            ok = ok and (c >= 0 if (lo_inc or v[1]) else c > 0)
        if hi:
            v = _split_semver(hi)
            if v is None:
                unsure = True
                continue
            c = _cmp(target, v[0])
            ok = ok and (c < 0 if (v[1] or not hi_inc) else c <= 0)
        if ok:
            return True
    return None if unsure else False


def _min_java_for(pred):
    """给提示用：满足谓词的最小 Java 大版本（8..40），找不到返回 None。"""
    for j in range(8, 41):
        if _semver_pred_ok(pred, (j,)) is True:
            return j
    return None


# ---------------------------------------------------------------------------
# jar 内容检查
# ---------------------------------------------------------------------------

def _read_text(zf, name):
    with zf.open(name) as f:
        return f.read().decode("utf-8-sig", errors="replace")


def _own_classes(zf):
    """jar 自身的 class（跳过 META-INF/，含 multi-release 的 versions/N/，以及 module-info）。"""
    return [i for i in zf.infolist()
            if i.filename.endswith(".class") and not i.filename.startswith("META-INF/")
            and not i.filename.endswith("module-info.class")]


def _class_java(zf, info_or_name):
    """单个 class 文件要求的 Java 大版本（major - 44）；读不到返回 None。"""
    try:
        with zf.open(info_or_name) as f:
            head = f.read(8)
    except Exception:
        return None
    if len(head) == 8 and head[:4] == b"\xca\xfe\xba\xbe":
        major = int.from_bytes(head[6:8], "big")
        return major - 44 if major >= 45 else None
    return None


def _max_class_java(zf):
    """jar 自身所有 class 里要求最高的 Java 大版本（诊断用）。"""
    vals = [v for v in (_class_java(zf, i) for i in _own_classes(zf)) if v]
    return max(vals) if vals else None


def _dominant_class_java(zf, share=0.25):
    """没有入口类可查时的兜底：至少 `share` 比例的 class 都满足不了的最低 Java 版本。
    多平台 jar（如 TAB 6：主体 Java 8，另带给 26.x 用的 Java 25 模块）里零星的
    高版本 class 只在对应平台上才加载，不能据此判定整个 jar 不兼容。"""
    vals = sorted((v for v in (_class_java(zf, i) for i in _own_classes(zf)) if v), reverse=True)
    if not vals:
        return None
    need = max(1, int(len(vals) * share))
    return vals[need - 1]


def _cls_path(name):
    name = str(name).split("::", 1)[0].strip()
    return name.replace(".", "/") + ".class" if name else None


def _mixin_classes(zf, names, config_names, skip_client=True):
    """mixin 配置（*.mixins.json）里服务端也会加载的类：mixins / server / plugin。"""
    out = []
    for cfg in config_names:
        if cfg not in names:
            continue
        try:
            data = json.loads(_read_text(zf, cfg))
        except (ValueError, OSError, KeyError):
            continue
        if not isinstance(data, dict):
            continue
        pkg = str(data.get("package") or "").strip(".")
        keys = ["mixins", "server"] if skip_client else ["mixins", "server", "client"]
        for k in keys:
            for m in data.get(k) or []:
                if isinstance(m, str) and m:
                    out.append(f"{pkg}.{m}" if pkg else m)
        if isinstance(data.get("plugin"), str):
            out.append(data["plugin"])
    return out


_FORGE_MARKERS = (b"Lnet/minecraftforge/fml/common/Mod;",
                  b"Lnet/minecraftforge/fml/common/Mod$EventBusSubscriber;")
_NEOFORGE_MARKERS = (b"Lnet/neoforged/fml/common/Mod;",
                     b"Lnet/neoforged/fml/common/Mod$EventBusSubscriber;",   # NeoForge 20.2–20.6
                     b"Lnet/neoforged/fml/common/EventBusSubscriber;")      # NeoForge 21+


def _mod_markers(loader_key, mc):
    """这个 loader 真正会扫描的 @Mod / @EventBusSubscriber 注解。多平台 jar（如 TAB）
    同时带 ForgeTAB 和 NeoForgeTAB，另一平台的入口类不会被加载，不能算进 Java 要求。
    NeoForge 1.20.1 还是 Forge 的包名（net.minecraftforge）。"""
    if loader_key == "forge":
        return _FORGE_MARKERS
    if mc is None:
        return _FORGE_MARKERS + _NEOFORGE_MARKERS
    if _cmp(mc, (1, 20, 2)) < 0:
        return _FORGE_MARKERS
    return _NEOFORGE_MARKERS


def _toml_mixin_configs(zf, names, loader_key, mc):
    """mods.toml / neoforge.mods.toml 里 [[mixins]] config = "x.mixins.json" 声明的配置。"""
    if _toml is None:
        return []
    if loader_key == "neoforge" and not (mc is not None and _cmp(mc, (1, 20, 2)) < 0):
        toml_name = "META-INF/neoforge.mods.toml"
        if toml_name not in names:
            toml_name = "META-INF/mods.toml"
    else:
        toml_name = "META-INF/mods.toml"
    if toml_name not in names:
        return []
    try:
        data = _toml.loads(_read_text(zf, toml_name))
    except Exception:
        return []
    out = []
    for m in data.get("mixins") or []:
        if isinstance(m, dict) and isinstance(m.get("config"), str):
            out.append(m["config"])
    return out


def _entry_classes(zf, names, loader_key, mc=None):
    """服务端启动时一定会加载的类：插件主类 / Fabric 入口与 mixin / Forge 的 @Mod 类。
    mc 是 (1, 20, 1) 这样的元组或 None，用来区分 NeoForge 1.20.1（Forge 包名）和新版。"""
    entries = []
    if loader_key in ("paper", "purpur", "folia", "spigot", "bukkit"):
        for yml in ("paper-plugin.yml", "plugin.yml"):
            if yml in names:
                text = _read_text(zf, yml)
                for key in ("main", "bootstrapper", "loader"):
                    m = re.search(rf"^{key}\s*:\s*['\"]?([\w.$]+)", text, re.M)
                    if m:
                        entries.append(m.group(1))
    elif loader_key in ("fabric", "quilt"):
        meta = None
        if loader_key == "quilt" and "quilt.mod.json" in names:
            try:
                q = json.loads(_read_text(zf, "quilt.mod.json"))
                meta = {"entrypoints": (q.get("quilt_loader") or {}).get("entrypoints") or {},
                        "mixins": q.get("mixin") or []}
            except (ValueError, OSError, AttributeError):
                meta = None
        if meta is None and "fabric.mod.json" in names:
            try:
                meta = json.loads(_read_text(zf, "fabric.mod.json"))
            except (ValueError, OSError):
                meta = None
        if isinstance(meta, dict):
            eps = meta.get("entrypoints") or {}
            if isinstance(eps, dict):
                for kind in ("main", "server", "preLaunch", "init", "server_init", "pre_launch"):
                    vals = eps.get(kind) or []
                    for v in (vals if isinstance(vals, list) else [vals]):
                        if isinstance(v, dict):
                            v = v.get("value")
                        if isinstance(v, str):
                            entries.append(v)
            cfgs = []
            mixins = meta.get("mixins") or []
            for m in (mixins if isinstance(mixins, list) else [mixins]):
                if isinstance(m, str):
                    cfgs.append(m)
                elif isinstance(m, dict) and m.get("environment", "*") != "client" and m.get("config"):
                    cfgs.append(m["config"])
            entries += _mixin_classes(zf, names, cfgs)
    elif loader_key in ("forge", "neoforge"):
        markers = _mod_markers(loader_key, mc)
        classes = _own_classes(zf)
        if len(classes) <= 20000:
            for info in classes:
                try:
                    with zf.open(info) as f:
                        data = f.read()
                except Exception:
                    continue
                if any(mk in data for mk in markers):
                    entries.append(info.filename[:-6])
        cfgs = []
        try:
            mf = _read_text(zf, "META-INF/MANIFEST.MF") if "META-INF/MANIFEST.MF" in names else ""
            m = re.search(r"^MixinConfigs:\s*(.+)$", mf, re.M)
            if m:
                cfgs += [c.strip() for c in m.group(1).split(",") if c.strip()]
        except OSError:
            pass
        cfgs += [c for c in _toml_mixin_configs(zf, names, loader_key, mc) if c not in cfgs]
        entries += _mixin_classes(zf, names, cfgs)
    paths = []
    for e in entries:
        p = _cls_path(e)   # "a.b.C" 和 "a/b/C" 两种写法都行
        if p and p in names:
            paths.append(p)
    return paths


def _required_class_java(zf, names, loader_key, mc=None):
    """jar 在这个 loader 上实际会加载的 class 要求的 Java 版本：优先看入口类 / mixin，
    找不到入口就按多数 class 的编译目标兜底。"""
    entry = _entry_classes(zf, names, loader_key, mc)
    vals = [v for v in (_class_java(zf, p) for p in entry) if v]
    if vals:
        return max(vals)
    return _dominant_class_java(zf)


def inspect_jar(path, loader_key, mc_version, java_major):
    """检查下载下来的 jar 能不能在 (loader, MC 版本, Java 大版本) 的服务端上加载。
    返回 None 表示没发现问题，否则返回中文原因。"""
    mc = _ver_tuple(mc_version)
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        return "下载的文件不是有效的 jar"
    with zf:
        names = set(zf.namelist())
        fabric_meta = "fabric.mod.json" in names
        quilt_meta = "quilt.mod.json" in names
        forge_meta = "META-INF/mods.toml" in names
        neo_meta = "META-INF/neoforge.mods.toml" in names
        plugin_meta = "plugin.yml" in names
        paper_plugin_meta = "paper-plugin.yml" in names

        # 1) 是不是给这个 loader 的 jar
        wrong = None
        if loader_key == "fabric" and not fabric_meta:
            wrong = "不是 Fabric 模组"
        elif loader_key == "quilt" and not (quilt_meta or fabric_meta):
            wrong = "不是 Quilt/Fabric 模组"
        elif loader_key == "forge":
            if not (forge_meta or "mcmod.info" in names):
                if mc is None or _cmp(mc, (1, 13)) >= 0 or fabric_meta or neo_meta or plugin_meta:
                    wrong = "不是 Forge 模组"
        elif loader_key == "neoforge":
            old_neo = mc is not None and _cmp(mc, (1, 20, 5)) < 0
            if not (neo_meta or (old_neo and forge_meta)):
                wrong = "不是 NeoForge 模组"
        elif loader_key in ("paper", "purpur", "folia"):
            if not (plugin_meta or paper_plugin_meta):
                wrong = "不是 Bukkit/Paper 插件"
        elif loader_key in ("spigot", "bukkit") and not plugin_meta:
            wrong = "不是 Bukkit 插件"
        if wrong:
            return wrong

        # 2) 元数据里声明的 Java / Minecraft 要求
        java_t = (int(java_major),) if java_major else None
        if loader_key in ("fabric", "quilt") and (fabric_meta or quilt_meta):
            deps = {}
            try:
                if quilt_meta and loader_key == "quilt":
                    q = json.loads(_read_text(zf, "quilt.mod.json"))
                    for d in (q.get("quilt_loader", {}) or {}).get("depends", []) or []:
                        if isinstance(d, dict) and d.get("id") in ("java", "minecraft"):
                            if not d.get("optional"):
                                deps[d["id"]] = d.get("versions", "*")
                elif fabric_meta:
                    deps = json.loads(_read_text(zf, "fabric.mod.json")).get("depends", {}) or {}
            except (ValueError, KeyError, OSError, AttributeError):
                deps = {}
            if java_t and isinstance(deps, dict) and _semver_pred_ok(deps.get("java"), java_t) is False:
                need = _min_java_for(deps.get("java"))
                return f"需要 Java {need or '更高版本'}（本服务端用 Java {java_major}）"
            if mc and isinstance(deps, dict) and _semver_pred_ok(deps.get("minecraft"), mc) is False:
                return f"不支持 Minecraft {mc_version}（要求 {deps.get('minecraft')}）"

        if loader_key in ("forge", "neoforge") and mc and _toml is not None:
            toml_name = ("META-INF/neoforge.mods.toml"
                         if loader_key == "neoforge" and neo_meta else "META-INF/mods.toml")
            if toml_name in names:
                try:
                    data = _toml.loads(_read_text(zf, toml_name))
                except Exception:
                    data = {}
                for dep_list in (data.get("dependencies") or {}).values():
                    if not isinstance(dep_list, list):
                        continue
                    for dep in dep_list:
                        if not isinstance(dep, dict) or dep.get("modId") != "minecraft":
                            continue
                        required = dep.get("mandatory", True) is True or dep.get("type") == "required"
                        if dep.get("type") in ("optional", "incompatible", "discouraged"):
                            required = False
                        rng = dep.get("versionRange", "")
                        if required and _maven_range_ok(rng, mc) is False:
                            return f"不支持 Minecraft {mc_version}（要求 {rng}）"

        if loader_key in ("paper", "purpur", "folia", "spigot", "bukkit") and mc:
            for yml in ("paper-plugin.yml", "plugin.yml"):
                if yml not in names:
                    continue
                try:
                    text = _read_text(zf, yml)
                except OSError:
                    continue
                m = re.search(r"^api-version\s*:\s*['\"]?([0-9][0-9.]*)", text, re.M)
                api = _ver_tuple(m.group(1).rstrip(".")) if m else None
                if api and _cmp(api, mc) > 0:
                    return f"插件要求服务端 API {m.group(1)}，高于 {mc_version}"
                break

        # 3) class 文件实际编译目标（Forge/插件的元数据里没有 Java 字段）。只看启动时
        #    一定会加载的入口类 / mixin，多平台 jar 里给别的平台用的高版本 class 不算。
        if java_major:
            try:
                need = _required_class_java(zf, names, loader_key, mc)
            except Exception:
                need = None
            if need and need > int(java_major):
                return f"需要 Java {need}（本服务端用 Java {java_major}）"
    return None


class ModDownloader:
    def __init__(self, db_path):
        self.db_path = db_path
        self.api_base = "https://api.modrinth.com/v2/project"
        self._session = None

    # ---------- HTTP ----------

    @property
    def session(self):
        if self._session is None:
            s = requests.Session()
            s.headers["User-Agent"] = _UA
            try:  # 代理 / CDN 偶发断连时自动重试两次，别直接判失败
                from urllib3.util.retry import Retry
                retry = Retry(total=2, connect=2, read=0, backoff_factor=0.5,
                              status_forcelist=(502, 503, 504), raise_on_status=False)
                adapter = requests.adapters.HTTPAdapter(max_retries=retry)
                s.mount("https://", adapter)
                s.mount("http://", adapter)
            except Exception:
                pass
            self._session = s
        return self._session

    @staticmethod
    def _loader_key(loader):
        return (loader or "").strip().lower()

    def get_candidate_versions(self, project_id, mc_version, loader):
        """返回 Modrinth 上 **精确** 支持 mc_version、且 loader 标签能在该服务端上
        运行的版本列表（release 优先，其次 beta、alpha；同类型内新→旧）。
        网络 / API 出错时抛 requests.RequestException / ValueError。"""
        accepted = _ACCEPTED_LOADERS.get(self._loader_key(loader))
        if not accepted or not mc_version:
            return []
        params = {
            "game_versions": json.dumps([mc_version]),
            "loaders": json.dumps(accepted),
        }
        url = f"{self.api_base}/{project_id}/version"
        r = self.session.get(url, params=params, timeout=15)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise ValueError("Modrinth 返回格式异常")
        acc = set(accepted)
        matched = [v for v in data
                   if isinstance(v, dict)
                   and mc_version in (v.get("game_versions") or [])
                   and acc & set(v.get("loaders") or [])
                   and any(str(f.get("filename", f.get("url", ""))).lower().endswith(".jar")
                           for f in (v.get("files") or []))]
        order = {"release": 0, "beta": 1, "alpha": 2}
        # sorted 是稳定排序，同类型内保留 Modrinth 的新→旧顺序
        return sorted(matched, key=lambda v: order.get(v.get("version_type"), 3))

    @staticmethod
    def _jar_files(version):
        """版本里的 jar 文件，primary 优先；源码 / 开发包排除。"""
        files = [f for f in (version.get("files") or [])
                 if str(f.get("filename", f.get("url", ""))).lower().endswith(".jar")
                 and not re.search(r"-(sources|dev|javadoc)\.jar$", str(f.get("filename", "")).lower())]
        files.sort(key=lambda f: 0 if f.get("primary") else 1)
        return files

    def get_download_url(self, project_id, mc_version, loader):
        """兼容旧接口：返回第一个候选版本主文件的 URL（不做 jar 内容检查），没有则 None。"""
        try:
            for v in self.get_candidate_versions(project_id, mc_version, loader):
                files = self._jar_files(v)
                if files:
                    return files[0].get("url")
        except (requests.RequestException, ValueError) as e:
            _say(f"  [API 错误] {project_id}: {e}")
        return None

    def _download(self, file_info, dest, attempts=3):
        """下载到 dest（调用方传 .part 路径）。校验 HTTP 状态、sha1、zip 头。
        传输中途断线会重试；最终失败抛异常（RequestException / OSError / ValueError），
        并删除半截文件。"""
        for i in range(attempts):
            try:
                return self._download_once(file_info, dest)
            except (requests.ConnectionError, requests.Timeout,
                    requests.exceptions.ChunkedEncodingError):
                if i == attempts - 1:
                    raise

    def _download_once(self, file_info, dest):
        url = file_info.get("url")
        if not url:
            raise ValueError("缺少下载地址")
        want_sha1 = ((file_info.get("hashes") or {}).get("sha1") or "").lower()
        h = hashlib.sha1()
        try:
            with self.session.get(url, stream=True, timeout=(15, 60)) as r:
                if r.status_code != 200:
                    raise ValueError(f"HTTP {r.status_code}")
                with open(dest, "wb") as out:
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk:
                            out.write(chunk)
                            h.update(chunk)
            if want_sha1 and h.hexdigest() != want_sha1:
                raise ValueError("文件校验失败（sha1 不一致）")
            if not zipfile.is_zipfile(dest):
                raise ValueError("下载内容不是 jar（可能是错误页面）")
        except BaseException:
            try:
                os.remove(dest)
            except OSError:
                pass
            raise

    # ---------- MOD_DATABASE.md ----------

    @staticmethod
    def _columns_from_header(cells):
        """根据表头找 名称 / ID / 平台 三列的位置；认不出就用 0/1/2。"""
        name_i, id_i, plat_i = 0, 1, 2
        if cells:
            for i, c in enumerate(cells):
                low = c.lower()
                if "modrinth" in low or low in ("id", "项目 id", "项目id"):
                    id_i = i
                elif "平台" in c or "platform" in low or "loader" in low:
                    plat_i = i
                elif "名称" in c or "name" in low:
                    name_i = i
        return name_i, id_i, plat_i

    def parse_database(self):
        """解析 MOD_DATABASE.md 里的 Markdown 表格 → [(名称, Modrinth ID, 平台标记集合)]。
        只认表头分隔行（| :--- |）之后的行，表头本身不会被当成条目。"""
        with open(self.db_path, "r", encoding="utf-8-sig") as f:
            lines = f.readlines()
        rows = []
        cols = None
        prev = None
        for raw in lines:
            s = raw.strip()
            if not s.startswith("|"):
                cols, prev = None, None
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if any(cells) and all(re.fullmatch(r":?-{3,}:?", c) for c in cells if c):
                cols = self._columns_from_header(prev)
                continue
            if cols is None:
                prev = cells
                continue
            name_i, id_i, plat_i = cols
            if len(cells) <= max(name_i, id_i, plat_i):
                continue
            mod_id = cells[id_i].strip("`").strip()
            if not mod_id:
                continue
            tags = {t for t in re.split(r"[\s/,，、+&|]+", cells[plat_i].lower()) if t}
            rows.append((cells[name_i], mod_id, tags))
        return rows

    @staticmethod
    def _safe_filename(name):
        s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name.replace(" ", "_")).strip(" .")
        return s or "mod"

    # ---------- 主流程 ----------

    def _install_one(self, name, mod_id, target_dir, mc_version, loader_key, java_major):
        """下载一个条目。返回 (ok: bool, 说明)。"""
        try:
            candidates = self.get_candidate_versions(mod_id, mc_version, loader_key)
        except requests.ConnectionError:
            raise
        except (requests.RequestException, ValueError) as e:
            return False, f"查询 Modrinth 出错: {e}"
        if not candidates:
            return False, "未找到兼容版本"

        final_path = os.path.join(target_dir, f"{self._safe_filename(name)}.jar")
        part_path = final_path + ".part"
        rejected = []   # (版本号, 原因)
        for version in candidates[:_MAX_CANDIDATES]:
            vnum = version.get("version_number") or version.get("name") or "?"
            reason = None
            for file_info in self._jar_files(version)[:3]:
                try:
                    self._download(file_info, part_path)
                except (requests.ConnectionError, requests.Timeout):
                    # API 能连上、只是 CDN 慢/断：算这一条失败，不算"整个网络不可用"
                    return False, f"下载 {vnum} 失败: 网络连接中断或超时（已重试）"
                except (requests.RequestException, OSError, ValueError) as e:
                    return False, f"下载 {vnum} 失败: {e}"
                reason = inspect_jar(part_path, loader_key, mc_version, java_major)
                if reason is None:
                    try:
                        os.replace(part_path, final_path)
                    except OSError as e:
                        try:
                            os.remove(part_path)
                        except OSError:
                            pass
                        return False, f"无法写入 {os.path.basename(final_path)}（文件可能被占用）: {e}"
                    note = f"{vnum}"
                    if rejected:
                        skipped = "; ".join(f"{v} {r}" for v, r in rejected)
                        note += f"（较新版本不兼容已跳过: {skipped}）"
                    return True, note
                try:
                    os.remove(part_path)
                except OSError:
                    pass
            if reason:
                rejected.append((vnum, reason))
        detail = "; ".join(f"{v}: {r}" for v, r in rejected) or "没有可用的 jar 文件"
        return False, f"没有能在此服务端运行的版本（{detail}）"

    def sync(self, target_dir, mc_version, selected_loader, java_major=None):
        """同步数据库中的模组 / 插件到 target_dir。

        只装 loader 精确匹配、游戏版本精确匹配、jar 内容与 Java 要求都通过检查的版本；
        新版本不兼容时自动退回较旧版本。返回 (成功数, 匹配条目数)。"""
        if not os.path.exists(self.db_path):
            _say(f"  [警告] 数据库文件不存在: {self.db_path}")
            return 0, 0

        loader_key = self._loader_key(selected_loader)
        if loader_key not in _ACCEPTED_LOADERS:
            _say(f"\n--- {selected_loader or '原版'} 服务端不支持模组/插件，跳过数据库同步 ---")
            return 0, 0
        if java_major is None:
            # 按这个 MC 版本要求的最低 Java 判断（服务器可能正好用它）；已有服务器的
            # hmsl_launch.json 若记了更高的下限（26.x 以 Mojang 元数据为准），以它为准。
            java_major = _required_java(mc_version)
            floor = _spec_java_floor(target_dir)
            if floor and floor > java_major:
                java_major = floor

        os.makedirs(target_dir, exist_ok=True)
        _say(f"\n--- 正在同步模组与插件 (目标: {target_dir}) ---")

        try:
            rows = self.parse_database()
        except (OSError, UnicodeDecodeError) as e:
            _say(f"  [警告] 无法读取数据库文件: {e}")
            return 0, 0

        row_tags = _ROW_TAGS[loader_key]
        success_count = 0
        total_matched = 0
        failures = []
        net_errors = 0

        for name, mod_id, tags in rows:
            if not ("universal" in tags or tags & row_tags):
                continue
            total_matched += 1
            _say(f"正在同步: {name} ({mod_id})...", end="")
            if net_errors >= _MAX_NET_ERRORS:
                _say(" [跳过: 网络不可用]")
                failures.append((name, "网络不可用"))
                continue
            try:
                ok, detail = self._install_one(name, mod_id, target_dir, mc_version,
                                               loader_key, java_major)
                net_errors = 0
            except requests.ConnectionError as e:
                net_errors += 1
                ok, detail = False, f"网络连接失败: {e.__class__.__name__}"
            if ok:
                success_count += 1
                _say(f" [成功] {detail}")
            else:
                failures.append((name, detail))
                tag = "跳过" if detail.startswith(("未找到", "没有能")) else "失败"
                _say(f" [{tag}: {detail}]")

        _say(f"\n同步完成: 成功 {success_count} / 匹配 {total_matched}")
        if failures:
            _say("以下条目未安装: " + "、".join(n for n, _ in failures))
        return success_count, total_matched
