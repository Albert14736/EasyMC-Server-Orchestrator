"""实例扫描器 —— 检测 Minecraft 服务端实例及其 loader / 游戏版本。

纯文件系统逻辑（无 GUI、无网络），方便 pytest 无头测试。
取代旧的根目录 `instance_scanner_test.py`（GUI 以前从那里 import —— 这是个打包
地雷：PyInstaller 常把 *_test.py 排除掉，打出来的 .app 会直接 ImportError）。

公开 API：
    InstanceScanner(base_dir).scan() -> list[dict]   # 向后兼容，GUI 在用
    scan_instances(base_dir)         -> list[dict]
    get_instance_details(path, name) -> dict
    detect_loader_and_version(path)  -> (loader, version)   # 任一可能为 None
    detect_eula(path)                -> bool   # eula.txt 里确实写了 eula=true

实例字典字段：name, path, version(str|None), type(loader str|None),
              eula(bool), hmsl_created(bool)
"""
from __future__ import annotations

import json
import os
import re
import zipfile
from typing import List, Optional, Tuple

# loader 显示名（保持稳定 —— 既用作实例字典的 type，也被调用方当 Modrinth
# loader facet 传出去，别随意改大小写）。
FORGE = "Forge"
NEOFORGE = "NeoForge"
FABRIC = "Fabric"
QUILT = "Quilt"
PAPER = "Paper"
SPIGOT = "Spigot"
BUKKIT = "Bukkit"
VANILLA = "Vanilla"

_KNOWN_LOADERS = {x.lower(): x for x in (FORGE, NEOFORGE, FABRIC, QUILT, PAPER,
                                        SPIGOT, BUKKIT, VANILLA)}

# 永远不是服务端实例的目录（比较时忽略大小写 —— Windows 上 Core/Tests 也算）。
_EXCLUDE_DIRS = {".git", ".venv", "venv", "env", "core", "tests", "tools",
                 "__pycache__", ".vscode", ".pytest_cache", ".idea"}

# MC 版本形如 1.20 / 1.20.4 / 1.7.10；要求以 "1." 打头以免误抓 build 号。
_MC_VERSION_RE = re.compile(r"\b(1\.\d{1,2}(?:\.\d{1,2})?)\b")
# 整串校验用：旧式 1.x.y，以及 2026 起的新版本号 26.1 / 26.1.1
_MC_VERSION_FULL_RE = re.compile(r"(?:1\.\d{1,2}(?:\.\d{1,2})?|[2-9]\d\.\d{1,2}(?:\.\d{1,2})?)")

# 顶层 jar 名里能认出来的服务端 jar（不含 *-installer.jar）。paper.jar / spigot.jar
# 这种不带版本号的名字也算；其余名字（server-1.20.1.jar、arclight-*.jar …）打开 jar
# 看 Main-Class 再决定（见 _looks_like_server）。
_SERVER_JAR_RE = re.compile(
    r"^(?:server\.jar"
    r"|minecraft_server\..+\.jar"
    r"|(?:fabric|quilt)-server.*\.jar"
    r"|(?:paper|purpur|folia|spigot|craftbukkit|bukkit)(?:[-_.].+)?\.jar"
    r"|(?:arclight|mohist|magma|youer|banner)(?:[-_.].+)?\.jar"
    r"|(?:neo)?forge-.+\.jar)$")

# 名字认不出来时最多打开几个顶层 jar 看内容（只读 zip 目录和 MANIFEST，很快）
_MAX_JARS_TO_INSPECT = 4

# 有其一就说明这是个服务端目录（HMSL 生成的 start.*、Forge 安装器的 run.*、启动配置）
_SERVER_MARKER_FILES = ("eula.txt", "server.properties", "start.bat", "start.sh",
                        "run.bat", "run.sh", "hmsl_launch.json", ".hmsl.json")


def _top_level_jars(path: str) -> List[str]:
    try:
        return [f for f in os.listdir(path)
                if f.lower().endswith(".jar") and os.path.isfile(os.path.join(path, f))]
    except OSError:
        return []


def _isdir(path: str, *parts: str) -> bool:
    return os.path.isdir(os.path.join(path, *parts))


def _is_mc_version(s: Optional[str]) -> bool:
    return bool(s) and bool(_MC_VERSION_FULL_RE.fullmatch(s.strip()))


def _ver_key(v: str):
    return tuple(int(x) for x in v.split(".") if x.isdigit())


