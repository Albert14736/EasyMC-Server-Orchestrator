"""
Headless tests for core.mod_scanner.

All HTTP is mocked via the lookup_fn injection point — tests never touch the
network. File operations use tmp_path so real mods/ folders are untouched.
"""
from __future__ import annotations

import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.mod_scanner import (
    ModInfo,
    ScanEntry,
    classify_mod,
    compute_jar_sha1,
    disable_mods,
    find_mods_dir,
    scan_server_mods,
)


# ---------- compute_jar_sha1 ----------

def test_compute_jar_sha1_matches_hashlib(tmp_path):
    """Our streamed SHA1 must equal hashlib's one-shot SHA1."""
    f = tmp_path / "fake.jar"
    payload = b"PK\x03\x04" + (b"a" * 200_000) + b"\xff\x00"  # 200KB pseudo-jar
    f.write_bytes(payload)
    assert compute_jar_sha1(str(f)) == hashlib.sha1(payload).hexdigest()


def test_compute_jar_sha1_handles_empty_file(tmp_path):
    f = tmp_path / "empty.jar"
    f.write_bytes(b"")
    assert compute_jar_sha1(str(f)) == hashlib.sha1(b"").hexdigest()


def test_compute_jar_sha1_chunked_matches_unchunked(tmp_path):
    """Stream with tiny chunks must still match — guards against off-by-one in iter loop."""
    f = tmp_path / "big.jar"
    payload = bytes(range(256)) * 4096  # 1MB of repeating bytes
    f.write_bytes(payload)
    assert compute_jar_sha1(str(f), chunk_size=17) == hashlib.sha1(payload).hexdigest()


# ---------- classify_mod (the bug-prone bit) ----------

@pytest.mark.parametrize("client,server,expected", [
    # Definitively client-only
    ("required", "unsupported", "client_only"),
    ("optional", "unsupported", "client_only"),
    ("unsupported", "unsupported", "client_only"),  # weird but exists in DB
    # The tricky case: server can skip it BUT client requires it
    ("required", "optional", "client_only"),
    # Server-compatible cases
    ("required", "required", "server_ok"),
    ("optional", "required", "server_ok"),
    ("unsupported", "required", "server_ok"),  # server-only mod
    ("optional", "optional", "server_ok"),
    ("unsupported", "optional", "server_ok"),
    # Unknown fields shouldn't crash
    ("unknown", "unknown", "server_ok"),
])
def test_classify_mod_matrix(client, server, expected):
    info = ModInfo("p", "title", client_side=client, server_side=server)
    assert classify_mod(info) == expected


def test_classify_mod_returns_unknown_when_info_is_none():
    assert classify_mod(None) == "unknown"


# ---------- find_mods_dir ----------

def test_find_mods_dir_prefers_mods_over_plugins(tmp_path):
    (tmp_path / "mods").mkdir()
    (tmp_path / "plugins").mkdir()
    assert find_mods_dir(str(tmp_path)) == str(tmp_path / "mods")


def test_find_mods_dir_falls_back_to_plugins(tmp_path):
    (tmp_path / "plugins").mkdir()
    assert find_mods_dir(str(tmp_path)) == str(tmp_path / "plugins")


def test_find_mods_dir_returns_none_when_neither_exists(tmp_path):
    assert find_mods_dir(str(tmp_path)) is None


# ---------- scan_server_mods (uses injected lookup_fn) ----------

def _make_server_with_mods(tmp_path, names):
    """Helper: create server_path/mods/ with the named jars (small dummy bytes)."""
    server = tmp_path / "srv"
    mods = server / "mods"
    mods.mkdir(parents=True)
    for n in names:
        (mods / n).write_bytes(b"FAKE_CONTENT_" + n.encode())
    return str(server), str(mods)


def test_scan_server_mods_classifies_each_jar(tmp_path):
    server, mods = _make_server_with_mods(tmp_path, ["client.jar", "server.jar", "unknown.jar"])
    server_jar_sha1 = compute_jar_sha1(os.path.join(mods, "server.jar"))
    client_jar_sha1 = compute_jar_sha1(os.path.join(mods, "client.jar"))

    def fake_lookup(sha1):
        if sha1 == server_jar_sha1:
            return ModInfo("p1", "ServerMod", client_side="optional", server_side="required")
        if sha1 == client_jar_sha1:
            return ModInfo("p2", "ClientMod", client_side="required", server_side="unsupported")
        return None  # unknown.jar

    report = scan_server_mods(server, lookup_fn=fake_lookup)
    assert len(report.entries) == 3
    by_name = {e.file_name: e for e in report.entries}
    assert by_name["server.jar"].status == "server_ok"
    assert by_name["client.jar"].status == "client_only"
    assert by_name["unknown.jar"].status == "unknown"


