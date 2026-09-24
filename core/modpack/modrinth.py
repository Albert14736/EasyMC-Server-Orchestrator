"""
Modrinth `.mrpack` provider.

Format spec: https://docs.modrinth.com/modpacks/format/

A .mrpack is a ZIP whose root contains modrinth.index.json plus optional
overrides/, client-overrides/, server-overrides/ directories. We:
  - download each manifest file that isn't env.server == "unsupported"
  - hash-verify every download (sha1 and/or sha512 — content-addressed =
    manifest can't lie)
  - extract overrides/ then server-overrides/ on top
  - skip client-overrides/ entirely (we're a server tool)

_extract_overrides() here is shared by every provider.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import io
import json
import os
import re
import urllib.parse
import zipfile
import zlib
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple

import requests

from core.mod_scanner import ModInfo, classify_mod

from .base import (
    ImportProgress,
    ImportResult,
    ModpackFile,
    ModpackManifest,
    ModpackProvider,
    archive_fingerprint,
    create_server_for_pack,
    discard_temp,
    find_zip_entry,
    hmsl_launch_file,
    is_client_only_path,
    note_launch_files_skipped,
    open_zip,
    read_zip_json,
    replace_from_stream,
    safe_target,
    summarize_problems,
    temp_path_for,
    without_launch_files,
    write_error_reason,
    zip_entries,
)


_USER_AGENT = "HMSL/0.1 modpack-importer"
_API = "https://api.modrinth.com"
_INDEX_FILENAME = "modrinth.index.json"

# Chunk size for batch endpoints. Modrinth allows up to ~150 ids but we
# keep it conservative to avoid 414 URI Too Long on the project query.
_BATCH_CHUNK = 50

# (connect, read) timeouts for Modrinth compat lookups. Short on purpose:
# after the first connection failure we stop asking (see _ModrinthSides).
_LOOKUP_TIMEOUT = (5, 15)
# (connect, read) for file downloads — read timeout is per chunk, not total.
_DOWNLOAD_TIMEOUT = (15, 60)

# Modrinth dependency keys we know about → server_factory loader names.
_LOADER_KEY_MAP = {
    "forge":          "Forge",
    "neoforge":       "NeoForge",
    "fabric-loader":  "Fabric",
    "quilt-loader":   "Fabric",   # Quilt is API-compat with Fabric server jars
}

_QUILT_WARNING = ("该整合包使用 Quilt 加载器，HMSL 会用 Fabric 服务端运行它；"
                  "只支持 Quilt 的模组可能无法加载。")
_OFFLINE_WARNING = ("无法连接 Modrinth，未能检查整合包自带的模组是否为纯客户端模组；"
                    "如果服务器启动报错，可在「模组扫描」里再检查一次。")


class ModrinthProvider(ModpackProvider):
    name = "modrinth"

    # ---------- detect ----------

    def detect(self, archive_path: str) -> bool:
        """Accept .mrpack by extension OR any zip whose root has modrinth.index.json."""
        if archive_path.lower().endswith(".mrpack"):
            return True
        if not archive_path.lower().endswith(".zip"):
            return False
        try:
            with zipfile.ZipFile(archive_path) as zf:
                return find_zip_entry(zf, _INDEX_FILENAME) is not None
        except (zipfile.BadZipFile, OSError):
            return False

    # ---------- parse ----------

    def parse(self, archive_path: str) -> ModpackManifest:
        with open_zip(archive_path) as zf:
            data = read_zip_json(zf, _INDEX_FILENAME)
        manifest = _manifest_from_index(data)
        manifest.source = archive_fingerprint(archive_path)
        return manifest

    # ---------- enrich ----------

    def enrich_compat(self, manifest: ModpackManifest,
                       progress_callback: Optional[Callable[[ImportProgress], None]] = None,
                       sides: Optional["_ModrinthSides"] = None) -> None:
        """
        For files lacking env metadata in the manifest (common in older or
        community-built mrpacks), batch-look up their sha1 on Modrinth and
        fill in env_server/env_client from the project's metadata.

        Mutates `manifest.files` in place. Best-effort: network failures leave
        env fields as-is, and those files will be installed (safe default).
        Once Modrinth has answered, manifest.compat_checked is set and later
        calls on the same manifest (apply() reusing the GUI preview's) are
        no-ops; results are also cached per sha1 for re-parsed manifests.
        """
        if manifest.compat_checked:
            return
        targets = [f for f in manifest.files if f.needs_compat_lookup()]
        if not targets:
            manifest.compat_checked = True
            return
        if progress_callback:
            progress_callback(ImportProgress(
                stage="checking_compat",
                message=f"反查 {len(targets)} 个未标注 env 的模组兼容性…",
                current=0, total=len(targets),
            ))

        sides = sides or _ModrinthSides()
        infos = sides.by_sha1(f.sha1 for f in targets if f.sha1)

        # Stamp env fields back onto the files. We deliberately OVERWRITE the
        # manifest's env when Modrinth has authoritative project metadata —
        # because mrpack tooling defaults to required/required when the
        # author didn't bother classifying, and we want the real signal.
        for f in targets:
            info = infos.get((f.sha1 or "").lower())
            if info is None:
                continue
            f.env_client = info.client_side or f.env_client
            f.env_server = info.server_side or f.env_server
        if not sides.offline:
            manifest.compat_checked = True

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

        # Step 1: parse manifest (or reuse the GUI preview's)
        report("parsing", "正在读取 modrinth.index.json…")
        try:
            manifest = self.prepared_manifest(archive_path, manifest)
        except ValueError as e:
            return ImportResult(False, "", str(e))
        warnings: List[str] = list(manifest.warnings)
        sides = _ModrinthSides()

        # Step 1.5: enrich missing env fields via Modrinth batch lookup.
        # No-op when everything is labeled or the preview already did it.
        self.enrich_compat(manifest, progress_callback=progress_callback, sides=sides)

        # Step 2: bootstrap server via existing create_server()
        report("creating_server", f"正在创建 {manifest.loader} {manifest.mc_version} 服务端…")
        cr = create_server_for_pack(manifest, server_name, parent_dir, env_manager,
                                    installer, downloader, warnings, report)
        if not cr.success:
            return ImportResult(False, cr.server_path or "", f"创建服务端失败：{cr.error}",
                                manifest=manifest, warnings=warnings)

        server_path = cr.server_path
        installed = 0
        skipped_client = len(manifest.skipped_client_files)
        dl_problems: List[Tuple[str, str]] = []

        # Step 3: download server-relevant files (never over HMSL's start script)
        targets = without_launch_files(manifest.server_files, warnings)
        report("downloading_files",
               f"将下载 {len(targets)} 个文件（跳过 {skipped_client} 个客户端专属）…",
               current=0, total=len(targets))
        for i, f in enumerate(targets, start=1):
            report("downloading_files", f.path, current=i, total=len(targets))
            try:
                _download_with_sha1_verify(f, server_path)
                installed += 1
            except Exception as e:
                dl_problems.append((f.path, str(e)))
                # Continue — one bad file shouldn't abort whole import
                report("downloading_files", f"⚠️ {f.path}: {e}", current=i, total=len(targets))
        if dl_problems:
            warnings.append(summarize_problems(
                f"{len(dl_problems)} 个文件下载失败，可稍后手动放进服务器目录", dl_problems))

        # Step 4: apply overrides → server-overrides (latter wins on conflict).
        # Override mods get the same client-only classification as manifest files —
        # many community modpacks bundle CF-only client mods (Iris, JEI, minimaps)
        # inline in overrides/mods/ and we don't want to ship them to the server.
        # client-overrides/ intentionally skipped wholesale.
        ov_mods_installed = ov_skipped = ov_failed = 0
        for prefix in ("overrides/", "server-overrides/"):
            report("applying_overrides", f"正在解压 {prefix.rstrip('/')}…")
            st = _extract_overrides(archive_path, server_path, prefix, warnings=warnings,
                                    progress_callback=progress_callback, sides=sides)
            ov_mods_installed += st.mods_installed
            ov_skipped += st.mods_skipped + st.client_skipped
            ov_failed += st.failed
        if sides.offline:
            warnings.append(_OFFLINE_WARNING)

        report("done", "整合包导入完成")
        # The server exists and overrides are applied: that's a successful
        # import even if some downloads failed (details are in warnings).
        return ImportResult(
            success=True,
            server_path=server_path,
            error=None,
            manifest=manifest,
            files_installed=installed + ov_mods_installed,
            files_skipped_client=skipped_client + ov_skipped,
            files_failed=len(dl_problems) + ov_failed,
            warnings=warnings,
        )


# ---------- private helpers ----------

def _manifest_from_index(data: dict) -> ModpackManifest:
    """Turn the raw JSON into our format-agnostic ModpackManifest."""
    deps = data.get("dependencies")
    if not isinstance(deps, dict):
        deps = {}
    mc_version = deps.get("minecraft", "")
    loader_name, loader_version, loader_key = _pick_loader(deps)
    warnings: List[str] = []
    if loader_key == "quilt-loader":
        loader_version = None   # a Quilt version number means nothing to the Fabric installer
        warnings.append(_QUILT_WARNING)

    raw_files = data.get("files")
    # De-dup by path: the same path listed twice would be downloaded twice and
    # counted twice. Later entries win, like a launcher overwriting the file.
    by_path: Dict[str, ModpackFile] = {}
    for e in raw_files if isinstance(raw_files, list) else []:
        if not isinstance(e, dict):
            continue
        mf = _file_from_entry(e)
        key = mf.path.replace("\\", "/").strip("/").lower()
        if not key:
            continue
        by_path[key] = mf

    return ModpackManifest(
        format="modrinth",
        name=str(data.get("name", "")),
        version=str(data.get("versionId", "")),
        mc_version=str(mc_version),
        loader=loader_name,
        loader_version=loader_version,
        summary=str(data.get("summary", "")),
        files=list(by_path.values()),
        warnings=warnings,
    )


def _pick_loader(deps: dict) -> Tuple[str, Optional[str], Optional[str]]:
    """Return (loader_name_for_server_factory, raw_version_or_None, dependency_key)."""
    for key, mapped in _LOADER_KEY_MAP.items():
        if key in deps:
            return mapped, str(deps[key]), key
    # Vanilla server (no loader). Fall back to Paper as the most useful pure-server fit.
    return "Paper", None, None


def _file_from_entry(entry: dict) -> ModpackFile:
    hashes = entry.get("hashes")
    hashes = hashes if isinstance(hashes, dict) else {}
    env = entry.get("env")
    env = env if isinstance(env, dict) else {}
    downloads = entry.get("downloads")
    if isinstance(downloads, str):
        downloads = [downloads]
    elif not isinstance(downloads, list):
        downloads = []
    size = entry.get("fileSize")

    def _str_or_none(v):
        return v.strip() if isinstance(v, str) and v.strip() else None

    return ModpackFile(
        path=str(entry.get("path", "") or ""),
        sha1=_str_or_none(hashes.get("sha1")),
        sha512=_str_or_none(hashes.get("sha512")),
        download_urls=[u.strip() for u in downloads if isinstance(u, str) and u.strip()],
        file_size=size if isinstance(size, int) else None,
        env_client=_str_or_none(env.get("client")),
        env_server=_str_or_none(env.get("server")),
    )


_TRANSIENT_NET_ERRORS = (requests.ConnectionError, requests.Timeout,
                         requests.exceptions.ChunkedEncodingError)


class _HostUnreachable(requests.ConnectionError):
    """A download skipped because its host already failed to connect repeatedly in this import."""


class HostBreaker:
    """
    Per-import fail-fast for file downloads: once TRIP files in a row from the
    same host could not even connect (no network, DNS / firewall block, dead
    mirror), later files from that host fail at once instead of each waiting
    out connect timeouts plus a retry — otherwise a 200-mod pack imported
    offline takes over half an hour to "finish". Other hosts (mirrors) are
    still tried. Installed for the duration of import_modpack() via
    download_breaker(); helpers called on their own don't use one.
    """
    TRIP = 2

    def __init__(self):
        self._fails: Dict[str, int] = {}
        self.skipped: Dict[str, int] = {}

    @staticmethod
    def _host(url: str) -> str:
        try:
            return urllib.parse.urlsplit(url).netloc.lower()
        except ValueError:
            return ""

    def check(self, url: str) -> None:
        host = self._host(url)
        if host and self._fails.get(host, 0) >= self.TRIP:
            self.skipped[host] = self.skipped.get(host, 0) + 1
            raise _HostUnreachable(f"网络不可用：连接 {host} 已连续失败，本次导入不再尝试该地址")

    def record(self, url: str, err: Optional[BaseException]) -> None:
        """err=None / an HTTP or hash error: the host answered. A ConnectionError: it didn't."""
        host = self._host(url)
        if not host:
            return
        if isinstance(err, requests.ConnectionError):
            self._fails[host] = self._fails.get(host, 0) + 1
        else:
            self._fails[host] = 0

    def summary(self) -> List[str]:
        return [f"无法连接 {host}（网络不可用或被拦截），本次导入之后没有再从该地址下载（跳过 {n} 次）；"
                f"网络恢复后可换个名称重新导入，或手动下载缺少的文件放进服务器目录。"
                for host, n in self.skipped.items()]


_BREAKER: "contextvars.ContextVar[Optional[HostBreaker]]" = contextvars.ContextVar(
    "hmsl_modpack_download_breaker", default=None)


@contextlib.contextmanager
def download_breaker():
    """Install a fresh HostBreaker for the downloads made in this thread until exit."""
    breaker = HostBreaker()
    token = _BREAKER.set(breaker)
    try:
        yield breaker
    finally:
        _BREAKER.reset(token)


def _download_to(url: str, target: str, user_agent: str,
                 expected: Dict[str, Optional[str]], attempts: int = 2) -> None:
    """
    Stream `url` into a temp file next to `target`, verify every hash given in
    `expected` ({"sha1": hex, "sha512": hex, "md5": hex}), then move it into
    place. A failed download never truncates or deletes an existing `target`.
    A stalled / dropped connection is retried once. Raises on any failure.
    """
    breaker = _BREAKER.get()
    if breaker is not None:
        breaker.check(url)
    for attempt in range(1, attempts + 1):
        try:
            _download_once(url, target, user_agent, expected)
        except _TRANSIENT_NET_ERRORS as e:
            if attempt < attempts:
                continue
            if breaker is not None:
                breaker.record(url, e)
            raise
        except (requests.RequestException, RuntimeError):   # HTTP error / hash mismatch
            if breaker is not None:
                breaker.record(url, None)
            raise
        else:
            if breaker is not None:
                breaker.record(url, None)
            return


def _download_once(url: str, target: str, user_agent: str,
                   expected: Dict[str, Optional[str]]) -> None:
    checks = {algo: v.lower() for algo, v in expected.items() if isinstance(v, str) and v}
    hashers = {algo: hashlib.new(algo) for algo in checks}
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = temp_path_for(target)
    try:
        # Open the file first: an unwritable / too-long path fails before any download.
        with open(tmp, "wb") as out:
            with requests.get(url, stream=True, headers={"User-Agent": user_agent},
                              timeout=_DOWNLOAD_TIMEOUT) as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=65536):
                    out.write(chunk)
                    for h in hashers.values():
                        h.update(chunk)
        for algo, want in checks.items():
            got = hashers[algo].hexdigest()
            if got != want:
                raise RuntimeError(f"{algo} 校验失败 (期望 {want[:8]}…, 实际 {got[:8]}…)")
        # Only a fully downloaded, hash-verified file replaces what's there
        os.replace(tmp, target)
    except BaseException:
        discard_temp(tmp)
        raise


