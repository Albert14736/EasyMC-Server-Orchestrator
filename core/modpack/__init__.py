"""
Modpack import subsystem — HMCL-style provider registry.

Public entry points:
    detect_provider(archive_path) -> Optional[ModpackProvider]
    import_modpack(archive_path, server_name, parent_dir, ..., *, manifest=None) -> ImportResult

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
from .modrinth import ModrinthProvider, download_breaker
from .multimc import MultiMCProvider


# Order matters: provider list is tried top-to-bottom. Use most-specific
# detectors first so e.g. MCBBS (which ALSO has manifestType=minecraftModpack
# in its meta) isn't claimed by CurseForge first.
#
#   1. Modrinth      .mrpack extension OR modrinth.index.json
#   2. MCBBS         mcbbs.packmeta present
#   3. HMCL Server   server-manifest.json present
#   4. HMCL Native   modpack.json + minecraft/pack.json both present
#   5. MultiMC       mmc-pack.json at root or in ONE top-level instance folder
#                    (game dir .minecraft/ or Prism's minecraft/)
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
    *,
    manifest: Optional[ModpackManifest] = None,
) -> ImportResult:
    """
    Two-phase import: detect provider → parse manifest → apply (creates server
    via server_factory + drops the modpack's files in).

    `manifest`: what provider.parse() (+ enrich_compat()) returned for this
    archive when the GUI built its preview. It is reused as-is — the import
    then skips exactly the client-only files the preview counted and doesn't
    query Modrinth a second time. Ignored (the archive is parsed again) when
    it came from another provider or the file changed since.

    Returns ImportResult and never raises: bad archives, missing manifests and
    unexpected errors alike come back as ImportResult(success=False, error=...),
    so a GUI worker thread can't die silently. Partial problems (some files
    not downloaded / not writable on this OS) keep success=True and are listed
    in ImportResult.warnings.
    """
    try:
        if not os.path.isfile(archive_path):
            return ImportResult(False, "", f"整合包文件不存在: {archive_path}")
        provider = detect_provider(archive_path)
        if not provider:
            return ImportResult(False, "", "未识别的整合包格式（暂不支持，或文件已损坏）")
        extra = {"manifest": manifest} if manifest is not None else {}
        # Fail fast on unreachable download hosts for the rest of this import
        with download_breaker() as breaker:
            result = provider.apply(
                archive_path=archive_path,
                server_name=server_name,
                parent_dir=parent_dir,
                env_manager=env_manager,
                installer=installer,
                downloader=downloader,
                progress_callback=progress_callback,
                **extra,
            )
        # Explain skipped hosts only when files are actually missing (not when
        # another mirror of the same files worked)
        notes = breaker.summary()
        if (notes and getattr(result, "success", False) and getattr(result, "files_failed", 0)
                and isinstance(result.warnings, list)):
            result.warnings.extend(w for w in notes if w not in result.warnings)
        return result
    except Exception as e:  # never let an import crash the caller's thread
        server_path = ""
        try:
            candidate = os.path.join(parent_dir, (server_name or "").strip())
            if (server_name or "").strip() and os.path.isdir(candidate):
                server_path = candidate
        except (TypeError, ValueError):
            pass
        return ImportResult(False, server_path,
                            f"导入过程中出现意外错误：{type(e).__name__}: {e}")


__all__ = [
    "ImportProgress",
    "ImportResult",
    "ModpackManifest",
    "ModpackProvider",
    "PROVIDERS",
    "detect_provider",
    "import_modpack",
]
