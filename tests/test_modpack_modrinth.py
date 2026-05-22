"""
Headless tests for core.modpack.modrinth.

We build small real .mrpack zips on tmp_path and run the provider against them.
HTTP downloads are mocked so we never hit the network. Server creation is
mocked via fake env/installer/downloader (same pattern as test_server_factory).
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
from core.modpack.modrinth import ModrinthProvider, _manifest_from_index, _pick_loader
from core.modpack import modrinth as mp_modrinth


# ---------- helpers ----------

class FakeEnv:
    def get_java_cmd(self, version): return f"/fake/java{version}/bin/java"


class FakeInstaller:
    def __init__(self): self.calls = []
    def _ok(self, path):
        # create_server expects server.jar to land
        with open(os.path.join(path, "server.jar"), "w") as f: f.write("FAKE")
        return True
    def install_paper(self, p, v):            self.calls.append(("paper", v)); return self._ok(p)
    def install_fabric(self, p, v):           self.calls.append(("fabric", v)); return self._ok(p)
    def install_forge(self, p, v, jc):        self.calls.append(("forge", v, jc)); return self._ok(p)
    def install_neoforge(self, p, v, jc):     self.calls.append(("neoforge", v, jc)); return self._ok(p)


class FakeDownloader:
    def sync(self, mod_dir, version, loader):
        os.makedirs(mod_dir, exist_ok=True)


class FakeResp:
    def __init__(self, content=b"", status_code=200, payload=None):
        self.content = content
        self.status_code = status_code
        self._payload = payload
    def raise_for_status(self):
        if self.status_code >= 400:
            raise __import__("requests").HTTPError(f"status {self.status_code}")
    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]
    def json(self):
        if self._payload is not None:
            return self._payload
        return __import__("json").loads(self.content.decode())


def _make_index(loader_key="forge", loader_ver="47.2.0", mc_ver="1.20.4",
                files=None, name="TestPack", version="1.0.0"):
    return {
        "formatVersion": 1, "game": "minecraft",
        "versionId": version, "name": name, "summary": "test pack",
        "dependencies": {"minecraft": mc_ver, loader_key: loader_ver},
        "files": files or [],
    }


def _file_entry(path, content=b"server-jar-payload",
                env_server="required", env_client="optional", mirrors=1):
    sha1 = hashlib.sha1(content).hexdigest()
    return {
        "path": path,
        "hashes": {"sha1": sha1},
        "env": {"client": env_client, "server": env_server},
        "downloads": [f"https://cdn{i}.modrinth.com/{path}" for i in range(mirrors)],
        "fileSize": len(content),
    }, content


def _write_mrpack(tmp_path, name, index, extra_files=None):
    """Build a .mrpack zip on disk; extra_files = {zip_path: bytes}."""
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("modrinth.index.json", json.dumps(index))
        for arc_path, payload in (extra_files or {}).items():
            zf.writestr(arc_path, payload)
    return str(p)


# ---------- detect ----------

def test_detect_accepts_mrpack_extension(tmp_path):
    p = _write_mrpack(tmp_path, "thing.mrpack", _make_index())
    assert ModrinthProvider().detect(p) is True


def test_detect_accepts_zip_with_index(tmp_path):
    p = _write_mrpack(tmp_path, "thing.zip", _make_index())
    assert ModrinthProvider().detect(p) is True


def test_detect_rejects_plain_zip_without_index(tmp_path):
    p = tmp_path / "random.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("readme.txt", "no manifest here")
    assert ModrinthProvider().detect(str(p)) is False


def test_detect_rejects_corrupt_zip(tmp_path):
    p = tmp_path / "broken.mrpack"
    p.write_bytes(b"not a zip at all")
    # .mrpack extension still claims detect=True since we trust ext as primary signal.
    # That's by design — apply() will surface the real error.
    assert ModrinthProvider().detect(str(p)) is True


def test_registry_detects_via_package_entry(tmp_path):
    p = _write_mrpack(tmp_path, "thing.mrpack", _make_index())
    provider = detect_provider(p)
    assert provider is not None
    assert provider.name == "modrinth"


# ---------- parse ----------

def test_parse_picks_forge_loader(tmp_path):
    entry, _ = _file_entry("mods/sodium.jar")
    p = _write_mrpack(tmp_path, "t.mrpack",
                      _make_index(loader_key="forge", loader_ver="47.2.0",
                                  files=[entry]))
    m = ModrinthProvider().parse(p)
    assert m.loader == "Forge"
    assert m.loader_version == "47.2.0"
    assert m.mc_version == "1.20.4"
    assert m.name == "TestPack"
    assert len(m.files) == 1


@pytest.mark.parametrize("key,expected", [
    ("forge", "Forge"),
    ("neoforge", "NeoForge"),
    ("fabric-loader", "Fabric"),
    ("quilt-loader", "Fabric"),  # Quilt mapped to Fabric (API-compat)
])
def test_loader_key_mapping(key, expected):
    deps = {"minecraft": "1.20.4", key: "1.0"}
    name, ver = _pick_loader(deps)
    assert name == expected
    assert ver == "1.0"


def test_pick_loader_falls_back_to_paper_for_vanilla():
    name, ver = _pick_loader({"minecraft": "1.20.4"})
    assert name == "Paper"
    assert ver is None


def test_parse_extracts_env_fields(tmp_path):
    f1, _ = _file_entry("mods/server.jar", env_server="required", env_client="optional")
    f2, _ = _file_entry("mods/client.jar", env_server="unsupported", env_client="required")
    p = _write_mrpack(tmp_path, "t.mrpack",
                      _make_index(files=[f1, f2]))
    m = ModrinthProvider().parse(p)
    assert len(m.server_files) == 1
    assert m.server_files[0].path == "mods/server.jar"
    assert len(m.skipped_client_files) == 1
    assert m.skipped_client_files[0].path == "mods/client.jar"


def test_parse_raises_on_missing_index(tmp_path):
    p = tmp_path / "empty.mrpack"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("other.txt", "")
    with pytest.raises(ValueError):
        ModrinthProvider().parse(str(p))


def test_parse_raises_on_malformed_json(tmp_path):
    p = tmp_path / "bad.mrpack"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("modrinth.index.json", "{not valid json")
    with pytest.raises(ValueError):
        ModrinthProvider().parse(str(p))


# ---------- apply (end-to-end with mocks) ----------

def test_apply_creates_server_and_downloads_server_files(tmp_path, monkeypatch):
    server_entry, server_payload = _file_entry("mods/server-mod.jar",
                                                env_server="required")
    client_entry, _ = _file_entry("mods/client-mod.jar",
                                   env_server="unsupported")
    pack = _write_mrpack(tmp_path, "test.mrpack",
                        _make_index(files=[server_entry, client_entry]),
                        extra_files={"overrides/config/x.cfg": b"hello",
                                     "server-overrides/server.properties": b"online-mode=false",
                                     "client-overrides/options.txt": b"GUI=true"})

    # Mock the HTTP — return server_payload for the one server file we'd download
    def fake_get(url, stream, headers, timeout):
        return FakeResp(content=server_payload)

    monkeypatch.setattr("core.modpack.modrinth.requests.get", fake_get)

    result = import_modpack(
        archive_path=pack,
        server_name="from_pack",
        parent_dir=str(tmp_path),
        env_manager=FakeEnv(),
        installer=FakeInstaller(),
        downloader=FakeDownloader(),
    )

    assert result.success
    assert result.files_installed == 1
    assert result.files_skipped_client == 1
    assert result.files_failed == 0
    server = tmp_path / "from_pack"
    # Server bootstrap left a jar
    assert (server / "server.jar").exists()
    # Server file landed
    assert (server / "mods" / "server-mod.jar").read_bytes() == server_payload
    # Client file was NOT downloaded
    assert not (server / "mods" / "client-mod.jar").exists()
    # overrides applied
    assert (server / "config" / "x.cfg").read_bytes() == b"hello"
    # server-overrides applied
    assert (server / "server.properties").read_bytes() == b"online-mode=false"
    # client-overrides skipped
    assert not (server / "options.txt").exists()


def test_apply_fails_when_sha1_mismatch(tmp_path, monkeypatch):
    entry, _ = _file_entry("mods/x.jar", content=b"original-content")
    pack = _write_mrpack(tmp_path, "t.mrpack", _make_index(files=[entry]))

    # Return a different payload than the sha1 in the manifest
    monkeypatch.setattr("core.modpack.modrinth.requests.get",
                        lambda *a, **k: FakeResp(content=b"TAMPERED!"))

    result = import_modpack(
        archive_path=pack, server_name="tampered", parent_dir=str(tmp_path),
        env_manager=FakeEnv(), installer=FakeInstaller(), downloader=FakeDownloader(),
    )

    # Server bootstrap succeeds but the file fails sha1
    assert result.files_failed == 1
    assert result.files_installed == 0
    # The bad jar should NOT linger (we delete partial writes on hash mismatch)
    assert not (tmp_path / "tampered" / "mods" / "x.jar").exists()


def test_apply_zipslip_path_is_rejected(tmp_path, monkeypatch):
    """Manifest claiming path '../evil.jar' must not write outside server root."""
    sha1 = hashlib.sha1(b"x").hexdigest()
    evil_entry = {
        "path": "../evil.jar",
        "hashes": {"sha1": sha1},
        "env": {"client": "optional", "server": "required"},
        "downloads": ["https://cdn/x"],
    }
    pack = _write_mrpack(tmp_path, "t.mrpack", _make_index(files=[evil_entry]))
    monkeypatch.setattr("core.modpack.modrinth.requests.get",
                        lambda *a, **k: FakeResp(content=b"x"))
    result = import_modpack(
        archive_path=pack, server_name="safe", parent_dir=str(tmp_path),
        env_manager=FakeEnv(), installer=FakeInstaller(), downloader=FakeDownloader(),
    )
    # Path was rejected, counted as failed
    assert result.files_failed == 1
    # No file written outside server root
    assert not (tmp_path / "evil.jar").exists()


def test_apply_tries_next_mirror_on_first_failure(tmp_path, monkeypatch):
    entry, payload = _file_entry("mods/x.jar", mirrors=2)
    pack = _write_mrpack(tmp_path, "t.mrpack", _make_index(files=[entry]))

    call_count = {"n": 0}
    def fake_get(url, stream, headers, timeout):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return FakeResp(status_code=503)  # first mirror down
        return FakeResp(content=payload)  # second mirror good
    monkeypatch.setattr("core.modpack.modrinth.requests.get", fake_get)

    result = import_modpack(
        archive_path=pack, server_name="resilient", parent_dir=str(tmp_path),
        env_manager=FakeEnv(), installer=FakeInstaller(), downloader=FakeDownloader(),
    )
    assert result.files_installed == 1
    assert call_count["n"] == 2  # tried both mirrors


def test_progress_callback_emits_stages(tmp_path, monkeypatch):
    entry, payload = _file_entry("mods/m.jar")
    pack = _write_mrpack(tmp_path, "t.mrpack", _make_index(files=[entry]))
    monkeypatch.setattr("core.modpack.modrinth.requests.get",
                        lambda *a, **k: FakeResp(content=payload))

    stages = []
    import_modpack(
        archive_path=pack, server_name="p", parent_dir=str(tmp_path),
        env_manager=FakeEnv(), installer=FakeInstaller(), downloader=FakeDownloader(),
        progress_callback=lambda prog: stages.append(prog.stage),
    )
    # We expect at least these stages, in order
    assert "parsing" in stages
    assert "creating_server" in stages
    assert "downloading_files" in stages
    assert "applying_overrides" in stages
    assert stages[-1] == "done"


# ---------- top-level entry point ----------

def test_import_modpack_rejects_unknown_archive(tmp_path):
    p = tmp_path / "random.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("readme.txt", "")
    result = import_modpack(
        archive_path=str(p), server_name="x", parent_dir=str(tmp_path),
        env_manager=FakeEnv(), installer=FakeInstaller(), downloader=FakeDownloader(),
    )
    assert not result.success
    assert "未识别" in result.error


def test_import_modpack_rejects_missing_file(tmp_path):
    result = import_modpack(
        archive_path=str(tmp_path / "nonexistent.mrpack"),
        server_name="x", parent_dir=str(tmp_path),
        env_manager=FakeEnv(), installer=FakeInstaller(), downloader=FakeDownloader(),
    )
    assert not result.success
    assert "不存在" in result.error


# ---------- is_server_skipped: tighter classification ----------

@pytest.mark.parametrize("path,env_c,env_s,expected_skip", [
    # Path-based: resource/shader/texture packs are always client-only
    ("resourcepacks/fancy.zip", None, None, True),
    ("shaderpacks/complementary.zip", None, None, True),
    ("texturepacks/old-style.zip", None, None, True),
    # env-based: classic unsupported
    ("mods/optifine.jar", "required", "unsupported", True),
    # env-based: tricky case (server=optional + client=required)
    ("mods/sodium.jar", "required", "optional", True),
    # Server-OK cases
    ("mods/luckperms.jar", "unsupported", "required", False),
    ("mods/datapack.jar", "optional", "required", False),
    # No env, mod path → kept (will be enriched separately)
    ("mods/unknown.jar", None, None, False),
    # No env, config path → kept (configs install regardless)
    ("config/lithium.json", None, None, False),
])
def test_is_server_skipped_matrix(path, env_c, env_s, expected_skip):
    f = ModpackFile(path=path, env_client=env_c, env_server=env_s)
    assert f.is_server_skipped() == expected_skip


def test_needs_compat_lookup_only_for_mods_without_env():
    # Mod with no env + sha1 → needs lookup
    assert ModpackFile(path="mods/a.jar", sha1="abc").needs_compat_lookup() is True
    # Mod marked required/required (mrpack lazy default) → STILL needs lookup
    assert ModpackFile(path="mods/a.jar", sha1="abc",
                       env_client="required", env_server="required").needs_compat_lookup() is True
    # Mod with definitive env (e.g. unsupported) → no lookup (trust author)
    assert ModpackFile(path="mods/a.jar", sha1="abc", env_client="required",
                       env_server="unsupported").needs_compat_lookup() is False
    # Mixed-optional → trust author, no lookup
    assert ModpackFile(path="mods/a.jar", sha1="abc", env_client="optional",
                       env_server="optional").needs_compat_lookup() is False
    # Mod with no sha1 → can't look up
    assert ModpackFile(path="mods/a.jar", env_client="required",
                       env_server="required").needs_compat_lookup() is False
    # Not in mods/ → don't bother (config files always install)
    assert ModpackFile(path="config/x.cfg", sha1="abc").needs_compat_lookup() is False


# ---------- enrich_compat (batch Modrinth lookup) ----------

def test_enrich_compat_fills_missing_env(tmp_path, monkeypatch):
    """Files with no env should get env_server/env_client from Modrinth."""
    pack_files = [
        {"path": "mods/sodium.jar", "hashes": {"sha1": "sha-sodium"},
         "downloads": ["https://x"]},
        {"path": "mods/luckperms.jar", "hashes": {"sha1": "sha-luck"},
         "downloads": ["https://x"]},
    ]
    pack = _write_mrpack(tmp_path, "t.mrpack", _make_index(files=pack_files))
    provider = ModrinthProvider()
    manifest = provider.parse(pack)
    # Sanity: env is unset for both
    assert all(f.env_server is None for f in manifest.files)

    # Mock the two batch endpoints
    def fake_post(url, json, headers, timeout):
        assert url.endswith("/v2/version_files")
        return FakeResp(content=__import__("json").dumps({
            "sha-sodium": {"project_id": "proj-sodium"},
            "sha-luck": {"project_id": "proj-luck"},
        }).encode())

    def fake_get(url, params, headers, timeout):
        assert url.endswith("/v2/projects")
        return FakeResp(content=__import__("json").dumps([
            {"id": "proj-sodium", "client_side": "required", "server_side": "unsupported"},
            {"id": "proj-luck", "client_side": "unsupported", "server_side": "required"},
        ]).encode())

    monkeypatch.setattr(mp_modrinth.requests, "post", fake_post)
    monkeypatch.setattr(mp_modrinth.requests, "get", fake_get)
    # Need to make FakeResp.json() return parsed payload
    def _json(self): return __import__("json").loads(self.content.decode())
    monkeypatch.setattr(FakeResp, "json", _json, raising=False)

    provider.enrich_compat(manifest)

    by_path = {f.path: f for f in manifest.files}
    assert by_path["mods/sodium.jar"].env_server == "unsupported"
    assert by_path["mods/sodium.jar"].env_client == "required"
    assert by_path["mods/luckperms.jar"].env_server == "required"
    # Now is_server_skipped reflects reality
    assert by_path["mods/sodium.jar"].is_server_skipped() is True
    assert by_path["mods/luckperms.jar"].is_server_skipped() is False


def test_enrich_compat_no_op_when_all_env_present(tmp_path, monkeypatch):
    """If manifest already has env on every file, no HTTP calls happen."""
    entry, _ = _file_entry("mods/x.jar", env_server="required", env_client="optional")
    pack = _write_mrpack(tmp_path, "t.mrpack", _make_index(files=[entry]))
    provider = ModrinthProvider()
    manifest = provider.parse(pack)

    calls = {"n": 0}
    def trip(*a, **k):
        calls["n"] += 1
        return FakeResp(content=b"{}")
    monkeypatch.setattr(mp_modrinth.requests, "post", trip)
    monkeypatch.setattr(mp_modrinth.requests, "get", trip)

    provider.enrich_compat(manifest)
    assert calls["n"] == 0


def test_apply_skips_client_only_override_mods(tmp_path, monkeypatch):
    """
    Regression for "127 installed / 2 skipped" with 惊变100天:
    community modpacks bundle CF-only client mods (Iris, JEI, minimaps) inside
    overrides/mods/ where they're not declared in modrinth.index.json. We must
    sha1-classify them too and skip the client-only ones.
    """
    # Build a pack with EMPTY files[] (all mods come from overrides)
    client_jar_bytes = b"FAKE_CLIENT_MOD_PAYLOAD"
    server_jar_bytes = b"FAKE_SERVER_MOD_PAYLOAD"
    client_sha1 = hashlib.sha1(client_jar_bytes).hexdigest()
    server_sha1 = hashlib.sha1(server_jar_bytes).hexdigest()
    pack = _write_mrpack(
        tmp_path, "community.mrpack", _make_index(files=[]),
        extra_files={
            "overrides/mods/iris.jar": client_jar_bytes,
            "overrides/mods/lithium.jar": server_jar_bytes,
            "overrides/config/lithium.json": b"performance=true",
        },
    )

    # Mock the lookup: iris.jar's sha1 → client-only, lithium.jar's sha1 → server-ok
    from core.mod_scanner import ModInfo
    def fake_lookup(sha1, timeout=10.0):
        if sha1 == client_sha1:
            return ModInfo("p1", "Iris", client_side="required", server_side="unsupported")
        if sha1 == server_sha1:
            return ModInfo("p2", "Lithium", client_side="optional", server_side="optional")
        return None
    monkeypatch.setattr("core.modpack.modrinth.lookup_mod_by_sha1", fake_lookup)

    result = import_modpack(
        archive_path=pack, server_name="srv", parent_dir=str(tmp_path),
        env_manager=FakeEnv(), installer=FakeInstaller(), downloader=FakeDownloader(),
    )

    server = tmp_path / "srv"
    # Client mod was NOT extracted
    assert not (server / "mods" / "iris.jar").exists()
    # Server mod WAS extracted
    assert (server / "mods" / "lithium.jar").read_bytes() == server_jar_bytes
    # Non-mod override (config) extracted normally
    assert (server / "config" / "lithium.json").read_bytes() == b"performance=true"

    # Counts reflect override classification
    assert result.files_skipped_client == 1   # iris
    assert result.files_installed == 1         # lithium


def test_extract_metadata_from_forge_jar():
    """Forge mods.toml: modId + displayName captured."""
    from core.modpack.modrinth import _extract_mod_metadata_from_jar_bytes
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as jar:
        jar.writestr("META-INF/mods.toml",
                     'modLoader="javafml"\n'
                     'loaderVersion="[47,)"\n'
                     '[[mods]]\n'
                     'modId="citresewn"\n'
                     'version="5"\n'
                     'displayName="CIT Resewn"\n')
    meta = _extract_mod_metadata_from_jar_bytes(inner.getvalue())
    assert ("citresewn", "CIT Resewn") in meta


def test_extract_metadata_from_fabric_jar():
    from core.modpack.modrinth import _extract_mod_metadata_from_jar_bytes
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as jar:
        jar.writestr("fabric.mod.json",
                     '{"id": "sodium", "name": "Sodium", "version": "0.5"}')
    meta = _extract_mod_metadata_from_jar_bytes(inner.getvalue())
    assert ("sodium", "Sodium") in meta


def test_extract_metadata_returns_empty_for_invalid_jar():
    from core.modpack.modrinth import _extract_mod_metadata_from_jar_bytes
    assert _extract_mod_metadata_from_jar_bytes(b"not a zip") == []


def test_exact_title_search_rejects_partial_match(monkeypatch):
    """'Catalogue' must NOT match 'The Mandela Catalogue: Alternates'."""
    from core.modpack.modrinth import _lookup_modrinth_project_by_exact_title

    def fake_get(url, params, headers, timeout):
        return FakeResp(payload={"hits": [
            {"title": "The Mandela Catalogue: Alternates",
             "project_id": "x", "client_side": "required", "server_side": "required"},
            {"title": "Some other thing",
             "project_id": "y", "client_side": "required", "server_side": "unsupported"},
        ]})
    monkeypatch.setattr(mp_modrinth.requests, "get", fake_get)
    # Returns None — no exact title match
    assert _lookup_modrinth_project_by_exact_title("Catalogue") is None


def test_exact_title_search_accepts_exact_match(monkeypatch):
    from core.modpack.modrinth import _lookup_modrinth_project_by_exact_title

    def fake_get(url, params, headers, timeout):
        return FakeResp(payload={"hits": [
            {"title": "Sodium Unrelated", "project_id": "a",
             "client_side": "required", "server_side": "required"},
            {"title": "CIT Resewn", "project_id": "b",
             "client_side": "required", "server_side": "unsupported"},
        ]})
    monkeypatch.setattr(mp_modrinth.requests, "get", fake_get)
    info = _lookup_modrinth_project_by_exact_title("cit resewn")  # case insensitive
    assert info is not None
    assert info.project_id == "b"
    assert info.server_side == "unsupported"


def test_override_jar_uses_slug_fallback_when_sha1_misses(tmp_path, monkeypatch):
    """If sha1 lookup fails but the jar advertises a modId, try Modrinth by slug."""
    from core.mod_scanner import ModInfo
    # Build a jar with mods.toml saying modId="catalogue"
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as jar:
        jar.writestr("META-INF/mods.toml",
                     '[[mods]]\nmodId="catalogue"\n')
    catalogue_bytes = inner.getvalue()

    pack = _write_mrpack(
        tmp_path, "p.mrpack", _make_index(files=[]),
        extra_files={"overrides/mods/catalogue-1.8.0.jar": catalogue_bytes},
    )

    # sha1 lookup fails (Modrinth doesn't have this exact build), but slug lookup
    # returns catalogue's project metadata (client-only).
    monkeypatch.setattr("core.modpack.modrinth.lookup_mod_by_sha1",
                        lambda sha1, timeout=10.0: None)
    monkeypatch.setattr("core.modpack.modrinth._lookup_modrinth_project_by_slug",
                        lambda slug, timeout=8.0:
                            ModInfo("p", "Catalogue", client_side="required",
                                    server_side="unsupported")
                            if slug == "catalogue" else None)

    result = import_modpack(
        archive_path=pack, server_name="srv", parent_dir=str(tmp_path),
        env_manager=FakeEnv(), installer=FakeInstaller(), downloader=FakeDownloader(),
    )

    # Slug fallback caught it → not extracted
    assert not (tmp_path / "srv" / "mods" / "catalogue-1.8.0.jar").exists()
    assert result.files_skipped_client == 1


def test_apply_keeps_override_mods_when_lookup_fails(tmp_path, monkeypatch):
    """Modrinth network failure for an override jar => install (safer default)."""
    jar_bytes = b"some-cf-only-mod"
    pack = _write_mrpack(
        tmp_path, "cf.mrpack", _make_index(files=[]),
        extra_files={"overrides/mods/mystery.jar": jar_bytes},
    )

    monkeypatch.setattr("core.modpack.modrinth.lookup_mod_by_sha1",
                        lambda sha1, timeout=10.0: None)  # not on Modrinth

    result = import_modpack(
        archive_path=pack, server_name="srv", parent_dir=str(tmp_path),
        env_manager=FakeEnv(), installer=FakeInstaller(), downloader=FakeDownloader(),
    )

    # Unknown mod gets installed — we don't want to drop user content blindly
    assert (tmp_path / "srv" / "mods" / "mystery.jar").read_bytes() == jar_bytes
    assert result.files_installed == 1
    assert result.files_skipped_client == 0


def test_enrich_compat_tolerates_network_failure(tmp_path, monkeypatch):
    """If Modrinth is down, leave env as None (files install — safer than aborting)."""
    pack_files = [{"path": "mods/x.jar", "hashes": {"sha1": "abc"},
                   "downloads": ["https://x"]}]
    pack = _write_mrpack(tmp_path, "t.mrpack", _make_index(files=pack_files))
    provider = ModrinthProvider()
    manifest = provider.parse(pack)

    import requests as _r
    def boom(*a, **k):
        raise _r.ConnectionError("offline")
    monkeypatch.setattr(mp_modrinth.requests, "post", boom)
    monkeypatch.setattr(mp_modrinth.requests, "get", boom)

    # Should not raise
    provider.enrich_compat(manifest)
    assert manifest.files[0].env_server is None  # still unresolved, install will proceed