def _download_with_sha1_verify(f: ModpackFile, server_root: str) -> None:
    """Try each mirror in order; verify sha1/sha512 if present; write to server_root/f.path."""
    if not f.download_urls:
        raise RuntimeError("文件清单没有下载链接")

    # Defend against zip-slip via "../" paths in manifest, and names this OS can't store
    target_abs, reason = safe_target(server_root, f.path)
    if target_abs is None:
        raise RuntimeError(f"非法路径（{reason}）：{f.path}")

    last_err: Optional[Exception] = None
    for url in f.download_urls:
        try:
            _download_to(url, target_abs, _USER_AGENT, {"sha1": f.sha1, "sha512": f.sha512})
            return
        except requests.RequestException as e:   # (subclass of OSError — keep first)
            last_err = e
        except OSError as e:
            # Local write problem (path too long, disk full…): other mirrors won't help
            raise RuntimeError(write_error_reason(target_abs, e)) from e
        except Exception as e:
            last_err = e
    raise RuntimeError(f"全部 {len(f.download_urls)} 个镜像都失败：{last_err}")


def _chunked(items, size):
    """Yield successive chunks of `items` of at most `size` elements."""
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ---------- overrides extraction (shared by every provider) ----------

class OverrideStats(NamedTuple):
    extracted: int        # files written
    mods_installed: int   # mods/*.jar written
    mods_skipped: int     # mods/*.jar skipped as client-only (Modrinth says so)
    client_skipped: int   # resourcepacks/ shaderpacks/ texturepacks/ items skipped
    failed: int           # entries that could not be written (bad name, too long, corrupt…)


