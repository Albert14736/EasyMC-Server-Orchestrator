import hashlib
import locale
import os
import queue
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import requests

from core.env_manager import NO_WINDOW_FLAGS, UTF8_JVM_FLAGS, java_tls_fix_args, needs_tls_fix

# PaperMC 的 Fill v3 API 要求带能识别软件的 User-Agent；其余源带上也无害。
USER_AGENT = "HMSL/1.0 (Hello Minecraft! Server Launcher; Minecraft server manager)"
# (连接超时, 读超时) —— 卡住的镜像/代理不会再让安装线程永远挂起。
HTTP_TIMEOUT = (15, 30)
# Forge/NeoForge 安装器要下载几百个库，慢网下给足时间，但不能无限等。
INSTALLER_TIMEOUT = 30 * 60

PAPER_API = "https://fill.papermc.io/v3"
FABRIC_META = "https://meta.fabricmc.net/v2"
FORGE_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge"
FORGE_PROMOS = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
NEOFORGE_MAVEN = "https://maven.neoforged.net/releases/net/neoforged"
NEOFORGE_API = "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged"
MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"

# 安装器输出里最吵、对用户没有意义的行（Forge 1.20.1 安装器会打印两万多行）：
# 只把顶格的进度行和带 error/failed 的行显示到日志里，缩进的明细行（逐个库、逐个类）跳过。
_NOISY_LINE = re.compile(r"^\s*(Considering library|Cant Find Class|File exists|Checksum validated|Extracting:)")
_IMPORTANT_LINE = re.compile(r"error|exception|failed|失败", re.I)
# 新版安装器写 installer.log，旧版写 <安装器文件名>.log
_INSTALLER_LOGS = ("installer.log",)


def _http_get(url, stream=False, **kw):
    headers = kw.pop("headers", {}) or {}
    headers.setdefault("User-Agent", USER_AGENT)
    return requests.get(url, headers=headers, timeout=kw.pop("timeout", HTTP_TIMEOUT),
                        stream=stream, **kw)


# 连不上服务器（断网、DNS、代理不可用、SSL 握手失败、超时）：给用户一句看得懂的话，原始异常只写进日志。
# requests 的 ProxyError / SSLError / ConnectTimeout 都是 ConnectionError 的子类。
_NET_ERRORS = (requests.ConnectionError, requests.Timeout)


def _net_error_text(exc: BaseException, url: Optional[str] = None) -> str:
    """'无法连接 <host>，请检查网络或代理设置'；host 优先取异常里实际请求的地址。"""
    req_url = getattr(getattr(exc, "request", None), "url", None) or url or ""
    try:
        host = urlparse(str(req_url)).hostname
    except ValueError:
        host = None
    return f"无法连接 {host or '下载服务器'}，请检查网络或代理设置"


