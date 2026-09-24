"""
Cross-platform Java environment detection.

macOS uses /usr/libexec/java_home (system tool).
Windows collects candidates from JAVA_HOME, the registry, common install dirs,
launcher-bundled runtimes (官方启动器 / HMCL / PCL) and a Python-side PATH walk.
Each candidate's actual version is verified by invoking `<java> -version`
(cached per path) so we never hand the caller a Java 8 binary when they asked
for 21.

Selection rule (find_java): exact major first; otherwise the NEAREST higher
major that is still inside the caller's compatible range. Never a lower major.
"""
from __future__ import annotations

import glob
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
from typing import Dict, List, Optional, Tuple

# winreg only exists on Windows; importing on macOS would crash module load.
if sys.platform == "win32":
    import winreg  # type: ignore
else:
    winreg = None  # type: ignore

# HMSL.exe 是 --windowed 程序，没有控制台；不加这个标志，每个 java -version
# 都会在 Win11 上弹出一个终端窗口。非 Windows 上为 0（无副作用）。
NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


# Order matters: most-likely first so the loop short-circuits fast.
_WIN_REGISTRY_ROOTS = [
    r"SOFTWARE\JavaSoft\JDK",
    r"SOFTWARE\JavaSoft\Java Development Kit",
    r"SOFTWARE\JavaSoft\Java Runtime Environment",
    r"SOFTWARE\Eclipse Adoptium\JDK",
    r"SOFTWARE\Eclipse Adoptium\JRE",
    r"SOFTWARE\Eclipse Foundation\JDK",
    r"SOFTWARE\Microsoft\JDK",
    r"SOFTWARE\Amazon Corretto",
    r"SOFTWARE\Azul Systems\Zulu",
    r"SOFTWARE\BellSoft\Liberica",
    r"SOFTWARE\Semeru",
]

# Glob patterns (relative to each Program Files root) for installer-default directories
_WIN_PROGRAM_FILES_GLOBS = [
    r"Java\jdk*\bin\java.exe",
    r"Java\jre*\bin\java.exe",
    r"Java\latest\*\bin\java.exe",
    r"Eclipse Adoptium\*\bin\java.exe",
    r"Eclipse Foundation\*\bin\java.exe",
    r"Microsoft\jdk*\bin\java.exe",
    r"Amazon Corretto\*\bin\java.exe",
    r"Zulu\*\bin\java.exe",
    r"BellSoft\*\bin\java.exe",
    r"Semeru\*\bin\java.exe",
    r"OpenJDK\*\bin\java.exe",
    r"GraalVM\*\bin\java.exe",
]

# 启动器自带的运行时（相对 %APPDATA% / %LOCALAPPDATA% / 用户目录）
_WIN_USER_GLOBS = [
    (r"APPDATA", r".minecraft\runtime\*\*\*\bin\java.exe"),
    (r"APPDATA", r".hmcl\java\*\*\bin\java.exe"),
    (r"LOCALAPPDATA", r"Packages\Microsoft.4297127D64EC6_8wekyb3d8bbwe\LocalCache\Local\runtime\*\*\*\bin\java.exe"),
    (r"USERPROFILE", r".jdks\*\bin\java.exe"),
    (r"USERPROFILE", r"scoop\apps\*\current\bin\java.exe"),
]

# Java 8 builds older than this lack the root CAs used by Mojang/Let's Encrypt
# (DigiCert Global Root G2 arrived in 8u91, ISRG Root X1 in 8u141): TLS to
# launchermeta/libraries fails with "PKIX path building failed".
_JAVA8_MIN_TLS_UPDATE = 141

JavaVersion = Tuple[int, int, int]   # (major, minor/security, update)

# 中文 Windows 上 Java 8/17 往管道里写的是 GBK；统一强制 UTF-8，HMSL 控制台才能正确显示中文，
# 发送的中文命令也按 UTF-8 读取（-Dstdout.encoding 是 Java 19+，-Dsun.stdout.encoding 是 Java 8-18）。
UTF8_JVM_FLAGS = [
    "-Dfile.encoding=UTF-8",
    "-Dstdout.encoding=UTF-8",
    "-Dstderr.encoding=UTF-8",
    "-Dsun.stdout.encoding=UTF-8",
    "-Dsun.stderr.encoding=UTF-8",
]


def parse_java_major_version(version_output: str) -> Optional[int]:
    """
    Extract the major version (8, 11, 17, 21, ...) from `java -version` output.

    Handles both legacy Java 8 form (`version "1.8.0_xxx"`) and modern form
    (`version "17.0.2"` / `version "21"`).
    """
    v = parse_java_version(version_output)
    return v[0] if v else None


