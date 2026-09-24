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

import os
import posixpath
import urllib.parse
import zipfile
from typing import Callable, Dict, List, Optional, Tuple

import requests

from core.config import get_curseforge_api_key
from core.mod_scanner import ModInfo, classify_mod

from .base import (
    ImportProgress,
    ImportResult,
    ModpackFile,
    ModpackManifest,
    ModpackProvider,
    archive_fingerprint,
    create_server_for_pack,
    find_zip_entry,
    load_json_object,
    open_zip,
    read_zip_json,
    safe_target,
    summarize_problems,
    write_error_reason,
)
from .modrinth import (
    _ModrinthSides,
    _chunked,
    _download_to,
    _extract_overrides,
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

# CurseForge classID → website section (for the "support the author" link).
_CF_CLASS_WEB: Dict[int, str] = {
    6:    "mc-mods",
    12:   "texture-packs",
    17:   "worlds",
    4471: "modpacks",
    4546: "customization",
    6552: "shaders",
    6945: "data-packs",
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

_QUILT_WARNING = ("该整合包使用 Quilt 加载器，HMSL 会用 Fabric 服务端运行它；"
                  "只支持 Quilt 的模组可能无法加载。")


def _config_path_hint() -> str:
    try:
        from core.config import default_config_path
        return default_config_path()
    except Exception:
        return "~/.hmsl/config.json"


def _no_key_warning(count: int) -> str:
    return (f"{count} 个 CurseForge 文件未下载：没有配置 CurseForge API key。"
            f"请在 {_config_path_hint()} 里填写 curseforge_api_key 后重新导入，"
            f"或手动下载这些模组放进 mods 文件夹。overrides 已正常解压。")


class CurseForgeProvider(ModpackProvider):
    name = "curseforge"

    # ---------- detect ----------

    def detect(self, archive_path: str) -> bool:
        if not archive_path.lower().endswith(".zip"):
            return False
        try:
            with zipfile.ZipFile(archive_path) as zf:
                info = find_zip_entry(zf, _MANIFEST_FILENAME)
                if info is None:
                    return False
                try:
                    data = load_json_object(zf.read(info), _MANIFEST_FILENAME)
                except ValueError:
                    return False
                return data.get("manifestType") == "minecraftModpack"
        except (zipfile.BadZipFile, OSError, RuntimeError):
            return False

    # ---------- parse ----------

    def parse(self, archive_path: str) -> ModpackManifest:
        with open_zip(archive_path) as zf:
            data = read_zip_json(zf, _MANIFEST_FILENAME)

        mc = data.get("minecraft")
        mc = mc if isinstance(mc, dict) else {}
        mc_version = str(mc.get("version", "") or "")
        loader, loader_version, is_quilt = _pick_loader(mc.get("modLoaders", []))
        warnings: List[str] = []
        if is_quilt:
            loader_version = None
            warnings.append(_QUILT_WARNING)

        # The 'overrides' field names the override directory; apply() reads it
        # again from the zip via _read_override_dir().

        files: List[ModpackFile] = []
        seen_ids = set()
        raw_files = data.get("files")
        for f in raw_files if isinstance(raw_files, list) else []:
            if not isinstance(f, dict):
                continue
            pid = f.get("projectID")
            fid = f.get("fileID")
            if not isinstance(pid, int) or not isinstance(fid, int):
                continue
            if fid in seen_ids:   # same file listed twice → install once
                continue
            seen_ids.add(fid)
            mf = ModpackFile(
                path=f"<curseforge:{pid}/{fid}>",  # resolved during apply()
                download_urls=[],                  # filled in by CF API
            )
            # Stash CF identifiers — used by apply()
            mf._cf_project_id = pid                # type: ignore[attr-defined]
            mf._cf_file_id = fid                   # type: ignore[attr-defined]
            mf._cf_required = f.get("required", True) is not False  # type: ignore[attr-defined]
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
            warnings=warnings,
            source=archive_fingerprint(archive_path),
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
        *,
        manifest: Optional[ModpackManifest] = None,
    ) -> ImportResult:
        def report(stage, msg, current=0, total=0):
            if progress_callback:
                progress_callback(ImportProgress(stage=stage, message=msg,
                                                 current=current, total=total))

        report("parsing", "正在读取 manifest.json…")
        try:
            manifest = self.prepared_manifest(archive_path, manifest)
        except ValueError as e:
            return ImportResult(False, "", str(e))
        warnings: List[str] = list(manifest.warnings)

        # Resolve override directory name (default "overrides")
        override_dir = _read_override_dir(archive_path)

        # Bootstrap server via existing create_server()
        report("creating_server", f"正在创建 {manifest.loader} {manifest.mc_version} 服务端…")
        cr = create_server_for_pack(manifest, server_name, parent_dir, env_manager,
                                    installer, downloader, warnings, report)
        if not cr.success:
            return ImportResult(False, cr.server_path or "",
                                f"创建服务端失败：{cr.error}", manifest=manifest,
                                warnings=warnings)
        server_path = cr.server_path

        # Files the pack marks required=false are disabled/optional in the
        # CurseForge profile — the CF app installs them disabled; a server
        # doesn't need them.
        wanted = [f for f in manifest.files if getattr(f, "_cf_required", True)]
        optional = len(manifest.files) - len(wanted)
        if optional:
            warnings.append(f"{optional} 个 CurseForge 文件在整合包里标记为可选/已禁用，未安装。")

        # Check API key
        api_key = get_curseforge_api_key()
        installed = failed = skipped_client = 0
        bypassed_mods: List[dict] = []
        sides = _ModrinthSides()

        if api_key and wanted:
            # Batch fetch file + mod metadata
            file_ids = [f._cf_file_id for f in wanted]   # type: ignore[attr-defined]
            mod_ids  = list({f._cf_project_id for f in wanted})  # type: ignore[attr-defined]

            report("checking_compat",
                   f"通过 CurseForge API 查询 {len(file_ids)} 个文件元数据…",
                   current=0, total=len(file_ids))
            file_meta = _cf_batch_get_files(file_ids, api_key)
            mod_meta  = _cf_batch_get_mods(mod_ids, api_key)
            compat = _cf_compat_lookup(file_meta, mod_meta, sides)

            # Download each file
            report("downloading_files",
                   f"开始下载 {len(wanted)} 个 CurseForge 文件…",
                   current=0, total=len(wanted))
            problems: List[Tuple[str, str]] = []
            for i, mf in enumerate(wanted, start=1):
                pid = mf._cf_project_id  # type: ignore[attr-defined]
                fid = mf._cf_file_id     # type: ignore[attr-defined]
                fi = file_meta.get(fid)
                mi = mod_meta.get(pid)

                name = _display_name(fi, mi, pid, fid)
                report("downloading_files", name,
                       current=i, total=len(wanted))

                outcome, bypass_info, detail = _install_cf_file(fi, mi, server_path,
                                                                compat.get(fid))
                if outcome == "client":
                    skipped_client += 1
                elif outcome == "ok":
                    installed += 1
                    if bypass_info:
                        bypassed_mods.append(bypass_info)
                else:
                    failed += 1
                    problems.append((name, detail or "下载失败"))
            if problems:
                warnings.append(summarize_problems(
                    f"{len(problems)} 个 CurseForge 文件未下载（可能是作者禁止第三方下载或网络问题，"
                    f"可手动从 CurseForge 下载后放进服务器目录）", problems))
        elif wanted:
            # No key — files[] downloads all fail. overrides will still apply.
            report("downloading_files",
                   "⚠️  未配置 CurseForge API key，files[] 部分无法下载",
                   current=0, total=len(wanted))
            failed = len(wanted)
            warnings.append(_no_key_warning(failed))

        # Extract overrides (with mod-jar client-only classification from modrinth.py)
        report("applying_overrides", f"正在解压 {override_dir}…")
        st = _extract_overrides(archive_path, server_path, override_dir + "/",
                                warnings=warnings, progress_callback=progress_callback,
                                sides=sides)
        installed += st.mods_installed
        skipped_client += st.mods_skipped + st.client_skipped
        failed += st.failed
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
            files_failed=failed,
            bypassed_mods=bypassed_mods,
            warnings=warnings,
        )


