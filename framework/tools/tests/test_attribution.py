"""attribution.py acceptance tests — tools/README.md #7: byte-exact NOTICE
check, provenance comment required per generated file, fail on loss."""

from __future__ import annotations

import json

from conftest import run_tool

HEADER = [
    "# Copyright (c) 2020 Example Corp",
    "# SPDX-License-Identifier: Apache-2.0",
]
NOTICE_TEXT = "Example Corp NOTICE\nthird-party notices\n"


def _scaffold(tmp_path):
    orig = tmp_path / "orig"
    port = tmp_path / "port"
    (orig / "src" / "pkg").mkdir(parents=True)
    (port / "crates" / "pkg" / "src" / "u1").mkdir(parents=True)
    (orig / "src" / "pkg" / "parse.py").write_text(
        "\n".join(HEADER) + "\n\ndef parse(s):\n    return s.split(',')\n")
    (orig / "NOTICE").write_text(NOTICE_TEXT)
    (port / "NOTICE").write_text(NOTICE_TEXT)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "project": {"name": "pkg", "python_path": "src/pkg"},
        "stage": 5,
        "units": [{"id": "u1", "python_path": "src/pkg/parse.py",
                   "rust_path": "crates/pkg/src/u1/u1.rs",
                   "deps": [], "order": 1, "status": "ported"}],
    }))
    return orig, port, state


def _port_file(port) -> None:
    (port / "crates" / "pkg" / "src" / "u1" / "u1.rs").write_text(
        "// Copyright (c) 2020 Example Corp\n"
        "// SPDX-License-Identifier: Apache-2.0\n"
        "// Ported from src/pkg/parse.py (Apache-2.0)\n"
        "\npub fn parse(s: &str) -> Vec<&str> { s.split(',').collect() }\n")


def test_clean_port_passes(monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    _port_file(port)
    code, out = run_tool(monkeypatch, capsys, "attribution.py", [
        "check", "--original", str(orig), "--port", str(port), "--state", str(state)])
    assert code == 0
    assert out["ok"] is True
    assert out["notices"][0]["status"] == "identical"
    assert out["provenance"][0]["declared_ok"] is True
    assert out["provenance"][0]["header_preserved"] is True


def test_notice_byte_drift_fails(monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    _port_file(port)
    (port / "NOTICE").write_text(NOTICE_TEXT.replace("third-party", "3rd party"))
    code, out = run_tool(monkeypatch, capsys, "attribution.py", [
        "check", "--original", str(orig), "--port", str(port), "--state", str(state)])
    assert code == 2
    assert any(f["check"] == "notice" for f in out["failures"])


def test_missing_notice_fails(monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    _port_file(port)
    (port / "NOTICE").unlink()
    code, out = run_tool(monkeypatch, capsys, "attribution.py", [
        "check", "--original", str(orig), "--port", str(port), "--state", str(state)])
    assert code == 2
    assert any(f["check"] == "notice" and f["status"] == "missing"
               for f in out["failures"])


def test_missing_provenance_fails(monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    (port / "crates" / "pkg" / "src" / "u1" / "u1.rs").write_text(
        "// Copyright (c) 2020 Example Corp\n"
        "// SPDX-License-Identifier: Apache-2.0\n"
        "pub fn parse(s: &str) -> Vec<&str> { s.split(',').collect() }\n")
    code, out = run_tool(monkeypatch, capsys, "attribution.py", [
        "check", "--original", str(orig), "--port", str(port), "--state", str(state)])
    assert code == 2
    assert any(f["check"] == "provenance" and f["status"] == "missing-provenance"
               for f in out["failures"])


def test_wrong_provenance_fails(monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    _port_file(port)
    p = port / "crates" / "pkg" / "src" / "u1" / "u1.rs"
    p.write_text(p.read_text().replace("src/pkg/parse.py", "src/pkg/other.py"))
    code, out = run_tool(monkeypatch, capsys, "attribution.py", [
        "check", "--original", str(orig), "--port", str(port), "--state", str(state)])
    assert code == 2
    assert any(f["check"] == "provenance" and f["status"] == "wrong-provenance"
               for f in out["failures"])


def test_stripped_header_fails(monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    (port / "crates" / "pkg" / "src" / "u1" / "u1.rs").write_text(
        "// Ported from src/pkg/parse.py (Apache-2.0)\n"
        "pub fn parse(s: &str) -> Vec<&str> { s.split(',').collect() }\n")
    code, out = run_tool(monkeypatch, capsys, "attribution.py", [
        "check", "--original", str(orig), "--port", str(port), "--state", str(state)])
    assert code == 2
    assert any(f["check"] == "header" for f in out["failures"])


def test_unattributed_stray_file_fails_but_allowlist_excuses(
        monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    _port_file(port)
    stray = port / "crates" / "pkg" / "src" / "u1" / "generated.rs"
    stray.write_text("// template scaffold\nfn x() {}\n")
    code, out = run_tool(monkeypatch, capsys, "attribution.py", [
        "check", "--original", str(orig), "--port", str(port), "--state", str(state)])
    assert code == 2
    assert any(f["check"] == "unattributed-file" for f in out["failures"])

    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps(
        {"crates/pkg/src/u1/generated.rs": "rust-template scaffold, not derived"}))
    code, out = run_tool(monkeypatch, capsys, "attribution.py", [
        "check", "--original", str(orig), "--port", str(port), "--state", str(state),
        "--allowlist", str(allow)])
    assert code == 0
    assert out["unattributed_files"] == []


def test_original_without_header_trivially_passes(monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    (orig / "src" / "pkg" / "parse.py").write_text("def parse(s):\n    return s\n")
    (port / "crates" / "pkg" / "src" / "u1" / "u1.rs").write_text(
        "// Ported from src/pkg/parse.py (MIT)\npub fn parse(s: &str) -> &str { s }\n")
    code, out = run_tool(monkeypatch, capsys, "attribution.py", [
        "check", "--original", str(orig), "--port", str(port), "--state", str(state)])
    assert code == 0
    row = out["provenance"][0]
    assert row["header_preserved"] is True  # nothing to preserve — noted, not failed
