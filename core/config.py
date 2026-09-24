"""
Persistent user settings for HMSL — currently just the CurseForge API key
and a few related preferences. Lives next to instance_registry data at
~/.hmsl/config.json.

The schema is intentionally a flat dict so future settings can be added
without migration overhead. Reads always degrade to defaults on any error
(missing file, bad JSON, unwritable disk) — never raises out of this module.

Windows: users hand-edit this file (the CurseForge key has no GUI yet), so
reads accept UTF-8 with a BOM (Notepad, PowerShell `-Encoding utf8`) and
UTF-16 (PowerShell 5.1 `>` / Out-File). A file that can't be parsed is backed
up to config.json.corrupt-<ts> before set_value() writes over it.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# Also checked: env var CF_API_KEY (case-insensitive). Lets you run HMSL
# without writing your key to disk if you'd rather pass it per-session.
_ENV_KEY_NAMES = ("HMSL_CURSEFORGE_API_KEY", "CF_API_KEY", "CURSEFORGE_API_KEY")

_LOCK = threading.RLock()


def default_config_path() -> str:
    return str(Path.home() / ".hmsl" / "config.json")


class _Corrupt(Exception):
    pass


def _load_strict(p: str) -> dict:
    """Missing → {}; unreadable/unparseable → _Corrupt."""
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise _Corrupt(str(e)) from e
    if not raw.strip():
        return {}
    try:
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            data = json.loads(raw.decode("utf-16"))
        else:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                if sys.platform != "win32":
                    raise
                # Notepad "ANSI" on a zh-CN system = cp936
                import locale
                text = raw.decode(locale.getpreferredencoding(False))
            data = json.loads(text)
    except (ValueError, LookupError) as e:  # JSONDecodeError / UnicodeDecodeError
        raise _Corrupt(str(e)) from e
    if not isinstance(data, dict):
        raise _Corrupt("顶层不是 JSON 对象")
    return data


def load(config_path: Optional[str] = None) -> dict:
    """Return the config dict; missing or malformed file ⇒ empty dict."""
    p = config_path or default_config_path()
    try:
        return _load_strict(p)
    except _Corrupt:
        return {}


def _replace_with_retry(src: str, dst: str, attempts: int = 10) -> None:
    """os.replace, retried on PermissionError (Windows sharing violation while
    another handle has dst open); if it stays locked, overwrite in place."""
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


def save(data: dict, config_path: Optional[str] = None) -> None:
    """Atomically replace the config file."""
    p = config_path or default_config_path()
    with _LOCK:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        dirpath = os.path.dirname(p) or "."
        fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=dirpath)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            _replace_with_retry(tmp, p)
        except Exception:
            try: os.unlink(tmp)
            except OSError: pass
            raise


def get(key: str, default: Any = None, config_path: Optional[str] = None) -> Any:
    return load(config_path).get(key, default)


def set_value(key: str, value: Any, config_path: Optional[str] = None) -> None:
    p = config_path or default_config_path()
    with _LOCK:
        try:
            data = _load_strict(p)
        except _Corrupt:
            # Keep the unreadable original instead of silently dropping its keys.
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            try:
                shutil.copy2(p, f"{p}.corrupt-{stamp}")
            except OSError:
                pass
            data = {}
        data[key] = value
        save(data, p)


# ---------- specific helpers ----------

def get_curseforge_api_key(config_path: Optional[str] = None) -> Optional[str]:
    """
    Resolution order:
      1. env var HMSL_CURSEFORGE_API_KEY / CF_API_KEY / CURSEFORGE_API_KEY
      2. config file "curseforge_api_key"
      3. None (no key configured — caller decides whether to fall back)
    """
    for name in _ENV_KEY_NAMES:
        v = os.environ.get(name)
        if v and v.strip():
            return v.strip()
    v = get("curseforge_api_key", None, config_path)
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def set_curseforge_api_key(key: str, config_path: Optional[str] = None) -> None:
    set_value("curseforge_api_key", key.strip(), config_path)
