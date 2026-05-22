"""
Headless tests for core.config. All paths use tmp_path so the real
~/.hmsl/config.json is never touched.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import config


def _path(tmp_path):
    return str(tmp_path / "subdir" / "config.json")


def test_load_missing_file_returns_empty(tmp_path):
    assert config.load(_path(tmp_path)) == {}


def test_load_malformed_json_returns_empty(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("not json {")
    assert config.load(str(p)) == {}


def test_load_non_dict_returns_empty(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps([1, 2, 3]))
    assert config.load(str(p)) == {}


def test_save_and_load_roundtrip(tmp_path):
    p = _path(tmp_path)
    config.save({"foo": "bar", "n": 42}, p)
    assert config.load(p) == {"foo": "bar", "n": 42}


def test_save_creates_parent_directory(tmp_path):
    p = str(tmp_path / "deep" / "nested" / "config.json")
    config.save({"x": 1}, p)
    assert os.path.isfile(p)


def test_set_value_persists(tmp_path):
    p = _path(tmp_path)
    config.set_value("alpha", "one", p)
    config.set_value("beta", "two", p)
    assert config.get("alpha", config_path=p) == "one"
    assert config.get("beta", config_path=p) == "two"


def test_get_returns_default_when_missing(tmp_path):
    p = _path(tmp_path)
    assert config.get("nope", default="fallback", config_path=p) == "fallback"


# ---------- CurseForge key resolution ----------

def test_curseforge_key_from_config_file(tmp_path, monkeypatch):
    # Clear env vars to prove file path is hit
    for k in ("HMSL_CURSEFORGE_API_KEY", "CF_API_KEY", "CURSEFORGE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    p = _path(tmp_path)
    config.set_curseforge_api_key("my-test-key-from-file", p)
    assert config.get_curseforge_api_key(p) == "my-test-key-from-file"


def test_curseforge_key_env_var_takes_precedence(tmp_path, monkeypatch):
    p = _path(tmp_path)
    config.set_curseforge_api_key("from-file", p)
    monkeypatch.setenv("HMSL_CURSEFORGE_API_KEY", "from-env")
    assert config.get_curseforge_api_key(p) == "from-env"


def test_curseforge_key_alternative_env_names(tmp_path, monkeypatch):
    p = _path(tmp_path)
    for k in ("HMSL_CURSEFORGE_API_KEY", "CF_API_KEY", "CURSEFORGE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CF_API_KEY", "via-cf")
    assert config.get_curseforge_api_key(p) == "via-cf"


def test_curseforge_key_returns_none_when_unset(tmp_path, monkeypatch):
    for k in ("HMSL_CURSEFORGE_API_KEY", "CF_API_KEY", "CURSEFORGE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert config.get_curseforge_api_key(_path(tmp_path)) is None


def test_curseforge_key_strips_whitespace(tmp_path, monkeypatch):
    p = _path(tmp_path)
    for k in ("HMSL_CURSEFORGE_API_KEY", "CF_API_KEY", "CURSEFORGE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    config.set_curseforge_api_key("  spaced-key  ", p)
    assert config.get_curseforge_api_key(p) == "spaced-key"


def test_curseforge_key_empty_string_treated_as_unset(tmp_path, monkeypatch):
    for k in ("HMSL_CURSEFORGE_API_KEY", "CF_API_KEY", "CURSEFORGE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    p = _path(tmp_path)
    config.set_curseforge_api_key("   ", p)
    assert config.get_curseforge_api_key(p) is None
