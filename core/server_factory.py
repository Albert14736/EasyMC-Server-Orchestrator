"""
Pure orchestration for creating a Minecraft server instance.

This module contains NO GUI code and NO direct prints to stdout — all status
flows through the optional progress_callback so it can be driven from a GUI,
a CLI, or a pytest harness equally. (The installer it drives still prints its
download/installer log; the GUI redirects that into its log box.)

Besides the server files, create_server writes:
  * hmsl_launch.json —— 机器可读的启动描述（java 路径 + 参数），launcher 直接按它启动 java；
  * start.bat / start.sh —— 给想双击手动启动的用户，内容与上面等价（只有控制台输出相关的参数不同，
    见 _render_bat）；用户改过它（比如调内存）之后，launcher 改为按脚本启动，修改照样生效（见 start_script_state）。
"""
from __future__ import annotations

import hashlib
import inspect
import os
import re
import shlex
import shutil
import sys
import traceback
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from core.env_manager import UTF8_JVM_FLAGS, java_is_64bit, java_major_version_of, java_tls_fix_args
from core.launcher import LAUNCH_SPEC_FORMAT, LAUNCHED_ENV_VAR, PIPE_CONSOLE_JVM_FLAGS, write_launch_spec
from core.server_installer import find_forge_launch_files


ProgressCallback = Callable[[float, str], None]

SUPPORTED_LOADERS = ("paper", "fabric", "forge", "neoforge", "vanilla")
_LOADER_LABEL = {"paper": "Paper", "fabric": "Fabric", "forge": "Forge",
                 "neoforge": "NeoForge", "vanilla": "原版 (Vanilla)"}


@dataclass
class CreateServerResult:
    success: bool
    server_path: str
    error: Optional[str] = None
    warnings: List[str] = field(default_factory=list)


# ---------- 名称校验 ----------

_ILLEGAL_NAME_CHARS = '<>:"/\\|?*'
_WIN_RESERVED_NAMES = ({"CON", "PRN", "AUX", "NUL"}
                       | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
                       | {"COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³"})


def validate_server_name(name: str) -> Optional[str]:
    """
    检查服务器（文件夹）名称，合法返回 None，否则返回中文错误信息。
    所有平台都按 Windows 的规则检查——服务器文件夹应当能在各系统间拷来拷去。
    """
    if name is None or not str(name).strip():
        return "服务器名称不能为空"
    name = str(name)
    bad = sorted({c for c in name if c in _ILLEGAL_NAME_CHARS})
    if bad:
        return f"服务器名称不能包含以下字符：{' '.join(bad)}"
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return "服务器名称不能包含控制字符（如换行、制表符）"
    if any(ord(c) > 0xFFFF for c in name):
        # Java 无法从含 emoji 等增补字符的目录加载 jar（URL 解码报错）
        return "服务器名称不能包含 emoji 等特殊符号"
    if name.rstrip(" .") != name:
        return "服务器名称不能以空格或句点结尾"
    stem = name.split(".")[0].rstrip(" ").upper()
    if stem in _WIN_RESERVED_NAMES:
        return f"“{name}”是 Windows 的保留设备名，不能用作文件夹名称"
    if len(name) > 100:
        return "服务器名称过长（最多 100 个字符）"
    return None


# ---------- Java 版本要求 ----------

