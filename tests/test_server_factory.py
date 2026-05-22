"""
Headless tests for core.server_factory.create_server.

These tests use fake EnvManager/Installer/Downloader so they run fully offline,
and they exercise the orchestration: directory creation, dispatch by loader,
eula.txt write, mod directory choice (plugins vs mods), and launch script.

Run from the project root with:
    python -m pytest tests/ -v
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.server_factory import (
    CreateServerResult,
    create_server,
    required_java_version,
)


class FakeEnv:
    def __init__(self):
        self.calls = []

    def get_java_cmd(self, version):
        self.calls.append(version)
        return f"/fake/java{version}/bin/java"


class FakeInstaller:
    """Records which installer method was called and always succeeds by default."""

    def __init__(self, succeed=True):
        self.calls = []
        self.succeed = succeed

    def _write_jar(self, path):
        with open(os.path.join(path, "server.jar"), "w") as f:
            f.write("FAKE_JAR")

    def install_paper(self, path, version):
        self.calls.append(("paper", path, version))
        if self.succeed:
            self._write_jar(path)
        return self.succeed

    def install_fabric(self, path, version):
        self.calls.append(("fabric", path, version))
        if self.succeed:
            self._write_jar(path)
        return self.succeed

    def install_forge(self, path, version, java_cmd):
        self.calls.append(("forge", path, version, java_cmd))
        if self.succeed:
            self._write_jar(path)
        return self.succeed

    def install_neoforge(self, path, version, java_cmd):
        self.calls.append(("neoforge", path, version, java_cmd))
        if self.succeed:
            self._write_jar(path)
        return self.succeed


class FakeDownloader:
    def __init__(self):
        self.calls = []

    def sync(self, mod_dir, version, loader):
        self.calls.append((mod_dir, version, loader))
        os.makedirs(mod_dir, exist_ok=True)


# ---------- required_java_version ----------

@pytest.mark.parametrize("mc_version,expected", [
    ("1.8.8", 8),
    ("1.12.2", 8),
    ("1.16.5", 8),
    ("1.17", 16),
    ("1.18.2", 17),
    ("1.20.1", 17),
    ("1.20.4", 17),
    ("1.20.5", 21),
    ("1.20.6", 21),
    ("1.21", 21),
    ("1.21.1", 21),
])
def test_required_java_version(mc_version, expected):
    """The string-comparison bug would have returned 21 for 1.8.8 — guard against regression."""
    assert required_java_version(mc_version) == expected


# ---------- create_server happy path ----------

def test_create_server_paper_writes_plugins_dir(tmp_path):
    env, inst, dl = FakeEnv(), FakeInstaller(), FakeDownloader()
    progress = []

    result = create_server(
        name="my_paper",
        version="1.20.4",
        loader="Paper",
        parent_dir=str(tmp_path),
        env_manager=env,
        installer=inst,
        downloader=dl,
        progress_callback=lambda f, m: progress.append((f, m)),
    )

    assert isinstance(result, CreateServerResult)
    assert result.success is True
    assert result.error is None
    server = tmp_path / "my_paper"
    assert server.is_dir()
    assert (server / "server.jar").exists()
    assert (server / "eula.txt").read_text().strip() == "eula=true"
    # Paper → plugins, not mods
    assert dl.calls == [(str(server / "plugins"), "1.20.4", "Paper")]
    # Java 17 for 1.20.4
    assert env.calls == [17]
    # Progress should run 0 → 1, monotonically non-decreasing
    fractions = [f for f, _ in progress]
    assert fractions[0] >= 0.10
    assert fractions[-1] == 1.0
    assert fractions == sorted(fractions)


def test_create_server_fabric_writes_mods_dir(tmp_path):
    env, inst, dl = FakeEnv(), FakeInstaller(), FakeDownloader()
    result = create_server(
        name="my_fabric",
        version="1.20.4",
        loader="Fabric",
        parent_dir=str(tmp_path),
        env_manager=env, installer=inst, downloader=dl,
    )
    assert result.success
    # Non-Paper → mods
    assert dl.calls[0][0].endswith(os.path.join("my_fabric", "mods"))


def test_create_server_forge_passes_java_cmd(tmp_path):
    """Forge/NeoForge need the actual java path to run --installServer."""
    env, inst, dl = FakeEnv(), FakeInstaller(), FakeDownloader()
    create_server(
        name="forge_srv", version="1.20.4", loader="Forge",
        parent_dir=str(tmp_path),
        env_manager=env, installer=inst, downloader=dl,
    )
    assert inst.calls[0][:3] == ("forge", str(tmp_path / "forge_srv"), "1.20.4")
    assert inst.calls[0][3] == "/fake/java17/bin/java"


def test_create_server_loader_case_insensitive(tmp_path):
    env, inst, dl = FakeEnv(), FakeInstaller(), FakeDownloader()
    result = create_server(
        name="s", version="1.20.4", loader="PAPER",
        parent_dir=str(tmp_path),
        env_manager=env, installer=inst, downloader=dl,
    )
    assert result.success
    assert inst.calls[0][0] == "paper"


def test_create_server_writes_platform_launch_script(tmp_path):
    env, inst, dl = FakeEnv(), FakeInstaller(), FakeDownloader()
    create_server(
        name="s", version="1.20.4", loader="Paper",
        parent_dir=str(tmp_path),
        env_manager=env, installer=inst, downloader=dl,
    )
    server = tmp_path / "s"
    if sys.platform == "win32":
        assert (server / "start.bat").exists()
        body = (server / "start.bat").read_text()
        assert "java" in body and "server.jar" in body
    else:
        script = server / "start.sh"
        assert script.exists()
        # Executable bit
        assert script.stat().st_mode & 0o111
        body = script.read_text()
        assert body.startswith("#!/bin/zsh")
        assert "server.jar" in body


def test_forge_launch_script_delegates_to_run_sh(tmp_path):
    """Forge has no server.jar — start.sh must delegate to Forge's run.sh."""
    env, inst, dl = FakeEnv(), FakeInstaller(), FakeDownloader()
    create_server(
        name="forge_srv", version="1.20.4", loader="Forge",
        parent_dir=str(tmp_path),
        env_manager=env, installer=inst, downloader=dl,
    )
    server = tmp_path / "forge_srv"
    if sys.platform == "win32":
        body = (server / "start.bat").read_text()
        assert "run.bat" in body
        assert "server.jar" not in body
    else:
        body = (server / "start.sh").read_text()
        assert "run.sh" in body
        assert "server.jar" not in body


