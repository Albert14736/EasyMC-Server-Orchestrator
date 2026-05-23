"""
Headless tests for the 4 secondary modpack providers added in Phase 3.3:
MultiMC, HMCL Native, HMCL Server, MCBBS.

Each test builds a minimal-but-valid zip on tmp_path. No network: HMCL
Server and MCBBS file downloads are stubbed via monkeypatch. Provider
registration order is also asserted so detection precedence is locked.
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

from core.modpack import PROVIDERS, detect_provider, import_modpack
from core.modpack.base import ModpackManifest
from core.modpack.hmcl_native import HMCLNativeProvider
from core.modpack.hmcl_server import HMCLServerProvider
from core.modpack.mcbbs import MCBBSProvider
from core.modpack.multimc import MultiMCProvider, _components_to_loader, _read_instance_name


# ---------- shared helpers ----------

class FakeEnv:
    def get_java_cmd(self, version): return f"/fake/java{version}/bin/java"


class FakeInstaller:
    def _ok(self, path):
        with open(os.path.join(path, "server.jar"), "w") as f: f.write("FAKE")
        return True
    def install_paper(self, p, v):        return self._ok(p)
    def install_fabric(self, p, v):       return self._ok(p)
    def install_forge(self, p, v, jc):    return self._ok(p)
    def install_neoforge(self, p, v, jc): return self._ok(p)


class FakeDownloader:
    def sync(self, mod_dir, version, loader): os.makedirs(mod_dir, exist_ok=True)


# ---------- registry order ----------

def test_provider_registry_order_is_specific_first():
    """Lock the order so detection precedence doesn't regress."""
    names = [p.name for p in PROVIDERS]
    assert names == ["modrinth", "mcbbs", "hmcl_server", "hmcl_native",
                     "multimc", "curseforge"]


# ---------- MultiMC ----------

def _write_multimc_zip(tmp_path, components, instance_name="MyPack",
                       with_minecraft_dir=True, wrap_in_subdir=False, name="pack.zip"):
    p = tmp_path / name
    base = (instance_name + "/") if wrap_in_subdir else ""
    pack_json = {"formatVersion": 1, "components": components}
    cfg = f"InstanceType=OneSix\nname={instance_name}\n"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr(f"{base}mmc-pack.json", json.dumps(pack_json))
        zf.writestr(f"{base}instance.cfg", cfg)
        if with_minecraft_dir:
            zf.writestr(f"{base}.minecraft/mods/sample.jar", b"FAKE_MOD")
            zf.writestr(f"{base}.minecraft/config/dummy.cfg", b"setting=1")
    return str(p)


def test_multimc_detect_root_layout(tmp_path):
    p = _write_multimc_zip(tmp_path, [
        {"uid": "net.minecraft", "version": "1.20.1"},
        {"uid": "net.minecraftforge", "version": "47.2.0"},
    ])
    assert MultiMCProvider().detect(p) is True


def test_multimc_detect_wrapped_in_subdir(tmp_path):
    """MultiMC export wraps everything in <InstanceName>/ — still detect."""
    p = _write_multimc_zip(tmp_path, [
        {"uid": "net.minecraft", "version": "1.20.1"},
    ], wrap_in_subdir=True)
    assert MultiMCProvider().detect(p) is True


def test_multimc_parse_extracts_loader_from_uid(tmp_path):
    p = _write_multimc_zip(tmp_path, [
        {"uid": "net.minecraft", "version": "1.20.1"},
        {"uid": "net.minecraftforge", "version": "47.2.0"},
    ])
    m = MultiMCProvider().parse(p)
    assert m.mc_version == "1.20.1"
    assert m.loader == "Forge"
    assert m.loader_version == "47.2.0"


@pytest.mark.parametrize("uid,expected", [
    ("net.minecraftforge",         "Forge"),
    ("net.neoforged",              "NeoForge"),
    ("net.fabricmc.fabric-loader", "Fabric"),
    ("org.quiltmc.quilt-loader",   "Fabric"),
])
def test_multimc_uid_mapping(uid, expected):
    mc, loader, _ver = _components_to_loader([
        {"uid": "net.minecraft", "version": "1.20.1"},
        {"uid": uid, "version": "1.0"},
    ])
    assert loader == expected


def test_multimc_components_to_loader_handles_vanilla_only():
    mc, loader, _ver = _components_to_loader([{"uid": "net.minecraft", "version": "1.20.1"}])
    assert mc == "1.20.1" and loader == "Paper"


