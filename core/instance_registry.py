"""
Persistent registry of all server instances HMSL has created.

The version-management page merges results from two sources:
  1. Scanning the script's own directory (existing behavior)
  2. This registry — which lets users put servers under ANY directory
     (D drive, external disk, Desktop, etc.) and still manage them.

Storage: JSON file at ~/.hmsl/instances.json by default. Atomic writes
(write to .tmp + rename) so a crash mid-save can't corrupt the file.

Windows notes:
  - Paths are compared with path_key() (case-insensitive, / vs \\, trailing
    slash, 8.3 short names) — never with raw strings.
  - os.replace() fails with a sharing violation while another handle (a
    second HMSL window, AV scanner, indexer) has the target open, so it is
    retried with a short backoff.
  - A file that exists but cannot be parsed (hand-edited, BOM, UTF-16 from
    PowerShell 5.1, truncated) is backed up to instances.json.corrupt-<ts>
    before anything is written over it.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional


SCHEMA_VERSION = 1

# One lock per process for load-modify-save (the GUI calls add() from worker
# threads while the main thread reads). Cross-process safety comes from the
# .lock file below.
_LOCK = threading.RLock()


def default_registry_path() -> str:
    return str(Path.home() / ".hmsl" / "instances.json")


def _is_network_path(p: str) -> bool:
    """Windows: UNC path (\\\\host\\share, \\\\?\\UNC\\...) or a mapped network
    drive. Touching such a path (realpath / isdir) blocks ~20 s per call when
    the NAS is switched off, so callers avoid it or bound it with a timeout.
    Only looks at the string and the drive type — never touches the network."""
    if sys.platform != "win32" or not p:
        return False
    s = str(p).replace("/", "\\")
    if s.startswith(("\\\\?\\", "\\\\.\\")):
        s = s[4:]
        if s[:4].upper() == "UNC\\":
            return True
    elif s.startswith("\\\\"):
        return True
    if len(s) >= 2 and s[1] == ":" and s[0].isalpha():
        try:
            import ctypes
            # DRIVE_REMOTE = 4. GetDriveTypeW asks the redirector, not the server.
            return ctypes.windll.kernel32.GetDriveTypeW(s[0] + ":\\") == 4
        except (OSError, AttributeError, ValueError):
            return False
    return False


def path_key(p: str) -> str:
    """Normalized key for comparing folder paths.

    abspath + realpath (expands 8.3 short names / true case on Windows where
    the path exists) + normcase (case-insensitive and / → \\ on Windows).
    Network paths (UNC / mapped drives) skip realpath: it blocks ~20 s when the
    server is unreachable, and add()/remove() compute a key for every entry.
    Only for comparing — don't display it. Returns "" for empty/invalid input.
    """
    if not p or not isinstance(p, (str, os.PathLike)):
        return ""
    try:
        p = os.path.abspath(os.path.expanduser(os.fspath(p)))
    except (TypeError, ValueError, OSError):
        return ""
    if not _is_network_path(p):
        try:
            p = os.path.realpath(p)
        except (OSError, ValueError, RuntimeError):
            pass
    return os.path.normcase(p)


# Network share roots ("\\\\host\\share" / "z:") whose last probe timed out,
# → time.monotonic() of that probe. Skipped for a while so every refresh of
# the version page doesn't wait again.
_UNREACHABLE: dict = {}
_UNREACHABLE_TTL = 60.0
_NET_PROBE_TIMEOUT = 4.0


def _share_root(p: str) -> str:
    return os.path.normcase(os.path.splitdrive(os.path.abspath(p))[0])


def _dir_states(paths: List[str]) -> List[Optional[bool]]:
    """os.path.isdir for each path. Local paths are checked directly; network
    paths in parallel daemon threads sharing one short deadline. None = could
    not tell in time (server off / very slow) — neither "exists" nor "gone"."""
    res: List[Optional[bool]] = [None] * len(paths)
    pending = []
    now = time.monotonic()
    for i, p in enumerate(paths):
        if not _is_network_path(p):
            try:
                res[i] = os.path.isdir(p)
            except (OSError, ValueError):
                res[i] = False
            continue
        root = _share_root(p)
        t = _UNREACHABLE.get(root)
        if t is not None and now - t < _UNREACHABLE_TTL:
            continue   # timed out a moment ago: stays None without waiting again
        pending.append((i, p, root))
    if not pending:
        return res

    done = {}

    def probe(i: int, p: str, root: str) -> None:
        try:
            ok = os.path.isdir(p)
        except (OSError, ValueError):
            ok = False
        done[i] = ok
        if ok:   # a late answer clears the "unreachable" mark for the next refresh
            _UNREACHABLE.pop(root, None)

    threads = []
    for i, p, root in pending:
        # daemon: a probe stuck on a dead server must not keep HMSL from exiting
        th = threading.Thread(target=probe, args=(i, p, root), daemon=True, name="hmsl-netdir")
        th.start()
        threads.append(th)
    deadline = time.monotonic() + _NET_PROBE_TIMEOUT
    for th in threads:
        th.join(max(0.0, deadline - time.monotonic()))
    for i, p, root in pending:
        if i in done:
            res[i] = done[i]
        else:
            _UNREACHABLE[root] = time.monotonic()
    return res


def _display_path(p: str) -> str:
    """Absolute path to store/show. On Windows use the on-disk long name and
    true case when that doesn't swap the drive (subst / mapped drives stay as
    the user typed them)."""
    ab = os.path.abspath(p)
    if sys.platform == "win32" and not _is_network_path(ab):
        try:
            real = os.path.realpath(ab)
            if real.startswith("\\\\?\\"):
                real = real[4:]
            if os.path.splitdrive(real)[0].lower() == os.path.splitdrive(ab)[0].lower():
                return real
        except (OSError, ValueError):
            pass
    return ab


def _decode_json_bytes(raw: bytes):
    """utf-8 (with or without BOM), UTF-16 with BOM (PowerShell 5.1 `>`), or —
    for a file hand-saved as ANSI on Windows — the locale code page (cp936)."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return json.loads(raw.decode("utf-16"))
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        if sys.platform != "win32":
            raise
        import locale
        text = raw.decode(locale.getpreferredencoding(False))
    return json.loads(text)


