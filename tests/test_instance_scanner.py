"""core/instance_scanner.py 的单元测试 —— 用 tmp_path 造合成实例，纯无头。"""
import os

import pytest

from core.instance_scanner import (
    InstanceScanner,
    detect_eula,
    detect_loader_and_version,
    get_instance_details,
    scan_instances,
)


# ---- helpers ----

def _touch(path, name, content=""):
    full = os.path.join(path, name)
    os.makedirs(os.path.dirname(full), exist_ok=True) if os.path.dirname(name) else None
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return full


def _mkdirs(path, *parts):
    full = os.path.join(path, *parts)
    os.makedirs(full, exist_ok=True)
    return full


# ---- loader + version detection ----

def test_forge_from_jar_name(tmp_path):
    d = tmp_path / "forge_inst"; d.mkdir()
    _touch(str(d), "forge-1.20.4-49.2.0-shim.jar")
    assert detect_loader_and_version(str(d)) == ("Forge", "1.20.4")


def test_forge_from_libraries_only(tmp_path):
    """整合包常没有顶层 forge jar，只有 libraries/net/minecraftforge。"""
    d = tmp_path / "modpack"; d.mkdir()
    _mkdirs(str(d), "libraries", "net", "minecraftforge")
    _mkdirs(str(d), "libraries", "net", "minecraft", "server", "1.20.1-20231207.112700")
    assert detect_loader_and_version(str(d)) == ("Forge", "1.20.1")


def test_forge_1_12_2(tmp_path):
    d = tmp_path / "old"; d.mkdir()
    _touch(str(d), "forge-1.12.2-14.23.5.2860-universal.jar")
    assert detect_loader_and_version(str(d)) == ("Forge", "1.12.2")


def test_neoforge_from_libraries(tmp_path):
    d = tmp_path / "neo"; d.mkdir()
    _mkdirs(str(d), "libraries", "net", "neoforged")
    loader, _ = detect_loader_and_version(str(d))
    assert loader == "NeoForge"


def test_fabric_from_launch_jar(tmp_path):
    d = tmp_path / "fab"; d.mkdir()
    _touch(str(d), "fabric-server-launch.jar")
    loader, _ = detect_loader_and_version(str(d))
    assert loader == "Fabric"


def test_fabric_from_libraries(tmp_path):
    d = tmp_path / "fab2"; d.mkdir()
    _mkdirs(str(d), "libraries", "net", "fabricmc")
    loader, _ = detect_loader_and_version(str(d))
    assert loader == "Fabric"


def test_paper_from_version_history(tmp_path):
    d = tmp_path / "paper"; d.mkdir()
    _touch(str(d), "version_history.json",
           '{"currentVersion":"git-Paper-196 (MC: 1.20.1)"}')
    assert detect_loader_and_version(str(d)) == ("Paper", "1.20.1")


def test_paper_wins_over_mixed_fabric_libs(tmp_path):
    """回归：Paper 目录里混入 spigot.yml + libraries/net/fabricmc 时，
    version_history.json 必须压过 Fabric/Spigot 判定（真实实例踩过的坑）。"""
    d = tmp_path / "mixed"; d.mkdir()
    _touch(str(d), "version_history.json",
           '{"currentVersion":"git-Paper-196 (MC: 1.20.1)"}')
    _touch(str(d), "spigot.yml", "settings:\n")
    _touch(str(d), "bukkit.yml", "settings:\n")
    _mkdirs(str(d), "libraries", "net", "fabricmc")
    _touch(str(d), "server.jar")
    assert detect_loader_and_version(str(d)) == ("Paper", "1.20.1")


def test_spigot_from_yml(tmp_path):
    d = tmp_path / "spig"; d.mkdir()
    _touch(str(d), "spigot.yml", "settings:\n")
    _touch(str(d), "server.jar")
    loader, _ = detect_loader_and_version(str(d))
    assert loader == "Spigot"


def test_vanilla_versioned_jar(tmp_path):
    d = tmp_path / "van"; d.mkdir()
    _touch(str(d), "minecraft_server.1.20.4.jar")
    assert detect_loader_and_version(str(d)) == ("Vanilla", "1.20.4")


def test_vanilla_plain_server_jar(tmp_path):
    d = tmp_path / "van2"; d.mkdir()
    _touch(str(d), "server.jar")
    loader, version = detect_loader_and_version(str(d))
    assert loader == "Vanilla"
    assert version is None


def test_unknown_empty_dir(tmp_path):
    d = tmp_path / "empty"; d.mkdir()
    assert detect_loader_and_version(str(d)) == (None, None)


# ---- eula ----

def test_eula_true(tmp_path):
    d = tmp_path / "e"; d.mkdir()
    _touch(str(d), "eula.txt", "eula=true\n")
    assert detect_eula(str(d)) is True


def test_eula_false(tmp_path):
    d = tmp_path / "e2"; d.mkdir()
    _touch(str(d), "eula.txt", "eula=false\n")
    assert detect_eula(str(d)) is False


def test_eula_missing(tmp_path):
    d = tmp_path / "e3"; d.mkdir()
    assert detect_eula(str(d)) is False


# ---- get_instance_details ----

def test_details_full(tmp_path):
    d = tmp_path / "我的服务器"; d.mkdir()
    _touch(str(d), "forge-1.20.4-49.2.0-shim.jar")
    _touch(str(d), "eula.txt", "eula=true")
    _touch(str(d), "start.sh", "#!/bin/sh\n")
    info = get_instance_details(str(d), "我的服务器")
    assert info["name"] == "我的服务器"
    assert info["type"] == "Forge"
    assert info["version"] == "1.20.4"
    assert info["eula"] is True
    assert info["hmsl_created"] is True


def test_details_keys_always_present(tmp_path):
    d = tmp_path / "bare"; d.mkdir()
    _touch(str(d), "server.jar")
    info = get_instance_details(str(d), "bare")
    assert set(info) >= {"name", "path", "version", "type", "eula", "hmsl_created"}
    assert info["hmsl_created"] is False


# ---- scan_instances ----

def test_scan_picks_servers_skips_others(tmp_path):
    base = tmp_path
    # 真实例：有 eula
    inst = base / "srv"; inst.mkdir()
    _touch(str(inst), "eula.txt", "eula=true")
    _touch(str(inst), "forge-1.20.4-1.jar")
    # 被排除目录
    (base / "core").mkdir(); _touch(str(base / "core"), "eula.txt", "eula=true")
    (base / ".venv").mkdir()
    # 普通目录（非服务端）
    plain = base / "notes"; plain.mkdir(); _touch(str(plain), "readme.txt")

    found = scan_instances(str(base))
    names = {i["name"] for i in found}
    assert "srv" in names
    assert "core" not in names      # 排除目录
    assert "notes" not in names     # 非服务端


def test_scan_nonexistent_base():
    assert scan_instances("/no/such/dir/xyz") == []


def test_scanner_class_wrapper(tmp_path):
    inst = tmp_path / "srv"; inst.mkdir()
    _touch(str(inst), "eula.txt", "eula=true")
    _touch(str(inst), "paper-1.20.4-435.jar")
    out = InstanceScanner(str(tmp_path)).scan()
    assert len(out) == 1
    assert out[0]["type"] == "Paper"
    assert out[0]["version"] == "1.20.4"