def parse_java_version(version_output: str) -> Optional[JavaVersion]:
    """
    `version "1.8.0_51"` -> (8, 0, 51); `version "17.0.12"` -> (17, 0, 12);
    `version "21" 2023-09-19` -> (21, 0, 0); `version "25-ea"` -> (25, 0, 0).
    """
    m = re.search(r'version "(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[._](\d+))?', version_output)
    if not m:
        return None
    a = int(m.group(1))
    b = int(m.group(2) or 0)
    c = int(m.group(3) or 0)
    d = int(m.group(4) or 0)
    if a == 1 and m.group(2):
        # 1.8.0_51 -> major 8, update 51
        return (b, c, d)
    # 17.0.12 -> (17, 0, 12); 21.0.7(.6) -> (21, 0, 7)
    return (a, b, c)


# ---------- probing (cached) ----------

_probe_lock = threading.Lock()
_probe_cache: Dict[str, Optional[Tuple[JavaVersion, bool]]] = {}


def _cache_key(java_path: str) -> str:
    try:
        return os.path.normcase(os.path.realpath(java_path))
    except (OSError, ValueError):
        return os.path.normcase(java_path)


def probe_java(java_path: str, timeout: float = 15.0) -> Optional[Tuple[JavaVersion, bool]]:
    """
    Run `<java> -version` once per path (cached) and return ((major, minor, update), is_64bit),
    or None if it is not a working Java.
    """
    if os.path.basename(java_path) == java_path:      # 裸 "java"：先按 PATH 解析成绝对路径
        java_path = shutil.which(java_path) or ""
        if not java_path:
            return None
    key = _cache_key(java_path)
    with _probe_lock:
        if key in _probe_cache:
            return _probe_cache[key]
    try:
        r = subprocess.run(
            [java_path, "-version"],
            capture_output=True, timeout=timeout,
            creationflags=NO_WINDOW_FLAGS,
        )
        # -version prints to STDERR; ASCII is all we need, so decode loosely.
        out = (r.stderr or b"").decode("utf-8", "replace") + (r.stdout or b"").decode("utf-8", "replace")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
        # 超时不缓存：冷启动/杀软扫描时可能偶发超时，下次还可以再试
        return None
    ver = parse_java_version(out)
    result = None
    if ver is not None:
        result = (ver, "64-bit" in out.lower())
    with _probe_lock:
        _probe_cache[key] = result
    return result


def java_version_of(java_path: str) -> Optional[JavaVersion]:
    info = probe_java(java_path)
    return info[0] if info else None


def java_major_version_of(java_path: str, timeout: float = 15.0) -> Optional[int]:
    """Run `<java> -version` and return its detected major version, or None."""
    info = probe_java(java_path, timeout=timeout)
    return info[0][0] if info else None


def java_is_64bit(java_path: str) -> Optional[bool]:
    info = probe_java(java_path)
    return info[1] if info else None


def needs_tls_fix(java_path: str) -> bool:
    """Old Java 8 (< 8u141) cannot verify Mojang/Let's Encrypt certificates."""
    v = java_version_of(java_path)
    return bool(v and v[0] == 8 and v[2] < _JAVA8_MIN_TLS_UPDATE)


def java_tls_fix_args(java_path: str) -> List[str]:
    """
    JVM flags that let an outdated Java 8 do TLS to Mojang / Forge / Fabric.
    On Windows we point it at the OS certificate store (always up to date);
    elsewhere there is no equivalent one-flag fix, so return nothing.
    """
    if sys.platform == "win32" and needs_tls_fix(java_path):
        return ["-Djavax.net.ssl.trustStoreType=Windows-ROOT"]
    return []


# ---------- platform: Darwin ----------

def _find_java_on_darwin(required: int) -> Optional[str]:
    try:
        out = subprocess.check_output(
            ["/usr/libexec/java_home", "-v", str(required)],
            stderr=subprocess.DEVNULL,
            encoding="utf-8", errors="replace",
            timeout=15,
        ).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    candidate = os.path.join(out, "bin", "java")
    return candidate if os.path.isfile(candidate) else None