def _replace_with_retry(src: str, dst: str, attempts: int = 10) -> None:
    """os.replace, retried on PermissionError (Windows sharing violation while
    another process briefly has dst open). If dst stays locked against
    delete/rename (someone keeps it open), fall back to overwriting it in
    place — not atomic, but the handle usually still allows writing."""
    delay = 0.02
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                break
            time.sleep(delay)
            delay = min(delay * 2, 0.3)
    try:
        with open(src, "rb") as fin, open(dst, "r+b") as fout:
            data = fin.read()
            fout.seek(0)
            fout.write(data)
            fout.truncate()
            fout.flush()
            os.fsync(fout.fileno())
    except OSError:
        os.replace(src, dst)  # re-raise the real sharing-violation error
        return
    try:
        os.unlink(src)
    except OSError:
        pass


@contextmanager
def _interprocess_lock(target: str, timeout: float = 5.0):
    """Best-effort exclusive lock on <target>.lock so two HMSL processes don't
    lose each other's add(). Never blocks forever; on timeout it proceeds."""
    lock_path = target + ".lock"
    fh = None
    locked = False
    try:
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        fh = open(lock_path, "a+b")
        deadline = time.monotonic() + timeout
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
    except OSError:
        pass
    try:
        yield
    finally:
        if fh is not None:
            try:
                if locked:
                    if sys.platform == "win32":
                        import msvcrt
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            fh.close()


class RegistryCorruptError(ValueError):
    """instances.json exists but can't be read/parsed (`unreadable` = True when
    the file couldn't even be opened, as opposed to bad content)."""

    unreadable = False


def _as_str(v) -> str:
    return v if isinstance(v, str) else ""


@dataclass
class RegistryEntry:
    name: str
    path: str          # absolute server folder path
    loader: str = ""
    mc_version: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RegistryEntry":
        # Hand-edited files may contain null / numbers / lists — keep only strings.
        return cls(
            name=_as_str(d.get("name")),
            path=_as_str(d.get("path")),
            loader=_as_str(d.get("loader")),
            mc_version=_as_str(d.get("mc_version")),
            created_at=_as_str(d.get("created_at")),
        )


