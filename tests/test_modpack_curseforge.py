"""
Headless tests for core.modpack.curseforge.

All CF API calls and downloads are mocked. We build small fake CF .zip
modpacks on tmp_path and verify the parse → fetch-metadata → classify →
download/skip pipeline. No real CF API key is used.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.modpack import detect_provider, import_modpack
from core.modpack.base import ModpackFile, ModpackManifest
from core.modpack.curseforge import CurseForgeProvider, _pick_loader
from core.modpack import curseforge as cf_mod


# ---------- helpers ----------

class FakeEnv:
    def get_java_cmd(self, version): return f"/fake/java{version}/bin/java"


class FakeInstaller:
    def __init__(self): self.calls = []
    def _ok(self, path):
        with open(os.path.join(path, "server.jar"), "w") as f: f.write("FAKE")
        return True
    def install_paper(self, p, v):        self.calls.append(("paper", v));    return self._ok(p)
    def install_fabric(self, p, v):       self.calls.append(("fabric", v));   return self._ok(p)
    def install_forge(self, p, v, jc):    self.calls.append(("forge", v));    return self._ok(p)
    def install_neoforge(self, p, v, jc): self.calls.append(("neoforge", v)); return self._ok(p)


class FakeDownloader:
    def sync(self, mod_dir, version, loader):
        os.makedirs(mod_dir, exist_ok=True)


class FakeResp:
    def __init__(self, payload=None, content=b"", status_code=200):
        self._payload = payload
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise __import__("requests").HTTPError(f"status {self.status_code}")

    def json(self):
        if self._payload is not None:
            return self._payload
        return json.loads(self.content.decode())

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]


def _make_manifest(loader_id="forge-14.23.5.2860", mc_version="1.12.2", files=None,
                   name="Test Pack", version="1.0", author="me", overrides="overrides"):
    return {
        "minecraft": {
            "version": mc_version,
            "modLoaders": [{"id": loader_id, "primary": True}],
        },
        "manifestType": "minecraftModpack",
        "manifestVersion": 1,
        "name": name, "version": version, "author": author,
        "overrides": overrides,
        "files": files or [],
    }


def _write_cf_zip(tmp_path, name, manifest_dict, extra_files=None):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest_dict))
        for arc_path, payload in (extra_files or {}).items():
            zf.writestr(arc_path, payload)
    return str(p)


# ---------- detect ----------

def test_detect_accepts_cf_zip(tmp_path):
    p = _write_cf_zip(tmp_path, "pack.zip", _make_manifest())
    assert CurseForgeProvider().detect(p) is True


def test_detect_rejects_mrpack_extension(tmp_path):
    """CF detect is .zip-only; .mrpack belongs to Modrinth."""
    p = _write_cf_zip(tmp_path, "pack.mrpack", _make_manifest())
    assert CurseForgeProvider().detect(p) is False


def test_detect_rejects_zip_without_manifesttype(tmp_path):
    """A random .zip with manifest.json that isn't a CF modpack."""
    p = tmp_path / "random.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"some": "other thing"}))
    assert CurseForgeProvider().detect(str(p)) is False


def test_registry_routes_cf_zip_to_cf_provider(tmp_path):
    p = _write_cf_zip(tmp_path, "p.zip", _make_manifest())
    provider = detect_provider(p)
    assert provider is not None
    assert provider.name == "curseforge"


def test_registry_routes_mrpack_to_modrinth_not_cf(tmp_path):
    """Provider ordering must not let CF claim .mrpack files."""
    p = tmp_path / "modrinth.mrpack"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("modrinth.index.json", json.dumps({"formatVersion": 1, "dependencies": {"minecraft": "1.20.4"}}))
    provider = detect_provider(str(p))
    assert provider is not None
    assert provider.name == "modrinth"


# ---------- parse ----------