def _version_key(v: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def _is_prerelease(v: str) -> bool:
    return bool(re.search(r"-(beta|alpha|rc|pre|snapshot)", v, re.I))


def _decode_line(raw: bytes) -> str:
    """优先 UTF-8；不是合法 UTF-8 的行（旧 Java / cmd 的本地代码页输出）再按系统代码页解码。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(locale.getpreferredencoding(False) or "latin-1", "replace")


# ---------- Mojang 版本元数据（原版服务端 / Java 版本要求共用） ----------

_mojang_lock = threading.Lock()
_mojang_manifest_cache: Optional[dict] = None
_mojang_version_cache: dict = {}


def mojang_version_json(mc_version: str) -> Optional[dict]:
    """piston-meta 上该版本的完整 JSON（含 downloads.server / javaVersion），失败返回 None。"""
    with _mojang_lock:
        if mc_version in _mojang_version_cache:
            return _mojang_version_cache[mc_version]
    def fetch_manifest() -> Optional[dict]:
        global _mojang_manifest_cache
        r = _http_get(MOJANG_MANIFEST)
        if r.status_code != 200:
            return None
        _mojang_manifest_cache = r.json()
        return _mojang_manifest_cache

    def find(manifest: dict) -> Optional[dict]:
        return next((v for v in manifest.get("versions", []) if v.get("id") == mc_version), None)

    try:
        manifest, fresh = _mojang_manifest_cache, False
        if manifest is None:
            manifest, fresh = fetch_manifest(), True
            if manifest is None:
                return None
        entry = find(manifest)
        if entry is None and not fresh:
            # 缓存的清单里没有：可能是程序运行期间新发布的版本，重新拉一次
            manifest = fetch_manifest()
            entry = find(manifest) if manifest else None
        if entry is None:
            return None
        r = _http_get(entry["url"])
        if r.status_code != 200:
            return None
        data = r.json()
    except (requests.RequestException, ValueError, KeyError):
        return None
    with _mojang_lock:
        _mojang_version_cache[mc_version] = data
    return data


def mojang_java_major(mc_version: str) -> Optional[int]:
    """Mojang 官方元数据声明的最低 Java 主版本（javaVersion.majorVersion），查不到返回 None。"""
    data = mojang_version_json(mc_version)
    try:
        return int(data["javaVersion"]["majorVersion"]) if data else None
    except (KeyError, TypeError, ValueError):
        return None


class ServerInstaller:
    def __init__(self):
        # 同一个 installer 可能被向导和整合包导入两个线程同时使用 → 错误信息按线程存。
        self._tls = threading.local()

    # ----- 诊断信息（create_server 会把它放进 CreateServerResult.error） -----

    @property
    def last_error(self) -> Optional[str]:
        return getattr(self._tls, "last_error", None)

    @last_error.setter
    def last_error(self, value: Optional[str]) -> None:
        self._tls.last_error = value

    def _fail(self, msg: str) -> bool:
        self.last_error = msg
        print(f"  [错误] {msg}")
        return False

    def _net_fail(self, exc: BaseException, url: Optional[str] = None) -> bool:
        """网络错误：日志里留原始异常，给用户的错误信息只说连不上哪个服务器。"""
        print(f"  [详情] {type(exc).__name__}: {exc}")
        return self._fail(_net_error_text(exc, url))

    # ----- Paper -----

    def install_paper(self, target_path, mc_version, build=None):
        """下载 PaperMC 服务端（Fill v3 API；旧的 api.papermc.io/v2 已下线，只返回 HTTP 410）"""
        self.last_error = None
        print(f"正在获取 Paper {mc_version} 构建信息...")
        api_url = f"{PAPER_API}/projects/paper/versions/{mc_version}/builds"
        try:
            r = _http_get(api_url)
            if r.status_code == 404:
                return self._fail(f"PaperMC 没有提供 Minecraft {mc_version} 的 Paper 服务端（该版本不受支持）。")
            if r.status_code != 200:
                return self._fail(f"PaperMC 接口返回 HTTP {r.status_code}：{r.text[:200]}")
            builds = [b for b in r.json() if isinstance(b, dict) and "server:default" in (b.get("downloads") or {})]
            if not builds:
                return self._fail(f"PaperMC 上 {mc_version} 没有可下载的构建。")

            chosen = None
            if build is not None and str(build).strip().isdigit():
                chosen = next((b for b in builds if str(b.get("id")) == str(build).strip()), None)
                if chosen is None:
                    print(f"  [警告] 未找到指定的 Paper 构建 #{build}，改用最新稳定构建。")
            if chosen is None:
                stable = [b for b in builds if str(b.get("channel", "")).upper() in ("STABLE", "RECOMMENDED")]
                chosen = max(stable or builds, key=lambda b: int(b.get("id", 0)))
            dl = chosen["downloads"]["server:default"]
            print(f"  已选择 Paper 构建 #{chosen.get('id')}（{chosen.get('channel', '?')}）")
            sha256 = (dl.get("checksums") or {}).get("sha256")
            if not self._download(dl["url"], target_path, "server.jar", sha256=sha256):
                return False
            # Paperclip 首次启动时把原版 jar 缓存在 cache/mojang_<mc>.jar
            self._predownload_vanilla(target_path, mc_version, f"cache/mojang_{mc_version}.jar")
            return True
        except _NET_ERRORS as e:
            return self._net_fail(e, api_url)
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            return self._fail(f"Paper 下载流程异常: {e}")

    # ----- Fabric -----

    def install_fabric(self, target_path, mc_version, loader_version=None):
        """下载 Fabric 服务端启动器（首次启动时它会自行下载原版服务端与依赖库）"""
        self.last_error = None
        print(f"正在获取 Fabric {mc_version} 版本信息...")
        try:
            r = _http_get(f"{FABRIC_META}/versions/loader/{mc_version}")
            loaders = r.json() if r.status_code == 200 else []
            if not loaders:
                return self._fail(f"Fabric 不支持 Minecraft {mc_version}（HTTP {r.status_code}）。")
            entries = [(e["loader"]["version"], bool(e["loader"].get("stable"))) for e in loaders]

            loader_ver = None
            if loader_version:
                wanted = str(loader_version).strip()
                if any(v == wanted for v, _ in entries):
                    loader_ver = wanted
                else:
                    print(f"  [警告] Fabric Loader {wanted} 不适用于 {mc_version}，改用最新稳定版。")
            if loader_ver is None:
                loader_ver = next((v for v, stable in entries if stable), entries[0][0])

            inst_r = _http_get(f"{FABRIC_META}/versions/installer")
            installers = inst_r.json() if inst_r.status_code == 200 else []
            if not installers:
                return self._fail(f"无法获取 Fabric 安装器版本列表（HTTP {inst_r.status_code}）。")
            inst_ver = next((i["version"] for i in installers if i.get("stable")), installers[0]["version"])

            print(f"  Fabric Loader {loader_ver} / Installer {inst_ver}")
            dl_url = f"{FABRIC_META}/versions/loader/{mc_version}/{loader_ver}/{inst_ver}/server/jar"
            if not self._download(dl_url, target_path, "server.jar"):
                return False
            # Fabric 服务端启动器把原版 jar 缓存在 .fabric/server/<mc>-server.jar
            self._predownload_vanilla(target_path, mc_version, f".fabric/server/{mc_version}-server.jar")
            return True
        except _NET_ERRORS as e:
            return self._net_fail(e, FABRIC_META)
        except (requests.RequestException, ValueError, KeyError, TypeError, IndexError) as e:
            return self._fail(f"Fabric 下载流程异常: {e}")

    # ----- Vanilla -----

    def install_vanilla(self, target_path, mc_version):
        """下载 Mojang 官方原版服务端（piston-meta 版本清单 → 版本 JSON → downloads.server）"""
        self.last_error = None
        print(f"正在获取原版 Minecraft {mc_version} 服务端信息...")
        info = mojang_version_json(mc_version)
        if info is None:
            return self._fail(f"未在 Mojang 版本清单中找到 {mc_version}（或无法连接 piston-meta.mojang.com）。")
        server = (info.get("downloads") or {}).get("server")
        if not server or not server.get("url"):
            return self._fail(f"Minecraft {mc_version} 没有官方服务端下载。")
        return self._download(server["url"], target_path, "server.jar", sha1=server.get("sha1"))

    # ----- Forge -----

    def install_forge(self, target_path, mc_version, java_cmd="java", forge_version=None):
        """下载并自动安装 Forge 服务端"""
        self.last_error = None
        print(f"正在获取 Forge {mc_version} 列表...")
        try:
            forge_ver = None
            if forge_version:
                forge_ver = str(forge_version).strip()
                if forge_ver.lower().startswith("forge-"):
                    forge_ver = forge_ver[len("forge-"):]
                if forge_ver.startswith(mc_version + "-"):
                    forge_ver = forge_ver[len(mc_version) + 1:]
                if forge_ver.endswith("-" + mc_version):
                    forge_ver = forge_ver[: -(len(mc_version) + 1)]
            if not forge_ver:
                # 1. 获取 Forge 版本列表：优先选择推荐版，没有则选最新版
                r = _http_get(FORGE_PROMOS)
                if r.status_code != 200:
                    return self._fail(f"无法获取 Forge 版本列表（HTTP {r.status_code}）。")
                promos = r.json().get("promos", {})
                forge_ver = promos.get(f"{mc_version}-recommended") or promos.get(f"{mc_version}-latest")
                if not forge_ver:
                    return self._fail(f"未找到适用于 {mc_version} 的 Forge 版本。")
            print(f"  Forge 版本: {mc_version}-{forge_ver}")

            # 2. 下载安装器：标准格式 (mc-forge)，失败再试旧版格式 (mc-forge-mc，常见于 1.7.10)
            downloaded = False
            for full in (f"{mc_version}-{forge_ver}", f"{mc_version}-{forge_ver}-{mc_version}"):
                url = f"{FORGE_MAVEN}/{full}/forge-{full}-installer.jar"
                if self._download(url, target_path, "forge-installer.jar", quiet_404=True):
                    downloaded = True
                    break
            if not downloaded:
                return self._fail(self.last_error or f"无法找到有效的 Forge {forge_ver} 下载链接。")

            # 3. 运行安装程序，并按产物（而不是退出码）判断成败
            print("正在执行 Forge 静默安装 (这可能需要几分钟，请耐心等待...)...")
            return self._run_and_verify_installer(target_path, java_cmd, "forge-installer.jar",
                                                  "Forge", mc_version)
        except _NET_ERRORS as e:
            return self._net_fail(e, FORGE_PROMOS)
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            return self._fail(f"Forge 下载/安装异常: {e}")

    # ----- NeoForge -----

    def _neoforge_versions(self, artifact: str) -> List[str]:
        """NeoForge 版本列表：先用 Reposilite JSON API，失败再读 maven-metadata.xml。"""
        try:
            r = _http_get(f"{NEOFORGE_API}/{artifact}")
            if r.status_code == 200:
                vs = r.json().get("versions") or []
                if vs:
                    return [str(v) for v in vs]
        except (requests.RequestException, ValueError, AttributeError):
            pass
        r = _http_get(f"{NEOFORGE_MAVEN}/{artifact}/maven-metadata.xml")
        if r.status_code != 200:
            raise requests.RequestException(f"HTTP {r.status_code}")
        root = ET.fromstring(r.content)
        return [v.text for v in root.findall(".//version") if v.text]

    def install_neoforge(self, target_path, mc_version, java_cmd="java", neoforge_version=None):
        """下载并自动安装 NeoForge 服务端 (针对 1.20.1+)"""
        self.last_error = None
        print(f"正在自动获取 NeoForge {mc_version} 的最新构建...")
        try:
            # NeoForge 命名规则:
            # 1.20.1 -> 仍以 net.neoforged:forge 发布，版本号 1.20.1-47.1.x
            # 1.20.2 -> 20.2.x   1.20.4 -> 20.4.x   1.21 -> 21.0.x   1.21.1 -> 21.1.x
            # 26.1   -> 26.1.0.x 26.1.2 -> 26.1.2.x（年份制版本号）
            parts = mc_version.split(".")
            if mc_version == "1.20.1":
                artifact, prefix = "forge", "1.20.1-"
            elif parts[0] == "1" and len(parts) >= 2:
                artifact, prefix = "neoforge", f"{parts[1]}.{parts[2] if len(parts) > 2 else '0'}."
            else:
                artifact = "neoforge"
                prefix = f"{parts[0]}.{parts[1] if len(parts) > 1 else '0'}.{parts[2] if len(parts) > 2 else '0'}."

            if neoforge_version:
                full_version = str(neoforge_version).strip()
                if full_version.lower().startswith("neoforge-"):
                    full_version = full_version[len("neoforge-"):]
                if artifact == "forge" and not full_version.startswith("1.20.1-"):
                    full_version = "1.20.1-" + full_version
            else:
                try:
                    all_versions = self._neoforge_versions(artifact)
                except _NET_ERRORS as e:
                    return self._net_fail(e, NEOFORGE_MAVEN)
                except (requests.RequestException, ET.ParseError) as e:
                    return self._fail(f"无法连接到 NeoForge Maven 仓库：{e}")
                matching = [v for v in all_versions if v.startswith(prefix)]
                # 排除 beta/alpha；只有测试版时才用测试版
                stable = [v for v in matching if not _is_prerelease(v)]
                pool = stable or matching
                if not pool:
                    return self._fail(f"未找到适用于 {mc_version} 的 NeoForge 版本。")
                full_version = max(pool, key=_version_key)
            print(f"  已找到 NeoForge 版本: {full_version}")

            dl_url = f"{NEOFORGE_MAVEN}/{artifact}/{full_version}/{artifact}-{full_version}-installer.jar"
            if not self._download(dl_url, target_path, "neoforge-installer.jar"):
                return False

            print("正在执行 NeoForge 静默安装 (请耐心等待...)...")
            return self._run_and_verify_installer(target_path, java_cmd, "neoforge-installer.jar",
                                                  "NeoForge", mc_version)
        except _NET_ERRORS as e:
            return self._net_fail(e, NEOFORGE_MAVEN)
        except (requests.RequestException, ValueError, KeyError, TypeError, IndexError) as e:
            return self._fail(f"NeoForge 自动化流程异常: {e}")

    # ----- 安装器执行 + 产物校验 -----

    def _run_installer(self, java_cmd, installer_name, target_path) -> Tuple[Optional[int], List[str]]:
        """运行 `java -jar <installer> --installServer`，边跑边把输出打到日志；返回 (退出码, 输出行)。"""
        # UTF-8 标志让安装器（Java 8/17）的输出也是 UTF-8，便于解码
        cmd = [java_cmd, *java_tls_fix_args(java_cmd), *UTF8_JVM_FLAGS,
               "-jar", installer_name, "--installServer"]
        try:
            proc = subprocess.Popen(cmd, cwd=target_path, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    creationflags=NO_WINDOW_FLAGS)
        except (FileNotFoundError, OSError) as e:
            return None, [f"无法启动 Java（{java_cmd}）：{e}"]

        lines: List[str] = []
        # 后台线程只负责读管道，行交给本线程打印：GUI 按线程接管 print（每个安装任务进各自的日志框），
        # 在读取线程里直接 print 的内容到不了新建服务器向导的日志框。
        out_q: "queue.Queue[Optional[str]]" = queue.Queue()

        def pump():
            try:
                assert proc.stdout is not None
                for raw in proc.stdout:
                    out_q.put(_decode_line(raw).rstrip("\r\n"))
            except (OSError, ValueError):
                pass
            finally:
                out_q.put(None)

        def show(line: str) -> None:
            lines.append(line)
            if len(lines) > 5000:
                del lines[:1000]
            if not line.strip() or _NOISY_LINE.match(line):
                return
            if not line[0].isspace() or _IMPORTANT_LINE.search(line):
                print(f"  | {line}")

        def drain() -> None:
            while True:
                try:
                    line = out_q.get_nowait()
                except queue.Empty:
                    return
                if line is None:
                    return
                show(line)

        def timed_out() -> Tuple[Optional[int], List[str]]:
            proc.kill()
            proc.wait()
            t.join(5)
            drain()
            lines.append(f"[HMSL] 安装器运行超过 {INSTALLER_TIMEOUT // 60} 分钟，已被终止。")
            return None, lines

        t = threading.Thread(target=pump, name="installer-output", daemon=True)
        t.start()
        deadline = time.monotonic() + INSTALLER_TIMEOUT
        exited_at: Optional[float] = None
        while True:
            now = time.monotonic()
            if now >= deadline:
                return timed_out()
            try:
                line = out_q.get(timeout=min(0.2, deadline - now))
            except queue.Empty:
                if proc.poll() is not None:
                    # 安装器已退出、但管道还被它派生的进程占着：最多再等 10 秒输出
                    exited_at = exited_at or now
                    if now - exited_at > 10:
                        break
                continue
            if line is None:
                break
            show(line)
        try:
            rc = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return timed_out()
        return rc, lines

    def _run_and_verify_installer(self, target_path, java_cmd, installer_name, label, mc_version) -> bool:
        for attempt in range(2):
            rc, lines = self._run_installer(java_cmd, installer_name, target_path)
            output = "\n".join(lines)
            produced = find_forge_launch_files(target_path)
            failed_marker = "There was an error during installation" in output
            if rc == 0 and produced and not failed_marker:
                print(f"  [成功] {label} 服务端环境已安装。")
                self._cleanup(target_path, installer_name)
                return True
            hint, network = _installer_hint(output, java_cmd)
            if attempt == 0 and network and rc is not None:
                # 网络抖动：再跑一次安装器（已下载且校验通过的库会被跳过）
                print(f"  [重试] {label} 安装器下载出错，正在重试...")
                continue
            break

        # 失败：保留安装器日志，把输出末尾和 .log 末尾一起交给调用方
        reason = []
        if rc is None:
            reason.append("安装器没有正常结束")
        elif rc != 0:
            reason.append(f"安装器退出码 {rc}")
        if not produced:
            reason.append("没有生成可启动的服务端文件（run.bat / 参数文件 / forge-*.jar）")
        if failed_marker:
            reason.append("安装器报告安装出错")
        # 去掉 Java 堆栈帧（"\tat ..."），只留真正说明问题的行
        meaningful = [l for l in lines if l.strip() and not re.match(r"\s+at |\s*\.\.\. \d+ more", l)]
        tail = "\n".join(meaningful[-15:])
        msg = f"{label} {mc_version} 安装失败：{'；'.join(reason)}。"
        if hint:
            msg += f"\n提示：{hint}"
        if tail:
            msg += f"\n安装器输出（末尾）：\n{tail}"
        for log_name in (installer_name + ".log",) + _INSTALLER_LOGS:
            log_tail = _file_tail(os.path.join(target_path, log_name), 15)
            if log_tail:
                if log_tail.strip() not in tail:
                    msg += f"\n安装器日志 {log_name}（末尾）：\n{log_tail}"
                break
        return self._fail(msg)

    def _cleanup(self, target_path, installer_name):
        """清理安装器和日志"""
        # run.bat / run.sh 保留给想手动启动的用户；HMSL 自己按 hmsl_launch.json 直接启动 java
        for name in (installer_name, installer_name + ".log") + _INSTALLER_LOGS:
            try:
                p = os.path.join(target_path, name)
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass

    # ----- 通用下载 -----

    def _predownload_vanilla(self, target_path, mc_version, rel_path):
        """
        Fabric 启动器 / Paperclip 首次启动时会用 Java 自己去下原版服务端（慢网下常中断，
        旧 Java 8 还会因证书问题失败）。这里提前用 Python 下好并校验 SHA-1，放到它们的缓存位置。
        失败不算安装失败——首次启动时它们还会自己再试。
        """
        info = mojang_version_json(mc_version)
        server = ((info or {}).get("downloads") or {}).get("server")
        if not server or not server.get("url"):
            return False
        dest_dir = os.path.join(target_path, os.path.dirname(rel_path))
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError:
            return False
        print("正在预下载原版服务端（首次启动时无需再下载）...")
        return self._download(server["url"], dest_dir, os.path.basename(rel_path),
                              sha1=server.get("sha1"), fatal=False)

    def _download(self, url, target_path, filename="server.jar", sha1=None, sha256=None, quiet_404=False,
                  fatal=True):
        """通用下载逻辑：先写 .part，校验通过再改名；网络抖动时重试一次。"""
        print(f"正在下载 {filename}...")
        dest = os.path.join(target_path, filename)
        tmp = dest + ".part"
        last_exc = None
        for attempt in range(2):
            try:
                r = _http_get(url, stream=True)
                if r.status_code != 200:
                    msg = f"下载请求失败 (HTTP {r.status_code})：{url}"
                    if fatal:
                        self.last_error = msg
                    if not (quiet_404 and r.status_code == 404):
                        print(f"  [{'错误' if fatal else '警告'}] {msg}")
                    return False
                h1, h256 = hashlib.sha1(), hashlib.sha256()
                size = 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        f.write(chunk)
                        h1.update(chunk)
                        h256.update(chunk)
                        size += len(chunk)
                if sha1 and h1.hexdigest().lower() != str(sha1).lower():
                    raise ValueError(f"SHA-1 校验失败（期望 {sha1}，实际 {h1.hexdigest()}）")
                if sha256 and h256.hexdigest().lower() != str(sha256).lower():
                    raise ValueError(f"SHA-256 校验失败（期望 {sha256}，实际 {h256.hexdigest()}）")
                os.replace(tmp, dest)
                print(f"  [成功] {filename} 下载完成（{size / 1048576:.1f} MB）。")
                return True
            except (requests.RequestException, ValueError, OSError) as e:
                last_exc = e
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                # 网络错误 / 校验失败重试一次；本地写文件出错（磁盘满、权限）不重试
                if attempt == 0 and isinstance(e, (requests.RequestException, ValueError)):
                    print(f"  [重试] 下载中断：{e}")
                    time.sleep(2)
                    continue
                break
        if not fatal:
            print(f"  [警告] 下载 {filename} 失败：{last_exc}")
            return False
        if isinstance(last_exc, _NET_ERRORS):
            print(f"  [详情] {type(last_exc).__name__}: {last_exc}")
            return self._fail(f"下载 {filename} 失败：{_net_error_text(last_exc, url)}")
        return self._fail(f"下载 {filename} 失败：{last_exc}")


# ---------- 安装产物探测（server_factory 生成启动参数也用它） ----------

_ARGFILE_DIRS = (
    ("libraries", "net", "minecraftforge", "forge"),
    ("libraries", "net", "neoforged", "neoforge"),
    ("libraries", "net", "neoforged", "forge"),
)


def _parse_run_script(path: str) -> Optional[List[str]]:
    """
    从 Forge/NeoForge 安装器生成的 run.bat / run.sh 里取出 java 之后的参数，例如
    `java @user_jvm_args.txt @libraries/.../win_args.txt %*` -> ['@user_jvm_args.txt', '@libraries/.../win_args.txt']
    （1.20.3+ 的 Forge 是 `-jar forge-x-shim.jar`，同样适用）。
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    candidates = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", "REM", "rem", "::", "@")):
            continue
        tokens = s.split()
        exe = tokens[0].strip('"').replace("\\", "/").split("/")[-1].lower()
        if exe not in ("java", "java.exe"):
            continue
        args = []
        for t in tokens[1:]:
            if t in ("||", "&&", "|", "&", ">", ">>", "2>&1"):   # 后面是 shell 语法，不是 java 参数
                break
            args.append(t)
        forwards = any(t in ("%*", '"$@"', "$@") for t in args)
        args = [t for t in args if t not in ("%*", '"$@"', "$@")]
        # 1.21+ 的 run 脚本先跑一遍 `java -jar *-shim.jar --onlyCheckJava` 检查 Java，那不是启动行
        if "--onlyCheckJava" in args:
            continue
        if any(t.startswith("@") or t == "-jar" for t in args):
            candidates.append((forwards, args))
    if not candidates:
        return None
    # 真正的启动行会把脚本参数（%* / "$@"）转给服务器；没有的话取最后一行
    forwarding = [a for fwd, a in candidates if fwd]
    return forwarding[-1] if forwarding else candidates[-1][1]


def _args_files_exist(server_path: str, args: List[str]) -> bool:
    for i, a in enumerate(args):
        if a.startswith("@") and not os.path.isfile(os.path.join(server_path, a[1:])):
            return False
        if a == "-jar" and (i + 1 >= len(args) or not os.path.isfile(os.path.join(server_path, args[i + 1]))):
            return False
    return True


def find_forge_launch_files(server_path: str) -> Optional[dict]:
    """
    检查 Forge/NeoForge 安装器实际产出了什么，返回启动参数：
      {"windows": [...], "unix": [...]}  —— java 之后、nogui 之前的参数
    1.17+：run.bat/run.sh 里的 @user_jvm_args.txt @libraries/.../win_args.txt（或 -jar *-shim.jar）
    1.16.5 及更早：没有 run 脚本，启动 forge-<mc>-<ver>.jar / forge-*-universal.jar
    都没有则返回 None（安装失败）。
    """
    win = _parse_run_script(os.path.join(server_path, "run.bat"))
    unix = _parse_run_script(os.path.join(server_path, "run.sh"))
    if win and not _args_files_exist(server_path, win):
        win = None
    if unix and not _args_files_exist(server_path, unix):
        unix = None
    if win or unix:
        if win is None:
            win = [a.replace("unix_args.txt", "win_args.txt") for a in unix]
        if unix is None:
            unix = [a.replace("win_args.txt", "unix_args.txt") for a in win]
        return {"windows": win, "unix": unix}

    # 没有 run 脚本但有参数文件（有人删了 run.bat）
    for parts in _ARGFILE_DIRS:
        base = os.path.join(server_path, *parts)
        try:
            vers = sorted(os.listdir(base), key=_version_key)
        except OSError:
            continue
        for ver in reversed(vers):
            rel = "/".join(parts + (ver,))
            if os.path.isfile(os.path.join(server_path, rel, "win_args.txt")):
                pre = ["@user_jvm_args.txt"] if os.path.isfile(os.path.join(server_path, "user_jvm_args.txt")) else []
                return {"windows": pre + [f"@{rel}/win_args.txt"], "unix": pre + [f"@{rel}/unix_args.txt"]}

    # 旧版 Forge（< 1.17）：universal / 普通 forge jar
    try:
        jars = [f for f in os.listdir(server_path) if f.lower().endswith(".jar")]
    except OSError:
        jars = []
    forge_jars = [f for f in jars if f.lower().startswith("forge-")
                  and "installer" not in f.lower() and not f.lower().endswith("-shim.jar")]
    if forge_jars:
        universal = [f for f in forge_jars if "universal" in f.lower()]
        jar = max(universal or forge_jars, key=_version_key)
        return {"windows": ["-jar", jar], "unix": ["-jar", jar]}
    return None


_NETWORK_MARKERS = ("failed to download", "download failed", "unknownhost", "timed out", "connection reset",
                    "peer shut down", "remote host terminated", "sockettimeout", "connectexception",
                    "connection refused", "invalid checksum", "these libraries failed",
                    # TLS 握手中断等 SSL 错误同样多是网络/代理抖动，重试常常就好了
                    "handshake", "sslexception", "javax.net.ssl")
# 证书校验失败：旧 Java 8（< 8u141）的证书库太旧时必然出现；新版 Java 上则多半是握手被网络/代理打断
# （Forge 安装器检查连通性失败时也统一打印 "Failed to validate certificates"）
_CERT_MARKERS = ("pkix", "unable to find valid certification path", "failed to validate certificates")


def _installer_hint(output: str, java_cmd: Optional[str] = None) -> Tuple[Optional[str], bool]:
    """根据安装器输出给出提示；第二个值表示是否像是网络问题（值得自动重试）。"""
    low = output.lower()
    if "unsupportedclassversionerror" in low:
        return "Java 版本不匹配（安装器需要更新的 Java）。", False
    cert_problem = any(m in low for m in _CERT_MARKERS)
    if cert_problem and java_cmd and needs_tls_fix(java_cmd):
        return "当前 Java 太旧，无法验证 HTTPS 证书。请安装新版 Java 8（8u141 以上）或 Java 17 后重试。", False
    if any(m in low for m in _NETWORK_MARKERS):
        return "部分文件下载失败，可能是网络不稳定，请稍后重试。", True
    if cert_problem:
        return "无法建立 HTTPS 连接（证书验证未通过），可能是网络不稳定或代理/安全软件拦截了 HTTPS，请检查网络或代理设置后重试。", True
    return None, False


def _file_tail(path: str, n: int) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read()[-20000:]
    except OSError:
        return ""
    text = "\n".join(_decode_line(l) for l in data.splitlines())
    return "\n".join([l for l in text.splitlines() if l.strip()][-n:])
