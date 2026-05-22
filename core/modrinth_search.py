"""
Modrinth search + version-resolve + download.

All HTTP lives here so the GUI can drive a "browse mods" window with just
three calls: search_mods → user picks one → install_mod (which is
get_project_versions + pick_best_version + download_to under the hood).

Designed for test injection — every call to requests is wrapped in a thin
public function that tests can monkeypatch.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

from core.mod_scanner import ModInfo, classify_mod

_API = "https://api.modrinth.com"
_UA = "HMSL/0.1 mod-browser"


# ---------- data classes ----------

@dataclass
class ModSearchHit:
    project_id: str
    slug: str
    title: str
    description: str
    downloads: int
    icon_url: Optional[str]
    client_side: str
    server_side: str
    project_type: str          # "mod" / "plugin" / "modpack" / ...
    categories: List[str] = field(default_factory=list)

    def to_mod_info(self) -> ModInfo:
        """Reuse the classifier from Phase 2 without re-fetching the project."""
        return ModInfo(
            project_id=self.project_id,
            project_title=self.title,
            client_side=self.client_side,
            server_side=self.server_side,
        )

    def is_client_only(self) -> bool:
        return classify_mod(self.to_mod_info()) == "client_only"


@dataclass
class SearchPage:
    hits: List[ModSearchHit]
    offset: int
    total_hits: int
    limit: int

    @property
    def has_next(self) -> bool:
        return self.offset + len(self.hits) < self.total_hits


@dataclass
class ProjectVersion:
    version_id: str
    name: str
    version_type: str          # "release" / "beta" / "alpha"
    game_versions: List[str]
    loaders: List[str]
    files: List[Dict]          # Modrinth file dicts (url, filename, primary, hashes)


# ---------- public API ----------

def search_mods(
    query: str = "",
    mc_version: Optional[str] = None,
    loader: Optional[str] = None,
    project_type: str = "mod",
    offset: int = 0,
    limit: int = 20,
    timeout: float = 10.0,
) -> SearchPage:
    """
    Query Modrinth's /v2/search with facets.

    Facet rules: outer list = AND, inner list = OR.
    e.g. [["versions:1.20.4"],["project_type:mod"],["categories:forge"]]
    """
    facets: List[List[str]] = [[f"project_type:{project_type}"]]
    if mc_version:
        facets.append([f"versions:{mc_version}"])
    if loader:
        facets.append([f"categories:{loader.lower()}"])

    params = {
        "query": query,
        "facets": json.dumps(facets),
        "limit": limit,
        "offset": offset,
        "index": "relevance",
    }
    r = requests.get(f"{_API}/v2/search", params=params,
                     headers={"User-Agent": _UA}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    hits = [_parse_hit(h) for h in data.get("hits", [])]
    return SearchPage(
        hits=hits,
        offset=int(data.get("offset", offset)),
        total_hits=int(data.get("total_hits", 0)),
        limit=int(data.get("limit", limit)),
    )


def get_project_versions(
    project_id: str,
    mc_version: Optional[str] = None,
    loader: Optional[str] = None,
    timeout: float = 10.0,
) -> List[ProjectVersion]:
    """Modrinth /v2/project/{id}/version, optionally filtered."""
    params: Dict[str, str] = {}
    if mc_version:
        params["game_versions"] = json.dumps([mc_version])
    if loader:
        params["loaders"] = json.dumps([loader.lower()])
    r = requests.get(f"{_API}/v2/project/{project_id}/version", params=params,
                     headers={"User-Agent": _UA}, timeout=timeout)
    r.raise_for_status()
    return [_parse_version(v) for v in r.json()]


def pick_best_version(versions: List[ProjectVersion]) -> Optional[ProjectVersion]:
    """Prefer release over beta over alpha; within a type Modrinth lists newest first."""
    for vt in ("release", "beta", "alpha"):
        for v in versions:
            if v.version_type == vt and v.files:
                return v
    return None


def pick_primary_file(version: ProjectVersion) -> Optional[Dict]:
    """The Modrinth file marked primary=True, else the first .jar."""
    for f in version.files:
        if f.get("primary"):
            return f
    for f in version.files:
        if str(f.get("filename", "")).lower().endswith(".jar"):
            return f
    return None


def download_to(url: str, dest_dir: str, filename: str, timeout: float = 60.0) -> str:
    """Stream-download a file to dest_dir/filename. Returns full target path."""
    os.makedirs(dest_dir, exist_ok=True)
    target = os.path.join(dest_dir, filename)
    r = requests.get(url, stream=True,
                     headers={"User-Agent": _UA}, timeout=timeout)
    r.raise_for_status()
    with open(target, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)
    return target


# ---------- helpers (parsers) ----------

def _parse_hit(d: dict) -> ModSearchHit:
    return ModSearchHit(
        project_id=d.get("project_id", ""),
        slug=d.get("slug", ""),
        title=d.get("title", ""),
        description=d.get("description", ""),
        downloads=int(d.get("downloads", 0)),
        icon_url=d.get("icon_url"),
        client_side=d.get("client_side", "unknown"),
        server_side=d.get("server_side", "unknown"),
        project_type=d.get("project_type", "mod"),
        categories=list(d.get("categories", [])),
    )


def _parse_version(d: dict) -> ProjectVersion:
    return ProjectVersion(
        version_id=d.get("id", ""),
        name=d.get("name", ""),
        version_type=d.get("version_type", "release"),
        game_versions=list(d.get("game_versions", [])),
        loaders=list(d.get("loaders", [])),
        files=list(d.get("files", [])),
    )
