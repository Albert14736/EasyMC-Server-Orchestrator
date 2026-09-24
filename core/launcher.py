"""
Launch and supervise a Minecraft server subprocess.

Pure / headless: no GUI imports. The GUI polls ServerProcess.read_line()
on a Tk after() loop to render live output; tests drive it with a fake script.

Two launch modes:
  * direct —— create_server 写了 hmsl_launch.json（java 路径 + 参数）：直接 Popen java，
    不经过 cmd.exe / start.bat，没有代码页、`pause`、`&` 路径、孤儿 java 这些问题。
  * script —— 没有启动描述的服务器（扫描到的 / 旧版 HMSL 建的 / 别人的整合包），以及用户手动改过
    HMSL 生成的 start.bat / start.sh 的服务器（改内存等修改要生效）：
    运行 start.bat（Windows，经 cmd.exe，隐藏窗口）或 start.sh。
"""
from __future__ import annotations

import json
import locale
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from core.env_manager import UTF8_JVM_FLAGS

# HMSL.exe 是 --windowed 程序：不加 CREATE_NO_WINDOW，cmd.exe / java.exe 会各弹出一个
# 空白终端窗口，关掉它还会直接杀死服务器。非 Windows 上为 0。
NO_WINDOW_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

LAUNCH_SPEC_NAME = "hmsl_launch.json"
LAUNCH_SPEC_FORMAT = 1

# start.bat 用它判断是不是 HMSL 启动的（是就不 pause，否则 cmd 会卡在 pause 上读管道）
LAUNCHED_ENV_VAR = "HMSL_LAUNCHED"

# HMSL 经管道读写服务器控制台，关掉 JLine 终端和 ANSI 颜色（Paper/Forge 的 TerminalConsoleAppender 认这两个属性）：
#   * Windows 上 jansi 会对 stdin 管道做 isatty 查询，而控制台线程正阻塞在同一管道的 ReadFile 上，
#     查询随之卡死——Paper 1.20.1 执行 stop 后存完档却永远退不出去；
#   * ANSI 颜色码在 GUI 文本框里显示成 "[38;5;14m" 这样的乱码。
# 只用于 HMSL 自己的启动；双击 start.bat / start.sh 时是真正的终端，不写进脚本。
PIPE_CONSOLE_JVM_FLAGS = ["-Dterminal.jline=false", "-Dterminal.ansi=false"]


def _script_name_for_platform() -> str:
    return "start.bat" if sys.platform == "win32" else "start.sh"


# ---------- output decoding ----------

def _fallback_codec() -> str:
    """非 UTF-8 行（cmd.exe 自己的提示、旧 Java 启动器报错）用控制台代码页解码。"""
    if sys.platform == "win32":
        try:
            import ctypes
            return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
        except Exception:
            pass
    return locale.getpreferredencoding(False) or "latin-1"


_FALLBACK_CODEC = _fallback_codec()

# ANSI 转义序列：GUI 文本框不认识。PIPE_CONSOLE_JVM_FLAGS 已让服务端不输出颜色，这里兜底处理
# 用户自己的启动脚本、以及自己拼颜色码直接打印的插件。
_ANSI_ESCAPE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]"          # CSI：颜色、光标控制（ESC [ 38;5;14 m）
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)?"      # OSC：窗口标题等
    r"|[()*+].?"                            # 字符集切换
    r"|[@-Z\\-_])?")                        # 其余两字节序列；落单的 ESC 也去掉


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text) if "\x1b" in text else text


def _decode_line(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode(_FALLBACK_CODEC, "replace")
        except LookupError:
            text = raw.decode("utf-8", "replace")
    return _strip_ansi(text)


# ---------- Windows process tree ----------

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.GetSystemTimeAsFileTime.argtypes = [ctypes.POINTER(wintypes.FILETIME)]
    _INVALID_HANDLE = ctypes.c_void_p(-1).value
    # PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
    _PROC_ACCESS = 0x0001 | 0x1000 | 0x00100000


def _process_table() -> Dict[int, Tuple[int, str]]:
    """{pid: (parent_pid, exe_name)} for every process (Windows only; {} elsewhere)."""
    if sys.platform != "win32":
        return {}
    table: Dict[int, Tuple[int, str]] = {}
    snap = _k32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == _INVALID_HANDLE:
        return table
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            table[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), entry.szExeFile)
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    return table


