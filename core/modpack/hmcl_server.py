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

import urllib.parse
import zipfile
from typing import Callable, List, Optional, Tuple

import requests

from .base import (
    ImportProgress,
    ImportResult,
    ModpackFile,
    ModpackManifest,
    ModpackProvider,
    archive_fingerprint,
    create_server_for_pack,
    find_zip_entry,
    open_zip,
    read_zip_json,
    safe_target,
    summarize_problems,
    without_launch_files,
    write_error_reason,
)
from .modrinth import _ModrinthSides, _download_to, _extract_overrides

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

_QUILT_WARNING = ("该整合包使用 Quilt 加载器，HMSL 会用 Fabric 服务端运行它；"
                  "只支持 Quilt 的模组可能无法加载。")


class HMCLServerProvider(ModpackProvider):
    name = "hmcl_server"

    def detect(self, archive_path: str) -> bool:
        if not archive_path.lower().endswith(".zip"):
            return False
        try:
            with zipfile.ZipFile(archive_path) as zf:
                return find_zip_entry(zf, _MANIFEST) is not None
        except (zipfile.BadZipFile, OSError):
            return False

    def parse(self, archive_path: str) -> ModpackManifest:
        with open_zip(archive_path) as zf:
            data = read_zip_json(zf, _MANIFEST)

        mc_version, loader, loader_version, is_quilt = _addons_to_loader(data.get("addons", []))
        warnings: List[str] = []
        if is_quilt:
            loader_version = None
            warnings.append(_QUILT_WARNING)
        file_api = data.get("fileApi")
        file_api = file_api.strip().rstrip("/") if isinstance(file_api, str) else ""

        files: List[ModpackFile] = []
        raw_files = data.get("files")
        for f in raw_files if isinstance(raw_files, list) else []:
            if not isinstance(f, dict):
                continue
            path = f.get("path")
            sha1 = f.get("hash")
            if not isinstance(path, str) or not path:
                continue
            urls: List[str] = []
            if file_api:
                urls.append(_file_api_url(file_api, path))
            files.append(ModpackFile(
                path=path,
                sha1=sha1 if isinstance(sha1, str) and sha1 else None,
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
            warnings=warnings,
            source=archive_fingerprint(archive_path),
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
        *,
        manifest: Optional[ModpackManifest] = None,
    ) -> ImportResult:
        def report(stage, msg, current=0, total=0):
            if progress_callback:
                progress_callback(ImportProgress(stage=stage, message=msg,
                                                 current=current, total=total))

        report("parsing", "正在读取 server-manifest.json…")
        try:
            manifest = self.prepared_manifest(archive_path, manifest)
        except ValueError as e:
            return ImportResult(False, "", str(e))
        warnings: List[str] = list(manifest.warnings)

        report("creating_server", f"正在创建 {manifest.loader} {manifest.mc_version} 服务端…")
        cr = create_server_for_pack(manifest, server_name, parent_dir, env_manager,
                                    installer, downloader, warnings, report)
        if not cr.success:
            return ImportResult(False, cr.server_path or "",
                                f"创建服务端失败：{cr.error}", manifest=manifest,
                                warnings=warnings)
        server_path = cr.server_path

        installed = 0
        problems: List[Tuple[str, str]] = []
        files = without_launch_files(manifest.files, warnings)   # never over HMSL's start script
        report("downloading_files",
               f"开始下载 {len(files)} 个文件…",
               current=0, total=len(files))
        for i, f in enumerate(files, start=1):
            report("downloading_files", f.path, current=i, total=len(files))
            if not f.download_urls:
                problems.append((f.path, "整合包没有提供下载地址 (fileApi)"))
                continue
            reason = _download_and_verify_reason(f.download_urls[0], server_path, f.path, f.sha1)
            if reason is None:
                installed += 1
            else:
                problems.append((f.path, reason))
        if problems:
            warnings.append(summarize_problems(
                f"{len(problems)} 个文件下载失败，可稍后手动放进服务器目录", problems))

        report("applying_overrides", "正在解压 overrides…")
        sides = _ModrinthSides()
        st = _extract_overrides(archive_path, server_path, "overrides/", warnings=warnings,
                                progress_callback=progress_callback, sides=sides)
        installed += st.mods_installed
        if sides.offline:
            warnings.append("无法连接 Modrinth，未能检查整合包自带的模组是否为纯客户端模组；"
                            "如果服务器启动报错，可在「模组扫描」里再检查一次。")

        report("done", "整合包导入完成")
        return ImportResult(
            success=True,
            server_path=server_path,
            error=None,
            manifest=manifest,
            files_installed=installed,
            files_skipped_client=st.mods_skipped + st.client_skipped,
            files_failed=len(problems) + st.failed,
            warnings=warnings,
        )


# ---------- helpers ----------

def _file_api_url(file_api: str, path: str) -> str:
    """{fileApi}/{path} with the path percent-encoded ('#', '?', '%', spaces, 中文 …)."""
    rel = path.replace("\\", "/").lstrip("/")
    return f"{file_api}/{urllib.parse.quote(rel, safe='/')}"


def _addons_to_loader(addons: list) -> Tuple[str, str, Optional[str], bool]:
    """addons[] → (mc_version, loader, loader_version, is_quilt)."""
    mc_version = ""; loader = "Paper"; loader_version = None; is_quilt = False
    if not isinstance(addons, list):
        return mc_version, loader, loader_version, is_quilt
    for a in addons:
        if not isinstance(a, dict): continue
        aid = str(a.get("id", "") or "").lower(); ver = a.get("version", "")
        if aid in _ADDON_MINECRAFT_IDS:
            mc_version = str(ver or "")
        elif aid in _ADDON_LOADER_MAP:
            loader = _ADDON_LOADER_MAP[aid]
            loader_version = str(ver) if ver else None
            is_quilt = aid == "quilt"
    return mc_version, loader, loader_version, is_quilt


def _download_and_verify_reason(url: str, server_root: str, rel_path: str,
                                expected_sha1: Optional[str]) -> Optional[str]:
    """
    Stream-download to server_root/rel_path, sha1-verify if hash known.
    Returns None on success, else a short Chinese reason. Goes through a temp
    file, so a failed download never deletes a file that was already there.
    """
    target, reason = safe_target(server_root, rel_path)
    if target is None:
        return reason
    try:
        _download_to(url, target, _USER_AGENT, {"sha1": expected_sha1})
        return None
    except requests.RequestException as e:   # (subclass of OSError — keep first)
        return f"下载失败：{e}"
    except OSError as e:
        return write_error_reason(target, e)
    except Exception as e:                   # hash mismatch
        return str(e)


def _download_and_verify(url: str, server_root: str, rel_path: str,
                          expected_sha1: Optional[str]) -> bool:
    """Stream-download to server_root/rel_path, sha1-verify if hash known."""
    return _download_and_verify_reason(url, server_root, rel_path, expected_sha1) is None
