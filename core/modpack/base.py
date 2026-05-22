"""
Provider ABC + shared dataclasses for the modpack import subsystem.

A ModpackProvider does three things:
  detect(path)      -> bool                          # is this my format?
  parse(path)       -> ModpackManifest                # extract metadata
  apply(path, ...)  -> ImportResult                   # create server, install files

GUI/CLI call into the package-level import_modpack(); they don't touch
providers directly. This keeps the surface stable as we add more providers.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, List, Optional


# Anything under these prefixes is 100% client-only by definition, no
# matter what the manifest says (or fails to say).
_CLIENT_ONLY_PATH_PREFIXES = (
    "resourcepacks/",
    "shaderpacks/",
    "texturepacks/",
)


@dataclass
class ModpackFile:
    """One file inside the modpack — mod jar, config, resource, etc."""
    path: str                  # relative to server root, e.g. "mods/sodium.jar"
    sha1: Optional[str] = None
    sha512: Optional[str] = None
    download_urls: List[str] = field(default_factory=list)
    file_size: Optional[int] = None
    # Modrinth env semantics: "required" / "optional" / "unsupported" / None.
    # None means the modpack manifest didn't fill this in — we'll try a hash
    # lookup to find the real values during enrich_compat().
    env_client: Optional[str] = None
    env_server: Optional[str] = None

    def is_server_skipped(self) -> bool:
        """
        True if this file should NOT be installed on a server.

        Three layers, in order of confidence:
        1. Path-based: resource/shader/texture packs are *definitively* client.
        2. server=unsupported (Modrinth flagged the project as no-server).
        3. server=optional + client=required: server gracefully skips it but
           client needs it ⇒ shipping to server has no value.

        Aligned with core.mod_scanner.classify_mod so behavior is consistent
        between modpack import and post-install scanning.
        """
        p = self.path.lower().replace("\\", "/")
        if any(p.startswith(prefix) for prefix in _CLIENT_ONLY_PATH_PREFIXES):
            return True
        if self.env_server == "unsupported":
            return True
        if self.env_server == "optional" and self.env_client == "required":
            return True
        return False

    def needs_compat_lookup(self) -> bool:
        """
        True when we should second-guess this file's env via Modrinth project
        metadata. Two cases qualify:

        1. env is unset (older or community packs often omit it).
        2. env says 'required/required' — this is mrpack tooling's lazy default
           and frequently masks a client-only mod (Sodium/Iris/Catalogue etc.)
           when the author didn't bother classifying. Modrinth's project-level
           server_side=unsupported is the authoritative override.

        Restricted to mods/ jars with a sha1 (the only thing we can look up).
        """
        in_mods = self.path.lower().replace("\\", "/").startswith("mods/")
        if not in_mods or not self.sha1:
            return False
        if self.env_server is None:
            return True
        if self.env_server == "required" and self.env_client == "required":
            return True
        return False


@dataclass
class ModpackManifest:
    """Format-agnostic, GUI-friendly view of an integrated modpack."""
    format: str                # "modrinth" / "curseforge" / "multimc" / ...
    name: str
    version: str
    mc_version: str
    loader: str                # "Forge" / "Fabric" / "NeoForge" / "Paper" / "Vanilla"
    loader_version: Optional[str] = None
    summary: str = ""
    files: List[ModpackFile] = field(default_factory=list)

    @property
    def server_files(self) -> List[ModpackFile]:
        return [f for f in self.files if not f.is_server_skipped()]

    @property
    def skipped_client_files(self) -> List[ModpackFile]:
        return [f for f in self.files if f.is_server_skipped()]


@dataclass
class ImportProgress:
    """Streamed during apply() so the GUI can render progress."""
    stage: str                 # "detecting" / "parsing" / "creating_server" /
                               # "downloading_files" / "applying_overrides" / "done"
    message: str               # human-readable detail
    current: int = 0           # for downloads: current file index
    total: int = 0


@dataclass
class ImportResult:
    success: bool
    server_path: str
    error: Optional[str] = None
    manifest: Optional[ModpackManifest] = None
    files_installed: int = 0
    files_skipped_client: int = 0
    files_failed: int = 0
    # CurseForge mods whose authors opted out of the public API but which we
    # downloaded via the public forgecdn CDN as a fallback (same approach
    # HMCL/PrismLauncher use). Each entry: {"name": ..., "cf_url": ...}.
    # Surfaced to the GUI so the user knows this happened and can support
    # the author at the listed CF page if they want.
    bypassed_mods: List[dict] = field(default_factory=list)


class ModpackProvider(ABC):
    """Subclasses register themselves in core/modpack/__init__.py PROVIDERS."""

    name: str = "unknown"   # short id used in ModpackManifest.format

    @abstractmethod
    def detect(self, archive_path: str) -> bool: ...

    @abstractmethod
    def parse(self, archive_path: str) -> ModpackManifest: ...

    @abstractmethod
    def apply(
        self,
        archive_path: str,
        server_name: str,
        parent_dir: str,
        env_manager,
        installer,
        downloader,
        progress_callback: Optional[Callable[[ImportProgress], None]] = None,
    ) -> ImportResult: ...