def test_multimc_apply_extracts_minecraft_contents(tmp_path):
    p = _write_multimc_zip(tmp_path, [
        {"uid": "net.minecraft", "version": "1.20.1"},
        {"uid": "net.minecraftforge", "version": "47.2.0"},
    ])
    result = import_modpack(
        archive_path=p, server_name="srv", parent_dir=str(tmp_path),
        env_manager=FakeEnv(), installer=FakeInstaller(), downloader=FakeDownloader(),
    )
    assert result.success
    server = tmp_path / "srv"
    assert (server / "mods" / "sample.jar").read_bytes() == b"FAKE_MOD"
    assert (server / "config" / "dummy.cfg").read_bytes() == b"setting=1"


def test_multimc_read_instance_name():
    text = "InstanceType=OneSix\nname=Cool Pack\nOverrideJavaArgs=false\n"
    assert _read_instance_name(text) == "Cool Pack"


# ---------- HMCL Native ----------

def _write_hmcl_native_zip(tmp_path, mc_version="1.20.1",
                            with_forge_marker=False, name="pack.zip"):
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("modpack.json", json.dumps({
            "name": "Native Pack", "version": "1.0",
            "author": "tester", "description": "test",
        }))
        zf.writestr("minecraft/pack.json", json.dumps({"jar": mc_version}))
        zf.writestr("minecraft/mods/sample.jar", b"FAKE_MOD")
        if with_forge_marker:
            zf.writestr("minecraft/libraries/forge-47.jar", b"FAKE_FORGE")
    return str(p)


def test_hmcl_native_detect_requires_both_manifests(tmp_path):
    p = _write_hmcl_native_zip(tmp_path)
    assert HMCLNativeProvider().detect(p) is True


def test_hmcl_native_detect_rejects_modpack_json_only(tmp_path):
    p = tmp_path / "incomplete.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("modpack.json", "{}")
    assert HMCLNativeProvider().detect(str(p)) is False


def test_hmcl_native_parse_extracts_name_and_mc_version(tmp_path):
    p = _write_hmcl_native_zip(tmp_path, mc_version="1.18.2")
    m = HMCLNativeProvider().parse(p)
    assert m.name == "Native Pack"
    assert m.mc_version == "1.18.2"
    # Loader guess defaults to Forge when mods/ exists
    assert m.loader == "Forge"


def test_hmcl_native_apply_installs_minecraft_dir(tmp_path):
    p = _write_hmcl_native_zip(tmp_path)
    result = import_modpack(
        archive_path=p, server_name="srv", parent_dir=str(tmp_path),
        env_manager=FakeEnv(), installer=FakeInstaller(), downloader=FakeDownloader(),
    )
    assert result.success
    assert (tmp_path / "srv" / "mods" / "sample.jar").exists()


# ---------- HMCL Server ----------

def _write_hmcl_server_zip(tmp_path, file_api="https://files.example.com/pack",
                            files=None, addons=None, name="pack.zip"):
    p = tmp_path / name
    data = {
        "name": "Server Pack", "version": "1.0",
        "author": "tester", "description": "server-side test",
        "fileApi": file_api,
        "files": files or [],
        "addons": addons or [
            {"id": "minecraft", "version": "1.20.1"},
            {"id": "forge", "version": "47.2.0"},
        ],
    }
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("server-manifest.json", json.dumps(data))
        zf.writestr("overrides/config/sample.cfg", b"hello")
    return str(p)


def test_hmcl_server_detect(tmp_path):
    p = _write_hmcl_server_zip(tmp_path)
    assert HMCLServerProvider().detect(p) is True


def test_hmcl_server_parse(tmp_path):
    payload = b"FAKE_MOD"
    sha1 = hashlib.sha1(payload).hexdigest()
    p = _write_hmcl_server_zip(tmp_path, files=[
        {"path": "mods/sample.jar", "hash": sha1},
    ])
    m = HMCLServerProvider().parse(p)
    assert m.loader == "Forge"
    assert m.mc_version == "1.20.1"
    assert len(m.files) == 1
    assert m.files[0].sha1 == sha1
    assert m.files[0].download_urls[0].endswith("/mods/sample.jar")


def test_hmcl_server_apply_downloads_and_verifies(tmp_path, monkeypatch):
    payload = b"REAL_PAYLOAD_MATCHING_HASH"
    sha1 = hashlib.sha1(payload).hexdigest()
    p = _write_hmcl_server_zip(tmp_path, files=[
        {"path": "mods/sample.jar", "hash": sha1},
    ])

    # Stub the file download
    from core.modpack import hmcl_server as hs
    class FakeResp:
        def __init__(self, content): self.content = content; self.status_code = 200
        def raise_for_status(self): pass
        def iter_content(self, chunk_size=65536):
            for i in range(0, len(self.content), chunk_size):
                yield self.content[i:i + chunk_size]
    monkeypatch.setattr(hs.requests, "get",
                        lambda url, *a, **k: FakeResp(payload))

    result = import_modpack(
        archive_path=p, server_name="srv", parent_dir=str(tmp_path),
        env_manager=FakeEnv(), installer=FakeInstaller(), downloader=FakeDownloader(),
    )
    assert result.success
    server = tmp_path / "srv"
    assert (server / "mods" / "sample.jar").read_bytes() == payload
    assert (server / "config" / "sample.cfg").read_bytes() == b"hello"