_EXTRACT_ERRORS = (OSError, zipfile.BadZipFile, RuntimeError, EOFError,
                   zlib.error, NotImplementedError)


def _extract_overrides(
    archive_path: str,
    server_root: str,
    prefix: str,
    warnings: Optional[List[str]] = None,
    progress_callback: Optional[Callable[[ImportProgress], None]] = None,
    sides: Optional["_ModrinthSides"] = None,
    exclude: Iterable[str] = (),
) -> OverrideStats:
    """
    Extract every member of the zip whose name starts with `prefix` into server_root.

    - resourcepacks/ shaderpacks/ texturepacks/ are client-only by definition → skipped.
    - `exclude`: extra rel paths to skip silently ("pack.json", or "versions/" for a dir).
    - start.bat / start.sh / hmsl_launch.json at the server root are HMSL's own
      launch files (written before the overrides): the pack's copies are skipped
      with a warning, never written over them.
    - For .jar files inside the `mods/` subdirectory of the override scope we
      look the jar up on Modrinth (batched) and SKIP it if the mod is
      client-only — same logic as the manifest-files path, so override-bundled
      CF mods don't sneak past our classifier.
    - Every entry is handled on its own: a name Windows can't store (':' '?'
      '|' …, trailing dot, CON/NUL…), a path over MAX_PATH or a corrupt member
      is skipped, counted in `failed` and summarised in `warnings`; it never
      aborts the import and never becomes an NTFS alternate data stream.
    """
    def report(stage, msg, current=0, total=0):
        if progress_callback:
            progress_callback(ImportProgress(stage=stage, message=msg,
                                             current=current, total=total))

    label = prefix.rstrip("/") or "整合包内容"
    excl = [e.replace("\\", "/").lower() for e in exclude]
    problems: List[Tuple[str, str]] = []
    extracted = mods_installed = mods_skipped = 0
    client_items: Set[str] = set()
    launch_skipped: List[str] = []

    try:
        zf = zipfile.ZipFile(archive_path)
    except (zipfile.BadZipFile, OSError) as e:
        if warnings is not None:
            warnings.append(f"无法读取整合包里的 {label}：{e}")
        return OverrideStats(0, 0, 0, 0, 1)

    with zf:
        members: List[Tuple[str, zipfile.ZipInfo]] = []
        for name, info in zip_entries(zf):
            if not name.startswith(prefix) or name.endswith("/") or info.is_dir():
                continue
            rel = name[len(prefix):]
            if not rel:
                continue
            rel_l = rel.replace("\\", "/").lower()
            if any(rel_l == e.rstrip("/") or (e.endswith("/") and rel_l.startswith(e))
                   for e in excl):
                continue
            if is_client_only_path(rel_l):
                # count "resourcepacks/Foo.zip" or "resourcepacks/Foo/" once
                client_items.add("/".join(rel_l.split("/")[:2]))
                continue
            launch_name = hmsl_launch_file(rel_l)
            if launch_name:
                launch_skipped.append(launch_name)
                continue
            members.append((rel, info))

        jars = [(rel, info) for rel, info in members
                if rel.replace("\\", "/").lower().startswith("mods/")
                and rel.lower().endswith(".jar")]
        client_only_jars: Set[str] = set()
        if jars:
            sides = sides if sides is not None else _ModrinthSides()
            client_only_jars = _classify_override_jars(zf, jars, report, sides)

        total = len(members)
        for i, (rel, info) in enumerate(members, start=1):
            if i == 1 or i == total or i % 50 == 0:
                report("applying_overrides", f"正在解压 {label}：{rel}", i, total)
            if rel in client_only_jars:
                mods_skipped += 1
                continue
            is_mod_jar = (rel.replace("\\", "/").lower().startswith("mods/")
                          and rel.lower().endswith(".jar"))

            target, reason = safe_target(server_root, rel)
            if target is None:
                problems.append((rel, reason))
                continue
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                # temp file + os.replace: a corrupt member or a write error
                # leaves a same-named file that was already there untouched
                with zf.open(info) as src:
                    replace_from_stream(src, target)
            except _EXTRACT_ERRORS as e:
                if isinstance(e, OSError):
                    problems.append((rel, write_error_reason(target, e)))
                elif isinstance(e, NotImplementedError):
                    problems.append((rel, "压缩方式不受支持，请用标准 zip(Deflate) 重新打包"))
                else:
                    problems.append((rel, f"压缩包内文件已损坏：{e}"))
                continue
            extracted += 1
            if is_mod_jar:
                mods_installed += 1

    note_launch_files_skipped(warnings, launch_skipped)
    if problems and warnings is not None:
        warnings.append(summarize_problems(
            f"{label} 中有 {len(problems)} 个文件未能写入，已跳过", problems))
    return OverrideStats(extracted, mods_installed, mods_skipped, len(client_items), len(problems))


