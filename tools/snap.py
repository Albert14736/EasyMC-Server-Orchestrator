#!/usr/bin/env python3
"""
HMSL GUI 自动截图工具 —— 给 Claude 用的，省去人工截图。

Usage:
    python tools/snap.py [ROUTE] [OUTPUT_PNG] [SETTLE]

Examples:
    python tools/snap.py                                # home → /tmp/hmsl_snap.png
    python tools/snap.py versions
    python tools/snap.py "detail:我的世界服务器"
    python tools/snap.py "config:我的世界服务器" /tmp/cfg.png 2.5

How it works (no AppleScript / no Accessibility permission needed):
  1. Kills any existing gui_main.py
  2. Launches `python gui_main.py --route ROUTE --snap OUT --settle SETTLE`
     The GUI activates itself to the foreground (AppKit), then captures itself
     **by CGWindowID** (`screencapture -l <id>`, via pyobjc/Quartz) so the shot
     is the HMSL window itself — never whatever window happens to overlap it.
  3. Returns when the process exits, prints the OUT path

依赖（仅截图用，普通运行/打包不需要；见 requirements.txt 的 dev 段）：
    pip install --only-binary=:all: 'pyobjc-framework-Quartz==9.2' \
                                    'pyobjc-framework-Cocoa==9.2'
  （py3.8 锁 9.2：新版 pyobjc 已弃 3.8。拿不到 Quartz 时自动回退区域截图。）

⚠️ 已知限制：从后台启动的进程抢不到真正的 key 窗口状态，macOS 会推迟**非顶层
   Canvas**（CTkScrollableFrame 内部）的文字绘制 —— server.properties / 模组配置
   这类滚动表单里的字段文字在截图里可能是空白。顶层内容（侧栏/头部/概览/按钮/
   tab）都正常。要核实滚动表单内容请用控件树 introspection 而非截图。

Only macOS (uses /usr/sbin/screencapture via the launched Python).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = "/tmp/hmsl_snap.png"
DEFAULT_SETTLE = 1.5


def main() -> int:
    route = sys.argv[1] if len(sys.argv) > 1 else ""
    out_path = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT
    settle = float(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_SETTLE

    if sys.platform != "darwin":
        print("[snap] macOS only (uses screencapture)", file=sys.stderr)
        return 2

    # Remove stale output so an early failure is obvious
    try: os.remove(out_path)
    except FileNotFoundError: pass

    # Kill any prior GUI
    subprocess.run(["pkill", "-f", "gui_main.py"], stderr=subprocess.DEVNULL)
    time.sleep(0.4)

    cmd = [sys.executable, "gui_main.py",
           "--snap", out_path, "--settle", str(settle)]
    if route:
        cmd += ["--route", route]

    # ~4s buffer over settle window for launch + render + screencap + clean exit
    timeout_s = settle + 5.0
    try:
        r = subprocess.run(cmd, cwd=PROJECT_ROOT,
                           capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        print(f"[snap] GUI didn't exit within {timeout_s}s — killing", file=sys.stderr)
        subprocess.run(["pkill", "-f", "gui_main.py"], stderr=subprocess.DEVNULL)
        return 3

    if r.returncode != 0:
        print(f"[snap] GUI exited with code {r.returncode}", file=sys.stderr)
        sys.stderr.write(r.stderr)
        return r.returncode

    if not os.path.isfile(out_path):
        print(f"[snap] no screenshot produced at {out_path}", file=sys.stderr)
        sys.stderr.write(r.stderr)
        return 4

    print(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