def _children_map(table: Dict[int, Tuple[int, str]]) -> Dict[int, List[int]]:
    children: Dict[int, List[int]] = {}
    for pid, (ppid, _name) in table.items():
        if pid != ppid:
            children.setdefault(ppid, []).append(pid)
    return children


def _descendants(root_pid: int, table: Optional[Dict[int, Tuple[int, str]]] = None) -> List[Tuple[int, str]]:
    """按进程快照里的父 PID 找后代（不核对身份，只用来判断状态，不用来结束进程）。"""
    table = _process_table() if table is None else table
    children = _children_map(table)
    out: List[Tuple[int, str]] = []
    stack = list(children.get(root_pid, []))
    seen = {root_pid}
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        out.append((pid, table[pid][1]))
        stack.extend(children.get(pid, []))
    return out


# 结束进程只通过句柄进行，并核对身份：Windows 的 PID 在进程退出后很快会分给别的程序，快照里的父 PID
# 也可能指向"PID 恰好相同"的旧进程。持有句柄期间该 PID 不会被复用，所以先开句柄、核对创建时间，再动手。

def _open_process(pid: int) -> Optional[int]:
    h = _k32.OpenProcess(_PROC_ACCESS, False, pid)
    return h or None


def _close_handle(h: Optional[int]) -> None:
    if h:
        try:
            _k32.CloseHandle(h)
        except Exception:
            pass


def _filetime_int(ft) -> int:
    return (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)


def _handle_ctime(h: int) -> Optional[int]:
    """进程创建时间（FILETIME，100ns 单位）；查询失败返回 None。"""
    c, e, k, u = (wintypes.FILETIME() for _ in range(4))
    if not _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
        return None
    return _filetime_int(c)


def _now_filetime() -> int:
    ft = wintypes.FILETIME()
    _k32.GetSystemTimeAsFileTime(ctypes.byref(ft))
    return _filetime_int(ft)


def _handle_exited(h: int) -> bool:
    return _k32.WaitForSingleObject(h, 0) == 0     # WAIT_OBJECT_0：进程已结束


def _tree_handles(root_pid: int, root_handle: int,
                  table: Dict[int, Tuple[int, str]], snap_time: int) -> List[Tuple[int, str, int]]:
    """
    root 的全部后代 [(pid, exe 名, 句柄)]，每个都打开了句柄并核对过身份，调用方负责关闭句柄：
      * 创建时间不早于父进程——否则是父进程 PID 被复用前留下的无关进程（"认错爹"）；
      * 创建时间不晚于拍快照的时刻——否则是快照之后 PID 又被复用的新进程。
    """
    root_ct = _handle_ctime(root_handle)
    if root_ct is None:
        return []
    children = _children_map(table)
    out: List[Tuple[int, str, int]] = []
    stack = [(pid, root_ct) for pid in children.get(root_pid, [])]
    seen = {root_pid}
    while stack:
        pid, parent_ct = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        h = _open_process(pid)
        if not h:
            continue
        ct = _handle_ctime(h)
        if ct is None or ct < parent_ct or ct > snap_time:
            _close_handle(h)
            continue
        out.append((pid, table[pid][1], h))
        stack.extend((c, ct) for c in children.get(pid, []))
    return out


def _kill_tree_win(root_pid: int, held: Sequence[Tuple[int, str, int]] = ()) -> None:
    """
    强制结束 root 及其全部后代，再补上事先持有句柄、但父进程已死所以从进程树上够不着的子进程（held）。
    全程经句柄操作（见 _tree_handles），不会误杀 PID 被复用后的无关程序。
    root 是 Popen 启动的进程，Popen 一直持有它的句柄，它的 PID 不会被复用。
    """
    opened: List[int] = []
    try:
        root_h = _open_process(root_pid)
        if root_h:
            opened.append(root_h)
            # 两轮：第一轮结束的过程中 cmd 可能刚派生出新进程（比如循环重启的脚本）
            for _round in range(2):
                table = _process_table()
                tree = _tree_handles(root_pid, root_h, table, _now_filetime())
                opened.extend(h for _pid, _name, h in tree)
                _k32.TerminateProcess(root_h, 1)   # 先结束根（cmd），它就不会再起新的 java
                for _pid, _name, h in tree:
                    _k32.TerminateProcess(h, 1)
                if not tree:
                    break
        for _pid, _name, h in held:
            if not _handle_exited(h):
                _k32.TerminateProcess(h, 1)
    finally:
        for h in opened:
            _close_handle(h)


