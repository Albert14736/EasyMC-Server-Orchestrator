"""
Modrinth `.mrpack` provider.

Format spec: https://docs.modrinth.com/modpacks/format/

A .mrpack is a ZIP whose root contains modrinth.index.json plus optional
overrides/, client-overrides/, server-overrides/ directories. We:
  - download each manifest file that isn't env.server == "unsupported"
  - sha1-verify every downloaded jar (content-addressed = manifest can't lie)
  - extract overrides/ then server-overrides/ on top
  - skip client-overrides/ entirely (we're a server tool)
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import posixpath
import re
import zipfile
from typing import Callable, Dict, List, Optional, Tuple

import requests

from core.mod_scanner import ModInfo, classify_mod, lookup_mod_by_sha1
from core.server_factory import CreateServerResult, create_server

from .base import (
    ImportProgress,
    ImportResult,
    ModpackFile,
    ModpackManifest,
    ModpackProvider,
)


_USER_AGENT = "HMSL/0.1 modpack-importer"
_API = "https://api.modrinth.com"
_INDEX_FILENAME = "modrinth.index.json"

# Chunk size for batch endpoints. Modrinth allows up to ~150 ids but we
# keep it conservative to avoid 414 URI Too Long on the project query.
_BATCH_CHUNK = 50

# Modrinth dependency keys we know about → server_factory loader names.
_LOADER_KEY_MAP = {
    "forge":          "Forge",
    "neoforge":       "NeoForge",
    "fabric-loader":  "Fabric",
    "quilt-loader":   "Fabric",   # Quilt is API-compat with Fabric server jars
}


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
                return _INDEX_FILENAME in zf.namelist()
        except (zipfile.BadZipFile, OSError):
            return False

    # ---------- parse ----------

    def parse(self, archive_path: str) -> ModpackManifest:
        with zipfile.ZipFile(archive_path) as zf:
            try:
                raw = zf.read(_INDEX_FILENAME)
            except KeyError as e:
                raise ValueError(f"{_INDEX_FILENAME} 不存在于该 zip 中") from e
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValueError(f"无法解析 {_INDEX_FILENAME}: {e}") from e
        return _manifest_from_index(data)

    # ---------- enrich ----------

    def enrich_compat(self, manifest: ModpackManifest,
                       progress_callback: Optional[Callable[[ImportProgress], None]] = None) -> None:
        """
        For files lacking env metadata in the manifest (common in older or
        community-built mrpacks), batch-look up their sha1 on Modrinth and
        fill in env_server/env_client from the project's metadata.

        Mutates `manifest.files` in place. Best-effort: network failures leave
        env fields as-is, and those files will be installed (safe default).
        """
        targets = [f for f in manifest.files if f.needs_compat_lookup()]
        if not targets:
            return
        if progress_callback:
            progress_callback(ImportProgress(
                stage="checking_compat",
                message=f"反查 {len(targets)} 个未标注 env 的模组兼容性…",
                current=0, total=len(targets),
            ))

        # Batch resolve: sha1 -> version_object (carries project_id)
        sha1_to_version: Dict[str, dict] = {}
        for chunk in _chunked([f.sha1 for f in targets], _BATCH_CHUNK):
            try:
                r = requests.post(
                    f"{_API}/v2/version_files",
                    json={"hashes": chunk, "algorithm": "sha1"},
                    headers={"User-Agent": _USER_AGENT},
                    timeout=15,
                )
                r.raise_for_status()
                sha1_to_version.update(r.json())
            except (requests.RequestException, ValueError):
                continue  # best-effort: just leave unresolved

        # Batch fetch: project_id -> project metadata
        project_ids = list({v.get("project_id") for v in sha1_to_version.values()
                            if isinstance(v, dict) and v.get("project_id")})
        project_by_id: Dict[str, dict] = {}
        for chunk in _chunked(project_ids, _BATCH_CHUNK):
            try:
                r = requests.get(
                    f"{_API}/v2/projects",
                    params={"ids": json.dumps(chunk)},
                    headers={"User-Agent": _USER_AGENT},
                    timeout=15,
                )
                r.raise_for_status()
                for p in r.json():
                    if isinstance(p, dict) and p.get("id"):
                        project_by_id[p["id"]] = p
            except (requests.RequestException, ValueError):
                continue

        # Stamp env fields back onto the files. We deliberately OVERWRITE the
        # manifest's env when Modrinth has authoritative project metadata —
        # because mrpack tooling defaults to required/required when the
        # author didn't bother classifying, and we want the real signal.
        for f in targets:
            version = sha1_to_version.get(f.sha1) if f.sha1 else None
            if not isinstance(version, dict):
                continue
            project = project_by_id.get(version.get("project_id", ""))
            if not project:
                continue
            f.env_client = project.get("client_side", f.env_client)
            f.env_server = project.get("server_side", f.env_server)

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

        # Step 1: parse manifest
        report("parsing", "正在读取 modrinth.index.json…")
        try:
            manifest = self.parse(archive_path)
        except ValueError as e:
            return ImportResult(False, "", str(e))

        # Step 1.5: enrich missing env fields via Modrinth batch lookup.
        # Safe to call even if everything is already labeled — it no-ops.
        self.enrich_compat(manifest, progress_callback=progress_callback)

        # Step 2: bootstrap server via existing create_server()
        report("creating_server", f"正在创建 {manifest.loader} {manifest.mc_version} 服务端…")
        cr: CreateServerResult = create_server(
            name=server_name,
            version=manifest.mc_version,
            loader=manifest.loader,
            parent_dir=parent_dir,
            env_manager=env_manager,
            installer=installer,
            downloader=downloader,
            progress_callback=None,  # internal create_server progress goes nowhere here
        )
        if not cr.success:
            return ImportResult(False, cr.server_path or "", f"创建服务端失败：{cr.error}",
                                manifest=manifest)

        server_path = cr.server_path
        installed = failed = 0
        skipped_client = len(manifest.skipped_client_files)

        # Step 3: download server-relevant files
        targets = manifest.server_files
        report("downloading_files",
               f"将下载 {len(targets)} 个文件（跳过 {skipped_client} 个客户端专属）…",
               current=0, total=len(targets))
        for i, f in enumerate(targets, start=1):
            report("downloading_files", f.path, current=i, total=len(targets))
            try:
                _download_with_sha1_verify(f, server_path)
                installed += 1
            except Exception as e:
                failed += 1
                # Continue — one bad file shouldn't abort whole import
                report("downloading_files", f"⚠️ {f.path}: {e}", current=i, total=len(targets))

        # Step 4: apply overrides → server-overrides (latter wins on conflict).
        # Override mods get the same client-only classification as manifest files —
        # many community modpacks bundle CF-only client mods (Iris, JEI, minimaps)
        # inline in overrides/mods/ and we don't want to ship them to the server.
        report("applying_overrides", "正在解压 overrides…")
        extracted1, ov_installed1, ov_skipped1 = _extract_overrides(archive_path, server_path, "overrides/")
        report("applying_overrides", "正在解压 server-overrides…")
        extracted2, ov_installed2, ov_skipped2 = _extract_overrides(archive_path, server_path, "server-overrides/")
        # client-overrides/ intentionally skipped wholesale

        ov_mods_installed = ov_installed1 + ov_installed2
        ov_mods_skipped = ov_skipped1 + ov_skipped2

        report("done", "整合包导入完成")
        return ImportResult(
            success=(failed == 0),
            server_path=server_path,
            error=None if failed == 0 else f"{failed} 个文件下载失败",
            manifest=manifest,
            files_installed=installed + ov_mods_installed,
            files_skipped_client=skipped_client + ov_mods_skipped,
            files_failed=failed,
        )


# ---------- private helpers ----------

def _manifest_from_index(data: dict) -> ModpackManifest:
    """Turn the raw JSON into our format-agnostic ModpackManifest."""
    deps = data.get("dependencies", {}) or {}
    mc_version = deps.get("minecraft", "")
    loader_name, loader_version = _pick_loader(deps)

    files = [_file_from_entry(e) for e in data.get("files", []) if isinstance(e, dict)]

    return ModpackManifest(
        format="modrinth",
        name=str(data.get("name", "")),
        version=str(data.get("versionId", "")),
        mc_version=str(mc_version),
        loader=loader_name,
        loader_version=loader_version,
        summary=str(data.get("summary", "")),
        files=files,
    )


def _pick_loader(deps: dict) -> Tuple[str, Optional[str]]:
    """Return (loader_name_for_server_factory, raw_version_or_None)."""
    for key, mapped in _LOADER_KEY_MAP.items():
        if key in deps:
            return mapped, str(deps[key])
    # Vanilla server (no loader). Fall back to Paper as the most useful pure-server fit.
    return "Paper", None


def _file_from_entry(entry: dict) -> ModpackFile:
    hashes = entry.get("hashes", {}) or {}
    env = entry.get("env", {}) or {}
    return ModpackFile(
        path=str(entry.get("path", "")),
        sha1=hashes.get("sha1"),
        sha512=hashes.get("sha512"),
        download_urls=[str(u) for u in entry.get("downloads", []) if u],
        file_size=entry.get("fileSize"),
        env_client=env.get("client"),
        env_server=env.get("server"),
    )


def _download_with_sha1_verify(f: ModpackFile, server_root: str) -> None:
    """Try each mirror in order; verify sha1 if present; write to server_root/f.path."""
    if not f.download_urls:
        raise RuntimeError("文件清单没有下载链接")

    target = os.path.join(server_root, f.path)
    # Defend against zip-slip via "../" paths in manifest
    target_abs = os.path.abspath(target)
    if not target_abs.startswith(os.path.abspath(server_root) + os.sep):
        raise RuntimeError(f"非法路径（zip-slip 防御）：{f.path}")

    os.makedirs(os.path.dirname(target_abs), exist_ok=True)

    last_err: Optional[Exception] = None
    for url in f.download_urls:
        try:
            r = requests.get(url, stream=True,
                             headers={"User-Agent": _USER_AGENT}, timeout=60)
            r.raise_for_status()
            h = hashlib.sha1()
            with open(target_abs, "wb") as out:
                for chunk in r.iter_content(chunk_size=65536):
                    out.write(chunk)
                    h.update(chunk)
            if f.sha1 and h.hexdigest().lower() != f.sha1.lower():
                raise RuntimeError(f"sha1 校验失败 (期望 {f.sha1[:8]}…, 实际 {h.hexdigest()[:8]}…)")
            return
        except Exception as e:
            last_err = e
            # Remove partial write before trying next mirror
            try: os.remove(target_abs)
            except OSError: pass
            continue
    raise RuntimeError(f"全部 {len(f.download_urls)} 个镜像都失败：{last_err}")


def _chunked(items, size):
    """Yield successive chunks of `items` of at most `size` elements."""
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _extract_overrides(archive_path: str, server_root: str, prefix: str) -> Tuple[int, int, int]:
    """
    Extract every member of the zip whose name starts with `prefix` into server_root.

    For .jar files inside the `mods/` subdirectory of the override scope, we do
    a Modrinth sha1 lookup and SKIP extracting if the mod is client-only —
    same logic as the manifest-files path, so override-bundled CF mods don't
    sneak past our classifier.

    Returns (files_extracted_total, override_mods_installed, override_mods_skipped).
    """
    server_root_abs = os.path.abspath(server_root)
    total_extracted = mods_installed = mods_skipped = 0
    with zipfile.ZipFile(archive_path) as zf:
        for member in zf.namelist():
            if not member.startswith(prefix) or member.endswith("/"):
                continue
            rel = member[len(prefix):]
            target = os.path.abspath(os.path.join(server_root_abs, rel))
            # zip-slip defense
            if not target.startswith(server_root_abs + os.sep):
                continue

            # Is this a mod jar? If so, classify and maybe skip.
            rel_posix = rel.replace("\\", "/").lower()
            is_mod_jar = rel_posix.startswith("mods/") and rel_posix.endswith(".jar")
            if is_mod_jar:
                if _override_jar_is_client_only(zf, member):
                    mods_skipped += 1
                    continue
                mods_installed += 1

            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                while True:
                    chunk = src.read(65536)
                    if not chunk:
                        break
                    dst.write(chunk)
            total_extracted += 1
    return total_extracted, mods_installed, mods_skipped


def _override_jar_is_client_only(zf: zipfile.ZipFile, member: str) -> bool:
    """
    Resolve an override mod jar's client/server compatibility via Modrinth,
    using THREE fallbacks (cheapest/most-precise first):

    1. sha1 hash lookup → version → project (works when this exact jar is
       on Modrinth).
    2. mod ID slug lookup (catches CF-uploaded mods whose Modrinth slug
       equals their modId, e.g. 'iris' / 'catalogue' style).
    3. displayName exact-title search (catches mods whose Modrinth slug is
       kebab-case but jar uses no-separator modId — e.g. modId='citresewn'
       but Modrinth slug='cit-resewn', and the displayName is 'CIT Resewn').

    Returns False on any error or unresolvable — we'd rather install an
    unknown mod than silently drop user content.
    """
    try:
        with zf.open(member) as f:
            jar_bytes = f.read()
    except (zipfile.BadZipFile, OSError, KeyError):
        return False

    # Fallback 1: sha1 lookup
    sha1 = hashlib.sha1(jar_bytes).hexdigest()
    info = lookup_mod_by_sha1(sha1)

    if info is None:
        metadata = _extract_mod_metadata_from_jar_bytes(jar_bytes)
        # Fallback 2: try each mod ID as a slug
        for mod_id, _disp in metadata:
            info = _lookup_modrinth_project_by_slug(mod_id)
            if info is not None:
                break
        # Fallback 3: try each displayName as an exact-title search
        if info is None:
            seen_titles = set()
            for _mid, disp in metadata:
                if not disp or disp in seen_titles:
                    continue
                seen_titles.add(disp)
                info = _lookup_modrinth_project_by_exact_title(disp)
                if info is not None:
                    break

    return classify_mod(info) == "client_only"


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
                        text = jar.read(path).decode("utf-8", errors="replace")
                    except Exception:
                        continue
                    out.extend(_parse_forge_mods_toml(text))
            # Fabric / Quilt JSON
            for path in ("fabric.mod.json", "quilt.mod.json"):
                if path in members:
                    try:
                        data = json.loads(jar.read(path).decode("utf-8", errors="replace"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    mid = data.get("id")
                    if isinstance(mid, str):
                        out.append((mid.lower(), data.get("name") if isinstance(data.get("name"), str) else None))
                    qid = (data.get("quilt_loader") or {}).get("id")
                    if isinstance(qid, str):
                        out.append((qid.lower(),
                                    ((data.get("quilt_loader") or {}).get("metadata") or {}).get("name")))
    except (zipfile.BadZipFile, OSError):
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


def _lookup_modrinth_project_by_slug(slug: str, timeout: float = 8.0) -> Optional[ModInfo]:
    """GET /v2/project/{slug}. Modrinth accepts mod IDs as slug interchangeably for many projects."""
    try:
        r = requests.get(f"{_API}/v2/project/{slug}",
                         headers={"User-Agent": _USER_AGENT}, timeout=timeout)
        if r.status_code != 200:
            return None
        d = r.json()
        return ModInfo(
            project_id=d.get("id", slug),
            project_title=d.get("title", slug),
            client_side=d.get("client_side", "unknown"),
            server_side=d.get("server_side", "unknown"),
        )
    except (requests.RequestException, ValueError):
        return None


def _lookup_modrinth_project_by_exact_title(title: str, timeout: float = 8.0) -> Optional[ModInfo]:
    """
    Search Modrinth and accept ONLY if a hit's title is an exact case-insensitive
    match for `title`. This is the safety net for mods whose modId differs from
    their Modrinth slug (e.g. modId='citresewn' but slug='cit-resewn').

    Exact-match guard prevents "Catalogue" from being matched against
    "Mandela Catalogue" etc.
    """
    if not title:
        return None
    try:
        r = requests.get(f"{_API}/v2/search",
                         params={"query": title, "limit": 5},
                         headers={"User-Agent": _USER_AGENT}, timeout=timeout)
        if r.status_code != 200:
            return None
        target = title.strip().lower()
        for h in r.json().get("hits", []):
            if h.get("title", "").strip().lower() == target:
                return ModInfo(
                    project_id=h.get("project_id", ""),
                    project_title=h.get("title", title),
                    client_side=h.get("client_side", "unknown"),
                    server_side=h.get("server_side", "unknown"),
                )
    except (requests.RequestException, ValueError):
        return None
    return None
