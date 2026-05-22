"""
Headless tests for core.modrinth_search.

All HTTP is mocked via monkeypatch — no network. We verify the facet payload,
parsing, sorting/picking logic, and that download_to streams to the right path.
"""
from __future__ import annotations

import json
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import modrinth_search as ms


# ---------- helpers ----------

class FakeResp:
    def __init__(self, payload=None, status_code=200, content=b""):
        self._payload = payload or {}
        self.status_code = status_code
        self._content = content

    def raise_for_status(self):
        if 400 <= self.status_code < 600:
            raise __import__("requests").HTTPError(f"status {self.status_code}")

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=65536):
        # yield in chunks of chunk_size
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i:i + chunk_size]


# ---------- search_mods ----------

def test_search_mods_builds_facets_for_mc_and_loader(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return FakeResp({"hits": [], "offset": 0, "total_hits": 0, "limit": 20})

    monkeypatch.setattr(ms.requests, "get", fake_get)
    ms.search_mods(query="sodium", mc_version="1.20.4", loader="Forge")

    assert captured["url"].endswith("/v2/search")
    facets = json.loads(captured["params"]["facets"])
    # AND of three groups, each a singleton OR
    assert ["project_type:mod"] in facets
    assert ["versions:1.20.4"] in facets
    assert ["categories:forge"] in facets  # lowercased
    assert captured["params"]["query"] == "sodium"


def test_search_mods_omits_optional_facets(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["params"] = params
        return FakeResp({"hits": [], "offset": 0, "total_hits": 0, "limit": 20})

    monkeypatch.setattr(ms.requests, "get", fake_get)
    ms.search_mods(query="utility")

    facets = json.loads(captured["params"]["facets"])
    assert ["project_type:mod"] in facets
    # No version filter, no loader filter
    assert not any(g[0].startswith("versions:") for g in facets)
    assert not any(g[0].startswith("categories:") for g in facets)


def test_search_mods_parses_hits(monkeypatch):
    payload = {
        "hits": [
            {
                "project_id": "abc123", "slug": "sodium", "title": "Sodium",
                "description": "Fast rendering", "downloads": 1234567,
                "icon_url": "https://cdn/icon.png",
                "client_side": "required", "server_side": "unsupported",
                "project_type": "mod", "categories": ["optimization", "fabric"],
            },
        ],
        "offset": 0, "total_hits": 1, "limit": 20,
    }
    monkeypatch.setattr(ms.requests, "get",
                        lambda *a, **k: FakeResp(payload))

    page = ms.search_mods(query="sodium")
    assert len(page.hits) == 1
    h = page.hits[0]
    assert h.title == "Sodium"
    assert h.downloads == 1234567
    assert h.client_side == "required"
    assert h.is_client_only() is True  # required + unsupported = client only


def test_search_page_has_next(monkeypatch):
    monkeypatch.setattr(ms.requests, "get",
                        lambda *a, **k: FakeResp({"hits": [{}] * 20, "offset": 0, "total_hits": 50, "limit": 20}))
    page = ms.search_mods()
    assert page.has_next is True


def test_search_page_no_more_when_offset_at_end(monkeypatch):
    monkeypatch.setattr(ms.requests, "get",
                        lambda *a, **k: FakeResp({"hits": [{}] * 10, "offset": 40, "total_hits": 50, "limit": 20}))
    page = ms.search_mods(offset=40)
    assert page.has_next is False


def test_search_pagination_passes_offset(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["offset"] = params["offset"]
        return FakeResp({"hits": [], "offset": params["offset"], "total_hits": 0, "limit": 20})

    monkeypatch.setattr(ms.requests, "get", fake_get)
    ms.search_mods(offset=40, limit=20)
    assert captured["offset"] == 40


# ---------- get_project_versions ----------

def test_get_project_versions_sends_filters(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return FakeResp([])

    monkeypatch.setattr(ms.requests, "get", fake_get)
    ms.get_project_versions("AANobbMI", mc_version="1.20.4", loader="Fabric")

    assert captured["url"].endswith("/v2/project/AANobbMI/version")
    assert json.loads(captured["params"]["game_versions"]) == ["1.20.4"]
    assert json.loads(captured["params"]["loaders"]) == ["fabric"]


def test_get_project_versions_omits_filters_when_none(monkeypatch):
    captured = {}
    monkeypatch.setattr(ms.requests, "get",
                        lambda url, **k: (captured.setdefault("params", k.get("params")), FakeResp([]))[1])
    ms.get_project_versions("AANobbMI")
    assert "game_versions" not in (captured["params"] or {})
    assert "loaders" not in (captured["params"] or {})


def test_get_project_versions_parses_files(monkeypatch):
    raw = [{
        "id": "ver1", "name": "Sodium 0.5.8", "version_type": "release",
        "game_versions": ["1.20.4"], "loaders": ["fabric"],
        "files": [
            {"url": "https://cdn/sodium.jar", "filename": "sodium-0.5.8.jar",
             "primary": True, "hashes": {"sha1": "abc"}},
        ],
    }]
    monkeypatch.setattr(ms.requests, "get", lambda *a, **k: FakeResp(raw))
    versions = ms.get_project_versions("AANobbMI")
    assert len(versions) == 1
    v = versions[0]
    assert v.version_id == "ver1"
    assert v.version_type == "release"
    assert v.files[0]["filename"] == "sodium-0.5.8.jar"


# ---------- pick_best_version ----------

def _v(vtype, has_files=True):
    return ms.ProjectVersion(
        version_id=f"id-{vtype}", name=vtype, version_type=vtype,
        game_versions=["1.20.4"], loaders=["fabric"],
        files=[{"filename": f"f-{vtype}.jar", "primary": True}] if has_files else [],
    )


def test_pick_best_prefers_release_over_beta_over_alpha():
    versions = [_v("alpha"), _v("beta"), _v("release")]
    assert ms.pick_best_version(versions).version_type == "release"


def test_pick_best_falls_back_to_beta_when_no_release():
    versions = [_v("alpha"), _v("beta")]
    assert ms.pick_best_version(versions).version_type == "beta"


def test_pick_best_skips_versions_with_no_files():
    versions = [_v("release", has_files=False), _v("beta")]
    assert ms.pick_best_version(versions).version_type == "beta"


def test_pick_best_returns_none_when_all_empty():
    versions = [_v("release", has_files=False), _v("beta", has_files=False)]
    assert ms.pick_best_version(versions) is None


def test_pick_best_returns_none_for_empty_list():
    assert ms.pick_best_version([]) is None


# ---------- pick_primary_file ----------

def test_pick_primary_file_prefers_primary_flag():
    v = ms.ProjectVersion("id", "n", "release", [], [], files=[
        {"filename": "secondary.jar", "primary": False},
        {"filename": "main.jar", "primary": True},
    ])
    assert ms.pick_primary_file(v)["filename"] == "main.jar"


def test_pick_primary_file_falls_back_to_first_jar():
    v = ms.ProjectVersion("id", "n", "release", [], [], files=[
        {"filename": "notes.txt"},
        {"filename": "mod.jar"},
    ])
    assert ms.pick_primary_file(v)["filename"] == "mod.jar"


def test_pick_primary_file_returns_none_when_no_files():
    v = ms.ProjectVersion("id", "n", "release", [], [], files=[])
    assert ms.pick_primary_file(v) is None


# ---------- download_to ----------

def test_download_to_writes_streamed_bytes(monkeypatch, tmp_path):
    payload = b"FAKE_JAR_CONTENTS" * 100
    monkeypatch.setattr(ms.requests, "get",
                        lambda url, stream, headers, timeout: FakeResp(content=payload))

    target = ms.download_to("https://cdn/file.jar", str(tmp_path / "mods"), "thing.jar")
    assert target == str(tmp_path / "mods" / "thing.jar")
    assert (tmp_path / "mods" / "thing.jar").read_bytes() == payload


def test_download_to_creates_dest_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ms.requests, "get",
                        lambda *a, **k: FakeResp(content=b"x"))
    nested = tmp_path / "a" / "b" / "c"
    ms.download_to("https://cdn/f.jar", str(nested), "f.jar")
    assert (nested / "f.jar").exists()


# ---------- is_client_only routing through classify_mod ----------

@pytest.mark.parametrize("client,server,expected", [
    ("required", "unsupported", True),    # classic client-only
    ("required", "optional", True),       # tricky case (server can skip but client requires)
    ("optional", "required", False),      # server-only mod
    ("optional", "optional", False),      # both optional
])
def test_search_hit_is_client_only(client, server, expected):
    h = ms.ModSearchHit("p", "s", "t", "d", 0, None, client, server, "mod")
    assert h.is_client_only() == expected
