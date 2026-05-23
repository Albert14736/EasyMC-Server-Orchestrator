"""
HMCL Server Modpack provider (`.zip` with `server-manifest.json`).

This is HMCL's dedicated server-side modpack format — exactly what HMSL is
about. The manifest lists files by (path, hash) pairs plus a `fileApi`
base URL where they live. We:

  1. parse server-manifest.json (no API key needed)
  2. derive mc_version + loader from addons[]
  3. download each file from {fileApi}/{path}, sha1-verify against manifest
  4. extract overrides/

Self-contained-ish: there's no third-party download gating; the fileApi
URL is set by the pack author and points at their own CDN.
"""
from __future__ import annotations

import hashlib
import json
import os
import zipfile
from typing import Callable, List, Optional, Tuple

import requests

from core.server_factory import CreateServerResult, create_server

from .base import (
    ImportProgress,
    ImportResult,
    ModpackFile,
    ModpackManifest,
    ModpackProvider,
)
from .modrinth import _extract_overrides

_USER_AGENT = "HMSL/0.1 modpack-importer (hmcl-server)"
_MANIFEST = "server-manifest.json"

# addons[].id → server_factory loader name
_ADDON_LOADER_MAP = {
    "forge":    "Forge",
    "neoforge": "NeoForge",
    "fabric":   "Fabric",
    "quilt":    "Fabric",  # Quilt → Fabric server jar
}
# Minecraft addon id: HMCL source says "minecraft", real-world MCBBS packs
# use "game". Accept both.
_ADDON_MINECRAFT_IDS = {"minecraft", "game"}


class HMCLServerProvider(ModpackProvider):
    name = "hmcl_server"

    def detect(self, archive_path: str) -> bool:
        if not archive_path.lower().endswith(".zip"):
            return False
        try:
            with zipfile.ZipFile(archive_path) as zf:
                return _MANIFEST in zf.namelist()
        except (zipfile.BadZipFile, OSError):
            return False

    def parse(self, archive_path: str) -> ModpackManifest:
        with zipfile.ZipFile(archive_path) as zf:
            try:
                data = json.loads(zf.read(_MANIFEST).decode("utf-8"))
            except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as e:
                raise ValueError(f"无法解析 {_MANIFEST}: {e}") from e

        mc_version, loader, loader_version = _addons_to_loader(data.get("addons", []))
        file_api = (data.get("fileApi") or "").rstrip("/")

        files: List[ModpackFile] = []
        for f in data.get("files", []) or []:
            if not isinstance(f, dict):
                continue
            path = f.get("path")
            sha1 = f.get("hash")
            if not isinstance(path, str) or not path:
                continue
            urls: List[str] = []
            if file_api:
                urls.append(f"{file_api}/{path}")
            files.append(ModpackFile(
                path=path,
                sha1=sha1 if isinstance(sha1, str) else None,
                download_urls=urls,
            ))

        return ModpackManifest(
            format="hmcl_server",
            name=str(data.get("name", "HMCL Server Modpack")),
            version=str(data.get("version", "")),
            mc_version=mc_version,
            loader=loader,
            loader_version=loader_version,
            summary=str(data.get("description", data.get("author", ""))),
            files=files,
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

        report("parsing", "正在读取 server-manifest.json…")
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

        installed = failed = 0
        report("downloading_files",
               f"开始下载 {len(manifest.files)} 个文件…",
               current=0, total=len(manifest.files))
        for i, f in enumerate(manifest.files, start=1):
            report("downloading_files", f.path, current=i, total=len(manifest.files))
            if not f.download_urls:
                failed += 1
                continue
            if _download_and_verify(f.download_urls[0], server_path, f.path, f.sha1):
                installed += 1
            else:
                failed += 1

        report("applying_overrides", "正在解压 overrides…")
        _extracted, ov_installed, ov_skipped = _extract_overrides(
            archive_path, server_path, "overrides/")
        installed += ov_installed

        report("done", "整合包导入完成")
        return ImportResult(
            success=(failed == 0),
            server_path=server_path,
            error=None if failed == 0 else f"{failed} 个文件下载失败",
            manifest=manifest,
            files_installed=installed,
            files_skipped_client=ov_skipped,
            files_failed=failed,
        )


# ---------- helpers ----------

def _addons_to_loader(addons: list) -> Tuple[str, str, Optional[str]]:
    mc_version = ""; loader = "Paper"; loader_version = None
    if not isinstance(addons, list):
        return mc_version, loader, loader_version
    for a in addons:
        if not isinstance(a, dict): continue
        aid = a.get("id", ""); ver = a.get("version", "")
        if aid in _ADDON_MINECRAFT_IDS:
            mc_version = str(ver)
        elif aid in _ADDON_LOADER_MAP:
            loader = _ADDON_LOADER_MAP[aid]
            loader_version = str(ver) if ver else None
    return mc_version, loader, loader_version


def _download_and_verify(url: str, server_root: str, rel_path: str,
                          expected_sha1: Optional[str]) -> bool:
    """Stream-download to server_root/rel_path, sha1-verify if hash known."""
    target = os.path.abspath(os.path.join(server_root, rel_path))
    if not target.startswith(os.path.abspath(server_root) + os.sep):
        return False  # zip-slip defense
    os.makedirs(os.path.dirname(target), exist_ok=True)
    try:
        r = requests.get(url, stream=True,
                         headers={"User-Agent": _USER_AGENT}, timeout=60)
        r.raise_for_status()
        h = hashlib.sha1()
        with open(target, "wb") as out:
            for chunk in r.iter_content(chunk_size=65536):
                out.write(chunk)
                h.update(chunk)
        if expected_sha1 and h.hexdigest().lower() != expected_sha1.lower():
            try: os.remove(target)
            except OSError: pass
            return False
        return True
    except (requests.RequestException, OSError):
        try: os.remove(target)
        except OSError: pass
        return False
