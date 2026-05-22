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
from .modrinth import ModrinthProvider


# Order matters: provider list is tried top-to-bottom. Put the most specific
# detectors first (Modrinth's distinct .mrpack extension is the easiest tell).
# CurseForge uses generic .zip but its detect() reads manifest.json's
# "manifestType" field so false positives are negligible.
PROVIDERS: List[ModpackProvider] = [
    ModrinthProvider(),
    CurseForgeProvider(),
    # Future providers go here: MultiMCProvider(), MCBBSProvider(), ...
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