def _listdir(path: str, *parts: str) -> List[str]:
    try:
        return os.listdir(os.path.join(path, *parts))
    except OSError:
        return []


def _mc_version_from_libraries(path: str) -> Optional[str]:
    """Forge/NeoForge/Fabric/Vanilla 会把原版 server jar 放在
    libraries/net/minecraft/server/<mcver>[-时间戳]/ 下 —— 最可靠的版本来源。"""
    for name in sorted(_listdir(path, "libraries", "net", "minecraft", "server")):
        cand = name.split("-", 1)[0]
        if _is_mc_version(cand):
            return cand
        m = _MC_VERSION_RE.search(name)
        if m:
            return m.group(1)
    return None


def _mc_version_from_run_dirs(path: str) -> Optional[str]:
    """首次启动后才出现的版本线索：
    versions/<mc>/（原版 bundler、Paper、Fabric 都会解出来）、
    .fabric/server/<mc>-server.jar、libraries/net/fabricmc/intermediary/<mc>/。"""
    cands = [n for n in _listdir(path, "versions") if _is_mc_version(n) and _isdir(path, "versions", n)]
    if cands:
        return max(cands, key=_ver_key)
    for n in _listdir(path, ".fabric", "server"):
        m = re.match(r"^(.+?)-server\.jar$", n)
        if m and _is_mc_version(m.group(1)):
            return m.group(1)
    cands = [n for n in _listdir(path, "libraries", "net", "fabricmc", "intermediary") if _is_mc_version(n)]
    if cands:
        return max(cands, key=_ver_key)
    return None