# ---------- launch spec (hmsl_launch.json) ----------

def read_launch_spec(server_path: str) -> Optional[dict]:
    """读 hmsl_launch.json；不存在或格式不对返回 None（调用方退回到启动脚本）。"""
    path = os.path.join(server_path, LAUNCH_SPEC_NAME)
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            spec = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(spec, dict) or not isinstance(spec.get("launch_args"), dict):
        return None
    for key in ("windows", "unix"):
        if not isinstance(spec["launch_args"].get(key), list):
            return None
    return spec


def write_launch_spec(server_path: str, spec: dict) -> str:
    path = os.path.join(server_path, LAUNCH_SPEC_NAME)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def _resolve_java(server_path: str, spec: dict) -> str:
    java = str(spec.get("java") or "java")
    if os.path.isfile(java):
        return java
    need = int(spec.get("java_min") or spec.get("java_major") or 0)
    max_major = spec.get("java_max")
    if os.path.basename(java) == java:        # 裸 "java"：按 PATH 找，但版本必须够
        found = shutil.which(java)
        if found:
            from core.env_manager import java_major_version_of
            major = java_major_version_of(found)
            if not need or (major is not None and major >= need
                            and (max_major is None or major <= int(max_major))):
                return found
    # 记录的 JDK 被卸载/移动了，或者服务器是从另一台电脑拷来的：按版本要求重新找
    found = None
    if need:
        try:
            from core.env_manager import EnvManager
            found = EnvManager().find_java(need, max_major)
        except Exception:
            found = None
    if not found:
        want = f"Java {need}" if need else "Java"
        raise FileNotFoundError(
            f"找不到启动该服务器所需的 {want}（记录的路径已不存在：{java}）。请先安装 {want} 再启动。")
    spec["java"] = found
    try:
        write_launch_spec(server_path, spec)   # 记住新路径，下次不用再找
    except OSError:
        pass
    return found


def build_command(server_path: str, spec: dict) -> List[str]:
    """由启动描述拼出完整的 java 命令行（列表形式，直接交给 CreateProcessW，不经过 shell）。"""
    java = _resolve_java(server_path, spec)
    key = "windows" if sys.platform == "win32" else "unix"
    jvm = [str(a) for a in spec.get("jvm_args", [])]
    # 旧版 HMSL 写的启动描述里没有这两个参数，也补上；用户在启动描述里自己设了的不动
    jvm += [f for f in PIPE_CONSOLE_JVM_FLAGS
            if not any(a.startswith(f.split("=", 1)[0] + "=") for a in jvm)]
    return ([java] + jvm
            + [str(a) for a in spec["launch_args"][key]]
            + [str(a) for a in spec.get("program_args", ["nogui"])])


# 旧版 HMSL 生成的启动脚本（带 pause / UTF-8 编码 / call run.bat），认出来就升级成启动描述
_OLD_BAT = re.compile(r'@echo off\r?\n(?:call run\.bat|"([^"\r\n]+)" -Xms2G -Xmx4G -jar server\.jar nogui)\r?\npause\r?\n?\Z')
_OLD_SH = re.compile(r'#!/bin/zsh\n(?:exec \./run\.sh nogui|"([^"\n]+)" -Xms2G -Xmx4G -jar server\.jar nogui)\n?\Z')


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as f:
            data = f.read(4096)
    except OSError:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(_FALLBACK_CODEC, "replace")


