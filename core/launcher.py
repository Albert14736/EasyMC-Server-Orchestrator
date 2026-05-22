"""
Launch and supervise a Minecraft server subprocess.

Pure / headless: no GUI imports. The GUI polls ServerProcess.read_line()
on a Tk after() loop to render live output; tests drive it with a fake script.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional


def _script_name_for_platform() -> str:
    return "start.bat" if sys.platform == "win32" else "start.sh"


@dataclass
class ServerProcess:
    proc: subprocess.Popen
    server_path: str
    _output_queue: "queue.Queue[Optional[str]]" = field(repr=False)
    _reader_thread: threading.Thread = field(repr=False)
    _eof_seen: bool = False

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
        if not self.is_alive() or self.proc.stdin is None or self.proc.stdin.closed:
            return
        try:
            self.proc.stdin.write(cmd.rstrip("\n") + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def stop(self, grace: float = 10.0) -> int:
        """
        Graceful shutdown: send 'stop' (MC server's quit command),
        wait `grace` seconds, then SIGTERM, then SIGKILL.
        Returns the final exit code (or -1 on hard kill).
        """
        if not self.is_alive():
            return self.proc.returncode if self.proc.returncode is not None else 0

        self.send_command("stop")
        deadline = time.monotonic() + grace
        while self.is_alive() and time.monotonic() < deadline:
            time.sleep(0.1)

        if self.is_alive():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        return self.proc.returncode if self.proc.returncode is not None else -1

    def wait(self, timeout: Optional[float] = None) -> int:
        return self.proc.wait(timeout=timeout)


def _spawn_reader(proc: subprocess.Popen, q: "queue.Queue[Optional[str]]") -> threading.Thread:
    def reader() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                q.put(line.rstrip("\r\n"))
        finally:
            q.put(None)

    t = threading.Thread(target=reader, name="server-stdout-reader", daemon=True)
    t.start()
    return t


def start_server(server_path: str) -> ServerProcess:
    """
    Launch the start script in `server_path` and return a ServerProcess handle.

    Raises FileNotFoundError if the platform's start script is missing.
    """
    if not os.path.isdir(server_path):
        raise FileNotFoundError(f"服务器目录不存在: {server_path}")
    script_path = os.path.join(server_path, _script_name_for_platform())
    if not os.path.isfile(script_path):
        raise FileNotFoundError(f"启动脚本不存在: {script_path}")

    if sys.platform == "win32":
        cmd: list = ["cmd.exe", "/c", script_path]
    else:
        cmd = [script_path]

    proc = subprocess.Popen(
        cmd,
        cwd=server_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        # MC server logs include emoji/CJK; Windows default cp936 would crash.
        encoding="utf-8", errors="replace",
    )

    q: "queue.Queue[Optional[str]]" = queue.Queue()
    reader_thread = _spawn_reader(proc, q)
    return ServerProcess(
        proc=proc,
        server_path=server_path,
        _output_queue=q,
        _reader_thread=reader_thread,
    )