# ---------- helpers ----------

def _pick_loader(mod_loaders: list) -> Tuple[str, Optional[str], bool]:
    """Pick the primary mod loader from manifest.minecraft.modLoaders[] → (loader, version, is_quilt)."""
    if not isinstance(mod_loaders, list):
        return "Paper", None, False
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
                return name, lid[len(prefix) + 1:], prefix == "quilt"
    return "Paper", None, False


def _read_override_dir(archive_path: str, default: str = "overrides") -> str:
    """The manifest's `overrides` field names the override directory ("./overrides" works too)."""
    try:
        with zipfile.ZipFile(archive_path) as zf:
            d = read_zip_json(zf, _MANIFEST_FILENAME)
        ov = d.get("overrides")
        if isinstance(ov, str) and ov.strip():
            norm = posixpath.normpath(ov.strip().replace("\\", "/")).strip("/")
            if norm and norm != "." and not norm.startswith(".."):
                return norm
    except Exception:
        pass
    return default


def _display_name(fi: Optional[dict], mi: Optional[dict], pid: int, fid: int) -> str:
    if fi and isinstance(fi.get("fileName"), str) and fi["fileName"]:
        return fi["fileName"]
    if mi and isinstance(mi.get("name"), str) and mi["name"]:
        return mi["name"]
    return f"{pid}_{fid}.jar"


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
            data = r.json()
            for f in (data.get("data", []) if isinstance(data, dict) else []) or []:
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
            data = r.json()
            for m in (data.get("data", []) if isinstance(data, dict) else []) or []:
                if isinstance(m, dict) and isinstance(m.get("id"), int):
                    out[m["id"]] = m
        except (requests.RequestException, ValueError):
            continue
    return out


