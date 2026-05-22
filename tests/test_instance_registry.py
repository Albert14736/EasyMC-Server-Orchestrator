"""
Headless tests for core.instance_registry.

All tests use tmp_path so they never touch the real ~/.hmsl/.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.instance_registry import InstanceRegistry, RegistryEntry


def _reg(tmp_path):
    return InstanceRegistry(str(tmp_path / "subdir" / "instances.json"))


def _entry(path, name="srv"):
    return RegistryEntry(name=name, path=str(path), loader="Paper", mc_version="1.20.4")


# ---------- load ----------

def test_load_missing_file_returns_empty(tmp_path):
    assert _reg(tmp_path).load() == []


def test_load_malformed_json_returns_empty(tmp_path):
    p = tmp_path / "instances.json"
    p.write_text("not json {{{")
    assert InstanceRegistry(str(p)).load() == []


def test_load_unexpected_shape_returns_empty(tmp_path):
    p = tmp_path / "instances.json"
    p.write_text(json.dumps(["just", "a", "list"]))
    assert InstanceRegistry(str(p)).load() == []


# ---------- add / save ----------

def test_add_persists_to_disk(tmp_path):
    reg = _reg(tmp_path)
    server = tmp_path / "alpha"; server.mkdir()
    reg.add(_entry(server, "alpha"))

    # Fresh instance reads the same file
    reg2 = InstanceRegistry(reg.path)
    entries = reg2.load()
    assert len(entries) == 1
    assert entries[0].name == "alpha"
    assert entries[0].path == str(server)
    assert entries[0].loader == "Paper"


def test_add_creates_parent_directory(tmp_path):
    """~/.hmsl/ might not exist yet on a fresh install."""
    nested = tmp_path / "deeply" / "nested" / "instances.json"
    reg = InstanceRegistry(str(nested))
    server = tmp_path / "s"; server.mkdir()
    reg.add(_entry(server))
    assert nested.is_file()


def test_add_duplicate_path_replaces(tmp_path):
    reg = _reg(tmp_path)
    server = tmp_path / "srv"; server.mkdir()
    reg.add(_entry(server, "old_name"))
    reg.add(_entry(server, "new_name"))  # same path
    entries = reg.load()
    assert len(entries) == 1
    assert entries[0].name == "new_name"


def test_add_stores_absolute_path(tmp_path, monkeypatch):
    """Even if caller passes a relative path, registry must store absolute."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rel_srv").mkdir()
    reg = _reg(tmp_path)
    reg.add(RegistryEntry(name="rel", path="rel_srv"))
    entries = reg.load()
    assert os.path.isabs(entries[0].path)


# ---------- remove ----------

def test_remove_existing(tmp_path):
    reg = _reg(tmp_path)
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    reg.add(_entry(a, "a"))
    reg.add(_entry(b, "b"))
    assert reg.remove(str(a)) is True
    remaining = [e.name for e in reg.load()]
    assert remaining == ["b"]


def test_remove_missing_returns_false(tmp_path):
    reg = _reg(tmp_path)
    assert reg.remove("/nope/nada") is False


# ---------- live_entries / prune_dead ----------

def test_live_entries_filters_dead_paths(tmp_path):
    reg = _reg(tmp_path)
    alive = tmp_path / "alive"; alive.mkdir()
    dead = tmp_path / "dead"; dead.mkdir()
    reg.add(_entry(alive, "alive"))
    reg.add(_entry(dead, "dead"))
    dead.rmdir()
    live = reg.live_entries()
    assert [e.name for e in live] == ["alive"]
    # load() still returns both — live_entries does not mutate file
    assert len(reg.load()) == 2


def test_prune_dead_removes_and_persists(tmp_path):
    reg = _reg(tmp_path)
    alive = tmp_path / "a"; alive.mkdir()
    dead = tmp_path / "d"; dead.mkdir()
    reg.add(_entry(alive, "a"))
    reg.add(_entry(dead, "d"))
    dead.rmdir()
    removed = reg.prune_dead()
    assert removed == 1
    assert [e.name for e in reg.load()] == ["a"]


def test_prune_dead_when_all_alive_returns_zero(tmp_path):
    reg = _reg(tmp_path)
    a = tmp_path / "a"; a.mkdir()
    reg.add(_entry(a, "a"))
    assert reg.prune_dead() == 0


# ---------- end-to-end ----------

def test_round_trip_unicode_paths(tmp_path):
    """Chinese folder names — must survive JSON round-trip."""
    reg = _reg(tmp_path)
    server = tmp_path / "中文服务器"; server.mkdir()
    reg.add(_entry(server, "我的世界"))
    entry = reg.load()[0]
    assert entry.name == "我的世界"
    assert entry.path == str(server)