def _migrate_legacy_hmsl(server_path: str) -> Optional[dict]:
    """
    旧版 HMSL 建的服务器（或在 Mac 上建、只有 start.sh 的服务器）：按磁盘上的实际文件
    生成 hmsl_launch.json 并重写启动脚本。认不出来或缺 Java 就返回 None（退回脚本启动）。
    """
    try:
        bat = os.path.join(server_path, "start.bat")
        sh = os.path.join(server_path, "start.sh")
        old_java = None
        recognized = False
        if os.path.isfile(bat):
            m = _OLD_BAT.match(_read_text(bat) or "")
            if m:
                recognized, old_java = True, m.group(1)
        elif os.path.isfile(sh):
            m = _OLD_SH.match(_read_text(sh) or "")
            # Windows 上只有 start.sh 的服务器（Mac 上创建的）没法用脚本启动，也一并处理
            if m or sys.platform == "win32":
                recognized, old_java = True, (m.group(1) if m else None)
        if not recognized:
            return None

        from core import server_factory
        from core.instance_scanner import detect_loader_and_version
        loader, version = detect_loader_and_version(server_path)
        loader_norm = (loader or "").strip().lower()
        if loader_norm not in ("forge", "neoforge"):
            loader_norm = loader_norm or "vanilla"
        java_min, java_max = (server_factory.java_version_range(version, loader_norm)
                              if version else (0, None))
        java = old_java if old_java and os.path.isfile(old_java) else None
        if java is None and java_min:
            from core.env_manager import EnvManager
            java = EnvManager().find_java(java_min, java_max)
        if not java:
            return None
        spec = server_factory.build_launch_spec(server_path, loader_norm, version or "",
                                                java, java_min or None, java_max)
        if spec is None:
            return None
        # 旧服务器原来经 run.bat / run.sh 启动、没有 -Xmx（JVM 默认用 1/4 内存）：不往 user_jvm_args.txt
        # 里补 HMSL 的默认内存，否则大型整合包服务器升级后会被悄悄限制在 4G
        server_factory.write_launch_files(server_path, spec, ensure_heap=False)
        return spec
    except Exception:
        return None


# ---------- process handle ----------

# 进程级登记表：GUI 退出前可以 stop_all()，避免把 java 留成无人看管的后台进程。
_running_lock = threading.Lock()
_running: List["ServerProcess"] = []


