"""
MCBBS modpack provider (`.zip` with `mcbbs.packmeta`).

The MCBBS format was forked from HMCL's, then extended by the Chinese
community to mix HMCL-style addon files (downloaded from a `fileApi`) with
CurseForge file references (projectID + fileID, downloaded via CF API).

Files[] entries are tagged:
  - `type: "addon"`  → fileApi + path, sha1 verified
  - `type: "curse"`  → CF projectID/fileID, downloaded via the same CF
                       provider machinery (including forgecdn fallback for
                       opted-out mods). Entries usually also carry fileName
                       and url, so — like HMCL — they can be downloaded
                       without a CurseForge API key too.

addons[] tells us mc_version + loader, same convention as HMCL Server.
Overrides live under `overrides/`.
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import zipfile
from typing import Callable, Dict, List, Optional, Tuple

import requests

from core.config import get_curseforge_api_key
from core.mod_scanner import classify_mod

from .base import (
    ImportProgress,
    ImportResult,
    ModpackFile,
    ModpackManifest,
    ModpackProvider,
    STAGED_SUFFIX,
    archive_fingerprint,
    create_server_for_pack,
    discard_temp,
    find_zip_entry,
    open_zip,
    read_zip_json,
    safe_target,
    summarize_problems,
    temp_path_for,
    write_error_reason,
)
from .curseforge import (
    _cf_batch_get_files,
    _cf_batch_get_mods,
    _cf_compat_lookup,
    _config_path_hint,
    _forgecdn_url,
    _install_cf_file,
)
from .hmcl_server import (
    _QUILT_WARNING,
    _addons_to_loader,
    _download_and_verify_reason,
    _file_api_url,
)
from .modrinth import _ModrinthSides, _download_to, _extract_overrides

_MANIFEST = "mcbbs.packmeta"
_USER_AGENT = "HMSL/0.1 modpack-importer (mcbbs)"


class MCBBSProvider(ModpackProvider):
    name = "mcbbs"

    def detect(self, archive_path: str) -> bool:
        # mcbbs.packmeta only exists in MCBBS packs. Claim the archive even if
        # the packmeta turns out to be broken, so parse() can explain what's
        # wrong instead of the pack falling through to the CurseForge
        # provider (MCBBS packs also ship a CF-style manifest.json).
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
            ftype = str(f.get("type", "addon")).lower()
            if ftype == "addon":
                path = f.get("path"); sha1 = f.get("hash")
                if not isinstance(path, str) or not path:
                    continue
                urls = [_file_api_url(file_api, path)] if file_api else []
                mf = ModpackFile(path=path,
                                  sha1=sha1 if isinstance(sha1, str) and sha1 else None,
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
                fname = f.get("fileName")
                mf._cf_file_name = fname if isinstance(fname, str) else ""  # type: ignore[attr-defined]
                url = f.get("url")
                mf._cf_url = url if isinstance(url, str) and url.lower().startswith(("http://", "https://")) else ""  # type: ignore[attr-defined]
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

        report("parsing", "正在读取 mcbbs.packmeta…")
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

        # Split files by kind
        addon_files = [f for f in manifest.files
                        if getattr(f, "_mcbbs_kind", "addon") == "addon"]
        curse_files = [f for f in manifest.files
                        if getattr(f, "_mcbbs_kind", "addon") == "curse"]

        installed = skipped_client = 0
        problems: List[Tuple[str, str]] = []
        bypassed_mods: List[dict] = []
        sides = _ModrinthSides()

        # ----- addon files (fileApi + sha1 verify) -----
        if addon_files:
            report("downloading_files",
                   f"开始下载 {len(addon_files)} 个 addon 文件…",
                   current=0, total=len(addon_files))
            for i, f in enumerate(addon_files, start=1):
                report("downloading_files", f.path, current=i, total=len(addon_files))
                if not f.download_urls:
                    problems.append((f.path, "整合包没有提供下载地址 (fileApi)"))
                    continue
                reason = _download_and_verify_reason(f.download_urls[0], server_path, f.path, f.sha1)
                if reason is None:
                    installed += 1
                else:
                    problems.append((f.path, reason))

        # ----- curse files (CF API if a key is set; else the entry's url/fileName) -----
        no_key_failed = 0
        if curse_files:
            api_key = get_curseforge_api_key()
            file_meta: Dict[int, dict] = {}
            mod_meta: Dict[int, dict] = {}
            compat: Dict[int, object] = {}
            if api_key:
                file_ids = [f._cf_file_id for f in curse_files]   # type: ignore[attr-defined]
                mod_ids  = list({f._cf_project_id for f in curse_files})  # type: ignore[attr-defined]
                report("checking_compat",
                       f"通过 CurseForge API 查询 {len(file_ids)} 个文件元数据…",
                       current=0, total=len(file_ids))
                file_meta = _cf_batch_get_files(file_ids, api_key)
                mod_meta  = _cf_batch_get_mods(mod_ids, api_key)
                compat = _cf_compat_lookup(file_meta, mod_meta, sides)

            report("downloading_files",
                   f"开始下载 {len(curse_files)} 个 CurseForge 文件…",
                   current=0, total=len(curse_files))
            direct: List[Tuple[str, str]] = []   # (display name, target) staged without the API
            for i, mf in enumerate(curse_files, start=1):
                pid = mf._cf_project_id  # type: ignore[attr-defined]
                fid = mf._cf_file_id     # type: ignore[attr-defined]
                fi = file_meta.get(fid); mi = mod_meta.get(pid)
                name = (fi or {}).get("fileName") or getattr(mf, "_cf_file_name", "") or f"{pid}_{fid}.jar"
                report("downloading_files", name,
                       current=i, total=len(curse_files))
                if fi and mi:
                    outcome, bypass_info, detail = _install_cf_file(fi, mi, server_path,
                                                                    compat.get(fid))  # type: ignore[arg-type]
                    if outcome == "client":     skipped_client += 1
                    elif outcome == "ok":
                        installed += 1
                        if bypass_info: bypassed_mods.append(bypass_info)
                    else:                       problems.append((name, detail or "下载失败"))
                    continue
                target, detail = _install_curse_direct(mf, server_path)
                if target:
                    direct.append((name, target))
                elif detail is None:            # nothing to download from without the API
                    if api_key:
                        problems.append((name, "CurseForge API 没有返回该文件的信息"))
                    else:
                        no_key_failed += 1
                else:
                    problems.append((name, detail))
            # Directly-downloaded jars never went through the client-only check:
            # hash the staged files, ask Modrinth (one batched request), then
            # move the server-side ones into mods/.
            kept, dropped, move_problems = _finish_direct(direct, sides)
            installed += kept
            skipped_client += dropped
            problems.extend(move_problems)

        if problems:
            warnings.append(summarize_problems(
                f"{len(problems)} 个文件未下载（网络/权限问题或作者完全锁死下载，"
                f"可手动下载后放进服务器目录）", problems))
        if no_key_failed:
            warnings.append(
                f"{no_key_failed} 个 CurseForge 文件未下载：整合包没有给出下载地址，"
                f"需要 CurseForge API key。请在 {_config_path_hint()} 里填写 "
                f"curseforge_api_key 后重新导入。")

        report("applying_overrides", "正在解压 overrides…")
        st = _extract_overrides(archive_path, server_path, "overrides/", warnings=warnings,
                                progress_callback=progress_callback, sides=sides)
        installed += st.mods_installed
        skipped_client += st.mods_skipped + st.client_skipped
        if sides.offline:
            warnings.append("无法连接 Modrinth，未能检查模组是否为纯客户端模组；"
                            "如果服务器启动报错，可在「模组扫描」里再检查一次。")

        report("done", "整合包导入完成")
        return ImportResult(
            success=True,
            server_path=server_path,
            error=None,
            manifest=manifest,
            files_installed=installed,
            files_skipped_client=skipped_client,
            files_failed=len(problems) + no_key_failed + st.failed,
            bypassed_mods=bypassed_mods,
            warnings=warnings,
        )


# ---------- helpers ----------

def _staged_path(target: str) -> str:
    """Where a direct download waits (beside its target) for the client-only check."""
    return temp_path_for(target, STAGED_SUFFIX)


def _install_curse_direct(mf: ModpackFile, server_path: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Download an MCBBS 'curse' entry without the CurseForge API, from the
    entry's own url or the public forgecdn URL built from fileID + fileName
    (what HMCL does). The jar is only STAGED at _staged_path(target);
    _finish_direct() moves it into place. Returns (target_path, None) on
    success, (None, reason) on failure, or (None, None) when there is
    nothing to try.
    """
    fid = getattr(mf, "_cf_file_id", None)
    raw_name = getattr(mf, "_cf_file_name", "") or ""
    file_name = posixpath.basename(raw_name.replace("\\", "/")).strip()
    url = getattr(mf, "_cf_url", "") or ""
    if not file_name and url:
        from urllib.parse import unquote, urlsplit
        file_name = posixpath.basename(unquote(urlsplit(url).path))
    if file_name in ("", ".", ".."):
        return None, None
    candidates: List[str] = []
    if url:
        candidates.append(url)
    if isinstance(fid, int):
        cdn = _forgecdn_url(fid, file_name)
        if cdn and cdn not in candidates:
            candidates.append(cdn)
    if not candidates:
        return None, None
    target, reason = safe_target(os.path.join(server_path, "mods"), file_name)
    if target is None:
        return None, reason
    last = "下载失败"
    for u in candidates:
        try:
            _download_to(u, _staged_path(target), _USER_AGENT, {})
            return target, None
        except requests.RequestException as e:   # (subclass of OSError — keep first)
            last = f"下载失败：{e}"
        except OSError as e:
            return None, write_error_reason(target, e)
        except Exception as e:
            last = str(e)
    return None, last