def test_parse_extracts_name_version_loader(tmp_path):
    p = _write_cf_zip(tmp_path, "p.zip", _make_manifest(
        loader_id="forge-47.2.0", mc_version="1.20.1",
        name="Mercury", version="2.3", author="me",
        files=[{"projectID": 1, "fileID": 100, "required": True}]))
    m = CurseForgeProvider().parse(p)
    assert m.format == "curseforge"
    assert m.name == "Mercury"
    assert m.version == "2.3"
    assert m.mc_version == "1.20.1"
    assert m.loader == "Forge"
    assert m.loader_version == "47.2.0"
    assert len(m.files) == 1


@pytest.mark.parametrize("loader_id,expected_name,expected_ver", [
    ("forge-14.23.5.2860",      "Forge",    "14.23.5.2860"),
    ("neoforge-20.4.190",       "NeoForge", "20.4.190"),
    ("fabric-loader-0.15.6",    "Fabric",   "loader-0.15.6"),  # matches fabric- prefix
    ("fabric-0.14.21",          "Fabric",   "0.14.21"),
    ("quilt-loader-0.21.0",     "Fabric",   "loader-0.21.0"),  # Quilt mapped to Fabric
])
def test_loader_prefix_mapping(loader_id, expected_name, expected_ver):
    name, ver = _pick_loader([{"id": loader_id, "primary": True}])
    assert name == expected_name
    assert ver == expected_ver


def test_pick_loader_falls_back_to_paper_for_unknown():
    name, ver = _pick_loader([{"id": "bukkit-1.0", "primary": True}])
    assert name == "Paper"
    assert ver is None


def test_pick_loader_handles_empty_list():
    assert _pick_loader([]) == ("Paper", None)


def test_pick_loader_prefers_primary_true():
    """If multiple loaders given, primary=True wins."""
    name, _v = _pick_loader([
        {"id": "fabric-0.14.21", "primary": False},
        {"id": "forge-47.2.0", "primary": True},
    ])
    assert name == "Forge"


def test_parse_handles_malformed_file_entries(tmp_path):
    """Garbage entries (missing projectID/fileID) get silently dropped."""
    p = _write_cf_zip(tmp_path, "p.zip", _make_manifest(files=[
        {"projectID": 1, "fileID": 100, "required": True},        # valid
        {"projectID": "not-an-int", "fileID": 200},                # bad type
        {"projectID": 3},                                          # missing fileID
        {"random": "junk"},                                        # totally bogus
    ]))
    m = CurseForgeProvider().parse(p)
    assert len(m.files) == 1
    assert m.files[0]._cf_project_id == 1   # type: ignore[attr-defined]


def test_parse_raises_on_missing_manifest(tmp_path):
    p = tmp_path / "empty.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("readme.txt", "")
    with pytest.raises(ValueError):
        CurseForgeProvider().parse(str(p))


# ---------- apply (end-to-end mocked) ----------

def test_apply_without_key_still_extracts_overrides(tmp_path, monkeypatch):
    """No CF API key: files[] all fail, but overrides/ extracts normally."""
    monkeypatch.setattr(cf_mod, "get_curseforge_api_key", lambda: None)
    pack = _write_cf_zip(tmp_path, "p.zip", _make_manifest(
        files=[{"projectID": 1, "fileID": 100, "required": True}]),
        extra_files={"overrides/config/test.cfg": b"hello",
                     "overrides/scripts/x.zs": b"// comment"})

    result = import_modpack(
        archive_path=pack, server_name="srv", parent_dir=str(tmp_path),
        env_manager=FakeEnv(), installer=FakeInstaller(), downloader=FakeDownloader(),
    )

    assert not result.success
    assert result.files_failed == 1   # CF file couldn't download
    assert result.files_installed >= 0  # overrides do go in
    server = tmp_path / "srv"
    assert (server / "config" / "test.cfg").read_bytes() == b"hello"
    assert (server / "scripts" / "x.zs").exists()
    assert "curseforge_api_key" in result.error