@dataclass(eq=False)
class ServerProcess:
    proc: subprocess.Popen
    server_path: str
    _output_queue: "queue.Queue[Optional[str]]" = field(repr=False)
    _reader_thread: threading.Thread = field(repr=False)
    _eof_seen: bool = False
    mode: str = "direct"                  # "direct"（直接启动 java）| "script"（start.bat / start.sh）
    _stop_requested: bool = field(default=False, repr=False)
    _stdin_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # 脚本模式（Windows）见过的 java 子进程：{pid: (exe 名, 进程句柄)}。持有句柄期间 PID 不会被复用，
    # 强制结束时经句柄操作，绝不会误杀之后拿到同一 PID 的其它程序；已退出的每轮巡查时清掉。
    _known_children: Dict[int, Tuple[str, int]] = field(default_factory=dict, repr=False)
    _children_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __del__(self) -> None:
        try:
            self._release_children(only_exited=False)
        except Exception:
            pass

    def is_alive(self) -> bool:
        return self.proc.poll() is None

    def read_line(self, timeout: float = 0.05) -> Optional[str]:
        """
        Pop the next line of stdout, or None if no line is ready within `timeout`.
        Returns None forever after the process exits and its stdout is drained.
        """
        try:
            line = self._output_queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if line is None:
            self._eof_seen = True
            return None
        return line

    def drain_lines(self, max_lines: int = 200) -> List[str]:
        """Pull every line currently waiting, up to a cap. Cheap to call from a GUI poll."""
        out: List[str] = []
        while len(out) < max_lines:
            line = self.read_line(timeout=0)
            if line is None:
                break
            out.append(line)
        return out

    def send_command(self, cmd: str) -> None:
        """Write a command line to the server's stdin (e.g. 'say hi', 'stop')."""
        if not self.is_alive() or self.proc.stdin is None:
            return
        with self._stdin_lock:
            if self.proc.stdin.closed:
                return
            try:
                # 服务端被强制为 UTF-8（见 UTF8_JVM_FLAGS），中文命令按 UTF-8 发送
                self.proc.stdin.write((cmd.rstrip("\r\n") + "\n").encode("utf-8"))
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass

    def _close_stdin(self) -> None:
        """关掉 stdin：脚本里的 `pause` 读到 EOF 立即返回，cmd.exe 随之退出。"""
        with self._stdin_lock:
            try:
                if self.proc.stdin is not None and not self.proc.stdin.closed:
                    self.proc.stdin.close()
            except (OSError, ValueError):
                pass

    def stop(self, grace: float = 10.0) -> int:
        """
        Graceful shutdown: send 'stop' (MC server's quit command) and wait up to
        `grace` seconds for the server to save and exit; after that force-kill the
        whole process tree (no orphan java.exe is left behind).
        Returns the final exit code (or -1 on hard kill).
        """
        if not self.is_alive():
            return self.proc.returncode if self.proc.returncode is not None else 0

        self._stop_requested = True
        self.send_command("stop")
        deadline = time.monotonic() + grace
        while self.is_alive() and time.monotonic() < deadline:
            time.sleep(0.1)

        if self.is_alive() and sys.platform != "win32":
            # macOS/Linux：先 SIGTERM（MC 的关闭钩子会存档），再强杀
            self._signal_group(signal.SIGTERM)
            try:
                self.proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                pass
        if self.is_alive():
            self.kill()
        return self.proc.returncode if self.proc.returncode is not None else -1

    def kill(self) -> int:
        """Force-kill the whole process tree immediately (java and any wrapper cmd.exe)."""
        if sys.platform == "win32":
            with self._children_lock:     # 巡查线程不会在此期间关闭/清理这些句柄
                held = [(pid, name, h) for pid, (name, h) in self._known_children.items()]
                _kill_tree_win(self.proc.pid, held)
        else:
            self._signal_group(signal.SIGKILL)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self._close_stdin()
        self._release_children(only_exited=True)
        return self.proc.returncode if self.proc.returncode is not None else -1

    def _signal_group(self, sig) -> None:
        try:
            os.killpg(self.proc.pid, sig)   # start_new_session=True → pgid == pid
        except (ProcessLookupError, PermissionError, OSError, AttributeError):
            try:
                self.proc.send_signal(sig)
            except OSError:
                pass

    def wait(self, timeout: Optional[float] = None) -> int:
        return self.proc.wait(timeout=timeout)

    # --- script mode (Windows): release `pause` once java is gone ---
    def _watch_script_children(self) -> None:
        """
        第三方/旧版 start.bat 往往以 `pause` 结尾；HMSL 握着 stdin 管道，cmd 会永远卡住，
        GUI 就一直显示“运行中”。这里盯着 cmd 的子进程：java 退出后（或脚本迟迟没起 java、
        只剩 cmd 自己）就关掉 stdin，让 pause 立即返回。
        """
        seen_java = False
        idle_since: Optional[float] = None
        started = time.monotonic()
        root_h = _open_process(self.proc.pid)     # Popen 持有 cmd 的句柄，这个 PID 不会被复用
        try:
            while self.proc.poll() is None:
                if self.proc.stdin is None or self.proc.stdin.closed:
                    return
                kids = self._scan_children(root_h)
                java_alive = any(name.lower() in ("java.exe", "javaw.exe") for _pid, name in kids)
                now = time.monotonic()
                if java_alive:
                    seen_java, idle_since = True, None
                elif self._stop_requested and (seen_java or not kids):
                    self._close_stdin()
                    return
                elif not kids:
                    if idle_since is None:
                        idle_since = now
                    elif now - idle_since >= (2.0 if seen_java else 8.0):
                        self._close_stdin()
                        return
                else:
                    idle_since = None
                # 开头几秒密集地看：启动即崩溃的 java（缺 jar、Java 版本不对）只活几十到几百毫秒，
                # 漏看了就会被当成"脚本还没起 java"，要多等 8 秒才放开 pause
                age = now - started
                time.sleep(0.02 if age < 2.0 else 0.1 if age < 5.0 else 0.5)
        finally:
            _close_handle(root_h)
            self._release_children(only_exited=True)

    def _scan_children(self, root_h: Optional[int]) -> List[Tuple[int, str]]:
        """
        一轮巡查：返回 cmd 的后代 [(pid, exe 名)]（不含 conhost）；新出现的 java 记进 _known_children
        并一直持有其句柄；已退出的从 _known_children 里清掉。
        """
        table = _process_table()
        if not root_h:        # 打不开 cmd 的句柄（理论上不会）：只看快照，不记录子进程
            return [(pid, name) for pid, name in _descendants(self.proc.pid, table)
                    if name.lower() != "conhost.exe"]
        tree = _tree_handles(self.proc.pid, root_h, table, _now_filetime())
        kids: List[Tuple[int, str]] = []
        with self._children_lock:
            self._release_children(only_exited=True, locked=True)
            for pid, name, h in tree:
                low = name.lower()
                if low != "conhost.exe":
                    kids.append((pid, name))
                if low in ("java.exe", "javaw.exe") and pid not in self._known_children:
                    self._known_children[pid] = (name, h)     # 句柄留着，kill() 经它结束进程
                else:
                    _close_handle(h)
        return kids

    def _release_children(self, only_exited: bool, locked: bool = False) -> None:
        """关闭 _known_children 里的句柄：only_exited=True 时只清理已经退出的。"""
        if sys.platform != "win32" or not self._known_children:
            return
        if not locked:
            with self._children_lock:
                self._release_children(only_exited, locked=True)
            return
        for pid, (_name, h) in list(self._known_children.items()):
            if not only_exited or _handle_exited(h):
                del self._known_children[pid]
                _close_handle(h)


