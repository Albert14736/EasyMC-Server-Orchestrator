"""
HMCL Native `.zip` modpack provider.

HMCL's own export format: a ZIP containing `modpack.json` (name/author/etc)
and `minecraft/pack.json` (where `jar` is the mc_version). Game content
lives under the zip's `minecraft/` subdir, which gets installed as the
server root.

Loader info is NOT in these manifest files — HMCL derives it from version
patches embedded elsewhere. For our server-side purpose, we make a
best-effort: scan minecraft/versions/ for a forge/fabric/neoforge marker,
and fall back to Paper if nothing matches.

Self-contained: no separate file downloads.
"""
from __future__ import annotations

import json
import os
import zipfile
from typing import Callable, Optional, Tuple

from core.server_factory import CreateServerResult, create_server

from .base import ImportProgress, ImportResult, ModpackManifest, ModpackProvider
from .modrinth import _extract_overrides

_MODPACK_JSON = "modpack.json"
_PACK_JSON = "minecraft/pack.json"
_CONTENT_PREFIX = "minecraft/"


class HMCLNativeProvider(ModpackProvider):
    name = "hmcl_native"

    def detect(self, archive_path: str) -> bool:
        if not archive_path.lower().endswith(".zip"):
            return False
        try:
            with zipfile.ZipFile(archive_path) as zf:
                names = set(zf.namelist())
                return _MODPACK_JSON in names and _PACK_JSON in names
        except (zipfile.BadZipFile, OSError):
            return False

    def parse(self, archive_path: str) -> ModpackManifest:
        with zipfile.ZipFile(archive_path) as zf:
            try:
                meta = json.loads(zf.read(_MODPACK_JSON).decode("utf-8"))
            except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as e:
                raise ValueError(f"无法解析 {_MODPACK_JSON}: {e}") from e
            try:
                pack = json.loads(zf.read(_PACK_JSON).decode("utf-8"))
            except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as e:
                raise ValueError(f"无法解析 {_PACK_JSON}: {e}") from e

            mc_version = str(pack.get("jar", ""))
            loader, loader_version = _guess_loader_from_zip(zf, mc_version)

        return ModpackManifest(
            format="hmcl_native",
            name=str(meta.get("name", "HMCL Modpack")),
            version=str(meta.get("version", "")),
            mc_version=mc_version,
            loader=loader,
            loader_version=loader_version,
            summary=str(meta.get("description", meta.get("author", ""))),
            files=[],
        )

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

        report("parsing", "正在读取 modpack.json…")
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

        report("applying_overrides", f"正在解压 {_CONTENT_PREFIX}…")
        _extracted, ov_installed, ov_skipped = _extract_overrides(
            archive_path, server_path, _CONTENT_PREFIX)

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

def _guess_loader_from_zip(zf: zipfile.ZipFile, mc_version: str) -> Tuple[str, Optional[str]]:
    """
    HMCL Native's loader info lives in minecraft/versions/<version>/<version>.json
    as a `patches` array. We do a light-touch scan for loader marker filenames
    in the zip — full patches parsing is overkill for our needs.
    """
    names = zf.namelist()
    forge_marker     = any("forge-"     in n.lower() and n.endswith(".jar") for n in names)
    fabric_marker    = any("fabric-loader" in n.lower() or "fabric-installer" in n.lower() for n in names)
    neoforge_marker  = any("neoforge-"  in n.lower() and n.endswith(".jar") for n in names)
    # mods/ folder presence strongly hints non-vanilla
    has_mods         = any(n.startswith(_CONTENT_PREFIX + "mods/") for n in names)

    if neoforge_marker:  return "NeoForge", None
    if forge_marker:     return "Forge", None
    if fabric_marker:    return "Fabric", None
    # Has mods/ but no obvious loader → guess Forge (most common Forge-era choice)
    if has_mods:         return "Forge", None
    return "Paper", None