def _classify_override_jars(zf: zipfile.ZipFile,
                            jars: List[Tuple[str, zipfile.ZipInfo]],
                            report: Callable[..., None],
                            sides: "_ModrinthSides") -> Set[str]:
    """
    Resolve bundled mod jars' client/server compatibility via Modrinth, using
    THREE fallbacks (cheapest/most-precise first), each batched where the API
    allows it:

    1. sha1 hash lookup → version → project (works when this exact jar is
       on Modrinth). One request for all jars.
    2. mod ID slug lookup (catches CF-uploaded mods whose Modrinth slug
       equals their modId, e.g. 'iris' / 'catalogue' style). One request.
    3. displayName exact-title search (catches mods whose Modrinth slug is
       kebab-case but jar uses no-separator modId — e.g. modId='citresewn'
       but Modrinth slug='cit-resewn', and the displayName is 'CIT Resewn').

    Returns the rel paths of client-only jars. Unknown / unresolvable / offline
    → not client-only: we'd rather install an unknown mod than silently drop
    user content.
    """
    total = len(jars)
    sha1_of: Dict[str, str] = {}
    meta_of: Dict[str, List[Tuple[str, Optional[str]]]] = {}
    for i, (rel, info) in enumerate(jars, start=1):
        report("checking_compat", f"正在检查模组兼容性：{os.path.basename(rel)}", i, total)
        try:
            with zf.open(info) as f:
                jar_bytes = f.read()
        except _EXTRACT_ERRORS:
            continue
        sha1_of[rel] = hashlib.sha1(jar_bytes).hexdigest()
        meta_of[rel] = _extract_mod_metadata_from_jar_bytes(jar_bytes)
        del jar_bytes

    # Fallback 1: sha1 lookup (batched)
    by_sha1 = sides.by_sha1(sha1_of.values())
    info_of: Dict[str, Optional[ModInfo]] = {rel: by_sha1.get(s) for rel, s in sha1_of.items()}

    # Fallback 2: try each mod ID as a slug (batched)
    unresolved = [rel for rel, inf in info_of.items() if inf is None]
    if unresolved and not sides.offline:
        by_slug = sides.by_slugs(mid for rel in unresolved for mid, _d in meta_of.get(rel, []))
        for rel in unresolved:
            for mid, _disp in meta_of.get(rel, []):
                if mid in by_slug:
                    info_of[rel] = by_slug[mid]
                    break

    # Fallback 3: try each displayName as an exact-title search (one request each)
    unresolved = [rel for rel, inf in info_of.items() if inf is None]
    for n, rel in enumerate(unresolved, start=1):
        if sides.offline:
            break
        report("checking_compat", f"正在按名称查找模组：{os.path.basename(rel)}", n, len(unresolved))
        seen_titles = set()
        for _mid, disp in meta_of.get(rel, []):
            if not disp or disp in seen_titles:
                continue
            seen_titles.add(disp)
            found = sides.by_title(disp)
            if found is not None:
                info_of[rel] = found
                break

    return {rel for rel, inf in info_of.items() if classify_mod(inf) == "client_only"}


