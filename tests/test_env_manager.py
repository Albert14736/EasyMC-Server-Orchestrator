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
    """`java -version` writes to stderr — make sure we read stderr.
    probe_java runs with capture_output=True, so stderr/stdout come back as bytes."""
    fake = mock.Mock()
    fake.stderr = b'openjdk version "17.0.2"\n'
    fake.stdout = b""
    monkeypatch.setattr(env_manager.subprocess, "run", lambda *a, **k: fake)
    assert java_major_version_of("/fake/java17-stderr") == 17


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


# ---------- Java selection (mocked installed table, runs on any OS) ----------
# The finder now goes through select_java(list_installed_javas(), required, max_major):
# exact major first, else the NEAREST HIGHER major in range, never a lower one.
# We feed the table at the _all_candidates + java_version_of seam.

def _mock_installed(monkeypatch, mapping):
    """mapping: {path: major}. _all_candidates yields the paths; java_version_of
    maps each to a (major, 0, 0) version tuple."""
    monkeypatch.setattr(env_manager, "_all_candidates", lambda: list(mapping))
    monkeypatch.setattr(env_manager, "java_version_of",
                        lambda p: (mapping[p], 0, 0) if p in mapping else None)


def test_find_java_prefers_exact_major(monkeypatch):
    """When multiple Javas exist, return the one whose major matches `required`."""
    _mock_installed(monkeypatch, {
        r"C:\jdk8\bin\java.exe": 8,
        r"C:\jdk17\bin\java.exe": 17,
        r"C:\jdk21\bin\java.exe": 21,
    })
    assert _find_java_on_windows(17) == r"C:\jdk17\bin\java.exe"
    assert _find_java_on_windows(21) == r"C:\jdk21\bin\java.exe"
    assert _find_java_on_windows(8) == r"C:\jdk8\bin\java.exe"


def test_find_java_never_falls_back_to_lower(monkeypatch):
    """New rule: never return a LOWER major. Ask for 21, only 17 installed -> None."""
    _mock_installed(monkeypatch, {r"C:\jdk17\bin\java.exe": 17})
    assert _find_java_on_windows(21) is None


def test_find_java_picks_nearest_higher_when_no_exact(monkeypatch):
    """No exact major -> nearest HIGHER within range, not a lower one."""
    _mock_installed(monkeypatch, {
        r"C:\jdk17\bin\java.exe": 17,
        r"C:\jdk21\bin\java.exe": 21,
        r"C:\jdk24\bin\java.exe": 24,
    })
    # need 18: closest higher is 21 (never 17)
    assert _find_java_on_windows(18) == r"C:\jdk21\bin\java.exe"


def test_select_java_respects_max_major():
    """max_major caps the nearest-higher search (old Forge/NeoForge reject newer class files)."""
    from core.env_manager import select_java
    cands = [(r"C:\jdk17\bin\java.exe", (17, 0, 0)), (r"C:\jdk21\bin\java.exe", (21, 0, 0))]
    assert select_java(cands, 17, max_major=17) == r"C:\jdk17\bin\java.exe"  # 21 excluded
    assert select_java(cands, 18, max_major=17) is None                      # nothing in [18,17]


def test_select_java_prefers_newest_update_within_major():
    """Among the same major, pick the newest update (an ancient 8u51 is the worst 8)."""
    from core.env_manager import select_java
    cands = [(r"C:\jdk8_51\bin\java.exe", (8, 0, 51)),
             (r"C:\jdk8_402\bin\java.exe", (8, 0, 402))]
    assert select_java(cands, 8) == r"C:\jdk8_402\bin\java.exe"


def test_find_java_empty_when_nothing_installed(monkeypatch):
    _mock_installed(monkeypatch, {})
    assert _find_java_on_windows(17) is None


def test_all_candidates_dedupes(monkeypatch):
    """The same install reached via several strategies is collected once."""
    dup = "/opt/jdk17/bin/java"
    monkeypatch.setattr(env_manager, "_candidates_from_java_home", lambda: [dup])
    monkeypatch.setattr(env_manager, "_candidates_on_darwin", lambda: [dup])
    monkeypatch.setattr(env_manager, "_candidates_from_path", lambda: [dup])
    monkeypatch.setattr(env_manager, "_candidates_on_linux", lambda: [dup])
    assert env_manager._all_candidates().count(dup) == 1


# ---------- get_java_cmd dispatch ----------

def test_get_java_cmd_falls_back_to_bare_java(monkeypatch):
    """Nothing suitable installed -> get_java_cmd still returns 'java' (legacy fallback)."""
    monkeypatch.setattr(env_manager.platform, "system", lambda: "Windows")
    _mock_installed(monkeypatch, {})
    assert EnvManager().get_java_cmd(17) == "java"


def test_get_java_cmd_returns_found_path(monkeypatch):
    monkeypatch.setattr(env_manager.platform, "system", lambda: "Windows")
    _mock_installed(monkeypatch, {r"C:\jdk17\bin\java.exe": 17})
    assert EnvManager().get_java_cmd(17) == r"C:\jdk17\bin\java.exe"


def test_get_java_cmd_linux_falls_back(monkeypatch):
    """Linux with no detectable Java -> bare 'java'."""
    monkeypatch.setattr(env_manager.platform, "system", lambda: "Linux")
    _mock_installed(monkeypatch, {})
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