def test_apply_with_key_downloads_and_classifies(tmp_path, monkeypatch):
    """Happy path with key: CF metadata fetched, mod-classID files downloaded,
    resource-pack-classID files skipped, mod jars sha1-classified vs Modrinth."""

    pack = _write_cf_zip(tmp_path, "p.zip", _make_manifest(
        files=[
            {"projectID": 1, "fileID": 101, "required": True},  # mod (server-ok)
            {"projectID": 2, "fileID": 102, "required": True},  # mod (client-only via Modrinth)
            {"projectID": 3, "fileID": 103, "required": True},  # resourcepack → skip
            {"projectID": 4, "fileID": 104, "required": True},  # no downloadUrl → fail
        ]))

    monkeypatch.setattr(cf_mod, "get_curseforge_api_key", lambda: "fake-key")

    server_jar_payload = b"FAKE_SERVER_MOD_JAR"
    server_sha1 = hashlib.sha1(server_jar_payload).hexdigest()
    client_jar_payload = b"FAKE_CLIENT_MOD_JAR"
    client_sha1 = hashlib.sha1(client_jar_payload).hexdigest()

    fake_files = {
        101: {"id": 101, "fileName": "lithium.jar",
              "downloadUrl": "https://cdn/lithium.jar", "fileLength": len(server_jar_payload),
              "hashes": [{"algo": 1, "value": server_sha1}]},
        102: {"id": 102, "fileName": "iris.jar",
              "downloadUrl": "https://cdn/iris.jar", "fileLength": len(client_jar_payload),
              "hashes": [{"algo": 1, "value": client_sha1}]},
        103: {"id": 103, "fileName": "fancy_pack.zip",
              "downloadUrl": "https://cdn/fancy_pack.zip", "fileLength": 100,
              "hashes": []},
        104: {"id": 104, "fileName": "optedout.jar",
              "downloadUrl": None,  # author opted out
              "hashes": []},
    }
    fake_mods = {
        1: {"id": 1, "name": "Lithium", "slug": "lithium", "classId": 6},
        2: {"id": 2, "name": "Iris", "slug": "iris", "classId": 6},
        3: {"id": 3, "name": "Fancy Pack", "slug": "fancy", "classId": 12},   # resourcepack
        4: {"id": 4, "name": "OptedOut", "slug": "optedout", "classId": 6},
    }

    monkeypatch.setattr(cf_mod, "_cf_batch_get_files",
                        lambda ids, key, timeout=15.0: fake_files)
    monkeypatch.setattr(cf_mod, "_cf_batch_get_mods",
                        lambda ids, key, timeout=15.0: fake_mods)

    # sha1 lookup: client_sha1 → client-only mod info, server_sha1 → server-ok
    from core.mod_scanner import ModInfo
    def fake_sha1_lookup(sha1, timeout=10.0):
        if sha1 == client_sha1:
            return ModInfo("iris", "Iris", client_side="required", server_side="unsupported")
        if sha1 == server_sha1:
            return ModInfo("lithium", "Lithium", client_side="optional", server_side="required")
        return None
    monkeypatch.setattr(cf_mod, "lookup_mod_by_sha1", fake_sha1_lookup)

    # Mock the actual file download (accept both stream-positional and
    # keyword-only calls — Modrinth slug fallback uses different signature).
    def fake_get(url, *args, **kwargs):
        if "lithium" in url:
            return FakeResp(content=server_jar_payload)
        if "iris" in url:
            return FakeResp(content=client_jar_payload)
        # Modrinth slug lookup will return 404-ish (no slug match) so we don't
        # confuse the classifier — just return a 404.
        if "modrinth.com" in url:
            return FakeResp(status_code=404)
        return FakeResp(content=b"x" * 100)
    monkeypatch.setattr(cf_mod.requests, "get", fake_get)

    result = import_modpack(
        archive_path=pack, server_name="from_cf", parent_dir=str(tmp_path),
        env_manager=FakeEnv(), installer=FakeInstaller(), downloader=FakeDownloader(),
    )

    # Lithium (server-ok mod) → installed
    server = tmp_path / "from_cf"
    assert (server / "mods" / "lithium.jar").read_bytes() == server_jar_payload
    # Iris (client-only mod) → NOT downloaded
    assert not (server / "mods" / "iris.jar").exists()
    # Resourcepack → NOT downloaded (path-based skip via classID 12)
    assert not (server / "resourcepacks" / "fancy_pack.zip").exists()
    # Opted-out mod → NOW downloaded via forgecdn CDN bypass (new behavior)
    assert (server / "mods" / "optedout.jar").exists()

    assert result.files_installed == 2       # lithium + optedout (via CDN bypass)
    assert result.files_skipped_client == 2  # iris + fancy_pack
    assert result.files_failed == 0          # CDN bypass saved optedout
    # Bypass surfaced honestly
    assert len(result.bypassed_mods) == 1
    assert result.bypassed_mods[0]["name"] == "OptedOut"