def _spawn_reader(proc: subprocess.Popen, q: "queue.Queue[Optional[str]]",
                  rewrite: Optional[Dict[str, Optional[str]]] = None) -> threading.Thread:
    """rewrite: {整行: 替换成的行 | None=不显示}，用来隐藏 HMSL 自己注入的环境变量回显。"""
    def reader() -> None:
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = _decode_line(raw.rstrip(b"\r\n"))
                if rewrite and line in rewrite:
                    line = rewrite[line]
                    if line is None:
                        continue
                q.put(line)
        except (OSError, ValueError):
            pass
        finally:
            q.put(None)

    t = threading.Thread(target=reader, name="server-stdout-reader", daemon=True)
    t.start()
    return t


def _script_command(server_path: str) -> Tuple[List[str], str]:
    """没有启动描述时用的启动脚本命令行。"""
    if sys.platform == "win32":
        for name in ("start.bat", "run.bat"):
            if os.path.isfile(os.path.join(server_path, name)):
                comspec = os.environ.get("ComSpec") or "cmd.exe"
                # 用相对路径 + cwd：命令行里不出现服务器路径，& ( ) ^ % 空格 中文都不会被 cmd 误解析；
                # /d 跳过注册表 AutoRun。
                return [comspec, "/d", "/c", ".\\" + name], name
        raise FileNotFoundError(f"启动脚本不存在: {os.path.join(server_path, 'start.bat')}")
    for name in ("start.sh", "run.sh"):
        p = os.path.join(server_path, name)
        if os.path.isfile(p):
            return [p], name
    raise FileNotFoundError(f"启动脚本不存在: {os.path.join(server_path, 'start.sh')}")


def _script_state(server_path: str, spec: dict) -> str:
    """'missing' | 'unchanged' | 'edited' | 'unknown'（读不了脚本时按启动描述启动）。"""
    try:
        from core.server_factory import start_script_state
        return start_script_state(server_path, spec)
    except Exception:
        return "unknown"


def _refresh_forge_args(server_path: str, spec: dict) -> None:
    """
    Forge/NeoForge 以安装器在磁盘上的产物为准：用户在服务器目录里重新运行了新版安装器（升级 Forge）、
    或在 run.bat 的 java 行上加了参数时，启动描述跟着更新（旧版 HMSL 经 run.bat 启动，这些改动同样生效）。
    """
    if str(spec.get("loader") or "").lower() not in ("forge", "neoforge"):
        return
    try:
        from core.server_installer import find_forge_launch_files
        fresh = find_forge_launch_files(server_path)
    except Exception:
        return
    if fresh and fresh != spec.get("launch_args"):
        spec["launch_args"] = fresh
        try:
            write_launch_spec(server_path, spec)
        except OSError:
            pass


def _refresh_script(server_path: str, spec: dict) -> None:
    """未被用户改过的启动脚本与启动描述保持一致（换了 Java 路径 / Forge 升级 / 模板更新后重写）。"""
    try:
        from core.server_factory import refresh_start_script
        refresh_start_script(server_path, spec)
    except Exception:
        pass


