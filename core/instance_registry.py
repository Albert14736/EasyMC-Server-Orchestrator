"""
Persistent registry of all server instances HMSL has created.

The version-management page merges results from two sources:
  1. Scanning the script's own directory (existing behavior)
  2. This registry — which lets users put servers under ANY directory
     (D drive, external disk, Desktop, etc.) and still manage them.

Storage: JSON file at ~/.hmsl/instances.json by default. Atomic writes
(write to .tmp + rename) so a crash mid-save can't corrupt the file.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional


SCHEMA_VERSION = 1


def default_registry_path() -> str:
    return str(Path.home() / ".hmsl" / "instances.json")


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
        return cls(
            name=d.get("name", ""),
            path=d.get("path", ""),
            loader=d.get("loader", ""),
            mc_version=d.get("mc_version", ""),
            created_at=d.get("created_at", ""),
        )


class InstanceRegistry:
    """Read/write a list of RegistryEntry to/from a JSON file."""

    def __init__(self, registry_path: Optional[str] = None):
        self.path = registry_path or default_registry_path()

    def load(self) -> List[RegistryEntry]:
        """Return all entries. Missing or malformed file → empty list."""
        if not os.path.isfile(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
        raw = data.get("instances", []) if isinstance(data, dict) else []
        return [RegistryEntry.from_dict(d) for d in raw if isinstance(d, dict)]

    def save(self, entries: List[RegistryEntry]) -> None:
        """Atomically replace the file."""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        payload = {
            "version": SCHEMA_VERSION,
            "instances": [e.to_dict() for e in entries],
        }
        # Write to tmp in same dir, then rename — rename is atomic on POSIX
        # and on Windows (since 3.3) when target doesn't exist or replace() is used.
        dirpath = os.path.dirname(self.path) or "."
        fd, tmp_path = tempfile.mkstemp(prefix=".instances-", suffix=".json", dir=dirpath)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def add(self, entry: RegistryEntry) -> None:
        """Add an entry. If one with the same absolute path exists, replace it."""
        target = os.path.abspath(entry.path)
        entry = RegistryEntry(
            name=entry.name, path=target, loader=entry.loader,
            mc_version=entry.mc_version, created_at=entry.created_at,
        )
        kept = [e for e in self.load() if os.path.abspath(e.path) != target]
        kept.append(entry)
        self.save(kept)

    def remove(self, path: str) -> bool:
        """Remove the entry matching `path` (by absolute path). Returns True if removed."""
        target = os.path.abspath(path)
        before = self.load()
        after = [e for e in before if os.path.abspath(e.path) != target]
        if len(after) == len(before):
            return False
        self.save(after)
        return True

    def live_entries(self) -> List[RegistryEntry]:
        """Entries whose path still exists on disk."""
        return [e for e in self.load() if os.path.isdir(e.path)]

    def prune_dead(self) -> int:
        """Drop entries whose path no longer exists. Returns count removed."""
        all_entries = self.load()
        live = [e for e in all_entries if os.path.isdir(e.path)]
        if len(live) == len(all_entries):
            return 0
        self.save(live)
        return len(all_entries) - len(live)
