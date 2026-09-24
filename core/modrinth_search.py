"""
Modrinth search + version-resolve + download.

All HTTP lives here so the GUI can drive a "browse mods" window with just
three calls: search_mods → user picks one → install_mod (which is
get_project_versions + pick_best_version + download_to under the hood).

Designed for test injection — every call to requests is wrapped in a thin
public function that tests can monkeypatch.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

from core.mod_scanner import ModInfo, classify_mod

_API = "https://api.modrinth.com"
_UA = "HMSL/0.1 mod-browser"

# 服务端类型 → Modrinth 上能在它上面运行的 loader 标签（精确匹配）。
# Paper 能跑 Spigot/Bukkit 插件；只标了 purpur / folia 的不保证能在 Paper 上跑。
_PLUGIN_LOADERS: Dict[str, List[str]] = {
    "paper": ["paper", "spigot", "bukkit"],
    "purpur": ["purpur", "paper", "spigot", "bukkit"],
    "spigot": ["spigot", "bukkit"],
    "bukkit": ["bukkit"],
    "folia": ["folia"],
}
_MOD_LOADERS: Dict[str, List[str]] = {
    "fabric": ["fabric"],
    "quilt": ["quilt", "fabric"],
    "forge": ["forge"],
    "neoforge": ["neoforge"],
}


class ModrinthError(requests.RequestException):
    """Modrinth 请求失败，消息是给用户看的简短中文（GUI 直接显示 str(e)）。"""


def loader_tags(loader: Optional[str]) -> List[str]:
    """把服务端类型（"Paper"/"Fabric"/...）映射成 Modrinth loader 标签列表；
    不认识的原样小写返回，None/空 → []。"""
    if not loader:
        return []
    key = loader.strip().lower()
    return list(_PLUGIN_LOADERS.get(key) or _MOD_LOADERS.get(key) or [key])


def is_plugin_loader(loader: Optional[str]) -> bool:
    return bool(loader) and loader.strip().lower() in _PLUGIN_LOADERS


def _get(url: str, params: dict, timeout: float):
    try:
        r = requests.get(url, params=params, headers={"User-Agent": _UA}, timeout=timeout)
    except (requests.ConnectionError, requests.Timeout) as e:
        raise ModrinthError("无法连接 Modrinth（网络不可用、超时或被代理拦截）") from e
    except requests.RequestException as e:
        raise ModrinthError(f"请求 Modrinth 失败：{e.__class__.__name__}") from e
    if r.status_code == 404:
        return r
    if r.status_code != 200:
        raise ModrinthError(f"Modrinth 返回错误 HTTP {r.status_code}")
    return r


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

    project_type="plugin" searches Modrinth plugins; the loader ("Paper" /
    "Spigot" / "Bukkit" / ...) is mapped to the plugin loaders that run on it,
    e.g. Paper → paper OR spigot OR bukkit. A plugin loader passed with the
    default project_type="mod" is treated as a plugin search too (a Paper
    server can't load mods).
    """
    if project_type == "mod" and is_plugin_loader(loader):
        project_type = "plugin"
    facets: List[List[str]] = [[f"project_type:{project_type}"]]
    if mc_version:
        facets.append([f"versions:{mc_version}"])
    tags = loader_tags(loader)
    if tags:
        facets.append([f"categories:{t}" for t in tags])

    params = {
        "query": query,
        "facets": json.dumps(facets),
        "limit": limit,
        "offset": offset,
        "index": "relevance",
    }
    r = _get(f"{_API}/v2/search", params, timeout)
    if r.status_code == 404:
        raise ModrinthError("Modrinth 搜索接口返回 HTTP 404")
    try:
        data = r.json()
    except ValueError as e:
        raise ModrinthError("Modrinth 返回了无法解析的数据") from e
    hits = [_parse_hit(h) for h in data.get("hits", []) if isinstance(h, dict)]
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
    project_type: Optional[str] = None,
) -> List[ProjectVersion]:
    """Modrinth /v2/project/{id}/version, optionally filtered.

    `loader` is the server type: "Paper" matches versions tagged paper, spigot
    or bukkit; "Fabric" only fabric; "Forge" never matches NeoForge-only
    builds. `mc_version` must match exactly. `project_type` is accepted for
    symmetry with search_mods; the loader alone decides plugin vs mod.
    """
    params: Dict[str, str] = {}
    if mc_version:
        params["game_versions"] = json.dumps([mc_version])
    tags = loader_tags(loader)
    if tags:
        params["loaders"] = json.dumps(tags)
    r = _get(f"{_API}/v2/project/{project_id}/version", params, timeout)
    if r.status_code == 404:
        raise ModrinthError("Modrinth 上找不到这个项目")
    try:
        data = r.json()
    except ValueError as e:
        raise ModrinthError("Modrinth 返回了无法解析的数据") from e
    versions = [_parse_version(v) for v in data if isinstance(v, dict)]
    # 本地再核对一遍（API 偶尔返回标签不符的版本）
    if tags:
        versions = [v for v in versions if set(tags) & set(v.loaders)]
    if mc_version:
        versions = [v for v in versions if mc_version in v.game_versions]
    return versions


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


def _safe_filename(name: str) -> str:
    """Windows 不允许的字符替换掉；只保留文件名部分，防止 ../ 之类跳出目录。"""
    name = os.path.basename(str(name).replace("\\", "/"))
    name = re.sub(r'[<>:"/\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "download.jar"


def download_to(url: str, dest_dir: str, filename: str, timeout: float = 60.0,
                sha1: Optional[str] = None) -> str:
    """Stream-download a file to dest_dir/filename. Returns full target path.

    Downloads to <name>.part first and only replaces the target once the HTTP
    status, the optional sha1 and (for .jar) the zip header check out, so a
    broken connection or an HTML error page never leaves a corrupt jar behind.
    """
    os.makedirs(dest_dir, exist_ok=True)
    target = os.path.join(dest_dir, _safe_filename(filename))
    tmp = target + ".part"
    h = hashlib.sha1()
    try:
        try:
            r = requests.get(url, stream=True,
                             headers={"User-Agent": _UA}, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise ModrinthError("下载失败：无法连接下载服务器（网络不可用或超时）") from e
        with r:
            if r.status_code != 200:
                raise ModrinthError(f"下载失败：HTTP {r.status_code}")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        h.update(chunk)
        if sha1 and h.hexdigest() != sha1.lower():
            raise ModrinthError("下载失败：文件校验不一致（sha1）")
        if target.lower().endswith(".jar") and not zipfile.is_zipfile(tmp):
            raise ModrinthError("下载失败：收到的不是有效的 jar 文件")
        os.replace(tmp, target)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
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
