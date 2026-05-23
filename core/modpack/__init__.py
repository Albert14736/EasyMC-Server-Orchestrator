"""
Modpack import subsystem — HMCL-style provider registry.

Public entry points:
    detect_provider(archive_path) -> Optional[ModpackProvider]
    import_modpack(archive_path, target_dir, ...) -> ImportResult

Adding a new format (e.g. CurseForge, MultiMC) means creating a new module
under core/modpack/ that defines a ModpackProvider subclass and appending
it to PROVIDERS below — no changes to GUI or the import entry point.
"""
from __future__ import annotations

import os
from typing import Callable, List, Optional

from .base import ImportProgress, ImportResult, ModpackManifest, ModpackProvider
from .curseforge import CurseForgeProvider
from .hmcl_native import HMCLNativeProvider
from .hmcl_server import HMCLServerProvider
from .mcbbs import MCBBSProvider
from .modrinth import ModrinthProvider
from .multimc import MultiMCProvider


# Order matters: provider list is tried top-to-bottom. Use most-specific
# detectors first so e.g. MCBBS (which ALSO has manifestType=minecraftModpack
# in its meta) isn't claimed by CurseForge first.
#
#   1. Modrinth      .mrpack extension OR modrinth.index.json
#   2. MCBBS         mcbbs.packmeta present
#   3. HMCL Server   server-manifest.json present
#   4. HMCL Native   modpack.json + minecraft/pack.json both present
#   5. MultiMC       mmc-pack.json present (may live under instance subdir)
#   6. CurseForge    manifest.json with manifestType=minecraftModpack
PROVIDERS: List[ModpackProvider] = [
    ModrinthProvider(),
    MCBBSProvider(),
    HMCLServerProvider(),
    HMCLNativeProvider(),
    MultiMCProvider(),
    CurseForgeProvider(),
]


def detect_provider(archive_path: str) -> Optional[ModpackProvider]:
    """Return the first provider that claims this archive, or None."""
    for p in PROVIDERS:
        try:
            if p.detect(archive_path):
                return p
        except Exception:
            continue
    return None


def import_modpack(
    archive_path: str,
    server_name: str,
    parent_dir: str,
    env_manager,
    installer,
    downloader,
    progress_callback: Optional[Callable[[ImportProgress], None]] = None,
) -> ImportResult:
    """
    Two-phase import: detect provider → parse manifest → apply (creates server
    via server_factory + drops the modpack's files in).

    Returns ImportResult; never raises for expected failures (bad archive,
    missing manifest, no matching provider). True bugs propagate.
    """
    if not os.path.isfile(archive_path):
        return ImportResult(False, "", f"整合包文件不存在: {archive_path}")
    provider = detect_provider(archive_path)
    if not provider:
        return ImportResult(False, "", "未识别的整合包格式（暂不支持，或文件已损坏）")
    return provider.apply(
        archive_path=archive_path,
        server_name=server_name,
        parent_dir=parent_dir,
        env_manager=env_manager,
        installer=installer,
        downloader=downloader,
        progress_callback=progress_callback,
    )


__all__ = [
    "ImportProgress",
    "ImportResult",
    "ModpackManifest",
    "ModpackProvider",
    "PROVIDERS",
    "detect_provider",
    "import_modpack",
]