def test_forgecdn_url_construction():
    """fileID 5441212 → /files/5441/212/foo.jar (HMCL's deterministic split)."""
    from core.modpack.curseforge import _forgecdn_url
    assert _forgecdn_url(5441212, "foo.jar") == \
        "https://edge.forgecdn.net/files/5441/212/foo.jar"
    # Edge cases
    assert _forgecdn_url(5170009, "x.jar") == \
        "https://edge.forgecdn.net/files/5170/9/x.jar"
    assert _forgecdn_url(0, "a") is None
    assert _forgecdn_url(123, "") is None


def test_install_falls_back_to_forgecdn_when_opted_out(tmp_path, monkeypatch):
    """Author opted out (downloadUrl=null) — we should still get the file via CDN
    and report the bypass in ImportResult.bypassed_mods."""
    pack = _write_cf_zip(tmp_path, "p.zip", _make_manifest(
        files=[{"projectID": 99, "fileID": 5441212, "required": True}]))

    monkeypatch.setattr(cf_mod, "get_curseforge_api_key", lambda: "fake-key")

    monkeypatch.setattr(cf_mod, "_cf_batch_get_files", lambda ids, key, timeout=15.0: {
        5441212: {"id": 5441212, "fileName": "architecturecraft-3.109.jar",
                  "downloadUrl": None,  # opted out
                  "hashes": [{"algo": 1, "value": "ff" * 20}]},
    })
    monkeypatch.setattr(cf_mod, "_cf_batch_get_mods", lambda ids, key, timeout=15.0: {
        99: {"id": 99, "name": "ArchitectureCraft Spocel",
             "slug": "architecturecraft-spocel-version", "classId": 6},
    })
    # Modrinth sha1 lookup returns None (not on Modrinth)
    monkeypatch.setattr(cf_mod, "lookup_mod_by_sha1", lambda sha1, timeout=10.0: None)

    cdn_payload = b"FAKE_BYPASSED_JAR_CONTENTS"
    fetched_urls = []
    def fake_get(url, *args, **kwargs):
        fetched_urls.append(url)
        if "edge.forgecdn.net" in url:
            return FakeResp(content=cdn_payload)
        return FakeResp(status_code=404)
    monkeypatch.setattr(cf_mod.requests, "get", fake_get)

    result = import_modpack(
        archive_path=pack, server_name="srv", parent_dir=str(tmp_path),
        env_manager=FakeEnv(), installer=FakeInstaller(), downloader=FakeDownloader(),
    )

    # File landed via CDN
    server = tmp_path / "srv"
    assert (server / "mods" / "architecturecraft-3.109.jar").read_bytes() == cdn_payload
    # Counts: 1 installed, 0 failed
    assert result.files_installed == 1
    assert result.files_failed == 0
    # Bypass tracked
    assert len(result.bypassed_mods) == 1
    bp = result.bypassed_mods[0]
    assert bp["name"] == "ArchitectureCraft Spocel"
    assert "architecturecraft-spocel-version" in bp["cf_url"]
    # We actually hit the CDN URL
    assert any("edge.forgecdn.net" in u for u in fetched_urls)


