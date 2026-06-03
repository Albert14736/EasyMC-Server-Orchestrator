"""实例扫描器 —— 检测 Minecraft 服务端实例及其 loader / 游戏版本。

纯文件系统逻辑（无 GUI、无网络），方便 pytest 无头测试。
取代旧的根目录 `instance_scanner_test.py`（GUI 以前从那里 import —— 这是个打包
地雷：PyInstaller 常把 *_test.py 排除掉，打出来的 .app 会直接 ImportError）。

公开 API：
    InstanceScanner(base_dir).scan() -> list[dict]   # 向后兼容，GUI 在用
    scan_instances(base_dir)         -> list[dict]
    get_instance_details(path, name) -> dict
    detect_loader_and_version(path)  -> (loader, version)   # 任一可能为 None
    detect_eula(path)                -> bool

实例字典字段：name, path, version(str|None), type(loader str|None),
              eula(bool), hmsl_created(bool)
"""
from __future__ import annotations

import os
import re
from typing import List, Optional, Tuple

# loader 显示名（保持稳定 —— 既用作实例字典的 type，也被调用方当 Modrinth
# loader facet 传出去，别随意改大小写）。
FORGE = "Forge"
NEOFORGE = "NeoForge"
FABRIC = "Fabric"
QUILT = "Quilt"
PAPER = "Paper"
SPIGOT = "Spigot"
BUKKIT = "Bukkit"
VANILLA = "Vanilla"

# 永远不是服务端实例的目录。
_EXCLUDE_DIRS = {".git", ".venv", "venv", "env", "core", "tests", "tools",
                 "__pycache__", ".vscode", ".pytest_cache", ".idea"}

# MC 版本形如 1.20 / 1.20.4 / 1.7.10；要求以 "1." 打头以免误抓 build 号。
_MC_VERSION_RE = re.compile(r"\b(1\.\d{1,2}(?:\.\d{1,2})?)\b")


def _top_level_jars(path: str) -> List[str]:
    try:
        return [f for f in os.listdir(path) if f.lower().endswith(".jar")]
    except OSError:
        return []


def _isdir(path: str, *parts: str) -> bool:
    return os.path.isdir(os.path.join(path, *parts))


def _mc_version_from_libraries(path: str) -> Optional[str]:
    """Forge/NeoForge/Fabric/Vanilla 会把原版 server jar 放在
    libraries/net/minecraft/server/<mcver>[-时间戳]/ 下 —— 最可靠的版本来源。"""
    base = os.path.join(path, "libraries", "net", "minecraft", "server")
    try:
        names = os.listdir(base)
    except OSError:
        return None
    for name in sorted(names):
        m = _MC_VERSION_RE.search(name)
        if m:
            return m.group(1)
    return None


