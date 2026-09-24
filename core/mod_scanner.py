"""
Scan a server's mod directory and classify each jar via Modrinth hash lookup.

The classification has three useful buckets:
  - client_only: definitely (or effectively) client-side; remove for server use
  - server_ok:   compatible with server
  - unknown:     not on Modrinth, or network failed — leave alone, let user decide

GUI runs scan_server_mods on a background thread and renders progress via the
optional progress_callback(current, total, current_filename).
"""
from __future__ import annotations

import hashlib
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import requests


_MODRINTH_API = "https://api.modrinth.com"
_USER_AGENT = "HMSL/0.1 (https://github.com/hmsl) mod-scanner"

# Parallel lookups: each jar costs two small HTTP round trips, so a few
# workers turn a 200-mod pack from minutes into seconds.
_LOOKUP_WORKERS = 8
# After this many network failures in one scan, stop hitting the network.
_MAX_NET_ERRORS = 2


@dataclass
class ModInfo:
    project_id: str
    project_title: str
    client_side: str  # "required" | "optional" | "unsupported" | "unknown"
    server_side: str  # same domain


@dataclass
class ScanEntry:
    file_path: str
    file_name: str
    status: str       # "client_only" | "server_ok" | "unknown" | "error"
    mod_info: Optional[ModInfo] = None
    error_message: Optional[str] = None


@dataclass
class ScanReport:
    server_path: str
    mods_dir: str          # empty string if no mods/ or plugins/ found
    entries: List[ScanEntry] = field(default_factory=list)

    def client_only(self) -> List[ScanEntry]:
        return [e for e in self.entries if e.status == "client_only"]

    def unknown(self) -> List[ScanEntry]:
        return [e for e in self.entries if e.status == "unknown"]

    def server_ok(self) -> List[ScanEntry]:
        return [e for e in self.entries if e.status == "server_ok"]

    def errors(self) -> List[ScanEntry]:
        return [e for e in self.entries if e.status == "error"]


def compute_jar_sha1(file_path: str, chunk_size: int = 65536) -> str:
    """Stream-hash a file — mods can be 100MB+, never .read() whole."""
    h = hashlib.sha1()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- Modrinth lookup ----------

_tls = threading.local()
_project_cache: Dict[str, dict] = {}
_project_cache_lock = threading.Lock()


def _session() -> requests.Session:
    """One keep-alive session per thread (requests.Session isn't thread-safe)."""
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = _USER_AGENT
        _tls.session = s
    return s


def _lookup_sha1(sha1: str, timeout: float = 10.0) -> Tuple[Optional[ModInfo], bool]:
    """Returns (info, network_error). info is None on 404 / network error / bad shape."""
    s = _session()
    try:
        r = s.get(
            f"{_MODRINTH_API}/v2/version_file/{sha1}",
            params={"algorithm": "sha1"}, timeout=timeout,
        )
        if r.status_code == 404:
            return None, False
        r.raise_for_status()
        version = r.json()
        project_id = version.get("project_id") if isinstance(version, dict) else None
        if not project_id:
            return None, False

        with _project_cache_lock:
            project = _project_cache.get(project_id)
        if project is None:
            r = s.get(f"{_MODRINTH_API}/v2/project/{project_id}", timeout=timeout)
            r.raise_for_status()
            project = r.json()
            if not isinstance(project, dict):
                return None, False
            with _project_cache_lock:
                _project_cache[project_id] = project
        return ModInfo(
            project_id=project_id,
            project_title=project.get("title", project_id),
            client_side=project.get("client_side", "unknown"),
            server_side=project.get("server_side", "unknown"),
        ), False
    except (requests.ConnectionError, requests.Timeout):
        return None, True
    except (requests.RequestException, ValueError):
        return None, False


def lookup_mod_by_sha1(sha1: str, timeout: float = 10.0) -> Optional[ModInfo]:
    """
    Hash → Modrinth version → Modrinth project metadata.
    Returns None on 404, network error, or any unexpected response shape.
    """
    return _lookup_sha1(sha1, timeout)[0]


def classify_mod(info: Optional[ModInfo]) -> str:
    """
    Decide whether a mod is client-only based on Modrinth side metadata.

    - server_side == 'unsupported' → definitively client-only
    - server_side == 'optional' AND client_side == 'required' → effectively
      client-only (server can skip it, but client demands it ⇒ no server value)
    - otherwise → server-compatible
    - None info → unknown
    """
    if info is None:
        return "unknown"
    if info.server_side == "unsupported":
        return "client_only"
    if info.server_side == "optional" and info.client_side == "required":
        return "client_only"
    return "server_ok"


def find_mods_dir(server_path: str) -> Optional[str]:
    """Prefer mods/ (Forge/Fabric/NeoForge); fall back to plugins/ (Paper)."""
    for sub in ("mods", "plugins"):
        candidate = os.path.join(server_path, sub)
        if os.path.isdir(candidate):
            return candidate
    return None


ProgressCallback = Callable[[int, int, str], None]  # (current, total, filename)


