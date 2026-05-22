"""
Scan a server's mod directory and classify each jar via Modrinth hash lookup.

The classification has three useful buckets:
  - client_only: definitely (or effectively) client-side; remove for server use
  - server_ok:   compatible with server
  - unknown:     not on Modrinth, or network failed — leave alone, let user decide

GUI runs scan_server_mods on a background thread and renders progress via the
optional progress_callback(current, total, current_filename).
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import requests


_MODRINTH_API = "https://api.modrinth.com"
_USER_AGENT = "HMSL/0.1 (https://github.com/hmsl) mod-scanner"


@dataclass
class ModInfo:
    project_id: str
    project_title: str
    client_side: str  # "required" | "optional" | "unsupported" | "unknown"
    server_side: str  # same domain


@dataclass
class ScanEntry:
    file_path: str
    file_name: str
    status: str       # "client_only" | "server_ok" | "unknown" | "error"
    mod_info: Optional[ModInfo] = None
    error_message: Optional[str] = None


@dataclass
class ScanReport:
    server_path: str
    mods_dir: str          # empty string if no mods/ or plugins/ found
    entries: List[ScanEntry] = field(default_factory=list)

    def client_only(self) -> List[ScanEntry]:
        return [e for e in self.entries if e.status == "client_only"]

    def unknown(self) -> List[ScanEntry]:
        return [e for e in self.entries if e.status == "unknown"]

    def server_ok(self) -> List[ScanEntry]:
        return [e for e in self.entries if e.status == "server_ok"]

    def errors(self) -> List[ScanEntry]:
        return [e for e in self.entries if e.status == "error"]


def compute_jar_sha1(file_path: str, chunk_size: int = 65536) -> str:
    """Stream-hash a file — mods can be 100MB+, never .read() whole."""
    h = hashlib.sha1()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def lookup_mod_by_sha1(sha1: str, timeout: float = 10.0) -> Optional[ModInfo]:
    """
    Hash → Modrinth version → Modrinth project metadata.
    Returns None on 404, network error, or any unexpected response shape.
    """
    headers = {"User-Agent": _USER_AGENT}
    try:
        r = requests.get(
            f"{_MODRINTH_API}/v2/version_file/{sha1}",
            params={"algorithm": "sha1"},
            headers=headers, timeout=timeout,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        version = r.json()
        project_id = version.get("project_id")
        if not project_id:
            return None

        r = requests.get(
            f"{_MODRINTH_API}/v2/project/{project_id}",
            headers=headers, timeout=timeout,
        )
        r.raise_for_status()
        project = r.json()
        return ModInfo(
            project_id=project_id,
            project_title=project.get("title", project_id),
            client_side=project.get("client_side", "unknown"),
            server_side=project.get("server_side", "unknown"),
        )
    except (requests.RequestException, ValueError):
        return None


def classify_mod(info: Optional[ModInfo]) -> str:
    """
    Decide whether a mod is client-only based on Modrinth side metadata.

    - server_side == 'unsupported' → definitively client-only
    - server_side == 'optional' AND client_side == 'required' → effectively
      client-only (server can skip it, but client demands it ⇒ no server value)
    - otherwise → server-compatible
    - None info → unknown
    """
    if info is None:
        return "unknown"
    if info.server_side == "unsupported":
        return "client_only"
    if info.server_side == "optional" and info.client_side == "required":
        return "client_only"
    return "server_ok"


def find_mods_dir(server_path: str) -> Optional[str]:
    """Prefer mods/ (Forge/Fabric/NeoForge); fall back to plugins/ (Paper)."""
    for sub in ("mods", "plugins"):
        candidate = os.path.join(server_path, sub)
        if os.path.isdir(candidate):
            return candidate
    return None


ProgressCallback = Callable[[int, int, str], None]  # (current, total, filename)


def scan_server_mods(
    server_path: str,
    progress_callback: Optional[ProgressCallback] = None,
    lookup_fn: Callable[[str], Optional[ModInfo]] = lookup_mod_by_sha1,
) -> ScanReport:
    """
    Walk `server_path/{mods,plugins}/*.jar`, hash each, classify via Modrinth.
    `lookup_fn` is injectable so tests can avoid network entirely.
    """
    mods_dir = find_mods_dir(server_path)
    if not mods_dir:
        return ScanReport(server_path=server_path, mods_dir="")

    try:
        jar_names = sorted(
            f for f in os.listdir(mods_dir)
            if f.lower().endswith(".jar")
            and os.path.isfile(os.path.join(mods_dir, f))
        )
    except OSError:
        return ScanReport(server_path=server_path, mods_dir=mods_dir)

    entries: List[ScanEntry] = []
    total = len(jar_names)
    for i, name in enumerate(jar_names, start=1):
        path = os.path.join(mods_dir, name)
        if progress_callback:
            progress_callback(i, total, name)
        try:
            sha1 = compute_jar_sha1(path)
        except OSError as e:
            entries.append(ScanEntry(path, name, status="error", error_message=str(e)))
            continue
        info = lookup_fn(sha1)
        entries.append(ScanEntry(path, name, status=classify_mod(info), mod_info=info))

    return ScanReport(server_path=server_path, mods_dir=mods_dir, entries=entries)


def disable_mods(entries: List[ScanEntry], mods_dir: str) -> int:
    """
    Move each entry's jar to `mods_dir/.disabled/` (created on demand).
    On filename collision, appends _1, _2, ... so the original disabled file
    is never overwritten. Returns the count of files successfully moved.

    This is intentionally REVERSIBLE — user can restore by moving back.
    """
    disabled_dir = os.path.join(mods_dir, ".disabled")
    os.makedirs(disabled_dir, exist_ok=True)

    moved = 0
    for entry in entries:
        if not os.path.isfile(entry.file_path):
            continue
        target = os.path.join(disabled_dir, entry.file_name)
        suffix = 1
        while os.path.exists(target):
            base, ext = os.path.splitext(entry.file_name)
            target = os.path.join(disabled_dir, f"{base}_{suffix}{ext}")
            suffix += 1
        try:
            os.rename(entry.file_path, target)
            moved += 1
        except OSError:
            continue
    return moved
