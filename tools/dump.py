#!/usr/bin/env python3
"""
HMSL 控件树 dump 工具 —— 给 Claude 用的，纯文本核实 UI，不截图不花 token。

跟 tools/snap.py 是姊妹工具：
    snap.py  → 出 PNG，看『长什么样』（布局/配色/对齐）
    dump.py  → 出 TXT，看『接线对不对』（按钮有没有绑 command、控件文字、
               可见性、层级）—— 尤其治『滚动表单文字截成空白』和『按钮忘接线』。

Usage:
    python tools/dump.py [ROUTE] [OUTPUT_TXT] [SETTLE]

Examples:
    python tools/dump.py                                 # home → /tmp/hmsl_tree.txt
    python tools/dump.py versions
    python tools/dump.py "config:我的世界服务器" /tmp/cfg.txt 2.5

不需要 Quartz / 前台激活 / 任何权限；跨平台可跑（含未来 Linux CI + xvfb）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = "/tmp/hmsl_tree.txt"
DEFAULT_SETTLE = 1.5


def main() -> int:
    route = sys.argv[1] if len(sys.argv) > 1 else ""
    out_path = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT
    settle = float(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_SETTLE

    # 清掉旧输出，早失败早暴露
    try:
        os.remove(out_path)
    except FileNotFoundError:
        pass

    subprocess.run(["pkill", "-f", "gui_main.py"], stderr=subprocess.DEVNULL)
    time.sleep(0.4)

    cmd = [sys.executable, "gui_main.py", "--dump-tree", out_path, "--settle", str(settle)]
    if route:
        cmd += ["--route", route]

    timeout_s = settle + 8.0
    try:
        r = subprocess.run(cmd, cwd=PROJECT_ROOT,
                           capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        print(f"[dump] GUI didn't exit within {timeout_s}s — killing", file=sys.stderr)
        subprocess.run(["pkill", "-f", "gui_main.py"], stderr=subprocess.DEVNULL)
        return 3

    if r.returncode != 0:
        print(f"[dump] GUI exited with code {r.returncode}", file=sys.stderr)
        sys.stderr.write(r.stderr)
        return r.returncode

    if not os.path.isfile(out_path):
        print(f"[dump] no dump produced at {out_path}", file=sys.stderr)
        sys.stderr.write(r.stderr)
        return 4

    print(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