def _finish_direct(staged: List[Tuple[str, str]], sides: _ModrinthSides,
                   ) -> Tuple[int, int, List[Tuple[str, str]]]:
    """
    Move the jars _install_curse_direct() staged into place — except the ones
    Modrinth says are client-only, whose staged copy is just discarded, so a
    same-named jar that was already in mods/ is never deleted.
    Returns (installed, skipped_client, [(name, reason) for jars that could not be moved]).
    """
    name_of: Dict[str, str] = {}
    for name, target in staged:
        name_of[target] = name          # the same fileName twice is one file
    sha1_of: Dict[str, str] = {}
    for target in name_of:
        try:
            h = hashlib.sha1()
            with open(_staged_path(target), "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            sha1_of[target] = h.hexdigest()
        except OSError:
            continue
    infos = sides.by_sha1(sha1_of.values()) if sha1_of else {}
    kept = dropped = 0
    problems: List[Tuple[str, str]] = []
    for target, name in name_of.items():
        tmp = _staged_path(target)
        s = sha1_of.get(target)
        if s is not None and classify_mod(infos.get(s)) == "client_only":
            discard_temp(tmp)
            dropped += 1
            continue
        try:
            os.replace(tmp, target)
            kept += 1
        except OSError as e:
            discard_temp(tmp)
            problems.append((name, write_error_reason(target, e)))
    return kept, dropped, problems