def _parse_mc_version(mc_version: str) -> Optional[Tuple[int, int, int]]:
    m = re.match(r"\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(mc_version or ""))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)


def required_java_version(mc_version: str) -> int:
    """
    Return the Java major version required by a given Minecraft version.

    Replaces the previous string comparison `version >= "1.20.5"`, which was
    wrong because '8' > '2' lexicographically meant "1.8.8" mapped to Java 21.
    Year-based versions (26.1, 26.2, …) need Java 25 per Mojang's metadata.
    """
    parts = _parse_mc_version(mc_version)
    if parts is None:
        return 21
    major, minor, patch = parts
    if major >= 26:
        return 25
    if major != 1:
        return 21
    if minor > 20 or (minor == 20 and patch >= 5) or minor >= 21:
        return 21
    if minor >= 18:
        return 17
    if minor == 17:
        return 16
    return 8


def java_version_range(mc_version: str, loader: str) -> Tuple[int, Optional[int]]:
    """
    (最低, 最高) Java 主版本；最高为 None 表示更新的都行。find_java 先找恰好等于最低版本的，
    没有再取范围内最接近的更高版本，绝不会用更低的。判断依据：
      * Forge < 1.17 只能跑在 Java 8 上（更高版本的类加载器改动会让它崩溃）；
      * 原版/Paper/Fabric ≤ 1.16：首选 8，没有 8 时 1.16.x 可用到 17、更早的到 11；
      * Forge/NeoForge 1.17–1.19 最多 17，1.20–1.20.4 最多 21（旧 ModLauncher/ASM 不认识更新的 class 版本）；
      * 其余（原版/Paper/Fabric 1.17+、1.20.5+、26.x）向上兼容，不设上限。
    """
    req = required_java_version(mc_version)
    parts = _parse_mc_version(mc_version)
    loader_norm = (loader or "").strip().lower()
    if parts and parts[0] == 1:
        minor, patch = parts[1], parts[2]
        if loader_norm == "forge" and minor < 17:
            return 8, 8
        if req == 8:
            return 8, (17 if minor >= 16 else 11)
        if loader_norm in ("forge", "neoforge"):
            if minor <= 19:
                return req, 17
            if minor == 20 and patch <= 4:
                return req, 21
    return req, None


def _range_text(java_min: int, java_max: Optional[int]) -> str:
    if java_max is None:
        return f"{java_min} 或更高版本"
    if java_max == java_min:
        return f"{java_min}"
    return f"{java_min}–{java_max}"


def _missing_java_message(version: str, java_min: int, java_max: Optional[int], env_manager) -> str:
    msg = (f"未找到 Java {java_min}：Minecraft {version} 需要 Java {_range_text(java_min, java_max)}。"
           f"请先安装 Java {java_min}（推荐 Eclipse Temurin 或 Microsoft Build of OpenJDK {java_min}），"
           f"安装后重新创建服务器。")
    lister = getattr(env_manager, "list_javas", None)
    if callable(lister):
        try:
            majors = sorted({v[0] for _p, v in lister()})
            msg += f"\n本机已检测到的 Java：{', '.join(map(str, majors)) if majors else '无'}"
        except Exception:
            pass
    return msg


# ---------- 启动描述 / 启动脚本 ----------

def _heap_args(java_cmd: str) -> List[str]:
    # 32 位 Java 最多只能分到约 1.4G 堆，-Xmx4G 会直接启动失败
    if java_is_64bit(java_cmd) is False:
        return ["-Xms512M", "-Xmx1G"]
    return ["-Xms2G", "-Xmx4G"]


def build_launch_spec(server_path: str, loader: str, mc_version: str, java_cmd: str,
                      java_min: Optional[int] = None, java_max: Optional[int] = None) -> Optional[dict]:
    """
    按服务器目录里实际存在的文件生成启动描述；找不到可启动的东西返回 None。
      Paper / Fabric / 原版：-jar server.jar
      Forge / NeoForge 1.17+：@user_jvm_args.txt @libraries/.../win_args.txt（取自安装器生成的 run.bat/run.sh）
      Forge < 1.17：-jar forge-<mc>-<ver>.jar（或 *-universal.jar）
    """
    loader_norm = (loader or "").strip().lower()
    launch = None
    if loader_norm in ("forge", "neoforge"):
        launch = find_forge_launch_files(server_path)
    if launch is None and os.path.isfile(os.path.join(server_path, "server.jar")):
        launch = {"windows": ["-jar", "server.jar"], "unix": ["-jar", "server.jar"]}
    if launch is None:
        return None

    heap = _heap_args(java_cmd)
    argfile_based = any(a == "@user_jvm_args.txt" for a in launch["windows"])
    jvm_args: List[str] = []
    if not argfile_based:
        jvm_args += heap           # Forge/NeoForge 的内存写在 user_jvm_args.txt，用户在那里改
    jvm_args += UTF8_JVM_FLAGS
    jvm_args += PIPE_CONSOLE_JVM_FLAGS     # 只给 HMSL 自己的启动用，启动脚本里不写（见 _script_jvm_args）
    jvm_args += java_tls_fix_args(java_cmd)
    return {
        "format": LAUNCH_SPEC_FORMAT,
        "generated_by": "HMSL",
        "loader": loader_norm,
        "mc_version": mc_version,
        "java": java_cmd,
        "java_major": java_major_version_of(java_cmd),
        "java_min": java_min,
        "java_max": java_max,
        "heap_args": heap,
        "jvm_args": jvm_args,
        "launch_args": launch,
        "program_args": ["nogui"],
    }


def _ensure_heap_in_user_jvm_args(server_path: str, heap: List[str]) -> None:
    """Forge/NeoForge：安装器生成的 user_jvm_args.txt 默认没有内存设置，补上 HMSL 的默认值。"""
    p = os.path.join(server_path, "user_jvm_args.txt")
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return
    if re.search(r"(?m)^\s*-Xm[sx]", text):
        return
    try:
        with open(p, "a", encoding="utf-8", newline="") as f:
            if text and not text.endswith("\n"):
                f.write("\n")
            # 只写 ASCII：Java 读参数文件用的是系统编码
            f.write("# Default heap size set by HMSL (edit as needed)\n" + "\n".join(heap) + "\n")
    except OSError:
        pass


def _oem_codec() -> str:
    try:
        "".encode("oem")
        return "oem"
    except LookupError:
        try:
            import ctypes
            return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
        except Exception:
            return "mbcs"


def _short_path(path: str) -> Optional[str]:
    """8.3 短路径（纯 ASCII），卷上关闭了短文件名时返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024)
        return buf.value if 0 < n < 1024 else None
    except Exception:
        return None


def _bat_quote(arg: str) -> str:
    arg = arg.replace("%", "%%")
    return f'"{arg}"' if re.search(r'[\s&()<>^|]', arg) else arg


# 启动脚本模板的代次：3 = 当前；2 = .bat 里去掉了全部 UTF-8 标志（含 -Dfile.encoding）；
# 1 = 早期版本（所有 UTF-8 标志都写进 .bat、说明文字不同）。
# 旧模板只用来认出"HMSL 生成、用户没改过"的脚本（start_script_state），不再用来写文件。
SCRIPT_TEMPLATE = 3
_BAT_REMARKS = {
    1: ["rem Generated by HMSL. HMSL itself starts the server from hmsl_launch.json;",
        "rem this script is for starting it by hand (double-click)."],
    2: ["rem Generated by HMSL for starting the server by hand (double-click).",
        "rem HMSL normally starts the server from hmsl_launch.json; once this file is",
        "rem edited (e.g. -Xmx for more memory), HMSL starts the server with this file."],
}
_BAT_REMARKS[3] = _BAT_REMARKS[2]

# 只决定控制台输出编码的参数（UTF8_JVM_FLAGS 里 -Dfile.encoding 以外的那些）
_CONSOLE_ENCODING_FLAGS = [f for f in UTF8_JVM_FLAGS if not f.startswith("-Dfile.encoding=")]


def _script_jvm_args(spec: dict) -> List[str]:
    """写进启动脚本的 JVM 参数：去掉只适用于 HMSL 管道控制台的参数（双击运行时是真正的终端）。"""
    return [str(a) for a in spec.get("jvm_args", []) if str(a) not in PIPE_CONSOLE_JVM_FLAGS]


def _render_bat(spec: dict, template: int = SCRIPT_TEMPLATE) -> bytes:
    """
    Windows 启动脚本。cmd.exe 按控制台（OEM）代码页读 .bat，所以用 OEM 编码写；
    java 路径里有 OEM 代码页表示不了的字符时改用 8.3 短路径，再不行就 chcp 65001 + UTF-8。
    HMSL 自己启动时设置了 HMSL_LAUNCHED，这时不 pause（否则 cmd 会卡在读管道上）。
    """
    def args(utf8_console: bool) -> List[str]:
        jvm = _script_jvm_args(spec)
        if not utf8_console and template >= 3:
            # 双击运行时 java 直接写 OEM（GBK）代码页的控制台：不强制 stdout/stderr 用 UTF-8，
            # System.out 按控制台代码页输出。-Dfile.encoding=UTF-8 必须保留：它是插件/模组读写配置和
            # 数据文件用的默认字符集，要和 HMSL 自己启动时（hmsl_launch.json）一致，否则换一种方式
            # 启动后，按一种字符集写下的中文配置会按另一种读成乱码。
            # 代价（只影响显示）：log4j 按默认字符集输出，双击的控制台里 log4j 日志行中的中文显示为
            # 乱码（Java 18+ 默认就是 UTF-8，本来也如此）；logs/latest.log 和 HMSL 的控制台都正常。
            jvm = [a for a in jvm if a not in _CONSOLE_ENCODING_FLAGS]
        elif not utf8_console and template == 2:
            jvm = [a for a in jvm if a not in UTF8_JVM_FLAGS]
        return (jvm + [str(a) for a in spec["launch_args"]["windows"]]
                + [str(a) for a in spec.get("program_args", ["nogui"])])

    def body(java_path: str, utf8: bool = False) -> str:
        lines = ["@echo off"]
        if utf8:
            lines.append("chcp 65001 >nul")
        lines += _BAT_REMARKS[template] + [
            'cd /d "%~dp0"',
            f'"{java_path.replace("%", "%%")}" ' + " ".join(_bat_quote(a) for a in args(utf8)),
            f"if not defined {LAUNCHED_ENV_VAR} pause",
        ]
        return "\r\n".join(lines) + "\r\n"

    java = str(spec["java"])
    codec = _oem_codec()
    try:
        return body(java).encode(codec)
    except UnicodeEncodeError:
        pass
    short = _short_path(java)
    if short and short != java:
        try:
            return body(short).encode(codec)
        except UnicodeEncodeError:
            pass
    return body(java, utf8=True).encode("utf-8")


def _render_sh(spec: dict, template: int = SCRIPT_TEMPLATE) -> str:
    args = ([str(spec["java"])] + _script_jvm_args(spec) + list(spec["launch_args"]["unix"])
            + list(spec.get("program_args", ["nogui"])))
    remarks = "" if template < 2 else (
        '# Generated by HMSL for starting the server by hand. HMSL normally starts the server from\n'
        '# hmsl_launch.json; once this file is edited (e.g. -Xmx for more memory), HMSL runs this file.\n')
    return ('#!/bin/zsh\n' + remarks
            + 'cd "$(dirname "$0")"\nexec ' + " ".join(shlex.quote(str(a)) for a in args) + "\n")


def _start_script_for_platform(spec: dict, template: int = SCRIPT_TEMPLATE) -> Tuple[str, bytes, bool]:
    """Return (filename, content bytes, needs_chmod) for the platform's launch script."""
    if sys.platform == "win32":
        return "start.bat", _render_bat(spec, template), False
    return "start.sh", _render_sh(spec, template).encode("utf-8"), True


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_script(server_path: str, spec: dict) -> str:
    """写平台启动脚本，并把它的 SHA-256 记进 spec["script"]（之后据此判断用户有没有改过它）。"""
    script_name, script_body, needs_chmod = _start_script_for_platform(spec)
    script_path = os.path.join(server_path, script_name)
    with open(script_path, "wb") as f:
        f.write(script_body)
    if needs_chmod:
        os.chmod(script_path, 0o755)
    spec["script"] = {"name": script_name, "sha256": _sha256(script_body)}
    return script_path


def write_launch_files(server_path: str, spec: dict, *, ensure_heap: bool = True) -> str:
    """
    写 hmsl_launch.json 和平台启动脚本，返回启动脚本路径。
    ensure_heap：Forge/NeoForge 新建的服务器往 user_jvm_args.txt 里补 HMSL 的默认内存；
    升级旧版 HMSL 建的服务器时传 False（它们一直按 JVM 默认内存运行，不能悄悄改成 4G 上限）。
    """
    if ensure_heap and any(a == "@user_jvm_args.txt" for a in spec["launch_args"].get("windows", [])):
        _ensure_heap_in_user_jvm_args(server_path, spec.get("heap_args") or ["-Xms2G", "-Xmx4G"])
    script_path = _write_script(server_path, spec)
    write_launch_spec(server_path, spec)
    return script_path


def start_script_state(server_path: str, spec: dict) -> str:
    """
    平台启动脚本（start.bat / start.sh）和 HMSL 为这份启动描述生成的是否一致：
      "missing"   —— 没有脚本；
      "unchanged" —— 就是 HMSL 写的那份（按 hmsl_launch.json 里记下的 SHA-256 判断）；
      "edited"    —— 用户改过（例如把 -Xmx4G 改成 -Xmx8G），launcher 此时改用脚本启动，让修改生效。
    """
    script_name = "start.bat" if sys.platform == "win32" else "start.sh"
    try:
        with open(os.path.join(server_path, script_name), "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return "missing"
    rec = spec.get("script")
    if isinstance(rec, dict) and rec.get("name") == script_name and rec.get("sha256"):
        return "unchanged" if _sha256(data) == str(rec["sha256"]).lower() else "edited"
    # 没有记录（早期版本写的启动描述）：和按各代模板生成的内容逐字节比较；都对不上就当作用户改过——
    # 宁可照脚本启动，也不能覆盖掉用户的修改
    try:
        for template in range(SCRIPT_TEMPLATE, 0, -1):
            if _start_script_for_platform(spec, template)[1] == data:
                return "unchanged"
    except (KeyError, TypeError, ValueError):
        pass
    return "edited"


def refresh_start_script(server_path: str, spec: dict) -> bool:
    """
    启动脚本未被用户修改、但和当前启动描述不一致（换了 Java 路径、Forge 升级、模板更新）时重写它，
    保持"双击 start.bat"和 HMSL 启动的效果一致。重写了返回 True；写不了（只读目录）忽略。
    """
    try:
        script_name, body, _chmod = _start_script_for_platform(spec)
        path = os.path.join(server_path, script_name)
        try:
            with open(path, "rb") as f:
                same = f.read() == body
        except FileNotFoundError:
            return False           # 用户删掉了脚本：不自作主张再生成
        rec = spec.get("script")
        if same and isinstance(rec, dict) and rec.get("sha256") == _sha256(body):
            return False
        _write_script(server_path, spec)
        write_launch_spec(server_path, spec)
        return True
    except (OSError, KeyError, TypeError, ValueError):
        return False


# ---------- helpers ----------

# 系统自动生成的元数据文件：只含这些的文件夹仍算空（Finder 一打开文件夹就会写入 .DS_Store，
# 非 HFS/APFS 卷上还有 ._* 附属文件；Windows 资源管理器会写 desktop.ini / Thumbs.db）
_OS_METADATA_NAMES = frozenset({".ds_store", ".localized", "desktop.ini", "thumbs.db"})


def _is_os_metadata(name: str) -> bool:
    return name.lower() in _OS_METADATA_NAMES or name.startswith("._")


def _meaningful_entries(path: str) -> List[str]:
    """目录里除系统元数据文件以外的条目；目录读不了时抛 OSError。"""
    return [n for n in os.listdir(path) if not _is_os_metadata(n)]


def dir_is_effectively_empty(path: str) -> bool:
    """
    文件夹是否"实际上是空的"：不存在，或只含系统自动生成的元数据文件（.DS_Store、._*、.localized、
    desktop.ini、Thumbs.db）。路径是文件、或文件夹读不了时返回 False（当作不能用来新建服务器）。
    create_server 和 GUI 判断"目标文件夹已存在且不为空"都用它。
    """
    if not os.path.lexists(path):
        return True
    if not os.path.isdir(path):
        return False
    try:
        return not _meaningful_entries(path)
    except OSError:
        return False


def _accepts_kw(fn, name: str) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for p in sig.parameters.values():
        if p.kind == p.VAR_KEYWORD:
            return True
        if p.name == name and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY):
            return True
    return False


def _remove_tree(path: str) -> None:
    """删除本次创建的目录（Windows 上用 \\\\?\\ 前缀，Fabric/Forge 的库路径常超过 260 字符）。"""
    p = os.path.abspath(path)
    if sys.platform == "win32" and not p.startswith("\\\\?\\"):
        p = ("\\\\?\\UNC\\" + p[2:]) if p.startswith("\\\\") else ("\\\\?\\" + p)

    def _retry(func, fpath, _exc):
        try:
            os.chmod(fpath, 0o700)
            func(fpath)
        except OSError:
            pass

    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(p, onexc=_retry)
        else:
            shutil.rmtree(p, onerror=_retry)
    except OSError:
        pass


def create_server(
    name: str,
    version: str,
    loader: str,
    parent_dir: str,
    env_manager,
    installer,
    downloader,
    progress_callback: Optional[ProgressCallback] = None,
    *,
    sync_mods: bool = True,
    loader_version: Optional[str] = None,
) -> CreateServerResult:
    """
    Create a new Minecraft server instance under `parent_dir/name`.

    sync_mods=False skips the MOD_DATABASE sync (modpack imports bring their own mods).
    loader_version pins the Fabric loader / Forge / NeoForge version or Paper build
    (None = newest recommended/stable).

    Returns a CreateServerResult; never raises for expected failures (bad or
    Windows-illegal name, OSError creating dirs, no suitable Java, network,
    installer failure) — the error text carries the installer diagnostics.
    A folder created by a failed attempt is removed again.
    """
    def report(frac: float, msg: str) -> None:
        if progress_callback:
            progress_callback(frac, msg)

    name = (name or "").strip()
    err = validate_server_name(name)
    if err:
        return CreateServerResult(False, "", err)
    loader_norm = (loader or "").strip().lower()
    if loader_norm not in SUPPORTED_LOADERS:
        return CreateServerResult(False, "", f"未知服务端类型: {loader}")
    label = _LOADER_LABEL[loader_norm]
    if not version or _parse_mc_version(version) is None:
        return CreateServerResult(False, "", f"无效的游戏版本: {version!r}")
    if not parent_dir or not os.path.isdir(parent_dir):
        return CreateServerResult(False, "", f"目标父目录不存在: {parent_dir}")

    server_path = os.path.join(parent_dir, name)
    if any(ord(c) > 0xFFFF for c in os.path.abspath(server_path)):
        return CreateServerResult(False, server_path,
                                  "服务器路径中包含 emoji 等特殊符号，Java 无法从这样的目录启动服务器，请换一个位置。")
    existed = os.path.lexists(server_path)
    if existed:
        if not os.path.isdir(server_path):
            return CreateServerResult(False, server_path, f"已存在同名文件，无法创建文件夹: {server_path}")
        try:
            os.listdir(server_path)
        except OSError as e:
            return CreateServerResult(False, server_path, f"无法访问目标文件夹 {server_path}：{e}")
        if not dir_is_effectively_empty(server_path):
            # 往旧服务器里再装一次会新旧文件混在一起（旧版模组残留），直接拒绝
            return CreateServerResult(False, server_path,
                                      f"目标文件夹已存在且不为空：{server_path}\n请换一个名称，或先删除旧文件夹。")

    # --- 先确定 Java：没有合适的 Java 就不必下载任何东西 ---
    java_min, java_max = java_version_range(version, loader_norm)
    parts = _parse_mc_version(version)
    if parts and parts[0] >= 26:
        # 年份制新版本：以 Mojang 元数据为准（表里只能猜）
        try:
            from core.server_installer import mojang_java_major
            mj = mojang_java_major(version)
            if mj and mj > java_min:
                java_min = mj
        except Exception:
            pass
    report(0.05, f"正在查找 Java {_range_text(java_min, java_max)}...")
    finder = getattr(env_manager, "find_java", None)
    if callable(finder):
        try:
            java_cmd = finder(java_min, java_max)
        except TypeError:
            java_cmd = finder(java_min)
        if not java_cmd:
            return CreateServerResult(False, server_path,
                                      _missing_java_message(version, java_min, java_max, env_manager))
    else:
        java_cmd = env_manager.get_java_cmd(java_min)
    java_major = java_major_version_of(java_cmd)

    report(0.10, f"准备目标目录 {server_path}")
    try:
        os.makedirs(server_path, exist_ok=True)
    except OSError as e:
        return CreateServerResult(False, server_path, f"无法创建服务器文件夹 {server_path}：{e}")

    warnings: List[str] = []

    def fail(msg: str) -> CreateServerResult:
        # 失败时清掉本次的半成品，免得它挡住用同一名称重试（原本就存在的空文件夹保留，
        # 其中的系统元数据文件也留着——desktop.ini 里可能有用户设的文件夹图标）
        if existed:
            try:
                children = os.listdir(server_path)
            except OSError:
                children = []
            for child in children:
                if _is_os_metadata(child):
                    continue
                p = os.path.join(server_path, child)
                if os.path.isdir(p) and not os.path.islink(p):
                    _remove_tree(p)
                else:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        else:
            _remove_tree(server_path)
        return CreateServerResult(False, server_path, msg, warnings)

    try:
        shown = f"Java {java_major}" if java_major else "Java"
        report(0.20, f"已锁定 {shown}（{java_cmd}），准备下载 {label} 服务端...")

        def call_install(method_name: str, *args, **optional):
            fn = getattr(installer, method_name, None)
            if fn is None:
                return None
            kw = {}
            for k, v in optional.items():
                if v is None:
                    continue
                if _accepts_kw(fn, k):
                    kw[k] = v
                else:
                    warnings.append(f"安装器不支持指定版本 {v}，已使用默认版本。")
            return fn(*args, **kw)

        if loader_norm == "paper":
            ok = call_install("install_paper", server_path, version, build=loader_version)
        elif loader_norm == "fabric":
            ok = call_install("install_fabric", server_path, version, loader_version=loader_version)
        elif loader_norm == "forge":
            ok = call_install("install_forge", server_path, version, java_cmd, forge_version=loader_version)
        elif loader_norm == "neoforge":
            ok = call_install("install_neoforge", server_path, version, java_cmd,
                              neoforge_version=loader_version)
        else:
            ok = call_install("install_vanilla", server_path, version)
        if ok is None:
            return fail(f"当前安装器不支持 {label} 服务端")
        if not ok:
            diag = getattr(installer, "last_error", None)
            if diag and str(diag).startswith(label.split(" ")[0]):
                return fail(str(diag))        # 安装器的诊断信息本身已经说明了是哪个服务端
            return fail(f"{label} 服务端安装失败" + (f"：{diag}" if diag else ""))

        report(0.55, "检查安装结果...")
        spec = build_launch_spec(server_path, loader_norm, version, java_cmd, java_min, java_max)
        if spec is None:
            return fail(f"{label} 安装后没有找到可启动的服务端文件（server.jar / run.bat / forge-*.jar），"
                        f"安装可能没有完成，请重试。")

        report(0.60, "写入 eula.txt...")
        with open(os.path.join(server_path, "eula.txt"), "w", encoding="utf-8") as f:
            f.write("eula=true\n")

        if loader_norm != "vanilla":
            mod_dir = os.path.join(server_path, "plugins" if loader_norm == "paper" else "mods")
            os.makedirs(mod_dir, exist_ok=True)
            if sync_mods and downloader is not None:
                report(0.70, "同步模组数据库...")
                kind = "插件" if loader_norm == "paper" else "模组"
                try:
                    kw = {}
                    if java_major and _accepts_kw(downloader.sync, "java_major"):
                        kw["java_major"] = java_major     # 按服务器实际使用的 Java 检查模组
                    res = downloader.sync(mod_dir, version, loader, **kw)
                except Exception as e:  # 同步只是锦上添花，失败不影响服务器本身
                    warnings.append(f"{kind}同步失败：{e}")
                else:
                    # sync 返回 (成功数, 匹配条目数)；旧版/测试替身可能返回 None
                    try:
                        done, matched = int(res[0]), int(res[1])
                    except (TypeError, ValueError, IndexError, KeyError):
                        done = matched = 0
                    if done < matched:
                        warnings.append(f"部分{kind}未安装：数据库中适用的 {matched} 个里装上了 {done} 个，"
                                        f"其余的原因见日志（[失败]/[跳过] 行）。")

        report(0.90, "生成启动脚本...")
        write_launch_files(server_path, spec)
    except Exception as e:
        if sys.stdout is not None:     # 打包后的 --windowed 程序里 stdout 可能是 None
            traceback.print_exc(file=sys.stdout)
        return fail(f"创建服务器时发生意外错误：{type(e).__name__}: {e}")

    report(1.0, "服务器部署完成")
    return CreateServerResult(True, server_path, None, warnings)