def _extract_mod_metadata_from_jar_bytes(jar_bytes: bytes) -> List[Tuple[str, Optional[str]]]:
    """
    Extract (modId, displayName) pairs from a Minecraft mod jar's metadata.

    Supports:
      - Forge / NeoForge: META-INF/mods.toml, META-INF/neoforge.mods.toml
      - Fabric / Quilt:   fabric.mod.json, quilt.mod.json

    Returns list of (mod_id_lower, display_name_or_None), preserving order and deduped.
    """
    out: List[Tuple[str, Optional[str]]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(jar_bytes)) as jar:
            members = set(jar.namelist())
            # Forge/NeoForge TOML
            for path in ("META-INF/mods.toml", "META-INF/neoforge.mods.toml"):
                if path in members:
                    try:
                        text = jar.read(path).decode("utf-8-sig", errors="replace")
                    except Exception:
                        continue
                    out.extend(_parse_forge_mods_toml(text))
            # Fabric / Quilt JSON
            for path in ("fabric.mod.json", "quilt.mod.json"):
                if path in members:
                    try:
                        data = json.loads(jar.read(path).decode("utf-8-sig", errors="replace"))
                    except Exception:
                        continue
                    if not isinstance(data, dict):
                        continue
                    mid = data.get("id")
                    if isinstance(mid, str):
                        out.append((mid.lower(), data.get("name") if isinstance(data.get("name"), str) else None))
                    ql = data.get("quilt_loader")
                    ql = ql if isinstance(ql, dict) else {}
                    qid = ql.get("id")
                    if isinstance(qid, str):
                        qmeta = ql.get("metadata")
                        qname = qmeta.get("name") if isinstance(qmeta, dict) else None
                        out.append((qid.lower(), qname if isinstance(qname, str) else None))
    except (zipfile.BadZipFile, OSError, RuntimeError, EOFError, zlib.error, NotImplementedError):
        return out
    # De-dup by (modId, displayName)
    seen = set(); uniq = []
    for pair in out:
        if pair not in seen:
            seen.add(pair); uniq.append(pair)
    return uniq


