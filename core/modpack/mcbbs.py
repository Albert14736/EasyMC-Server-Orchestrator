"""
MCBBS modpack provider (`.zip` with `mcbbs.packmeta`).

The MCBBS format was forked from HMCL's, then extended by the Chinese
community to mix HMCL-style addon files (downloaded from a `fileApi`) with
CurseForge file references (projectID + fileID, downloaded via CF API).

Files[] entries are tagged:
  - `type: "addon"`  → fileApi + path, sha1 verified
  - `type: "curse"`  → CF projectID/fileID, downloaded via the same CF
                       provider machinery (including forgecdn fallback for
                       opted-out mods)

addons[] tells us mc_version + loader, same convention as HMCL Server.
Overrides live under `overrides/`.
"""
from __future__ import annotations

import json
import os
import zipfile
from typing import Callable, Dict, List, Optional, Tuple

import requests

from core.config import get_curseforge_api_key
from core.server_factory import CreateServerResult, create_server

from .base import (
    ImportProgress,
    ImportResult,
    ModpackFile,
    ModpackManifest,
    ModpackProvider,
)
from .curseforge import _cf_batch_get_files, _cf_batch_get_mods, _install_cf_file
from .hmcl_server import _addons_to_loader, _download_and_verify
from .modrinth import _extract_overrides

_MANIFEST = "mcbbs.packmeta"


class MCBBSProvider(ModpackProvider):
    name = "mcbbs"

    def detect(self, archive_path: str) -> bool:
        if not archive_path.lower().endswith(".zip"):
            return False
        try:
            with zipfile.ZipFile(archive_path) as zf:
                if _MANIFEST not in zf.namelist():
                    return False
                try:
                    data = json.loads(zf.read(_MANIFEST).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return False
                return isinstance(data, dict) and data.get("manifestType") == "minecraftModpack"
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
            ftype = str(f.get("type", "addon")).lower()
            if ftype == "addon":
                path = f.get("path"); sha1 = f.get("hash")
                if not isinstance(path, str) or not path:
                    continue
                urls = [f"{file_api}/{path}"] if file_api else []
                mf = ModpackFile(path=path,
                                  sha1=sha1 if isinstance(sha1, str) else None,
                                  download_urls=urls)
                mf._mcbbs_kind = "addon"  # type: ignore[attr-defined]
                files.append(mf)
            elif ftype == "curse":
                pid = f.get("projectID"); fid = f.get("fileID")
                if not isinstance(pid, int) or not isinstance(fid, int):
                    continue
                mf = ModpackFile(path=f"<mcbbs-curse:{pid}/{fid}>", download_urls=[])
                mf._mcbbs_kind = "curse"  # type: ignore[attr-defined]
                mf._cf_project_id = pid  # type: ignore[attr-defined]
                mf._cf_file_id = fid     # type: ignore[attr-defined]
                files.append(mf)

        return ModpackManifest(
            format="mcbbs",
            name=str(data.get("name", "MCBBS Modpack")),
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

        report("parsing", "正在读取 mcbbs.packmeta…")
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

        # Split files by kind
        addon_files = [f for f in manifest.files
                        if getattr(f, "_mcbbs_kind", "addon") == "addon"]
        curse_files = [f for f in manifest.files
                        if getattr(f, "_mcbbs_kind", "addon") == "curse"]

        installed = failed = skipped_client = 0
        bypassed_mods: List[dict] = []

        # ----- addon files (fileApi + sha1 verify) -----
        if addon_files:
            report("downloading_files",
                   f"开始下载 {len(addon_files)} 个 addon 文件…",
                   current=0, total=len(addon_files))
            for i, f in enumerate(addon_files, start=1):
                report("downloading_files", f.path, current=i, total=len(addon_files))
                if not f.download_urls:
                    failed += 1
                    continue
                if _download_and_verify(f.download_urls[0], server_path, f.path, f.sha1):
                    installed += 1
                else:
                    failed += 1

        # ----- curse files (need CF API key; reuse curseforge.py machinery) -----
        if curse_files:
            api_key = get_curseforge_api_key()
            if not api_key:
                # No CF key — fail the curse portion, surface to user
                failed += len(curse_files)
            else:
                file_ids = [f._cf_file_id for f in curse_files]   # type: ignore[attr-defined]
                mod_ids  = list({f._cf_project_id for f in curse_files})  # type: ignore[attr-defined]
                report("checking_compat",
                       f"通过 CurseForge API 查询 {len(file_ids)} 个文件元数据…",
                       current=0, total=len(file_ids))
                file_meta = _cf_batch_get_files(file_ids, api_key)
                mod_meta  = _cf_batch_get_mods(mod_ids, api_key)

                report("downloading_files",
                       f"开始下载 {len(curse_files)} 个 CurseForge 文件…",
                       current=0, total=len(curse_files))
                for i, mf in enumerate(curse_files, start=1):
                    pid = mf._cf_project_id  # type: ignore[attr-defined]
                    fid = mf._cf_file_id     # type: ignore[attr-defined]
                    fi = file_meta.get(fid); mi = mod_meta.get(pid)
                    name = fi.get("fileName", "") if fi else f"{pid}_{fid}.jar"
                    report("downloading_files", name,
                           current=i, total=len(curse_files))
                    outcome, bypass_info = _install_cf_file(fi, mi, server_path)
                    if outcome == "client":     skipped_client += 1
                    elif outcome == "ok":
                        installed += 1
                        if bypass_info: bypassed_mods.append(bypass_info)
                    else:                       failed += 1

        report("applying_overrides", "正在解压 overrides…")
        _extracted, ov_installed, ov_skipped = _extract_overrides(
            archive_path, server_path, "overrides/")
        installed += ov_installed
        skipped_client += ov_skipped

        report("done", "整合包导入完成")

        err = None
        if failed:
            if not get_curseforge_api_key() and curse_files:
                err = (f"⚠️  {failed} 个文件未下载。若 mcbbs 包含 CurseForge 模组，"
                       f"请在 ~/.hmsl/config.json 配置 curseforge_api_key 后重试。")
            else:
                err = f"{failed} 个文件未下载（网络/权限问题或作者完全锁死下载）"

        return ImportResult(
            success=(failed == 0),
            server_path=server_path,
            error=err,
            manifest=manifest,
            files_installed=installed,
            files_skipped_client=skipped_client,
            files_failed=failed,
            bypassed_mods=bypassed_mods,
        )
