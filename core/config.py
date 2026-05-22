"""
Persistent user settings for HMSL — currently just the CurseForge API key
and a few related preferences. Lives next to instance_registry data at
~/.hmsl/config.json.

The schema is intentionally a flat dict so future settings can be added
without migration overhead. Reads always degrade to defaults on any error
(missing file, bad JSON, unwritable disk) — never raises out of this module.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

# Also checked: env var CF_API_KEY (case-insensitive). Lets you run HMSL
# without writing your key to disk if you'd rather pass it per-session.
_ENV_KEY_NAMES = ("HMSL_CURSEFORGE_API_KEY", "CF_API_KEY", "CURSEFORGE_API_KEY")


def default_config_path() -> str:
    return str(Path.home() / ".hmsl" / "config.json")


def load(config_path: Optional[str] = None) -> dict:
    """Return the config dict; missing or malformed file ⇒ empty dict."""
    p = config_path or default_config_path()
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict, config_path: Optional[str] = None) -> None:
    """Atomically replace the config file."""
    p = config_path or default_config_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    dirpath = os.path.dirname(p) or "."
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=dirpath)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise


def get(key: str, default: Any = None, config_path: Optional[str] = None) -> Any:
    return load(config_path).get(key, default)


def set_value(key: str, value: Any, config_path: Optional[str] = None) -> None:
    data = load(config_path)
    data[key] = value
    save(data, config_path)


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