def _parse_forge_mods_toml(text: str) -> List[Tuple[str, Optional[str]]]:
    """
    Parse Forge's mods.toml — a streamlined TOML with [[mods]] sections.
    We don't pull in a TOML library; the [[mods]] block we care about is
    simple enough for regex.
    """
    out: List[Tuple[str, Optional[str]]] = []
    # Split on [[mods]] section markers, skipping anything before the first one
    sections = re.split(r'^\[\[mods\]\]', text, flags=re.MULTILINE)[1:]
    for sec in sections:
        # Stop at next top-level [section]
        sec = re.split(r'^\[', sec, maxsplit=1, flags=re.MULTILINE)[0]
        m_id = re.search(r'modId\s*=\s*"([A-Za-z0-9_\-]+)"', sec)
        m_disp = re.search(r'displayName\s*=\s*"([^"]+)"', sec)
        if m_id:
            out.append((m_id.group(1).lower(),
                        m_disp.group(1) if m_disp else None))
    return out


# ---------- Modrinth side-compat lookups (batched, cached, fail-fast) ----------

# Process-wide caches so the GUI preview and apply() share results.
# Value None = "Modrinth answered: not found" (a network failure is NOT cached).
_SHA1_CACHE: Dict[str, Optional[ModInfo]] = {}
_SLUG_CACHE: Dict[str, Optional[ModInfo]] = {}
_TITLE_CACHE: Dict[str, Optional[ModInfo]] = {}