class InstanceRegistry:
    """Read/write a list of RegistryEntry to/from a JSON file."""

    def __init__(self, registry_path: Optional[str] = None):
        self.path = registry_path or default_registry_path()
        # Human-readable (Chinese) note about the last problem, e.g. a corrupt
        # file that was backed up. None when everything was fine.
        self.last_warning: Optional[str] = None

    # ---------- reading ----------

    def _load_strict(self) -> List[RegistryEntry]:
        """Missing file → []. Present but unreadable → RegistryCorruptError."""
        if not os.path.isfile(self.path):
            return []
        raw = None
        for i in range(5):
            try:
                with open(self.path, "rb") as f:
                    raw = f.read()
                break
            except PermissionError as e:   # AV / indexer briefly holding it
                if i == 4:
                    err = RegistryCorruptError(str(e))
                    err.unreadable = True
                    raise err from e
                time.sleep(0.05 * (i + 1))
            except OSError as e:
                err = RegistryCorruptError(str(e))
                err.unreadable = True
                raise err from e
        if not raw.strip():
            return []
        try:
            data = _decode_json_bytes(raw)
        except (ValueError, LookupError) as e:  # JSONDecodeError / UnicodeDecodeError
            raise RegistryCorruptError(str(e)) from e
        raw_list = data.get("instances", []) if isinstance(data, dict) else []
        if not isinstance(raw_list, list):
            return []
        entries = [RegistryEntry.from_dict(d) for d in raw_list if isinstance(d, dict)]
        return [e for e in entries if e.path.strip()]

    def load(self) -> List[RegistryEntry]:
        """Return all entries. Missing or malformed file → empty list (never raises)."""
        with _LOCK:
            try:
                return self._load_strict()
            except RegistryCorruptError as e:
                self.last_warning = f"实例注册表文件无法解析（{e}），已忽略：{self.path}"
                return []

    def _load_for_update(self) -> List[RegistryEntry]:
        """load() for read-modify-write: a corrupt file is backed up first so
        the following save() never silently destroys it."""
        try:
            return self._load_strict()
        except RegistryCorruptError as e:
            if getattr(e, "unreadable", False):
                # Couldn't even read it (locked / no permission): its content may be
                # fine, so never write over it.
                self.last_warning = f"无法读取实例注册表（{e}），本次未写入：{self.path}"
                raise
            backup = self._backup_corrupt()
            self.last_warning = (f"实例注册表文件已损坏（{e}），"
                                 f"原文件已备份为 {backup or '（备份失败）'}")
            if backup is None:
                # Can't preserve it — refuse to overwrite rather than lose data.
                raise
            return []

    def _backup_corrupt(self) -> Optional[str]:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"{self.path}.corrupt-{stamp}"
        n = 1
        while os.path.exists(backup):
            backup = f"{self.path}.corrupt-{stamp}-{n}"
            n += 1
        try:
            shutil.copy2(self.path, backup)
            return backup
        except OSError:
            return None

    # ---------- writing ----------

    def save(self, entries: List[RegistryEntry]) -> None:
        """Atomically replace the file."""
        with _LOCK:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            payload = {
                "version": SCHEMA_VERSION,
                "instances": [e.to_dict() for e in entries],
            }
            # Write to tmp in same dir, then os.replace — atomic on POSIX and
            # Windows, but on Windows it can hit a sharing violation while
            # another handle has the target open, hence the retry.
            dirpath = os.path.dirname(self.path) or "."
            fd, tmp_path = tempfile.mkstemp(prefix=".instances-", suffix=".json", dir=dirpath)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                _replace_with_retry(tmp_path, self.path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

    def add(self, entry: RegistryEntry) -> None:
        """Add an entry. If one with the same folder (path_key) exists, replace it."""
        target_key = path_key(entry.path)
        if not target_key:
            raise ValueError("实例路径不能为空")
        entry = RegistryEntry(
            name=entry.name, path=_display_path(entry.path), loader=entry.loader,
            mc_version=entry.mc_version, created_at=entry.created_at,
        )
        with _LOCK, _interprocess_lock(self.path):
            kept = [e for e in self._load_for_update() if path_key(e.path) != target_key]
            kept.append(entry)
            self.save(kept)

    def remove(self, path: str) -> bool:
        """Remove the entry matching `path` (same folder via path_key). Returns True if removed."""
        target_key = path_key(path)
        if not target_key:
            return False
        with _LOCK, _interprocess_lock(self.path):
            before = self._load_for_update()
            after = [e for e in before if path_key(e.path) != target_key]
            if len(after) == len(before):
                return False
            self.save(after)
            return True

    def live_entries(self) -> List[RegistryEntry]:
        """Entries whose path still exists on disk (one per folder, newest wins).
        Servers on an unreachable network share are left out for now (checked
        with a short timeout instead of ~20 s each) but stay in the file."""
        out: List[RegistryEntry] = []
        seen = {}
        entries = self.load()
        states = _dir_states([e.path for e in entries])
        for e, alive in zip(entries, states):
            if not alive:
                continue
            k = path_key(e.path)
            if k in seen:
                out[seen[k]] = e
            else:
                seen[k] = len(out)
                out.append(e)
        return out

    def prune_dead(self) -> int:
        """Drop entries whose path no longer exists. Returns count removed.
        A server on a network share is only dropped when the share itself
        answers and the folder is gone — a NAS that is switched off (or a
        disconnected mapped drive) doesn't make its servers "dead"."""
        with _LOCK, _interprocess_lock(self.path):
            all_entries = self._load_for_update()
            states = _dir_states([e.path for e in all_entries])
            net_missing = sorted({_share_root(e.path) for e, st in zip(all_entries, states)
                                  if st is False and _is_network_path(e.path)})
            root_ok = dict(zip(net_missing, _dir_states([r + "\\" for r in net_missing])))
            live = [e for e, st in zip(all_entries, states)
                    if st is not False
                    or (_is_network_path(e.path) and not root_ok.get(_share_root(e.path)))]
            if len(live) == len(all_entries):
                return 0
            self.save(live)
            return len(all_entries) - len(live)
