"""
MultiMC `.zip` modpack provider.

Format: ZIP containing mmc-pack.json (component list) + instance.cfg
(properties file) at root, and the actual game/mod content under a
`.minecraft/` subdirectory (sometimes a sibling, sometimes within the
top-level "instance" folder).

MultiMC packs are SELF-CONTAINED — all mods live inside the zip under
.minecraft/mods/ . There's no separate file download step, so no API key
needed and no network for the install. Just parse + extract.
"""
from __future__ import annotations

import configparser
import io
import json
import os
import zipfile
from typing import Callable, Dict, List, Optional, Tuple

from core.server_factory import CreateServerResult, create_server

from .base import (
    ImportProgress,
    ImportResult,
    ModpackFile,
    ModpackManifest,
    ModpackProvider,
)
from .modrinth import _extract_overrides

_MMC_PACK = "mmc-pack.json"
_INSTANCE_CFG = "instance.cfg"

# MultiMC component uid → server_factory loader name
_UID_LOADER_MAP = {
    "net.minecraftforge":             "Forge",
    "net.neoforged":                  "NeoForge",
    "net.fabricmc.fabric-loader":     "Fabric",
    "org.quiltmc.quilt-loader":       "Fabric",  # Quilt API-compat with Fabric
}
_UID_MINECRAFT = "net.minecraft"


class MultiMCProvider(ModpackProvider):
    name = "multimc"

    # ---------- detect ----------

    def detect(self, archive_path: str) -> bool:
        if not archive_path.lower().endswith(".zip"):
            return False
        try:
            with zipfile.ZipFile(archive_path) as zf:
                # mmc-pack.json may be at root OR inside a single top-level folder
                # (MultiMC's "export instance" puts everything under <InstanceName>/)
                return any(n.endswith(_MMC_PACK) for n in zf.namelist())
        except (zipfile.BadZipFile, OSError):
            return False

    # ---------- parse ----------

    def parse(self, archive_path: str) -> ModpackManifest:
        with zipfile.ZipFile(archive_path) as zf:
            pack_path = _find_in_zip(zf, _MMC_PACK)
            if not pack_path:
                raise ValueError(f"{_MMC_PACK} 不存在于该 zip 中")
            try:
                data = json.loads(zf.read(pack_path).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise ValueError(f"无法解析 {_MMC_PACK}: {e}") from e

            cfg_path = _find_in_zip(zf, _INSTANCE_CFG)
            instance_name = ""
            if cfg_path:
                try:
                    cfg_text = zf.read(cfg_path).decode("utf-8", errors="replace")
                    instance_name = _read_instance_name(cfg_text)
                except (UnicodeDecodeError, OSError):
                    pass

        mc_version, loader, loader_version = _components_to_loader(
            data.get("components", []))

        # MultiMC packs don't list individual files in the manifest; everything
        # is extracted from .minecraft/. So manifest.files stays empty.
        return ModpackManifest(
            format="multimc",
            name=instance_name or "MultiMC Modpack",
            version="",  # MultiMC has no version field
            mc_version=mc_version,
            loader=loader,
            loader_version=loader_version,
            summary="",
            files=[],
        )

    # ---------- apply ----------

    def apply(
        self,
        archive_path: str,
        server_name: str,
        parent_dir: str,
        env_manager,
        installer,
        downloader,
        progress_callback: Optional[Callable[[ImportProgress], None]] = None,
    ) -> ImportResult:
        def report(stage, msg, current=0, total=0):
            if progress_callback:
                progress_callback(ImportProgress(stage=stage, message=msg,
                                                 current=current, total=total))

        report("parsing", "正在读取 mmc-pack.json…")
        try:
            manifest = self.parse(archive_path)
        except ValueError as e:
            return ImportResult(False, "", str(e))

        report("creating_server", f"正在创建 {manifest.loader} {manifest.mc_version} 服务端…")
        cr: CreateServerResult = create_server(
            name=server_name, version=manifest.mc_version,
            loader=manifest.loader, parent_dir=parent_dir,
            env_manager=env_manager, installer=installer, downloader=downloader,
        )
        if not cr.success:
            return ImportResult(False, cr.server_path or "",
                                f"创建服务端失败：{cr.error}", manifest=manifest)
        server_path = cr.server_path

        # MultiMC packs put game content under .minecraft/ (sometimes inside an
        # instance-name top-level folder, e.g. "MyPack/.minecraft/"). Find the
        # right prefix and extract that as our overrides root.
        prefix = _find_minecraft_prefix(archive_path)
        if not prefix:
            return ImportResult(False, server_path,
                                "未找到 .minecraft 内容目录", manifest=manifest)

        report("applying_overrides", f"正在解压 {prefix}…")
        _extracted, ov_installed, ov_skipped = _extract_overrides(
            archive_path, server_path, prefix)

        report("done", "整合包导入完成")
        return ImportResult(
            success=True,
            server_path=server_path,
            manifest=manifest,
            files_installed=ov_installed,
            files_skipped_client=ov_skipped,
            files_failed=0,
        )


# ---------- helpers ----------

def _find_in_zip(zf: zipfile.ZipFile, basename: str) -> Optional[str]:
    """Return the full zip-entry path whose basename matches, or None."""
    for n in zf.namelist():
        if n.endswith("/" + basename) or n == basename:
            return n
    return None


def _read_instance_name(cfg_text: str) -> str:
    """Parse MultiMC's INI-style instance.cfg; return `name` if present."""
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    # MultiMC's cfg has no [section] header — synthesize one
    try:
        parser.read_string("[DEFAULT]\n" + cfg_text)
    except configparser.Error:
        return ""
    return parser["DEFAULT"].get("name", "").strip()


def _components_to_loader(components: list) -> Tuple[str, str, Optional[str]]:
    """
    From MultiMC's components list, return (mc_version, loader, loader_version).
    Falls back to ("", "Paper", None) when no Minecraft component is present.
    """
    mc_version = ""
    loader = "Paper"
    loader_version: Optional[str] = None
    if not isinstance(components, list):
        return mc_version, loader, loader_version
    for c in components:
        if not isinstance(c, dict):
            continue
        uid = c.get("uid", "")
        ver = c.get("version", "")
        if not isinstance(uid, str):
            continue
        if uid == _UID_MINECRAFT:
            mc_version = str(ver)
        elif uid in _UID_LOADER_MAP:
            loader = _UID_LOADER_MAP[uid]
            loader_version = str(ver) if ver else None
    return mc_version, loader, loader_version


def _find_minecraft_prefix(archive_path: str) -> Optional[str]:
    """
    Locate the .minecraft directory inside the zip. MultiMC's export wraps
    the instance in <InstanceName>/, so .minecraft/ might be at:
      .minecraft/
      MyPack/.minecraft/
      Instances/MyPack/.minecraft/
    Return the prefix (ending in '/') of the .minecraft directory.
    """
    try:
        with zipfile.ZipFile(archive_path) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return None
    for n in names:
        # look for any path ending in '.minecraft/' (directory entry) or
        # whose first 'something/.minecraft/' segment exists
        parts = n.split("/")
        for i, seg in enumerate(parts):
            if seg == ".minecraft":
                return "/".join(parts[:i + 1]) + "/"
    return None
