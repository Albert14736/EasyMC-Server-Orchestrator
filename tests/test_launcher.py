"""
Headless tests for core.launcher.

Uses a Python script as a fake Minecraft server: it echoes its stdin back to
stdout, prints a banner on startup, and quits cleanly when it receives 'stop'.
This avoids needing Java or a real server.jar.
"""
from __future__ import annotations

import os
import stat
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.launcher import start_server


FAKE_SERVER_PY = r"""\
import sys
sys.stdout.write("[FAKE] server starting\n")
sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if line == "stop":
        sys.stdout.write("[FAKE] received stop, bye\n")
        sys.stdout.flush()
        break
    sys.stdout.write(f"[FAKE] echo: {line}\n")
    sys.stdout.flush()
"""


def _make_fake_server_dir(tmp_path):
    """Create a tmp server dir whose start script execs a Python fake server."""
    server_dir = tmp_path / "fake_srv"
    server_dir.mkdir()
    py_path = server_dir / "fake_server.py"
    py_path.write_text(FAKE_SERVER_PY)

    if sys.platform == "win32":
        bat = server_dir / "start.bat"
        bat.write_text(f'@echo off\r\n"{sys.executable}" "{py_path}"\r\n', encoding="utf-8")
    else:
        sh = server_dir / "start.sh"
        sh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{py_path}"\n')
        sh.chmod(sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(server_dir)


def _wait_for_line(sp, contains: str, timeout: float = 5.0):
    """Drain lines until one containing `contains` arrives, or timeout."""
    deadline = time.monotonic() + timeout
    seen = []
    while time.monotonic() < deadline:
        line = sp.read_line(timeout=0.1)
        if line is not None:
            seen.append(line)
            if contains in line:
                return seen
    raise AssertionError(f"timeout waiting for {contains!r}; seen={seen}")


# ---------- error paths ----------

def test_start_server_missing_dir(tmp_path):
    with pytest.raises(FileNotFoundError):
        start_server(str(tmp_path / "nope"))


def test_start_server_missing_script(tmp_path):
    (tmp_path / "empty_srv").mkdir()
    with pytest.raises(FileNotFoundError):
        start_server(str(tmp_path / "empty_srv"))


# ---------- happy path ----------

def test_start_server_reads_banner(tmp_path):
    sp = start_server(_make_fake_server_dir(tmp_path))
    try:
        _wait_for_line(sp, "server starting")
        assert sp.is_alive()
    finally:
        sp.stop(grace=2.0)


def test_send_command_round_trips(tmp_path):
    sp = start_server(_make_fake_server_dir(tmp_path))
    try:
        _wait_for_line(sp, "server starting")
        sp.send_command("hello world")
        _wait_for_line(sp, "echo: hello world")
    finally:
        sp.stop(grace=2.0)


def test_stop_triggers_graceful_exit(tmp_path):
    sp = start_server(_make_fake_server_dir(tmp_path))
    _wait_for_line(sp, "server starting")
    code = sp.stop(grace=3.0)
    assert not sp.is_alive()
    assert code == 0  # fake server exited cleanly on receiving 'stop'


def test_drain_lines_returns_batch(tmp_path):
    sp = start_server(_make_fake_server_dir(tmp_path))
    try:
        # Wait for banner
        _wait_for_line(sp, "server starting")
        # Send several commands
        for i in range(3):
            sp.send_command(f"msg{i}")
        # Give the echo loop time to flush
        time.sleep(0.3)
        batch = sp.drain_lines()
        joined = "\n".join(batch)
        assert "echo: msg0" in joined
        assert "echo: msg1" in joined
        assert "echo: msg2" in joined
    finally:
        sp.stop(grace=2.0)


def test_send_command_after_stop_is_noop(tmp_path):
    sp = start_server(_make_fake_server_dir(tmp_path))
    _wait_for_line(sp, "server starting")
    sp.stop(grace=2.0)
    # Should not raise even though the process is gone
    sp.send_command("ignored")
    assert not sp.is_alive()