def start_server(server_path: str) -> ServerProcess:
    """
    Launch the server in `server_path` and return a ServerProcess handle.

    Uses hmsl_launch.json (java launched directly) when present, otherwise the
    platform's start script. A start script the user edited after HMSL generated
    it (e.g. a bigger -Xmx) wins over hmsl_launch.json, so the edit takes effect.
    Raises FileNotFoundError if the directory, the required Java, or the start
    script is missing.
    """
    if not os.path.isdir(server_path):
        raise FileNotFoundError(f"服务器目录不存在: {server_path}")

    env = dict(os.environ)
    env[LAUNCHED_ENV_VAR] = "1"
    notices: List[str] = []
    rewrite: Dict[str, Optional[str]] = {}

    spec = read_launch_spec(server_path)
    if spec is None:
        spec = _migrate_legacy_hmsl(server_path)
    script_state = "missing"
    if spec is not None:
        script_state = _script_state(server_path, spec)
        if script_state == "edited":
            # 用户改过 HMSL 生成的启动脚本（最常见的是改 -Xmx 调内存）：照脚本启动，修改才会生效
            name = _script_name_for_platform()
            notices.append(f"[HMSL] 检测到 {name} 已被手动修改，本次按 {name} 启动（其中的内存等参数会生效）。")
            spec = None
    if spec is not None:
        _refresh_forge_args(server_path, spec)
        cmd = build_command(server_path, spec)
        mode = "direct"
        if script_state == "unchanged":
            _refresh_script(server_path, spec)
    else:
        cmd, _script = _script_command(server_path)
        mode = "script"
        if sys.platform == "win32":
            # 脚本里的 java 我们改不了参数，用 JAVA_TOOL_OPTIONS 让它输出 UTF-8、不用 JLine/ANSI
            # （见 PIPE_CONSOLE_JVM_FLAGS；脚本的 java 行上写明的同名参数优先于 JAVA_TOOL_OPTIONS）
            original = env.get("JAVA_TOOL_OPTIONS", "")
            injected = (original + " " + " ".join(UTF8_JVM_FLAGS + PIPE_CONSOLE_JVM_FLAGS)).strip()
            env["JAVA_TOOL_OPTIONS"] = injected
            # 每个 JVM 启动时都会往 stderr 回显 "Picked up JAVA_TOOL_OPTIONS: …"：
            # HMSL 自己加的部分不显示（用户原本就设置了的，照旧显示用户的那部分）
            rewrite[f"Picked up JAVA_TOOL_OPTIONS: {injected}"] = (
                f"Picked up JAVA_TOOL_OPTIONS: {original.strip()}" if original.strip() else None)

    popen_kw = dict(cwd=server_path, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, env=env)
    if sys.platform == "win32":
        popen_kw["creationflags"] = NO_WINDOW_FLAGS
    else:
        popen_kw["start_new_session"] = True   # 自成进程组，stop/kill 能连同子进程一起处理
    try:
        proc = subprocess.Popen(cmd, **popen_kw)
    except FileNotFoundError as e:
        raise FileNotFoundError(f"无法启动服务器（{cmd[0]}）：{e}") from e

    q: "queue.Queue[Optional[str]]" = queue.Queue()
    for line in notices:
        q.put(line)
    reader_thread = _spawn_reader(proc, q, rewrite)
    sp = ServerProcess(
        proc=proc,
        server_path=server_path,
        _output_queue=q,
        _reader_thread=reader_thread,
        mode=mode,
    )
    if mode == "script" and sys.platform == "win32":
        threading.Thread(target=sp._watch_script_children, name="server-script-watch",
                         daemon=True).start()
    with _running_lock:
        _running[:] = [s for s in _running if s.is_alive()]
        _running.append(sp)
    return sp


def running_servers() -> List[ServerProcess]:
    """本进程启动、仍在运行的服务器。"""
    with _running_lock:
        _running[:] = [s for s in _running if s.is_alive()]
        return list(_running)


def stop_all(grace: float = 30.0) -> None:
    """并行地对所有仍在运行的服务器执行 stop(grace)（GUI 退出前调用）。"""
    threads = [threading.Thread(target=s.stop, args=(grace,), daemon=True) for s in running_servers()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(grace + 30)