def _cf_class_id(mod_info: Optional[dict]) -> int:
    cid = mod_info.get("classId") if isinstance(mod_info, dict) else None
    return cid if isinstance(cid, int) else 6


def _cf_compat_lookup(file_meta: Dict[int, dict], mod_meta: Dict[int, dict],
                      sides: Optional[_ModrinthSides] = None) -> Dict[int, Optional[ModInfo]]:
    """
    Modrinth side-compat for every CF mod file in one go: batched sha1 lookup,
    then a batched slug lookup for the rest. Returns {fileID: ModInfo|None}.
    """
    sides = sides or _ModrinthSides()
    sha1_of: Dict[int, str] = {}
    slug_of: Dict[int, str] = {}
    for fid, fi in file_meta.items():
        mi = mod_meta.get(fi.get("modId")) if isinstance(fi.get("modId"), int) else None
        if _CF_CLASS_PATH.get(_cf_class_id(mi), "mods") != "mods":
            continue
        sha1 = _extract_cf_hash(fi, 1)
        if sha1:
            sha1_of[fid] = sha1
        if mi and isinstance(mi.get("slug"), str) and mi["slug"]:
            slug_of[fid] = mi["slug"].lower()
    by_sha1 = sides.by_sha1(sha1_of.values())
    out: Dict[int, Optional[ModInfo]] = {fid: by_sha1.get(s) for fid, s in sha1_of.items()}
    unresolved = [fid for fid in slug_of if out.get(fid) is None]
    if unresolved and not sides.offline:
        by_slug = sides.by_slugs(slug_of[fid] for fid in unresolved)
        for fid in unresolved:
            if slug_of[fid] in by_slug:
                out[fid] = by_slug[slug_of[fid]]
    return out


def _cf_web_url(mod_info: dict) -> str:
    links = mod_info.get("links")
    site = links.get("websiteUrl") if isinstance(links, dict) else None
    if isinstance(site, str) and site.startswith("https://www.curseforge.com/"):
        return site
    slug = mod_info.get("slug")
    if not isinstance(slug, str) or not slug:
        return ""
    section = _CF_CLASS_WEB.get(_cf_class_id(mod_info), "mc-mods")
    return f"https://www.curseforge.com/minecraft/{section}/{slug}"