def _candidates_on_darwin() -> List[str]:
    """`java_home -V` lists every registered JVM (on stderr)."""
    found: List[str] = []
    try:
        r = subprocess.run(["/usr/libexec/java_home", "-V"],
                           capture_output=True, timeout=15)
        text = (r.stderr or b"").decode("utf-8", "replace") + (r.stdout or b"").decode("utf-8", "replace")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        text = ""
    for line in text.splitlines():
        # `    17.0.2 (arm64) "Eclipse Adoptium" - "OpenJDK 17.0.2" /Library/.../Contents/Home`
        s = line.strip()
        if s.startswith("/"):
            home = s
        else:
            m = re.search(r'"\s+(/.+)$', s)
            if not m:
                continue
            home = m.group(1)
        cand = os.path.join(home, "bin", "java")
        if os.path.isfile(cand):
            found.append(cand)
    for pattern in ("/Library/Java/JavaVirtualMachines/*/Contents/Home/bin/java",
                    os.path.expanduser("~/Library/Java/JavaVirtualMachines/*/Contents/Home/bin/java"),
                    "/opt/homebrew/opt/openjdk*/bin/java",
                    "/usr/local/opt/openjdk*/bin/java"):
        found.extend(glob.glob(pattern))
    return found


# ---------- platform: Windows ----------

def _candidates_from_java_home() -> List[str]:
    java_home = os.environ.get("JAVA_HOME")
    if not java_home:
        return []
    exe = "java.exe" if sys.platform == "win32" else "java"
    cand = os.path.join(java_home.strip().strip('"'), "bin", exe)
    return [cand] if os.path.isfile(cand) else []


def _candidates_from_registry() -> List[str]:
    """Walk the well-known Java vendor keys (HKLM 64/32-bit views + HKCU), collect JavaHome paths."""
    if winreg is None:
        return []
    found: List[str] = []
    views = [
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_CURRENT_USER, 0),
    ]
    for hive, view in views:
        for root_path in _WIN_REGISTRY_ROOTS:
            try:
                root_key = winreg.OpenKey(hive, root_path, 0, winreg.KEY_READ | view)
            except OSError:
                continue
            try:
                for i in range(0, 256):  # version subkeys, bounded
                    try:
                        version_key_name = winreg.EnumKey(root_key, i)
                    except OSError:
                        break
                    try:
                        vk = winreg.OpenKey(root_key, version_key_name, 0, winreg.KEY_READ | view)
                    except OSError:
                        continue
                    # Path can live under "JavaHome" (Oracle), "InstallationPath" (Zulu)
                    # or under "hotspot\MSI\Path" (Adoptium) — try a few names.
                    home = (_read_str_value(vk, "JavaHome")
                            or _read_str_value(vk, "InstallationPath")
                            or _read_nested_path(vk))
                    vk.Close()
                    if home:
                        cand = os.path.join(home, "bin", "java.exe")
                        if os.path.isfile(cand):
                            found.append(cand)
            finally:
                root_key.Close()
    return found


def _read_str_value(key, name: str) -> Optional[str]:
    if winreg is None:
        return None
    try:
        val, _ = winreg.QueryValueEx(key, name)
        return val if isinstance(val, str) else None
    except OSError:
        return None


def _read_nested_path(key) -> Optional[str]:
    """Adoptium-style: SOFTWARE\\Eclipse Adoptium\\JDK\\<ver>\\hotspot\\MSI\\Path"""
    if winreg is None:
        return None
    for sub in (r"hotspot\MSI", r"openj9\MSI"):
        try:
            k = winreg.OpenKey(key, sub)
        except OSError:
            continue
        try:
            v = _read_str_value(k, "Path")
            if v:
                return v
        finally:
            k.Close()
    return None


def _candidates_from_common_dirs() -> List[str]:
    out: List[str] = []
    roots = []
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        v = os.environ.get(var)
        if v and v not in roots:
            roots.append(v)
    for fallback in (r"C:\Program Files", r"C:\Program Files (x86)"):
        if fallback not in roots:
            roots.append(fallback)
    for root in roots:
        for pattern in _WIN_PROGRAM_FILES_GLOBS:
            out.extend(glob.glob(os.path.join(root, pattern)))
    for var, pattern in _WIN_USER_GLOBS:
        base = os.environ.get(var)
        if base:
            out.extend(glob.glob(os.path.join(base, pattern)))
    return out


def _candidates_from_path() -> List[str]:
    """
    Walk PATH in Python (Unicode-safe, no subprocess, no console flash).
    Replaces `where java`, whose output is in the console code page and turned
    every CJK directory into U+FFFD. Unlike shutil.which this returns EVERY
    java on PATH, not just the first one.
    """
    exe = "java.exe" if sys.platform == "win32" else "java"
    out: List[str] = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        d = os.path.expandvars(d.strip().strip('"'))
        if not d:
            continue
        cand = os.path.join(d, exe)
        if os.path.isfile(cand):
            out.append(cand)
    return out


