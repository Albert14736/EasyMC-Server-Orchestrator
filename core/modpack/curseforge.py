"""
CurseForge `.zip` modpack provider.

Format: ZIP whose root contains manifest.json with
`"manifestType": "minecraftModpack"`. Each entry in files[] is a
(projectID, fileID) reference into CurseForge's database — to download we
must call the official CF API, which requires an x-api-key.

Resolution order for the API key (see core/config.py):
  1. env var HMSL_CURSEFORGE_API_KEY / CF_API_KEY / CURSEFORGE_API_KEY
  2. ~/.hmsl/config.json -> "curseforge_api_key"
  3. None — parsing still works; downloads in files[] get skipped + counted
     as failures, but overrides/ extraction proceeds.

Client-only detection layers (same idea as Modrinth provider):
  1. CF classID-based target path (12=resourcepacks, 6552=shaderpacks → skip)
  2. sha1 from CF file metadata → Modrinth project compat fields
  3. CF mod's slug → Modrinth slug lookup
"""
from __future__ import annotations

import json
import os
import zipfile
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import requests

from core.config import get_curseforge_api_key
from core.mod_scanner import classify_mod, lookup_mod_by_sha1
from core.server_factory import CreateServerResult, create_server

from .base import (
    ImportProgress,
    ImportResult,
    ModpackFile,
    ModpackManifest,
    ModpackProvider,
)
from .modrinth import (
    _chunked,
    _extract_overrides,
    _lookup_modrinth_project_by_slug,
)

_CF_API = "https://api.curseforge.com/v1"
_USER_AGENT = "HMSL/0.1 modpack-importer (curseforge)"
_MANIFEST_FILENAME = "manifest.json"

# CurseForge classID → server-relative subdirectory for installed file.
# These IDs are stable across the CF API and match what HMCL uses.
_CF_CLASS_PATH: Dict[int, str] = {
    6:    "mods",
    12:   "resourcepacks",
    17:   "saves",
    4546: "config",
    6552: "shaderpacks",
    6945: "datapacks",
}

# Subdirs that are 100% client-only regardless of mod metadata.
_CLIENT_ONLY_SUBDIRS = {"resourcepacks", "shaderpacks", "texturepacks"}

# manifest.minecraft.modLoaders[].id prefix → server_factory loader name.
_LOADER_PREFIX_MAP = {
    "forge":    "Forge",
    "neoforge": "NeoForge",
    "fabric":   "Fabric",   # also matches fabric-loader-X
    "quilt":    "Fabric",   # Quilt is API-compat with Fabric server jars
}