def _paper_mc_version(path: str) -> Optional[str]:
    """Paper 写 version_history.json：{"currentVersion":"git-Paper-196 (MC: 1.20.1)"}
    —— Paper 独有，也是它最可靠的版本来源（jar 常被改名成 server.jar）。"""
    try:
        with open(os.path.join(path, "version_history.json"), "r",
                  encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return None
    m = re.search(r"MC:\s*(1\.\d{1,2}(?:\.\d{1,2})?|[2-9]\d\.\d{1,2}(?:\.\d{1,2})?)", content)
    return m.group(1) if m else None


def _hmsl_marker(path: str) -> Tuple[Optional[str], Optional[str]]:
    """HMSL 创建实例时可能写下的启动/实例信息（hmsl_launch.json / .hmsl.json）。
    只取 loader 与 MC 版本两个字段，认不出就返回 (None, None)。"""
    for fname in (".hmsl.json", "hmsl_launch.json"):
        try:
            with open(os.path.join(path, fname), "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        loader = None
        for k in ("loader", "type", "server_type"):
            v = data.get(k)
            if isinstance(v, str) and v.strip().lower() in _KNOWN_LOADERS:
                loader = _KNOWN_LOADERS[v.strip().lower()]
                break
        version = None
        for k in ("mc_version", "minecraft_version", "game_version", "version"):
            v = data.get(k)
            if isinstance(v, str) and _is_mc_version(v):
                version = v.strip()
                break
        if loader or version:
            return loader, version
    return None, None


def _inspect_server_jar(jar_path: str) -> Tuple[Optional[str], Optional[str]]:
    """看 jar 里面：Main-Class 判断 loader，内置的 version.json / install.properties /
    versions.list 给出 MC 版本。只读 zip 目录和几个小文件，大 jar 也很快。"""
    try:
        with zipfile.ZipFile(jar_path) as z:
            names = set(z.namelist())

            def read(n: str) -> str:
                try:
                    with z.open(n) as f:
                        return f.read(1 << 20).decode("utf-8", errors="replace")
                except (KeyError, OSError, zipfile.BadZipFile, RuntimeError):
                    return ""

            main = ""
            if "META-INF/MANIFEST.MF" in names:
                m = re.search(r"^Main-Class:\s*(\S+)", read("META-INF/MANIFEST.MF"), re.M)
                main = m.group(1) if m else ""

            version = None
            if "install.properties" in names:  # Fabric / Quilt 服务端启动器
                m = re.search(r"^game-version\s*=\s*(\S+)", read("install.properties"), re.M)
                if m and _is_mc_version(m.group(1)):
                    version = m.group(1)
            if version is None and "version.json" in names:  # 原版 bundler / Paperclip
                try:
                    vid = json.loads(read("version.json")).get("id")
                    if isinstance(vid, str) and _is_mc_version(vid):
                        version = vid
                except (ValueError, AttributeError):
                    pass
            versions_list = read("META-INF/versions.list") if "META-INF/versions.list" in names else ""
            if version is None and versions_list:
                for line in versions_list.splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 2 and _is_mc_version(parts[1]):
                        version = parts[1]
                        break
            if version is None and "patch.json" in names:  # 旧版 Paperclip（1.17 以前）
                try:
                    pv = json.loads(read("patch.json")).get("version")
                    if isinstance(pv, str) and _is_mc_version(pv):
                        version = pv
                except (ValueError, AttributeError):
                    pass
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
        return None, None

    low = main.lower()
    jar_low = os.path.basename(jar_path).lower()
    loader = None
    if low.startswith("io.izzel.arclight"):
        # Arclight（Forge/NeoForge/Fabric + Bukkit 混合端）：分支只写在文件名里
        loader = (NEOFORGE if "neoforge" in jar_low
                  else FABRIC if "fabric" in jar_low else FORGE)
    elif low.startswith(("com.mohistmc.", "org.magmafoundation.")):
        # Mohist / Magma：Forge + Bukkit 混合端（Youer = NeoForge 版，Banner = Fabric 版）
        loader = (NEOFORGE if "youer" in jar_low
                  else FABRIC if "banner" in jar_low else FORGE)
    elif low.startswith("net.fabricmc."):
        loader = FABRIC
    elif low.startswith("org.quiltmc."):
        loader = QUILT
    elif "paperclip" in low or low.startswith("io.papermc."):
        loader = PAPER
    elif low.startswith("org.bukkit.craftbukkit"):
        loader = SPIGOT if ("spigot" in versions_list.lower()
                            or any(n.startswith(("org/spigotmc/", "META-INF/maven/org.spigotmc/"))
                                   for n in names)) else BUKKIT
        if version is None:
            m = _MC_VERSION_RE.search(versions_list)
            version = m.group(1) if m else None
    elif low.startswith("net.minecraftforge.") or low.startswith("cpw.mods."):
        loader = FORGE
    elif low in ("net.minecraft.bundler.main", "net.minecraft.server.main",
                 "net.minecraft.server.minecraftserver"):
        loader = VANILLA
    if loader is not None and version is None:
        m = _MC_VERSION_RE.search(jar_low)   # 混合端：arclight-forge-1.20.1-….jar
        version = m.group(1) if m else None
    return loader, version


def _jar_candidates(path: str, jars: List[str]) -> List[str]:
    """需要打开看看的顶层 jar：server.jar 优先，其次名字认得出的，再其次其它 jar
    （最多 _MAX_JARS_TO_INSPECT 个）；安装器 jar 不看。"""
    usable = [j for j in jars if not j.lower().endswith("-installer.jar")]
    named = sorted((j for j in usable if _SERVER_JAR_RE.match(j.lower())),
                   key=lambda j: (j.lower() != "server.jar", j.lower()))
    others = sorted((j for j in usable if not _SERVER_JAR_RE.match(j.lower())),
                    key=str.lower)[:_MAX_JARS_TO_INSPECT]
    return [os.path.join(path, j) for j in (named[:6] + others)]


def _neoforge_mc_version(path: str) -> Optional[str]:
    """libraries/net/neoforged/neoforge/21.1.77 → 1.21.1；20.4.237 → 1.20.4；
    NeoForge 1.20.1 用的是 net/neoforged/forge/1.20.1-47.1.x。"""
    vers = _listdir(path, "libraries", "net", "neoforged", "neoforge")
    for v in sorted(vers, reverse=True):
        parts = v.split("-", 1)[0].split(".")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            major, minor = int(parts[0]), int(parts[1])
            if 20 <= major <= 21:
                return f"1.{major}" if minor == 0 else f"1.{major}.{minor}"
    for v in _listdir(path, "libraries", "net", "neoforged", "forge"):
        cand = v.split("-", 1)[0]
        if _is_mc_version(cand):
            return cand
    return None


def _forge_mc_version(path: str) -> Optional[str]:
    for v in sorted(_listdir(path, "libraries", "net", "minecraftforge", "forge")):
        cand = v.split("-", 1)[0]
        if _is_mc_version(cand):
            return cand
    return None


def detect_loader_and_version(path: str) -> Tuple[Optional[str], Optional[str]]:
    """尽力从实例文件夹推断 (loader, mc_version)，任一都可能为 None。

    只读 `path` 下的文件系统（包括打开顶层服务端 jar 看 Main-Class / 内置版本信息），
    不联网。检测顺序按"信号唯一性"由强到弱排：
      - HMSL 自己写的实例信息（有就直接信）
      - NeoForge 必须早于 Forge：NeoForge 1.21 的 libraries 里也有 net/minecraftforge/srgutils
      - Paper 的 version_history.json / Paperclip jar 必须早于 Fabric/Spigot：
        Paper 目录里常混入 spigot.yml 和 net/fabricmc/mapping-io
      - 服务端 jar 内部（Fabric 启动器 / Paperclip / 原版 bundler 都会被存成 server.jar）
    """
    jars = [(j, j.lower()) for j in _top_level_jars(path)]
    loader: Optional[str] = None
    version: Optional[str] = None

    marker_loader, marker_version = _hmsl_marker(path)

    # --- NeoForge: neoforge-<build>.jar 或 libraries/net/neoforged/{neoforge,forge}/ ---
    if (any(low.startswith("neoforge-") and not low.endswith("-installer.jar") for _o, low in jars)
            or _isdir(path, "libraries", "net", "neoforged", "neoforge")
            or _isdir(path, "libraries", "net", "neoforged", "forge")):
        loader, version = NEOFORGE, _neoforge_mc_version(path)

    # --- Forge: forge-<mc>-<build>.jar 或 libraries/net/minecraftforge/forge/
    #     （整合包常没有顶层 jar，只在 libraries 里有）---
    if loader is None:
        for _orig, low in jars:
            m = re.match(r"forge-(1\.\d{1,2}(?:\.\d{1,2})?)-", low)
            if m and not low.endswith("-installer.jar"):
                loader, version = FORGE, m.group(1)
                break
    if loader is None and (_isdir(path, "libraries", "net", "minecraftforge", "forge")
                           or (_isdir(path, "libraries", "net", "minecraftforge")
                               and not _isdir(path, "libraries", "net", "neoforged"))):
        loader, version = FORGE, _forge_mc_version(path)

    # --- Paper：version_history.json 强信号（须早于 Fabric/Spigot）---
    if loader is None:
        pv = _paper_mc_version(path)
        if pv is not None:
            loader, version = PAPER, pv
        else:
            for _orig, low in jars:
                if low.startswith(("paper", "purpur", "folia")) and not low.endswith("-installer.jar"):
                    m = _MC_VERSION_RE.search(low)
                    loader, version = PAPER, (m.group(1) if m else None)
                    break

    # --- 服务端 jar 内部：Fabric 启动器 / Paperclip / 原版 bundler / Spigot ---
    if loader is None or version is None:
        for jar_path in _jar_candidates(path, [o for o, _l in jars]):
            jl, jv = _inspect_server_jar(jar_path)
            if jl is None:
                continue
            if loader is None:
                loader = jl
                version = version or jv
                break
            if jl == loader and version is None and jv:
                version = jv
                break

    # --- Fabric / Quilt（目录信号；net/fabricmc 只认 fabric-loader，Paper 也带 mapping-io）---
    if loader is None:
        if (any(low.startswith("fabric-server") for _o, low in jars)
                or _isdir(path, ".fabric")
                or os.path.isfile(os.path.join(path, "fabric-server-launcher.properties"))
                or _isdir(path, "libraries", "net", "fabricmc", "fabric-loader")):
            loader = FABRIC
        elif (any(low.startswith("quilt-server") for _o, low in jars)
              or _isdir(path, ".quilt")
              or _isdir(path, "libraries", "org", "quiltmc", "quilt-loader")):
            loader = QUILT

    # --- Spigot / Bukkit（jar 名或 yml 标记；Paper 已在上面拦截）---
    if loader is None:
        for _orig, low in jars:
            if low.startswith("spigot"):
                m = _MC_VERSION_RE.search(low)
                loader, version = SPIGOT, (m.group(1) if m else None)
                break
            if low.startswith("craftbukkit") or low.startswith("bukkit"):
                m = _MC_VERSION_RE.search(low)
                loader, version = BUKKIT, (m.group(1) if m else None)
                break
    if loader is None and os.path.isfile(os.path.join(path, "spigot.yml")):
        loader = SPIGOT
    elif loader is None and os.path.isfile(os.path.join(path, "bukkit.yml")):
        loader = BUKKIT

    # --- Vanilla: minecraft_server.<mc>.jar / server.jar ---
    if loader is None:
        for _orig, low in jars:
            m = re.match(r"minecraft_server\.(1\.\d{1,2}(?:\.\d{1,2})?)\.jar", low)
            if m:
                loader, version = VANILLA, m.group(1)
                break
        if loader is None and any(low == "server.jar" for _o, low in jars):
            loader = VANILLA

    # --- 版本兜底：libraries（Forge/NeoForge/Fabric/Vanilla），再看首次启动解出的目录 ---
    if version is None:
        version = _mc_version_from_libraries(path)
    if version is None:
        version = _mc_version_from_run_dirs(path)
    if version is None and loader in (FORGE, NEOFORGE, VANILLA, SPIGOT, BUKKIT):
        for _orig, low in jars:
            m = re.match(r"minecraft_server\.(1\.\d{1,2}(?:\.\d{1,2})?)\.jar", low)
            if m:
                version = m.group(1)
                break

    # HMSL 自己记下的信息最可信
    if marker_loader:
        loader = marker_loader
    if marker_version:
        version = marker_version
    return loader, version


def detect_eula(path: str) -> bool:
    """eula.txt 里确实有 `eula=true`（忽略注释行、大小写和等号两侧空格）。
    只有文件存在、或写着 eula=false，都算未同意。"""
    try:
        with open(os.path.join(path, "eula.txt"), "r",
                  encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip().lstrip("﻿")
                if not s or s.startswith(("#", "!")):
                    continue
                key, sep, value = s.partition("=")
                if not sep:
                    key, sep, value = s.partition(":")
                if sep and key.strip().lower() == "eula":
                    return value.strip().lower() == "true"
    except OSError:
        return False
    return False


def _looks_like_server(path: str) -> bool:
    """有 eula.txt / server.properties / 启动脚本 / HMSL 启动配置，或顶层有认得出的
    服务端 jar，或有 libraries/net/minecraft/server。
    名字认不出的顶层 jar（server-1.20.1.jar、arclight-….jar 等）打开看 Main-Class，
    是已知服务端的也算。启动器的客户端版本目录（.minecraft/versions/1.20.1-Fabric 0.15.11/，
    里面的客户端 jar 主类是 net.minecraft.client.main.Main）和只放着 *-installer.jar
    的目录都不算。"""
    for marker in _SERVER_MARKER_FILES:
        if os.path.isfile(os.path.join(path, marker)):
            return True
    jars = _top_level_jars(path)
    others = []
    for f in jars:
        low = f.lower()
        if low.endswith("-installer.jar"):
            continue
        if _SERVER_JAR_RE.match(low):
            return True
        others.append(f)
    if _isdir(path, "libraries", "net", "minecraft", "server"):
        return True
    for f in sorted(others, key=str.lower)[:_MAX_JARS_TO_INSPECT]:
        if _inspect_server_jar(os.path.join(path, f))[0] is not None:
            return True
    return False


def get_instance_details(path: str, name: str) -> dict:
    """解析单个实例文件夹为标准字典。"""
    loader, version = detect_loader_and_version(path)
    hmsl_created = any(os.path.isfile(os.path.join(path, f))
                       for f in ("start.sh", "start.bat", "hmsl_launch.json", ".hmsl.json"))
    return {
        "name": name,
        "path": path,
        "version": version,   # str | None
        "type": loader,       # loader 显示名 str | None
        "eula": detect_eula(path),
        "hmsl_created": hmsl_created,
    }


def scan_instances(base_dir: str) -> List[dict]:
    """扫描 base_dir 下所有一级子目录，返回看起来像服务端实例的那些。"""
    if not base_dir or not os.path.isdir(base_dir):
        return []
    try:
        items = sorted(os.listdir(base_dir))
    except OSError:
        return []
    out: List[dict] = []
    for item in items:
        if item.lower() in _EXCLUDE_DIRS or item.startswith("."):
            continue
        item_path = os.path.join(base_dir, item)
        try:
            if os.path.isdir(item_path) and _looks_like_server(item_path):
                out.append(get_instance_details(item_path, item))
        except OSError:
            continue
    return out


class InstanceScanner:
    """向后兼容包装；新代码请直接用模块级函数。"""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir

    def scan(self) -> List[dict]:
        return scan_instances(self.base_dir)


if __name__ == "__main__":
    import sys
    base = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    print(f"=== 扫描 {base} ===")
    found = scan_instances(base)
    if not found:
        print("未发现任何有效的服务器实例。")
    for inst in found:
        loader = inst["type"] or "未知"
        version = inst["version"] or "未知"
        eula = "✅" if inst["eula"] else "❌"
        print(f"- {inst['name']}  [{loader} {version}]  EULA {eula}")
        print(f"    {inst['path']}")