def scan_server_mods(
    server_path: str,
    progress_callback: Optional[ProgressCallback] = None,
    lookup_fn: Callable[[str], Optional[ModInfo]] = lookup_mod_by_sha1,
    max_workers: int = _LOOKUP_WORKERS,
) -> ScanReport:
    """
    Walk `server_path/{mods,plugins}/*.jar`, hash each, classify via Modrinth.
    `lookup_fn` is injectable so tests can avoid network entirely.

    Jars are hashed + looked up on a small thread pool; entries keep the
    sorted-by-name order, and progress_callback is called from THIS thread
    with current = 1..total as jars finish.
    """
    mods_dir = find_mods_dir(server_path)
    if not mods_dir:
        return ScanReport(server_path=server_path, mods_dir="")

    try:
        jar_names = sorted(
            f for f in os.listdir(mods_dir)
            if f.lower().endswith(".jar")
            and os.path.isfile(os.path.join(mods_dir, f))
        )
    except OSError:
        return ScanReport(server_path=server_path, mods_dir=mods_dir)

    # Default lookup: stop hitting the network once it's clearly down, so an
    # offline scan doesn't pay a connect timeout for every jar.
    net_errors = [0]
    net_lock = threading.Lock()
    if lookup_fn is lookup_mod_by_sha1:
        def lookup(sha1: str) -> Optional[ModInfo]:
            with net_lock:
                if net_errors[0] >= _MAX_NET_ERRORS:
                    return None
            info, net_err = _lookup_sha1(sha1)
            if net_err:
                with net_lock:
                    net_errors[0] += 1
            return info
    else:
        lookup = lookup_fn

    def work(name: str) -> ScanEntry:
        path = os.path.join(mods_dir, name)
        try:
            sha1 = compute_jar_sha1(path)
        except OSError as e:
            return ScanEntry(path, name, status="error", error_message=str(e))
        try:
            info = lookup(sha1)
        except Exception:  # an injected lookup shouldn't kill the whole scan
            info = None
        return ScanEntry(path, name, status=classify_mod(info), mod_info=info)

    total = len(jar_names)
    results: List[Optional[ScanEntry]] = [None] * total
    if total:
        workers = max(1, min(max_workers, total))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="hmsl-modscan") as pool:
            futures = {pool.submit(work, name): i for i, name in enumerate(jar_names)}
            done = 0
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    results[i] = fut.result()
                except Exception as e:
                    name = jar_names[i]
                    results[i] = ScanEntry(os.path.join(mods_dir, name), name,
                                           status="error", error_message=str(e))
                done += 1
                if progress_callback:
                    progress_callback(done, total, jar_names[i])

    entries = [e for e in results if e is not None]
    return ScanReport(server_path=server_path, mods_dir=mods_dir, entries=entries)


# ---------- disabling ----------

def _fs(p: str) -> str:
    """Windows: add the \\\\?\\ prefix to long paths so rename/makedirs work
    past MAX_PATH even when LongPathsEnabled is off. No-op elsewhere."""
    if sys.platform != "win32":
        return p
    p = os.path.abspath(p)
    if len(p) < 240 or p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def _describe_move_error(e: OSError) -> str:
    winerr = getattr(e, "winerror", None)
    if isinstance(e, PermissionError) or winerr in (5, 32, 33):
        if sys.platform == "win32":
            return "文件被占用，无法移动（服务器可能正在运行——Windows 会锁定已加载的模组 jar，请先停止服务器再禁用）"
        return f"没有权限移动该文件：{e.strerror or e}"
    if isinstance(e, FileNotFoundError):
        return "文件不存在或路径过长，无法移动"
    return f"移动失败：{e.strerror or e}"


def disable_mods_detailed(entries: List[ScanEntry], mods_dir: str) -> Tuple[int, List[Tuple[str, str]]]:
    """
    Move each entry's jar to `mods_dir/.disabled/` (created on demand).
    On filename collision, appends _1, _2, ... so the original disabled file
    is never overwritten.

    Returns (moved_count, [(file_name, error_message), ...]) — every entry that
    was not moved is listed with a Chinese reason (locked by a running server,
    missing, path too long, ...).

    This is intentionally REVERSIBLE — user can restore by moving back.
    """
    failed: List[Tuple[str, str]] = []
    disabled_dir = os.path.join(mods_dir, ".disabled")
    try:
        os.makedirs(_fs(disabled_dir), exist_ok=True)
    except OSError as e:
        msg = f"无法创建 .disabled 目录：{e.strerror or e}"
        return 0, [(entry.file_name, msg) for entry in entries]

    moved = 0
    for entry in entries:
        if not os.path.isfile(_fs(entry.file_path)):
            failed.append((entry.file_name, "文件不存在（可能已被移动或删除）"))
            continue
        target = os.path.join(disabled_dir, entry.file_name)
        suffix = 1
        while os.path.exists(_fs(target)):
            base, ext = os.path.splitext(entry.file_name)
            target = os.path.join(disabled_dir, f"{base}_{suffix}{ext}")
            suffix += 1
        try:
            os.rename(_fs(entry.file_path), _fs(target))
            moved += 1
        except OSError as e:
            failed.append((entry.file_name, _describe_move_error(e)))
    return moved, failed


def disable_mods(entries: List[ScanEntry], mods_dir: str) -> int:
    """
    Same as disable_mods_detailed but only returns the count of files
    successfully moved (kept for existing callers).
    """
    return disable_mods_detailed(entries, mods_dir)[0]
