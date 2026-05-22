"""
Pure orchestration for creating a Minecraft server instance.

This module contains NO GUI code and NO direct prints to stdout — all status
flows through the optional progress_callback so it can be driven from a GUI,
a CLI, or a pytest harness equally.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Callable, Optional


ProgressCallback = Callable[[float, str], None]


@dataclass
class CreateServerResult:
    success: bool
    server_path: str
    error: Optional[str] = None


def required_java_version(mc_version: str) -> int:
    """
    Return the Java major version required by a given Minecraft version.

    Replaces the previous string comparison `version >= "1.20.5"`, which was
    wrong because '8' > '2' lexicographically meant "1.8.8" mapped to Java 21.
    """
    try:
        parts = [int(x) for x in mc_version.split(".")]
    except ValueError:
        return 21
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts[0], parts[1], parts[2]
    if major != 1:
        return 21
    if minor > 20 or (minor == 20 and patch >= 5) or minor >= 21:
        return 21
    if minor >= 18:
        return 17
    if minor == 17:
        return 16
    return 8


def _start_script_for_platform(java_cmd: str, loader: str) -> tuple[str, str, bool]:
    """
    Return (filename, content, needs_chmod) for the platform's launch script.

    Forge/NeoForge installers generate their own `run.sh`/`run.bat` that wire up
    `@user_jvm_args.txt` and the libraries arg-file — they do NOT produce a
    `server.jar`, so we delegate to those instead of running `-jar server.jar`.
    Paper/Fabric do produce a plain server.jar, so we launch it directly.
    """
    loader_norm = loader.strip().lower()
    uses_forge_run = loader_norm in ("forge", "neoforge")

    if sys.platform == "win32":
        if uses_forge_run:
            body = "@echo off\r\ncall run.bat\r\npause\r\n"
        else:
            body = (
                "@echo off\r\n"
                f'"{java_cmd}" -Xms2G -Xmx4G -jar server.jar nogui\r\n'
                "pause\r\n"
            )
        return "start.bat", body, False

    if uses_forge_run:
        body = "#!/bin/zsh\nexec ./run.sh nogui\n"
    else:
        body = f'#!/bin/zsh\n"{java_cmd}" -Xms2G -Xmx4G -jar server.jar nogui\n'
    return "start.sh", body, True


def create_server(
    name: str,
    version: str,
    loader: str,
    parent_dir: str,
    env_manager,
    installer,
    downloader,
    progress_callback: Optional[ProgressCallback] = None,
) -> CreateServerResult:
    """
    Create a new Minecraft server instance under `parent_dir/name`.

    Returns a CreateServerResult; never raises for expected failures
    (network, bad loader). Truly unexpected exceptions still propagate.
    """
    def report(frac: float, msg: str) -> None:
        if progress_callback:
            progress_callback(frac, msg)

    if not name or not name.strip():
        return CreateServerResult(False, "", "服务器名称不能为空")
    if not os.path.isdir(parent_dir):
        return CreateServerResult(False, "", f"目标父目录不存在: {parent_dir}")

    server_path = os.path.join(parent_dir, name.strip())
    report(0.10, f"准备目标目录 {server_path}")
    os.makedirs(server_path, exist_ok=True)

    java_major = required_java_version(version)
    java_cmd = env_manager.get_java_cmd(java_major)
    report(0.20, f"已锁定 Java {java_major}，准备下载 {loader} 服务端...")

    loader_norm = loader.strip().lower()
    install_dispatch = {
        "paper": lambda: installer.install_paper(server_path, version),
        "fabric": lambda: installer.install_fabric(server_path, version),
        "forge": lambda: installer.install_forge(server_path, version, java_cmd),
        "neoforge": lambda: installer.install_neoforge(server_path, version, java_cmd),
    }
    if loader_norm not in install_dispatch:
        return CreateServerResult(False, server_path, f"未知服务端类型: {loader}")

    if not install_dispatch[loader_norm]():
        return CreateServerResult(False, server_path, f"{loader} 服务端安装失败")

    report(0.60, "写入 eula.txt...")
    with open(os.path.join(server_path, "eula.txt"), "w", encoding="utf-8") as f:
        f.write("eula=true\n")

    report(0.70, "同步模组数据库...")
    mod_dir_name = "plugins" if loader_norm == "paper" else "mods"
    downloader.sync(os.path.join(server_path, mod_dir_name), version, loader)

    report(0.90, "生成启动脚本...")
    script_name, script_body, needs_chmod = _start_script_for_platform(java_cmd, loader_norm)
    script_path = os.path.join(server_path, script_name)
    with open(script_path, "w", encoding="utf-8", newline="") as f:
        f.write(script_body)
    if needs_chmod:
        os.chmod(script_path, 0o755)

    report(1.0, "服务器部署完成")
    return CreateServerResult(True, server_path, None)
