"""
Cross-platform Java environment detection.

macOS uses /usr/libexec/java_home -v N (system tool).
Windows tries four strategies in order: JAVA_HOME, registry, common install
dirs, then `where java`. Each candidate's actual major version is verified
by invoking `<java> -version` so we never hand the caller a Java 8 binary
when they asked for 21.
"""
from __future__ import annotations

import glob
import os
import platform
import re
import subprocess
import sys
from typing import List, Optional

# winreg only exists on Windows; importing on macOS would crash module load.
if sys.platform == "win32":
    import winreg  # type: ignore
else:
    winreg = None  # type: ignore


# Order matters: most-likely first so the loop short-circuits fast.
_WIN_REGISTRY_ROOTS = [
    r"SOFTWARE\JavaSoft\JDK",
    r"SOFTWARE\JavaSoft\Java Development Kit",
    r"SOFTWARE\JavaSoft\Java Runtime Environment",
    r"SOFTWARE\Eclipse Adoptium\JDK",
    r"SOFTWARE\Eclipse Foundation\JDK",
    r"SOFTWARE\Microsoft\JDK",
    r"SOFTWARE\Amazon Corretto",
    r"SOFTWARE\Azul Systems\Zulu",
    r"SOFTWARE\Semeru",
]

# Glob roots for installer-default directories
_WIN_COMMON_GLOBS = [
    r"C:\Program Files\Java\jdk*\bin\java.exe",
    r"C:\Program Files\Java\jre*\bin\java.exe",
    r"C:\Program Files\Eclipse Adoptium\jdk*\bin\java.exe",
    r"C:\Program Files\Eclipse Foundation\jdk*\bin\java.exe",
    r"C:\Program Files\Microsoft\jdk*\bin\java.exe",
    r"C:\Program Files\Amazon Corretto\jdk*\bin\java.exe",
    r"C:\Program Files\Zulu\zulu*\bin\java.exe",
    r"C:\Program Files (x86)\Java\jdk*\bin\java.exe",
    r"C:\Program Files (x86)\Java\jre*\bin\java.exe",
]


def parse_java_major_version(version_output: str) -> Optional[int]:
    """
    Extract the major version (8, 11, 17, 21, ...) from `java -version` output.

    Handles both legacy Java 8 form (`version "1.8.0_xxx"`) and modern form
    (`version "17.0.2"` / `version "21"`).
    """
    m = re.search(r'version "(\d+)(?:\.(\d+))?', version_output)
    if not m:
        return None
    major = int(m.group(1))
    if major == 1 and m.group(2):
        return int(m.group(2))
    return major


def java_major_version_of(java_path: str, timeout: float = 5.0) -> Optional[int]:
    """Run `<java> -version` and return its detected major version, or None."""
    try:
        r = subprocess.run(
            [java_path, "-version"],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    # -version prints to STDERR; concat both just to be safe.
    return parse_java_major_version((r.stderr or "") + (r.stdout or ""))


# ---------- platform: Darwin ----------

def _find_java_on_darwin(required: int) -> Optional[str]:
    try:
        out = subprocess.check_output(
            ["/usr/libexec/java_home", "-v", str(required)],
            stderr=subprocess.DEVNULL,
            encoding="utf-8", errors="replace",
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    candidate = os.path.join(out, "bin", "java")
    return candidate if os.path.isfile(candidate) else None


# ---------- platform: Windows ----------

def _candidates_from_java_home() -> List[str]:
    java_home = os.environ.get("JAVA_HOME")
    if not java_home:
        return []
    cand = os.path.join(java_home, "bin", "java.exe")
    return [cand] if os.path.isfile(cand) else []


def _candidates_from_registry() -> List[str]:
    """Walk the well-known Java vendor keys under HKLM, collect JavaHome paths."""
    if winreg is None:
        return []
    found: List[str] = []
    for root_path in _WIN_REGISTRY_ROOTS:
        try:
            root_key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root_path)
        except OSError:
            continue
        try:
            for i in range(0, 64):  # version subkeys, bounded
                try:
                    version_key_name = winreg.EnumKey(root_key, i)
                except OSError:
                    break
                try:
                    vk = winreg.OpenKey(root_key, version_key_name)
                except OSError:
                    continue
                # Path can live under "JavaHome" (Oracle) or under "hotspot\MSI\Path"
                # (Adoptium) — try a few names.
                home = _read_str_value(vk, "JavaHome") or _read_nested_path(vk)
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
    try:
        hotspot = winreg.OpenKey(key, r"hotspot\MSI")
    except OSError:
        return None
    try:
        return _read_str_value(hotspot, "Path")
    finally:
        hotspot.Close()


def _candidates_from_common_dirs() -> List[str]:
    out: List[str] = []
    for pattern in _WIN_COMMON_GLOBS:
        out.extend(glob.glob(pattern))
    return out


def _candidates_from_where() -> List[str]:
    try:
        r = subprocess.run(
            ["where", "java"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    if r.returncode != 0:
        return []
    return [line.strip() for line in (r.stdout or "").splitlines() if line.strip()]


def _find_java_on_windows(required: int) -> Optional[str]:
    """Walk all strategies, return the first candidate whose actual version matches."""
    seen = set()
    all_candidates: List[str] = []
    for getter in (_candidates_from_java_home,
                   _candidates_from_registry,
                   _candidates_from_common_dirs,
                   _candidates_from_where):
        for c in getter():
            c_norm = os.path.normcase(os.path.abspath(c))
            if c_norm in seen:
                continue
            seen.add(c_norm)
            all_candidates.append(c)
    for c in all_candidates:
        if java_major_version_of(c) == required:
            return c
    return None


# ---------- public ----------

class EnvManager:
    def __init__(self):
        # Lock the project root to this file's parent's parent. SCRIPT_DIR is
        # used by GUI to default the "create server" directory.
        self.script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def get_java_cmd(self, required_version: int) -> str:
        """
        Return an absolute java executable path matching `required_version`,
        or the bare string "java" if nothing matches (so the caller still runs
        and surfaces a clearer downstream error).
        """
        system = platform.system()
        if system == "Darwin":
            found = _find_java_on_darwin(required_version)
        elif system == "Windows":
            found = _find_java_on_windows(required_version)
        else:
            # Linux: rely on PATH for now. Future: walk update-alternatives.
            found = None
        return found or "java"
