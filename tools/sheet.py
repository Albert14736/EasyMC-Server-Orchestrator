#!/usr/bin/env python3
"""
HMSL 联络图（contact sheet）—— 给 Claude 用的：把多个页面各截一张、缩小、拼成
一张带标签的网格图。这样核实整套界面时，Claude 只需读『一张』图而不是 N 张，
图片 token 大约省到原来的 1/(格子数)。

Usage:
    python tools/sheet.py [OUT_PNG] [route ...]

Examples:
    python tools/sheet.py                                  # 默认三页 → /tmp/hmsl_sheet.png
    python tools/sheet.py /tmp/s.png home versions download
    python tools/sheet.py /tmp/s.png home "detail:我的世界服务器" "config:我的世界服务器"

依赖：Pillow（仅此 dev 工具用；正常运行/打包不需要）。底层复用 tools/snap.py，
所以同样只支持 macOS（按窗口 ID 精确截图，无视 Claude 全屏遮挡）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TOOLS_DIR)
SNAP = os.path.join(TOOLS_DIR, "snap.py")

DEFAULT_ROUTES = ["home", "versions", "download"]
CELL_W = 560          # 每格缩略宽度（越小越省 token）
COLS = 3
LABEL_H = 30
PAD = 10
SETTLE = 1.6
BG = (26, 26, 26)     # 深色，贴合 HMSL 主题
FG = (235, 235, 235)
ERR = (200, 90, 90)

# macOS 上能渲染中文的字体候选（路由名常含中文）
_FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
]


def _load_font(size=18):
    for p in _FONT_CANDIDATES:
        if os.path.isfile(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _snap_one(route: str, out_png: str) -> bool:
    """调 snap.py 截单页；成功返回 True。"""
    cmd = [sys.executable, SNAP, route, out_png, str(SETTLE)]
    try:
        r = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True,
                           text=True, timeout=SETTLE + 8)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0 and os.path.isfile(out_png)


def _cell(route: str, png_path: str, ok: bool, font) -> Image.Image:
    """做一格：顶部标签条 + 下面缩略图（失败则画红字占位）。"""
    if ok:
        img = Image.open(png_path).convert("RGB")
        h = max(1, round(CELL_W * img.height / img.width))
        img = img.resize((CELL_W, h), Image.LANCZOS)
    else:
        h = round(CELL_W * 0.62)
        img = Image.new("RGB", (CELL_W, h), (40, 30, 30))
        d = ImageDraw.Draw(img)
        d.text((12, h // 2 - 10), "截图失败 / 路由无效", fill=ERR, font=font)

    cell = Image.new("RGB", (CELL_W, LABEL_H + h), BG)
    d = ImageDraw.Draw(cell)
    d.rectangle([0, 0, CELL_W, LABEL_H], fill=(38, 38, 38))
    d.text((8, 6), route, fill=FG if ok else ERR, font=font)
    cell.paste(img, (0, LABEL_H))
    return cell


def main() -> int:
    args = sys.argv[1:]
    out_path = os.path.abspath(args[0]) if args else "/tmp/hmsl_sheet.png"
    routes = args[1:] if len(args) > 1 else list(DEFAULT_ROUTES)

    font = _load_font()
    cells = []
    with tempfile.TemporaryDirectory() as td:
        for i, route in enumerate(routes):
            png = os.path.join(td, f"snap_{i}.png")
            ok = _snap_one(route, png)
            print(f"  [{'OK ' if ok else 'ERR'}] {route}", file=sys.stderr)
            cells.append(_cell(route, png, ok, font))

    if not cells:
        print("[sheet] no routes", file=sys.stderr)
        return 2

    cols = min(COLS, len(cells))
    rows = (len(cells) + cols - 1) // cols
    cw = max(c.width for c in cells)
    ch = max(c.height for c in cells)
    W = cols * cw + (cols + 1) * PAD
    H = rows * ch + (rows + 1) * PAD
    sheet = Image.new("RGB", (W, H), BG)
    for idx, c in enumerate(cells):
        r, col = divmod(idx, cols)
        x = PAD + col * (cw + PAD)
        y = PAD + r * (ch + PAD)
        sheet.paste(c, (x, y))

    sheet.save(out_path)
    print(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