def test_hmcl_server_apply_rejects_sha1_mismatch(tmp_path, monkeypatch):
    p = _write_hmcl_server_zip(tmp_path, files=[
        {"path": "mods/x.jar", "hash": "0" * 40},  # wrong hash
    ])
    from core.modpack import hmcl_server as hs
    class FakeResp:
        content = b"TAMPERED"; status_code = 200
        def raise_for_status(self): pass
        def iter_content(self, chunk_size=65536): yield self.content
    monkeypatch.setattr(hs.requests, "get", lambda *a, **k: FakeResp())

    result = import_modpack(
        archive_path=p, server_name="srv", parent_dir=str(tmp_path),
        env_manager=FakeEnv(), installer=FakeInstaller(), downloader=FakeDownloader(),
    )
    assert not result.success
    assert result.files_failed == 1
    # Bad file removed
    assert not (tmp_path / "srv" / "mods" / "x.jar").exists()


# ---------- MCBBS ----------

def _write_mcbbs_zip(tmp_path, files=None, addons=None,
                      file_api="https://files.example.com", name="pack.zip"):
    p = tmp_path / name
    data = {
        "manifestType": "minecraftModpack",
        "manifestVersion": 2,
        "name": "MCBBS Pack", "version": "1.0",
        "author": "tester", "description": "mcbbs hybrid test",
        "fileApi": file_api,
        "addons": addons or [
            {"id": "minecraft", "version": "1.20.1"},
            {"id": "forge", "version": "47.2.0"},
        ],
        "files": files or [],
    }
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("mcbbs.packmeta", json.dumps(data))
        zf.writestr("overrides/config/hello.cfg", b"hi")
    return str(p)


def test_mcbbs_detect(tmp_path):
    p = _write_mcbbs_zip(tmp_path)
    assert MCBBSProvider().detect(p) is True


def test_mcbbs_detect_rejects_wrong_manifesttype(tmp_path):
    p = tmp_path / "bad.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("mcbbs.packmeta", json.dumps({"manifestType": "something_else"}))
    assert MCBBSProvider().detect(str(p)) is False


def test_mcbbs_parse_splits_addon_and_curse_files(tmp_path):
    p = _write_mcbbs_zip(tmp_path, files=[
        {"type": "addon", "path": "mods/local.jar", "hash": "abc"},
        {"type": "curse", "projectID": 1, "fileID": 100},
        {"type": "curse", "projectID": 2, "fileID": 200},
    ])
    m = MCBBSProvider().parse(p)
    assert len(m.files) == 3
    addons = [f for f in m.files if getattr(f, "_mcbbs_kind", None) == "addon"]
    curses = [f for f in m.files if getattr(f, "_mcbbs_kind", None) == "curse"]
    assert len(addons) == 1 and addons[0].path == "mods/local.jar"
    assert len(curses) == 2


def test_mcbbs_apply_handles_curse_without_key(tmp_path, monkeypatch):
    """When CF key is missing, mcbbs's curse files fail but addon/overrides work."""
    from core.modpack import mcbbs as mb
    monkeypatch.setattr(mb, "get_curseforge_api_key", lambda: None)

    p = _write_mcbbs_zip(tmp_path, files=[
        {"type": "curse", "projectID": 1, "fileID": 100},
    ])
    result = import_modpack(
        archive_path=p, server_name="srv", parent_dir=str(tmp_path),
        env_manager=FakeEnv(), installer=FakeInstaller(), downloader=FakeDownloader(),
    )
    assert result.files_failed == 1   # the curse file
    assert (tmp_path / "srv" / "config" / "hello.cfg").exists()  # overrides still applied
    assert "curseforge_api_key" in (result.error or "")


# ---------- mutual exclusion: no provider crosstalk ----------

def test_each_format_routes_to_correct_provider(tmp_path):
    """Each format's distinct sample lands on the right provider."""
    multimc = _write_multimc_zip(tmp_path, [{"uid": "net.minecraft", "version": "1.20.1"}],
                                  name="mc.zip")
    hmcl_native = _write_hmcl_native_zip(tmp_path, name="hn.zip")
    hmcl_server = _write_hmcl_server_zip(tmp_path, name="hs.zip")
    mcbbs = _write_mcbbs_zip(tmp_path, name="mb.zip")

    assert detect_provider(multimc).name == "multimc"
    assert detect_provider(hmcl_native).name == "hmcl_native"
    assert detect_provider(hmcl_server).name == "hmcl_server"
    assert detect_provider(mcbbs).name == "mcbbs"