# What Modrinth accepts in /v2/projects?ids=[...] — anything else 400s the whole batch.
_SLUG_RE = re.compile(r"[a-z0-9_\-.]{2,64}")


def _mod_info(p: dict, fallback_id: str = "") -> ModInfo:
    return ModInfo(
        project_id=str(p.get("id") or p.get("project_id") or fallback_id),
        project_title=str(p.get("title") or fallback_id),
        client_side=str(p.get("client_side") or "unknown"),
        server_side=str(p.get("server_side") or "unknown"),
    )


class _ModrinthSides:
    """
    Batch lookups of Modrinth client_side/server_side. Fails fast: after the
    first connection error / timeout every later call returns "unknown" at
    once, so an unreachable Modrinth (common on CN networks) costs one short
    timeout per import instead of ~25 s per bundled jar.
    """

    def __init__(self):
        self.offline = False

    def _request(self, method: str, url: str, **kw) -> Optional[requests.Response]:
        if self.offline:
            return None
        try:
            r = requests.request(method, url, headers={"User-Agent": _USER_AGENT},
                                 timeout=_LOOKUP_TIMEOUT, **kw)
        except (requests.ConnectionError, requests.Timeout):
            self.offline = True
            return None
        except requests.RequestException:
            return None
        return r if r.status_code == 200 else None

    def _projects(self, ids: Iterable[str]) -> Tuple[List[dict], Set[str]]:
        """GET /v2/projects (ids and/or slugs) → (projects, the ids whose request succeeded)."""
        out: List[dict] = []
        answered: Set[str] = set()
        for chunk in _chunked(list(dict.fromkeys(ids)), _BATCH_CHUNK):
            r = self._request("GET", f"{_API}/v2/projects", params={"ids": json.dumps(chunk)})
            if r is None:
                continue
            try:
                data = r.json()
            except ValueError:
                continue
            if isinstance(data, list):
                out.extend(p for p in data if isinstance(p, dict))
                answered.update(chunk)
        return out, answered

    def by_sha1(self, sha1s: Iterable[str]) -> Dict[str, ModInfo]:
        """{sha1_lower: ModInfo} for the hashes Modrinth knows."""
        wanted = list(dict.fromkeys(s.lower() for s in sha1s if isinstance(s, str) and s))
        todo = [s for s in wanted if s not in _SHA1_CACHE]
        project_of: Dict[str, str] = {}
        for chunk in _chunked(todo, _BATCH_CHUNK):
            r = self._request("POST", f"{_API}/v2/version_files",
                              json={"hashes": chunk, "algorithm": "sha1"})
            if r is None:
                continue
            try:
                data = r.json()
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            for s in chunk:
                v = data.get(s)
                if isinstance(v, dict) and v.get("project_id"):
                    project_of[s] = str(v["project_id"])
                else:
                    _SHA1_CACHE[s] = None
        if project_of:
            found, _answered = self._projects(set(project_of.values()))
            projects = {p.get("id"): p for p in found}
            for s, pid in project_of.items():
                if pid in projects:
                    _SHA1_CACHE[s] = _mod_info(projects[pid], pid)
        return {s: _SHA1_CACHE[s] for s in wanted if _SHA1_CACHE.get(s) is not None}

    def by_slugs(self, slugs: Iterable[str]) -> Dict[str, ModInfo]:
        """{slug_lower: ModInfo}. Modrinth accepts mod IDs as slugs for many projects."""
        wanted = [s for s in dict.fromkeys(x.lower() for x in slugs if isinstance(x, str))
                  if _SLUG_RE.fullmatch(s)]
        todo = [s for s in wanted if s not in _SLUG_CACHE]
        if todo:
            found, answered = self._projects(todo)
            got = {str(p.get("slug", "")).lower(): p for p in found}
            for s in todo:
                if s in got:
                    _SLUG_CACHE[s] = _mod_info(got[s], s)
                elif s in answered:
                    _SLUG_CACHE[s] = None
        return {s: _SLUG_CACHE[s] for s in wanted if _SLUG_CACHE.get(s) is not None}

    def by_title(self, title: str) -> Optional[ModInfo]:
        """
        Search Modrinth and accept ONLY if a hit's title is an exact case-insensitive
        match for `title`. This is the safety net for mods whose modId differs from
        their Modrinth slug (e.g. modId='citresewn' but slug='cit-resewn').

        Exact-match guard prevents "Catalogue" from being matched against
        "Mandela Catalogue" etc.
        """
        target = (title or "").strip().lower()
        if not target:
            return None
        if target in _TITLE_CACHE:
            return _TITLE_CACHE[target]
        r = self._request("GET", f"{_API}/v2/search", params={"query": title, "limit": 5})
        if r is None:
            return None
        found: Optional[ModInfo] = None
        try:
            hits = r.json().get("hits", [])
        except (ValueError, AttributeError):
            return None
        for h in hits if isinstance(hits, list) else []:
            if isinstance(h, dict) and str(h.get("title", "")).strip().lower() == target:
                found = _mod_info(h, title)
                break
        _TITLE_CACHE[target] = found
        return found


def _lookup_modrinth_project_by_slug(slug: str, timeout: float = 8.0) -> Optional[ModInfo]:
    """GET /v2/project/{slug} (single lookup; kept for callers outside the batch path)."""
    return _ModrinthSides().by_slugs([slug]).get((slug or "").lower())


def _lookup_modrinth_project_by_exact_title(title: str, timeout: float = 8.0) -> Optional[ModInfo]:
    """Exact-title Modrinth search (single lookup; kept for callers outside the batch path)."""
    return _ModrinthSides().by_title(title)
