"""
Headless tests for core.env_manager.

These run on macOS but cover Windows-only code paths via mocks, so a Mac
developer can still red/green the Windows logic before shipping.
"""
from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import env_manager
from core.env_manager import (
    EnvManager,
    _find_java_on_windows,
    java_major_version_of,
    parse_java_major_version,
)


# ---------- parse_java_major_version (pure) ----------

@pytest.mark.parametrize("text,expected", [
    # Oracle JDK 8 — legacy "1.8" form
    ('java version "1.8.0_311"\nJava(TM) SE Runtime Environment...', 8),
    # OpenJDK 8 — same legacy form
    ('openjdk version "1.8.0_312"\nOpenJDK Runtime Environment...', 8),
    # Modern Oracle / OpenJDK 11+
    ('openjdk version "17.0.2" 2022-01-18\nOpenJDK Runtime Environment Temurin-17.0.2+8', 17),
    ('java version "21" 2023-09-19\nJava(TM) SE Runtime Environment (build 21+35-2513)', 21),
    # Patch version
    ('openjdk version "21.0.1" 2023-10-17', 21),
    # Adoptium Temurin
    ('openjdk version "17.0.9" 2023-10-17\nOpenJDK Runtime Environment Temurin-17.0.9+9', 17),
])
def test_parse_java_major_version(text, expected):
    assert parse_java_major_version(text) == expected


def test_parse_java_major_version_returns_none_on_garbage():
    assert parse_java_major_version("not a java -version output at all") is None
    assert parse_java_major_version("") is None


# ---------- java_major_version_of (subprocess wrapper) ----------

def test_java_major_version_of_uses_stderr(monkeypatch):
    """`java -version` writes to stderr — make sure we read stderr."""
    fake = mock.Mock()
    fake.stderr = 'openjdk version "17.0.2"\n'
    fake.stdout = ""
    monkeypatch.setattr(env_manager.subprocess, "run", lambda *a, **k: fake)
    assert java_major_version_of("/fake/java") == 17


def test_java_major_version_of_handles_missing_binary(monkeypatch):
    def raise_fnf(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(env_manager.subprocess, "run", raise_fnf)
    assert java_major_version_of("/does/not/exist") is None


def test_java_major_version_of_handles_timeout(monkeypatch):
    import subprocess as sp
    def raise_timeout(*a, **k):
        raise sp.TimeoutExpired(cmd="java", timeout=5)
    monkeypatch.setattr(env_manager.subprocess, "run", raise_timeout)
    assert java_major_version_of("/slow/java") is None


# ---------- Windows finder (mocked, runs on macOS) ----------

def _mock_strategies(monkeypatch, java_home=None, registry=(), common=(), where=()):
    monkeypatch.setattr(env_manager, "_candidates_from_java_home",
                        lambda: ([java_home] if java_home else []))
    monkeypatch.setattr(env_manager, "_candidates_from_registry", lambda: list(registry))
    monkeypatch.setattr(env_manager, "_candidates_from_common_dirs", lambda: list(common))
    monkeypatch.setattr(env_manager, "_candidates_from_where", lambda: list(where))


def test_find_java_windows_prefers_correct_version(monkeypatch):
    """When multiple Javas exist, return the one matching `required`."""
    _mock_strategies(
        monkeypatch,
        registry=[r"C:\jdk8\bin\java.exe", r"C:\jdk17\bin\java.exe", r"C:\jdk21\bin\java.exe"],
    )
    versions = {
        r"C:\jdk8\bin\java.exe": 8,
        r"C:\jdk17\bin\java.exe": 17,
        r"C:\jdk21\bin\java.exe": 21,
    }
    monkeypatch.setattr(env_manager, "java_major_version_of",
                        lambda p, timeout=5.0: versions.get(p))
    assert _find_java_on_windows(17) == r"C:\jdk17\bin\java.exe"
    assert _find_java_on_windows(21) == r"C:\jdk21\bin\java.exe"
    assert _find_java_on_windows(8) == r"C:\jdk8\bin\java.exe"


def test_find_java_windows_returns_none_when_version_missing(monkeypatch):
    _mock_strategies(monkeypatch, registry=[r"C:\jdk17\bin\java.exe"])
    monkeypatch.setattr(env_manager, "java_major_version_of", lambda p, timeout=5.0: 17)
    # User asks for 21, only 17 installed — fail (caller can show a helpful error)
    assert _find_java_on_windows(21) is None


def test_find_java_windows_dedupes_candidates(monkeypatch):
    """JAVA_HOME and registry might point at the same install — only check once."""
    dup_path = r"C:\jdk17\bin\java.exe"
    _mock_strategies(monkeypatch, java_home=dup_path, registry=[dup_path], where=[dup_path])
    seen = []
    def check_version(p, timeout=5.0):
        seen.append(p)
        return 17
    monkeypatch.setattr(env_manager, "java_major_version_of", check_version)
    _find_java_on_windows(17)
    assert len(seen) == 1


def test_find_java_windows_empty_when_no_strategies_hit(monkeypatch):
    _mock_strategies(monkeypatch)
    assert _find_java_on_windows(17) is None


# ---------- get_java_cmd dispatch ----------

def test_get_java_cmd_falls_back_to_bare_java(monkeypatch):
    """If platform returns None, we still return 'java' so caller can try PATH."""
    monkeypatch.setattr(env_manager.platform, "system", lambda: "Windows")
    monkeypatch.setattr(env_manager, "_find_java_on_windows", lambda req: None)
    assert EnvManager().get_java_cmd(17) == "java"


def test_get_java_cmd_returns_found_path(monkeypatch):
    monkeypatch.setattr(env_manager.platform, "system", lambda: "Windows")
    monkeypatch.setattr(env_manager, "_find_java_on_windows",
                        lambda req: r"C:\jdk17\bin\java.exe")
    assert EnvManager().get_java_cmd(17) == r"C:\jdk17\bin\java.exe"


def test_get_java_cmd_linux_falls_back(monkeypatch):
    """Linux path uses bare 'java' — we haven't implemented detection there yet."""
    monkeypatch.setattr(env_manager.platform, "system", lambda: "Linux")
    assert EnvManager().get_java_cmd(17) == "java"


# ---------- script_dir locking ----------

def test_script_dir_is_absolute():
    em = EnvManager()
    assert os.path.isabs(em.script_dir)
    # Should point to the project root (parent of core/)
    assert os.path.isdir(os.path.join(em.script_dir, "core"))


# ---------- macOS path still works (smoke) ----------

@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only path")
def test_darwin_get_java_cmd_returns_string():
    """On a real Mac, get_java_cmd must return a string (path or 'java')."""
    em = EnvManager()
    result = em.get_java_cmd(17)
    assert isinstance(result, str) and len(result) > 0