def _paper_mc_version(path: str) -> Optional[str]:
    """Paper 写 version_history.json：{"currentVersion":"git-Paper-196 (MC: 1.20.1)"}
    —— Paper 独有，也是它最可靠的版本来源（jar 常被改名成 server.jar）。"""
    try:
        with open(os.path.join(path, "version_history.json"), "r",
                  encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return None
    m = re.search(r"MC:\s*(1\.\d{1,2}(?:\.\d{1,2})?)", content)
    return m.group(1) if m else None


def detect_loader_and_version(path: str) -> Tuple[Optional[str], Optional[str]]:
    """尽力从实例文件夹推断 (loader, mc_version)，任一都可能为 None。

    只读 `path` 下的文件系统；不解 jar 内部、不联网。
    检测顺序按"信号唯一性"由强到弱排——例如 Paper 目录里常混入 spigot.yml
    或 fabricmc 库，所以 Paper 的 version_history.json 必须早于 Fabric/Spigot 判定。"""
    jars = [(j, j.lower()) for j in _top_level_jars(path)]
    loader: Optional[str] = None
    version: Optional[str] = None

    # --- Forge: forge-<mc>-<build>.jar 或 libraries/net/minecraftforge/
    #     （整合包常没有顶层 jar，只在 libraries 里有）---
    for _orig, low in jars:
        m = re.match(r"forge-(1\.\d{1,2}(?:\.\d{1,2})?)-", low)
        if m:
            loader, version = FORGE, m.group(1)
            break
    if loader is None and _isdir(path, "libraries", "net", "minecraftforge"):
        loader = FORGE

    # --- NeoForge: neoforge-<build>.jar 或 libraries/net/neoforged/ ---
    if loader is None:
        if (any(low.startswith("neoforge-") for _o, low in jars)
                or _isdir(path, "libraries", "net", "neoforged")):
            loader = NEOFORGE

    # --- Paper：version_history.json 强信号（须早于 Fabric/Spigot）---
    if loader is None:
        pv = _paper_mc_version(path)
        if pv is not None:
            loader, version = PAPER, pv
        else:
            for _orig, low in jars:
                if low.startswith("paper"):
                    m = _MC_VERSION_RE.search(low)
                    loader, version = PAPER, (m.group(1) if m else None)
                    break

    # --- Fabric / Quilt ---
    if loader is None:
        if (any(low.startswith("fabric-server") for _o, low in jars)
                or _isdir(path, ".fabric")
                or os.path.isfile(os.path.join(path, "fabric-server-launcher.properties"))
                or _isdir(path, "libraries", "net", "fabricmc")):
            loader = FABRIC
        elif (any(low.startswith("quilt-server") for _o, low in jars)
              or _isdir(path, "libraries", "org", "quiltmc")):
            loader = QUILT

    # --- Spigot / Bukkit（jar 名或 yml 标记；Paper 已在上面拦截）---
    if loader is None:
        for _orig, low in jars:
            if low.startswith("spigot"):
                m = _MC_VERSION_RE.search(low)
                loader, version = SPIGOT, (m.group(1) if m else None)
                break
            if low.startswith("craftbukkit") or low.startswith("bukkit"):
                m = _MC_VERSION_RE.search(low)
                loader, version = BUKKIT, (m.group(1) if m else None)
                break
    if loader is None and os.path.isfile(os.path.join(path, "spigot.yml")):
        loader = SPIGOT
    elif loader is None and os.path.isfile(os.path.join(path, "bukkit.yml")):
        loader = BUKKIT

    # --- Vanilla: minecraft_server.<mc>.jar / server.jar ---
    if loader is None:
        for _orig, low in jars:
            m = re.match(r"minecraft_server\.(1\.\d{1,2}(?:\.\d{1,2})?)\.jar", low)
            if m:
                loader, version = VANILLA, m.group(1)
                break
        if loader is None and any(low == "server.jar" for _o, low in jars):
            loader = VANILLA

    # --- 版本兜底：从 libraries 里捞（覆盖 Forge/NeoForge/Fabric/Vanilla）---
    if version is None:
        version = _mc_version_from_libraries(path)

    return loader, version


def detect_eula(path: str) -> bool:
    try:
        with open(os.path.join(path, "eula.txt"), "r",
                  encoding="utf-8", errors="replace") as f:
            return "eula=true" in f.read().lower()
    except OSError:
        return False


def _looks_like_server(path: str) -> bool:
    """eula.txt 是最强信号；否则看顶层有没有 server/loader 特征 jar。"""
    if os.path.isfile(os.path.join(path, "eula.txt")):
        return True
    for f in _top_level_jars(path):
        low = f.lower()
        if any(k in low for k in ("server", "forge", "fabric", "paper",
                                  "neoforge", "quilt", "spigot")):
            return True
    return False


def get_instance_details(path: str, name: str) -> dict:
    """解析单个实例文件夹为标准字典。"""
    loader, version = detect_loader_and_version(path)
    hmsl_created = (os.path.isfile(os.path.join(path, "start.sh"))
                    or os.path.isfile(os.path.join(path, "start.bat")))
    return {
        "name": name,
        "path": path,
        "version": version,   # str | None
        "type": loader,       # loader 显示名 str | None
        "eula": detect_eula(path),
        "hmsl_created": hmsl_created,
    }


def scan_instances(base_dir: str) -> List[dict]:
    """扫描 base_dir 下所有一级子目录，返回看起来像服务端实例的那些。"""
    if not base_dir or not os.path.isdir(base_dir):
        return []
    out: List[dict] = []
    for item in sorted(os.listdir(base_dir)):
        if item in _EXCLUDE_DIRS or item.startswith("."):
            continue
        item_path = os.path.join(base_dir, item)
        if os.path.isdir(item_path) and _looks_like_server(item_path):
            out.append(get_instance_details(item_path, item))
    return out


class InstanceScanner:
    """向后兼容包装；新代码请直接用模块级函数。"""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir

    def scan(self) -> List[dict]:
        return scan_instances(self.base_dir)


if __name__ == "__main__":
    import sys
    base = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    print(f"=== 扫描 {base} ===")
    found = scan_instances(base)
    if not found:
        print("未发现任何有效的服务器实例。")
    for inst in found:
        loader = inst["type"] or "未知"
        version = inst["version"] or "未知"
        eula = "✅" if inst["eula"] else "❌"
        print(f"- {inst['name']}  [{loader} {version}]  EULA {eula}")
        print(f"    {inst['path']}")