def _install_cf_file(file_info: Optional[dict], mod_info: Optional[dict],
                     server_path: str,
                     compat_info: Optional[ModInfo] = None,
                     ) -> Tuple[str, Optional[dict], Optional[str]]:
    """
    Returns (outcome, bypass_info, failure_reason):
      - ("ok",       None, None)         downloaded from CF's own URL
      - ("ok",       bypass_dict, None)  downloaded via forgecdn CDN fallback
                                          (mod author opted out, but we got it anyway)
      - ("client",   None, None)         client-only, intentionally skipped
      - ("fail",     None, reason)       no metadata / bad name / all URLs failed

    `bypass_dict` shape: {"name": str, "cf_url": str}. Caller appends this to
    ImportResult.bypassed_mods so the GUI can show a transparent "we bypassed
    these opt-outs, same as HMCL does" notice.

    For mod jars, `compat_info` is the Modrinth sha1/slug lookup result from
    _cf_compat_lookup() — opt-out has nothing to do with client/server side,
    so we still respect it. Downloads are verified against the CF hashes.
    """
    if not file_info or not mod_info:
        return "fail", None, "CurseForge API 没有返回该文件的信息"

    file_id = file_info.get("id")
    # fileName comes from the API: keep only the base name so it can't
    # climb out of mods/ ("..\\..\\x.jar" — backslash is a separator on Windows).
    raw_name = file_info.get("fileName")
    file_name = posixpath.basename(raw_name.replace("\\", "/")).strip() \
        if isinstance(raw_name, str) else ""
    if file_name in ("", ".", ".."):
        file_name = f"cf_{file_id or 'unknown'}.jar"
    official_url = file_info.get("downloadUrl")  # None when author opted out
    if not isinstance(official_url, str) or not official_url:
        official_url = None

    class_id = _cf_class_id(mod_info)
    sub = _CF_CLASS_PATH.get(class_id, "mods")

    # Path-based skip (resource/shader packs) — applies regardless of opt-out
    if sub in _CLIENT_ONLY_SUBDIRS:
        return "client", None, None

    # Mod jar: cross-check Modrinth metadata for client-only classification
    if sub == "mods" and classify_mod(compat_info) == "client_only":
        return "client", None, None

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

    target_path, reason = safe_target(os.path.join(server_path, sub), file_name)
    if target_path is None:
        return "fail", None, reason
    expected = {"sha1": _extract_cf_hash(file_info, 1), "md5": _extract_cf_hash(file_info, 2)}

    last_err = "没有可用的下载地址"
    hash_err = None
    for url in candidates:
        try:
            _download_to(url, target_path, _USER_AGENT, expected)
        except requests.RequestException as e:   # (subclass of OSError — keep first)
            last_err = f"下载失败：{e}"
            continue
        except OSError as e:
            return "fail", None, write_error_reason(target_path, e)
        except Exception as e:                   # hash mismatch etc.
            hash_err = hash_err or str(e)
            continue
        if bypassed_via_cdn and url != official_url:
            return "ok", {
                "name": mod_info.get("name", file_name),
                "cf_url": _cf_web_url(mod_info),
            }, None
        return "ok", None, None
    # A hash mismatch is the more useful explanation than a later CDN 403/404
    return "fail", None, hash_err or last_err


def _forgecdn_url(file_id: int, file_name: str) -> Optional[str]:
    """
    Construct the public CDN URL for a CurseForge file. Used as fallback when
    the API's downloadUrl is null (mod author opted out of third-party API).

    Pattern: /files/{file_id // 1000}/{file_id % 1000}/{file_name}
    Both the official launcher and all major third-party launchers use this
    (file name percent-encoded, as PrismLauncher does).
    """
    if not file_id or not file_name:
        return None
    a, b = file_id // 1000, file_id % 1000
    return f"https://edge.forgecdn.net/files/{a}/{b}/{urllib.parse.quote(file_name, safe='')}"


def _extract_cf_hash(file_info: dict, algo: int) -> Optional[str]:
    """CF response: hashes = [{algo: 1, value: '...'}, {algo: 2, ...}]. algo 1 = sha1, 2 = md5."""
    hashes = file_info.get("hashes")
    for h in hashes if isinstance(hashes, list) else []:
        if isinstance(h, dict) and h.get("algo") == algo:
            v = h.get("value")
            if isinstance(v, str) and v.strip():
                return v.strip().lower()
    return None


def _extract_cf_sha1(file_info: dict) -> Optional[str]:
    """algo 1 = sha1 (kept for callers of the old name)."""
    return _extract_cf_hash(file_info, 1)