def test_neoforge_launch_script_delegates_to_run_sh(tmp_path):
    """Same as Forge — NeoForge also ships its own run.sh, no server.jar."""
    env, inst, dl = FakeEnv(), FakeInstaller(), FakeDownloader()
    create_server(
        name="neo_srv", version="1.20.4", loader="NeoForge",
        parent_dir=str(tmp_path),
        env_manager=env, installer=inst, downloader=dl,
    )
    server = tmp_path / "neo_srv"
    script = server / ("start.bat" if sys.platform == "win32" else "start.sh")
    body = script.read_text()
    assert "run." in body  # delegates to run.sh / run.bat
    assert "server.jar" not in body


# ---------- create_server error paths ----------

def test_create_server_rejects_empty_name(tmp_path):
    env, inst, dl = FakeEnv(), FakeInstaller(), FakeDownloader()
    result = create_server(
        name="   ", version="1.20.4", loader="Paper",
        parent_dir=str(tmp_path),
        env_manager=env, installer=inst, downloader=dl,
    )
    assert not result.success
    assert "名称" in result.error
    assert inst.calls == []


def test_create_server_rejects_missing_parent_dir(tmp_path):
    env, inst, dl = FakeEnv(), FakeInstaller(), FakeDownloader()
    result = create_server(
        name="s", version="1.20.4", loader="Paper",
        parent_dir=str(tmp_path / "does_not_exist"),
        env_manager=env, installer=inst, downloader=dl,
    )
    assert not result.success
    assert inst.calls == []


def test_create_server_rejects_unknown_loader(tmp_path):
    env, inst, dl = FakeEnv(), FakeInstaller(), FakeDownloader()
    result = create_server(
        name="s", version="1.20.4", loader="Bukkit",
        parent_dir=str(tmp_path),
        env_manager=env, installer=inst, downloader=dl,
    )
    assert not result.success
    assert "Bukkit" in result.error
    assert inst.calls == []


def test_create_server_returns_failure_when_installer_fails(tmp_path):
    env, inst, dl = FakeEnv(), FakeInstaller(succeed=False), FakeDownloader()
    result = create_server(
        name="s", version="1.20.4", loader="Paper",
        parent_dir=str(tmp_path),
        env_manager=env, installer=inst, downloader=dl,
    )
    assert not result.success
    assert "Paper" in result.error
    # Sync should NOT be called if installer failed
    assert dl.calls == []
    # And no eula.txt
    assert not (tmp_path / "s" / "eula.txt").exists()