def test_scan_server_mods_reports_progress(tmp_path):
    server, _ = _make_server_with_mods(tmp_path, ["a.jar", "b.jar", "c.jar"])
    progress_calls = []
    scan_server_mods(
        server,
        progress_callback=lambda i, n, name: progress_calls.append((i, n, name)),
        lookup_fn=lambda s: None,
    )
    assert progress_calls == [(1, 3, "a.jar"), (2, 3, "b.jar"), (3, 3, "c.jar")]


def test_scan_server_mods_skips_non_jar_files(tmp_path):
    server, mods = _make_server_with_mods(tmp_path, ["real.jar"])
    (tmp_path / "srv" / "mods" / "readme.txt").write_text("not a mod")
    (tmp_path / "srv" / "mods" / "config.toml").write_text("[]")
    report = scan_server_mods(server, lookup_fn=lambda s: None)
    assert [e.file_name for e in report.entries] == ["real.jar"]


def test_scan_server_mods_returns_empty_when_no_mods_dir(tmp_path):
    srv = tmp_path / "bare_server"
    srv.mkdir()
    report = scan_server_mods(str(srv), lookup_fn=lambda s: None)
    assert report.entries == []
    assert report.mods_dir == ""


def test_scan_server_mods_works_for_paper_plugins(tmp_path):
    server = tmp_path / "paper_srv"
    plugins = server / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "luckperms.jar").write_bytes(b"fake")
    report = scan_server_mods(str(server), lookup_fn=lambda s: None)
    assert report.mods_dir == str(plugins)
    assert len(report.entries) == 1


# ---------- ScanReport convenience methods ----------

def test_scan_report_filters_by_status():
    a = ScanEntry("/a", "a.jar", status="client_only")
    b = ScanEntry("/b", "b.jar", status="server_ok")
    c = ScanEntry("/c", "c.jar", status="unknown")
    d = ScanEntry("/d", "d.jar", status="error", error_message="boom")
    from core.mod_scanner import ScanReport
    r = ScanReport(server_path="/s", mods_dir="/s/mods", entries=[a, b, c, d])
    assert r.client_only() == [a]
    assert r.server_ok() == [b]
    assert r.unknown() == [c]
    assert r.errors() == [d]


# ---------- disable_mods ----------

def test_disable_mods_moves_files_to_disabled_subdir(tmp_path):
    server, mods = _make_server_with_mods(tmp_path, ["client_a.jar", "client_b.jar", "keep.jar"])
    entries = [
        ScanEntry(os.path.join(mods, "client_a.jar"), "client_a.jar", "client_only"),
        ScanEntry(os.path.join(mods, "client_b.jar"), "client_b.jar", "client_only"),
    ]
    moved = disable_mods(entries, mods)
    assert moved == 2
    # Originals gone
    assert not os.path.exists(os.path.join(mods, "client_a.jar"))
    assert not os.path.exists(os.path.join(mods, "client_b.jar"))
    # Disabled dir created with both
    disabled = os.path.join(mods, ".disabled")
    assert sorted(os.listdir(disabled)) == ["client_a.jar", "client_b.jar"]
    # Untouched mod still there
    assert os.path.exists(os.path.join(mods, "keep.jar"))


def test_disable_mods_handles_filename_collision(tmp_path):
    """If a same-named jar is already disabled, don't overwrite — append _1, _2."""
    server, mods = _make_server_with_mods(tmp_path, ["dup.jar"])
    # Pre-populate disabled dir with a same-name file
    disabled = os.path.join(mods, ".disabled")
    os.makedirs(disabled)
    (tmp_path / "srv" / "mods" / ".disabled" / "dup.jar").write_bytes(b"OLD_DISABLED")

    entries = [ScanEntry(os.path.join(mods, "dup.jar"), "dup.jar", "client_only")]
    moved = disable_mods(entries, mods)
    assert moved == 1
    names = sorted(os.listdir(disabled))
    assert names == ["dup.jar", "dup_1.jar"]
    # The OLD content is preserved (not overwritten)
    assert (tmp_path / "srv" / "mods" / ".disabled" / "dup.jar").read_bytes() == b"OLD_DISABLED"


def test_disable_mods_silently_skips_missing_files(tmp_path):
    """Stale entries (file deleted between scan & disable) must not crash."""
    server, mods = _make_server_with_mods(tmp_path, [])
    entries = [ScanEntry(os.path.join(mods, "ghost.jar"), "ghost.jar", "client_only")]
    assert disable_mods(entries, mods) == 0


def test_disable_mods_creates_disabled_dir_lazily(tmp_path):
    server, mods = _make_server_with_mods(tmp_path, ["x.jar"])
    assert not os.path.exists(os.path.join(mods, ".disabled"))
    disable_mods(
        [ScanEntry(os.path.join(mods, "x.jar"), "x.jar", "client_only")],
        mods,
    )
    assert os.path.isdir(os.path.join(mods, ".disabled"))