def _candidates_from_where() -> List[str]:
    """Kept for backward compatibility; now a pure-Python PATH walk (see above)."""
    return _candidates_from_path()


def _candidates_on_linux() -> List[str]:
    out = _candidates_from_java_home() + _candidates_from_path()
    out.extend(glob.glob("/usr/lib/jvm/*/bin/java"))
    out.extend(glob.glob(os.path.expanduser("~/.sdkman/candidates/java/*/bin/java")))
    return out


def _all_candidates() -> List[str]:
    if sys.platform == "win32":
        getters = (_candidates_from_java_home, _candidates_from_registry,
                   _candidates_from_common_dirs, _candidates_from_path)
        raw: List[str] = []
        for g in getters:
            try:
                raw.extend(g())
            except Exception:
                continue
    elif sys.platform == "darwin":
        raw = _candidates_from_java_home() + _candidates_on_darwin() + _candidates_from_path()
    else:
        raw = _candidates_on_linux()
    seen = set()
    out: List[str] = []
    for c in raw:
        key = _cache_key(c)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def list_installed_javas() -> List[Tuple[str, JavaVersion]]:
    """Every working Java we can find, with its version: [(path, (major, minor, update)), ...]."""
    result: List[Tuple[str, JavaVersion]] = []
    for c in _all_candidates():
        v = java_version_of(c)
        if v is not None:
            result.append((c, v))
    return result


def select_java(candidates: List[Tuple[str, JavaVersion]], required: int,
                max_major: Optional[int] = None) -> Optional[str]:
    """
    Pick from [(path, version)]: exact major first, then the nearest higher major
    (<= max_major when given). Among the same major prefer the newest update
    (an ancient 8u51 is the worst Java 8 to pick). Never returns a lower major.
    """
    ok = [(p, v) for p, v in candidates
          if v[0] >= required and (max_major is None or v[0] <= max_major)]
    if not ok:
        return None
    ok.sort(key=lambda pv: (pv[1][0] - required, tuple(-x for x in pv[1])))
    return ok[0][0]


def _find_java_on_windows(required: int, max_major: Optional[int] = None) -> Optional[str]:
    """Walk all strategies; exact major first, then the nearest compatible higher major."""
    return select_java(list_installed_javas(), required, max_major)


# ---------- public ----------

class EnvManager:
    def __init__(self):
        # SCRIPT_DIR = 项目根，GUI 用它作为"创建服务器"的默认目录。
        if getattr(sys, "frozen", False):
            # PyInstaller 打包后 __file__ 指向临时解包目录，不能用。改用可执行程序所在的
            # 位置，让服务器建在程序旁边（用户看得见、可写）。Mac 的 .app 与 Windows 单
            # 文件 exe 结构不同，分别处理：
            exe = os.path.abspath(sys.executable)
            if sys.platform == "darwin" and ".app/Contents/MacOS" in exe:
                # macOS .app：.../HMSL.app/Contents/MacOS/HMSL → 上溯 4 级到 .app 的同级目录
                self.script_dir = os.path.dirname(os.path.dirname(os.path.dirname(
                    os.path.dirname(exe))))
            else:
                # Windows / Linux 单文件 exe：直接用 exe 所在目录
                self.script_dir = os.path.dirname(exe)
        else:
            self.script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def find_java(self, required: int, max_major: Optional[int] = None) -> Optional[str]:
        """
        Return an absolute java executable path for `required`: exact major first,
        else the nearest installed higher major (not above `max_major` when given).
        Returns None when no suitable Java is installed — never a lower major.
        """
        system = platform.system()
        if system == "Darwin":
            exact = _find_java_on_darwin(required)
            if exact and java_major_version_of(exact) == required:
                return exact
            return select_java(list_installed_javas(), required, max_major)
        return select_java(list_installed_javas(), required, max_major)

    def get_java_cmd(self, required_version: int) -> str:
        """
        Return an absolute java executable path for `required_version` (see find_java),
        or the bare string "java" if nothing suitable is installed (legacy behaviour;
        new callers should use find_java and report the missing Java instead).
        """
        return self.find_java(required_version) or "java"

    def list_javas(self) -> List[Tuple[str, JavaVersion]]:
        return list_installed_javas()