class CurseForgeProvider(ModpackProvider):
    name = "curseforge"

    # ---------- detect ----------

    def detect(self, archive_path: str) -> bool:
        if not archive_path.lower().endswith(".zip"):
            return False
        try:
            with zipfile.ZipFile(archive_path) as zf:
                if _MANIFEST_FILENAME not in zf.namelist():
                    return False
                try:
                    data = json.loads(zf.read(_MANIFEST_FILENAME).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return False
                return isinstance(data, dict) and data.get("manifestType") == "minecraftModpack"
        except (zipfile.BadZipFile, OSError):
            return False

    # ---------- parse ----------

    def parse(self, archive_path: str) -> ModpackManifest:
        with zipfile.ZipFile(archive_path) as zf:
            try:
                raw = zf.read(_MANIFEST_FILENAME)
            except KeyError as e:
                raise ValueError(f"{_MANIFEST_FILENAME} 不存在于该 zip 中") from e
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValueError(f"无法解析 {_MANIFEST_FILENAME}: {e}") from e

        mc = data.get("minecraft", {}) or {}
        mc_version = str(mc.get("version", ""))
        loader, loader_version = _pick_loader(mc.get("modLoaders", []))

        # The 'overrides' field names the override directory (default "overrides").
        # Stash it on the manifest's summary slot temporarily? No — better keep summary
        # for the human description and put the override dir on the file objects' channel.
        # We'll read it again from the zip during apply().

        files: List[ModpackFile] = []
        for f in data.get("files", []):
            if not isinstance(f, dict):
                continue
            pid = f.get("projectID")
            fid = f.get("fileID")
            if not isinstance(pid, int) or not isinstance(fid, int):
                continue
            mf = ModpackFile(
                path=f"<curseforge:{pid}/{fid}>",  # resolved during apply()
                download_urls=[],                  # filled in by CF API
            )
            # Stash CF identifiers — used by apply()
            mf._cf_project_id = pid                # type: ignore[attr-defined]
            mf._cf_file_id = fid                   # type: ignore[attr-defined]
            mf._cf_required = bool(f.get("required", True))  # type: ignore[attr-defined]
            files.append(mf)

        return ModpackManifest(
            format="curseforge",
            name=str(data.get("name", "")),
            version=str(data.get("version", "")),
            mc_version=mc_version,
            loader=loader,
            loader_version=loader_version,
            summary=str(data.get("author", "")),  # CF manifests don't have a summary
            files=files,
        )

    # ---------- apply ----------

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

        report("parsing", "正在读取 manifest.json…")
        try:
            manifest = self.parse(archive_path)
        except ValueError as e:
            return ImportResult(False, "", str(e))

        # Resolve override directory name (default "overrides")
        override_dir = _read_override_dir(archive_path)

        # Bootstrap server via existing create_server()
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

        # Check API key
        api_key = get_curseforge_api_key()
        installed = failed = skipped_client = 0
        bypassed_mods: List[dict] = []

        if api_key and manifest.files:
            # Batch fetch file + mod metadata
            file_ids = [f._cf_file_id for f in manifest.files]   # type: ignore[attr-defined]
            mod_ids  = list({f._cf_project_id for f in manifest.files})  # type: ignore[attr-defined]

            report("checking_compat",
                   f"通过 CurseForge API 查询 {len(file_ids)} 个文件元数据…",
                   current=0, total=len(file_ids))
            file_meta = _cf_batch_get_files(file_ids, api_key)
            mod_meta  = _cf_batch_get_mods(mod_ids, api_key)

            # Download each file
            report("downloading_files",
                   f"开始下载 {len(manifest.files)} 个 CurseForge 文件…",
                   current=0, total=len(manifest.files))
            for i, mf in enumerate(manifest.files, start=1):
                pid = mf._cf_project_id  # type: ignore[attr-defined]
                fid = mf._cf_file_id     # type: ignore[attr-defined]
                fi = file_meta.get(fid)
                mi = mod_meta.get(pid)

                name = fi.get("fileName", "") if fi else f"{pid}_{fid}.jar"
                report("downloading_files", name,
                       current=i, total=len(manifest.files))

                outcome, bypass_info = _install_cf_file(fi, mi, server_path)
                if outcome == "client":
                    skipped_client += 1
                elif outcome == "ok":
                    installed += 1
                    if bypass_info:
                        bypassed_mods.append(bypass_info)
                else:
                    failed += 1
        elif manifest.files:
            # No key — files[] downloads all fail. overrides will still apply.
            report("downloading_files",
                   "⚠️  未配置 CurseForge API key，files[] 部分无法下载",
                   current=0, total=len(manifest.files))
            failed = len(manifest.files)

        # Extract overrides (with mod-jar client-only classification from modrinth.py)
        report("applying_overrides", f"正在解压 {override_dir}…")
        extracted, ov_installed, ov_skipped = _extract_overrides(
            archive_path, server_path, override_dir + "/")
        installed += ov_installed
        skipped_client += ov_skipped

        report("done", "整合包导入完成")

        err = None
        if failed:
            if not api_key:
                err = (f"⚠️  {failed} 个 CurseForge 文件未下载。"
                       f"请在 ~/.hmsl/config.json 配置 curseforge_api_key 后重试。"
                       f"已解压 overrides ({ov_installed} 个 mod 安装，{ov_skipped} 跳过)。")
            else:
                err = (f"{failed} 个文件未下载——可能是 mod 作者禁用了第三方下载，"
                       f"或者网络问题。建议手动从 CurseForge 下载后丢进 mods/。")
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


# ---------- helpers ----------

def _pick_loader(mod_loaders: list) -> Tuple[str, Optional[str]]:
    """Pick the primary mod loader from manifest.minecraft.modLoaders[]."""
    if not isinstance(mod_loaders, list):
        return "Paper", None
    # primary=True wins; otherwise first entry
    sorted_loaders = sorted(mod_loaders,
                            key=lambda x: not (isinstance(x, dict) and x.get("primary", False)))
    for ml in sorted_loaders:
        if not isinstance(ml, dict):
            continue
        lid = ml.get("id", "")
        if not isinstance(lid, str):
            continue
        for prefix, name in _LOADER_PREFIX_MAP.items():
            if lid.lower().startswith(prefix + "-"):
                return name, lid[len(prefix) + 1:]
    return "Paper", None


def _read_override_dir(archive_path: str, default: str = "overrides") -> str:
    """The manifest's `overrides` field names the override directory."""
    try:
        with zipfile.ZipFile(archive_path) as zf:
            raw = zf.read(_MANIFEST_FILENAME)
        d = json.loads(raw.decode("utf-8"))
        ov = d.get("overrides")
        if isinstance(ov, str) and ov:
            return ov.strip("/")
    except Exception:
        pass
    return default


def _cf_batch_get_files(file_ids: List[int], api_key: str,
                         timeout: float = 15.0) -> Dict[int, dict]:
    """POST /v1/mods/files — returns dict[fileID -> file_dict]."""
    if not file_ids:
        return {}
    out: Dict[int, dict] = {}
    headers = {
        "x-api-key": api_key,
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }
    for chunk in _chunked(sorted(set(file_ids)), 100):
        try:
            r = requests.post(f"{_CF_API}/mods/files",
                              json={"fileIds": list(chunk)},
                              headers=headers, timeout=timeout)
            r.raise_for_status()
            for f in r.json().get("data", []) or []:
                if isinstance(f, dict) and isinstance(f.get("id"), int):
                    out[f["id"]] = f
        except (requests.RequestException, ValueError):
            continue  # best-effort: missing ids surface as "fail"
    return out


def _cf_batch_get_mods(mod_ids: List[int], api_key: str,
                        timeout: float = 15.0) -> Dict[int, dict]:
    """POST /v1/mods — returns dict[modID -> mod_dict] (for classID + slug)."""
    if not mod_ids:
        return {}
    out: Dict[int, dict] = {}
    headers = {
        "x-api-key": api_key,
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }
    for chunk in _chunked(sorted(set(mod_ids)), 100):
        try:
            r = requests.post(f"{_CF_API}/mods",
                              json={"modIds": list(chunk)},
                              headers=headers, timeout=timeout)
            r.raise_for_status()
            for m in r.json().get("data", []) or []:
                if isinstance(m, dict) and isinstance(m.get("id"), int):
                    out[m["id"]] = m
        except (requests.RequestException, ValueError):
            continue
    return out


def _install_cf_file(file_info: Optional[dict], mod_info: Optional[dict],
                     server_path: str) -> Tuple[str, Optional[dict]]:
    """
    Returns (outcome, bypass_info):
      - ("ok",       None)         downloaded from CF's own URL
      - ("ok",       bypass_dict)  downloaded via forgecdn CDN fallback
                                    (mod author opted out, but we got it anyway)
      - ("client",   None)         client-only, intentionally skipped
      - ("fail",     None)         no metadata / both URLs failed

    `bypass_dict` shape: {"name": str, "cf_url": str}. Caller appends this to
    ImportResult.bypassed_mods so the GUI can show a transparent "we bypassed
    these opt-outs, same as HMCL does" notice.

    For mod jars, runs the same Modrinth sha1+slug client-only classification
    we use elsewhere — opt-out has nothing to do with client/server side, so
    we still respect it.
    """
    if not file_info or not mod_info:
        return "fail", None

    file_id = file_info.get("id")
    file_name = file_info.get("fileName") or f"cf_{file_id or 'unknown'}.jar"
    official_url = file_info.get("downloadUrl")  # None when author opted out

    class_id = mod_info.get("classId") if isinstance(mod_info.get("classId"), int) else 6
    sub = _CF_CLASS_PATH.get(class_id, "mods")

    # Path-based skip (resource/shader packs) — applies regardless of opt-out
    if sub in _CLIENT_ONLY_SUBDIRS:
        return "client", None

    # Mod jar: cross-check Modrinth metadata for client-only classification
    if sub == "mods":
        sha1 = _extract_cf_sha1(file_info)
        info = lookup_mod_by_sha1(sha1) if sha1 else None
        if info is None:
            slug = mod_info.get("slug")
            if isinstance(slug, str) and slug:
                info = _lookup_modrinth_project_by_slug(slug)
        if classify_mod(info) == "client_only":
            return "client", None

    # Pick download URL: official → forgecdn CDN fallback for opted-out mods.
    # CF gates the public API but does NOT lock the CDN; all major launchers
    # (HMCL, PrismLauncher, MultiMC, Modrinth) construct CDN URLs here.
    bypassed_via_cdn = False
    candidates: List[str] = []
    if official_url:
        candidates.append(official_url)
    if isinstance(file_id, int):
        cdn = _forgecdn_url(file_id, file_name)
        if cdn and cdn not in candidates:
            candidates.append(cdn)
            if not official_url:
                bypassed_via_cdn = True

    target_dir = os.path.join(server_path, sub)
    os.makedirs(target_dir, exist_ok=True)
    target_path = os.path.join(target_dir, file_name)

    for url in candidates:
        try:
            r = requests.get(url, stream=True,
                             headers={"User-Agent": _USER_AGENT}, timeout=60)
            r.raise_for_status()
            with open(target_path, "wb") as out:
                for chunk in r.iter_content(chunk_size=65536):
                    out.write(chunk)
            if bypassed_via_cdn and url != official_url:
                slug = mod_info.get("slug", "")
                return "ok", {
                    "name": mod_info.get("name", file_name),
                    "cf_url": f"https://www.curseforge.com/minecraft/mc-mods/{slug}"
                              if slug else "",
                }
            return "ok", None
        except (requests.RequestException, OSError):
            try: os.remove(target_path)
            except OSError: pass
            continue
    return "fail", None


def _forgecdn_url(file_id: int, file_name: str) -> Optional[str]:
    """
    Construct the public CDN URL for a CurseForge file. Used as fallback when
    the API's downloadUrl is null (mod author opted out of third-party API).

    Pattern: /files/{file_id // 1000}/{file_id % 1000}/{file_name}
    Both the official launcher and all major third-party launchers use this.
    """
    if not file_id or not file_name:
        return None
    a, b = file_id // 1000, file_id % 1000
    return f"https://edge.forgecdn.net/files/{a}/{b}/{file_name}"


def _extract_cf_sha1(file_info: dict) -> Optional[str]:
    """CF response: hashes = [{algo: 1, value: '...'}, {algo: 2, ...}]. algo 1 = sha1."""
    for h in file_info.get("hashes", []) or []:
        if isinstance(h, dict) and h.get("algo") == 1:
            v = h.get("value")
            if isinstance(v, str) and v:
                return v.lower()
    return None