def test_install_does_not_record_bypass_when_official_url_works(tmp_path, monkeypatch):
    """A normal mod (downloadUrl present) must NOT appear in bypassed_mods."""
    pack = _write_cf_zip(tmp_path, "p.zip", _make_manifest(
        files=[{"projectID": 1, "fileID": 100, "required": True}]))

    monkeypatch.setattr(cf_mod, "get_curseforge_api_key", lambda: "fake-key")
    monkeypatch.setattr(cf_mod, "_cf_batch_get_files", lambda ids, key, timeout=15.0: {
        100: {"id": 100, "fileName": "normal.jar",
              "downloadUrl": "https://cdn/normal.jar", "hashes": []},
    })
    monkeypatch.setattr(cf_mod, "_cf_batch_get_mods", lambda ids, key, timeout=15.0: {
        1: {"id": 1, "name": "Normal Mod", "slug": "normal", "classId": 6},
    })
    monkeypatch.setattr(cf_mod, "lookup_mod_by_sha1", lambda sha1, timeout=10.0: None)
    monkeypatch.setattr(cf_mod.requests, "get",
                        lambda url, *a, **k: FakeResp(content=b"normal"))

    result = import_modpack(
        archive_path=pack, server_name="srv", parent_dir=str(tmp_path),
        env_manager=FakeEnv(), installer=FakeInstaller(), downloader=FakeDownloader(),
    )
    assert result.files_installed == 1
    assert result.bypassed_mods == []  # No bypass recorded for normal downloads


def test_install_fails_when_both_official_and_cdn_fail(tmp_path, monkeypatch):
    """Edge case: CF gave us a URL but it's dead AND CDN refuses too → fail."""
    pack = _write_cf_zip(tmp_path, "p.zip", _make_manifest(
        files=[{"projectID": 7, "fileID": 9999999, "required": True}]))

    monkeypatch.setattr(cf_mod, "get_curseforge_api_key", lambda: "fake-key")
    monkeypatch.setattr(cf_mod, "_cf_batch_get_files", lambda ids, key, timeout=15.0: {
        9999999: {"id": 9999999, "fileName": "ghost.jar",
                  "downloadUrl": "https://cdn/ghost.jar", "hashes": []},
    })
    monkeypatch.setattr(cf_mod, "_cf_batch_get_mods", lambda ids, key, timeout=15.0: {
        7: {"id": 7, "name": "Ghost", "slug": "ghost", "classId": 6},
    })
    monkeypatch.setattr(cf_mod, "lookup_mod_by_sha1", lambda sha1, timeout=10.0: None)
    monkeypatch.setattr(cf_mod.requests, "get",
                        lambda url, *a, **k: FakeResp(status_code=404))

    result = import_modpack(
        archive_path=pack, server_name="srv", parent_dir=str(tmp_path),
        env_manager=FakeEnv(), installer=FakeInstaller(), downloader=FakeDownloader(),
    )
    assert result.files_failed == 1
    assert result.files_installed == 0
    assert result.bypassed_mods == []


def test_apply_uses_custom_override_dir_from_manifest(tmp_path, monkeypatch):
    """manifest.overrides can name a non-default directory."""
    monkeypatch.setattr(cf_mod, "get_curseforge_api_key", lambda: None)
    pack = _write_cf_zip(tmp_path, "p.zip",
                          _make_manifest(files=[], overrides="my_overrides"),
                          extra_files={"my_overrides/config/x.cfg": b"yo"})
    result = import_modpack(
        archive_path=pack, server_name="srv", parent_dir=str(tmp_path),
        env_manager=FakeEnv(), installer=FakeInstaller(), downloader=FakeDownloader(),
    )
    assert (tmp_path / "srv" / "config" / "x.cfg").read_bytes() == b"yo"
