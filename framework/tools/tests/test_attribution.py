"""attribution.py acceptance tests — tools/README.md #7: byte-exact NOTICE
check, provenance comment required per generated file, fail on loss."""

from __future__ import annotations

import json
from pathlib import Path

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


def _run(monkeypatch, capsys, orig, port, state, *extra):
    return run_tool(monkeypatch, capsys, "attribution.py", [
        "check", "--original", str(orig), "--port", str(port), "--state", str(state),
        *extra])


def test_clean_port_passes(monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    _port_file(port)
    code, out = _run(monkeypatch, capsys, orig, port, state)
    assert code == 0
    assert out["ok"] is True
    assert out["notices"][0]["status"] == "identical"
    assert out["provenance"][0]["declared_ok"] is True
    assert out["provenance"][0]["header_preserved"] is True


def test_notice_byte_drift_fails(monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    _port_file(port)
    (port / "NOTICE").write_text(NOTICE_TEXT.replace("third-party", "3rd party"))
    code, out = _run(monkeypatch, capsys, orig, port, state)
    assert code == 2
    assert any(f["check"] == "notice" for f in out["failures"])


def test_missing_notice_fails(monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    _port_file(port)
    (port / "NOTICE").unlink()
    code, out = _run(monkeypatch, capsys, orig, port, state)
    assert code == 2
    assert any(f["check"] == "notice" and f["status"] == "missing"
               for f in out["failures"])


def test_missing_provenance_fails(monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    (port / "crates" / "pkg" / "src" / "u1" / "u1.rs").write_text(
        "// Copyright (c) 2020 Example Corp\n"
        "// SPDX-License-Identifier: Apache-2.0\n"
        "pub fn parse(s: &str) -> Vec<&str> { s.split(',').collect() }\n")
    code, out = _run(monkeypatch, capsys, orig, port, state)
    assert code == 2
    assert any(f["check"] == "provenance" and f["status"] == "missing-provenance"
               for f in out["failures"])


def test_wrong_provenance_fails(monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    _port_file(port)
    p = port / "crates" / "pkg" / "src" / "u1" / "u1.rs"
    p.write_text(p.read_text().replace("src/pkg/parse.py", "src/pkg/other.py"))
    code, out = _run(monkeypatch, capsys, orig, port, state)
    assert code == 2
    assert any(f["check"] == "provenance" and f["status"] == "wrong-provenance"
               for f in out["failures"])


def test_stripped_header_fails(monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    (port / "crates" / "pkg" / "src" / "u1" / "u1.rs").write_text(
        "// Ported from src/pkg/parse.py (Apache-2.0)\n"
        "pub fn parse(s: &str) -> Vec<&str> { s.split(',').collect() }\n")
    code, out = _run(monkeypatch, capsys, orig, port, state)
    assert code == 2
    assert any(f["check"] == "header" for f in out["failures"])


def test_unattributed_stray_file_fails_but_allowlist_excuses(
        monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    _port_file(port)
    stray = port / "crates" / "pkg" / "src" / "u1" / "generated.rs"
    stray.write_text("// template scaffold\nfn x() {}\n")
    code, out = _run(monkeypatch, capsys, orig, port, state)
    assert code == 2
    assert any(f["check"] == "unattributed-file" for f in out["failures"])

    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps(
        {"crates/pkg/src/u1/generated.rs": "rust-template scaffold, not derived"}))
    code, out = _run(monkeypatch, capsys, orig, port, state,
                     "--allowlist", str(allow))
    assert code == 0
    assert out["unattributed_files"] == []


def test_original_without_header_trivially_passes(monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    (orig / "src" / "pkg" / "parse.py").write_text("def parse(s):\n    return s\n")
    (port / "crates" / "pkg" / "src" / "u1" / "u1.rs").write_text(
        "// Ported from src/pkg/parse.py (MIT)\npub fn parse(s: &str) -> &str { s }\n")
    code, out = _run(monkeypatch, capsys, orig, port, state)
    assert code == 0
    row = out["provenance"][0]
    assert row["header_preserved"] is True  # nothing to preserve — noted, not failed


# ---- issue #54: provenance grammar accepts `<path> @ <sha>` ----------------

def test_sha_pinned_provenance_passes(monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    (port / "crates" / "pkg" / "src" / "u1" / "u1.rs").write_text(
        "// Copyright (c) 2020 Example Corp\n"
        "// SPDX-License-Identifier: Apache-2.0\n"
        "// Ported from src/pkg/parse.py @ 3e5a348 (no LICENSE file at port time)\n"
        "\npub fn parse(s: &str) -> Vec<&str> { s.split(',').collect() }\n")
    code, out = _run(monkeypatch, capsys, orig, port, state)
    assert code == 0
    row = out["provenance"][0]
    assert row["declared_ok"] is True
    assert row["declared_paths"] == ["src/pkg/parse.py"]
    assert row["header_preserved"] is True


def test_sha_pin_without_parenthetical_passes(monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    (port / "crates" / "pkg" / "src" / "u1" / "u1.rs").write_text(
        "// Copyright (c) 2020 Example Corp\n"
        "// SPDX-License-Identifier: Apache-2.0\n"
        "// Ported from src/pkg/parse.py @ 3e5a348\n"
        "\npub fn parse(s: &str) -> Vec<&str> { s.split(',').collect() }\n")
    code, out = _run(monkeypatch, capsys, orig, port, state)
    assert code == 0
    assert out["provenance"][0]["declared_paths"] == ["src/pkg/parse.py"]


def test_quoted_provenance_path_with_spaces_passes(monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    (orig / "src" / "pkg" / "odd name.py").write_text("# header line\n")
    data = json.loads(state.read_text())
    data["units"][0]["python_path"] = "src/pkg/odd name.py"
    state.write_text(json.dumps(data))
    (port / "crates" / "pkg" / "src" / "u1" / "u1.rs").write_text(
        '// Ported from "src/pkg/odd name.py" @ 3e5a348 (Apache-2.0)\n'
        "// header line\nfn x() {}\n")
    code, out = _run(monkeypatch, capsys, orig, port, state)
    assert code == 0
    assert out["provenance"][0]["declared_paths"] == ["src/pkg/odd name.py"]


# ---- issue #54: --original resolution --------------------------------------

def test_original_defaults_to_preflight_target_root(monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    _port_file(port)
    data = json.loads(state.read_text())
    data["preflight"] = {"target": {"root": str(orig)}}
    state.write_text(json.dumps(data))
    code, out = run_tool(monkeypatch, capsys, "attribution.py", [
        "check", "--port", str(port), "--state", str(state)])
    assert code == 0
    assert Path(out["original"]) == orig.resolve()
    assert out["ok"] is True


def test_original_package_dir_still_runs_header_checks(monkeypatch, capsys, tmp_path):
    # the natural-but-wrong spelling `--original <repo>/src` must resolve
    # units and run header checks, not silently skip them (issue #54)
    orig, port, state = _scaffold(tmp_path)
    _port_file(port)
    code, out = _run(monkeypatch, capsys, orig / "src", port, state)
    assert code == 0
    row = out["provenance"][0]
    assert row["header_preserved"] is True
    assert not [w for w in out["warnings"] if w["check"] == "original-missing"]


def test_missing_original_and_no_preflight_root_is_usage_error(
        monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    _port_file(port)
    code, out = run_tool(monkeypatch, capsys, "attribution.py", [
        "check", "--port", str(port), "--state", str(state)])
    assert code == 1
    assert out["error"]["kind"] == "usage"
    assert "preflight.target.root" in out["error"]["detail"]


def test_preflight_root_wins_only_when_flag_absent(monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    _port_file(port)
    data = json.loads(state.read_text())
    data["preflight"] = {"target": {"root": str(tmp_path / "nowhere")}}
    state.write_text(json.dumps(data))
    code, out = _run(monkeypatch, capsys, orig, port, state)  # flag overrides state
    assert code == 0
    assert Path(out["original"]) == orig.resolve()


# ---- issue #54: scaffold-owned files ---------------------------------------

def test_scaffold_provenance_header_excuses_stray(monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    _port_file(port)
    (port / "crates" / "pkg" / "src" / "lib.rs").write_text(
        "// Scaffold provenance: rust-template @ b19c502b16df\npub fn x() {}\n")
    # '#' marker form (python shim, justfile-style comments)
    (port / "crates" / "pkg" / "src" / "u1" / "shim.py").write_text(
        "# Scaffold provenance: state.py shim, not a unit\nx = 1\n")
    code, out = _run(monkeypatch, capsys, orig, port, state)
    assert code == 0
    assert out["unattributed_files"] == []
    assert sorted(s["path"] for s in out["scaffold_files"]) == [
        "crates/pkg/src/lib.rs", "crates/pkg/src/u1/shim.py"]
    assert out["counts"]["scaffold_files"] == 2


def test_scaffold_files_report_their_provenance(monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    _port_file(port)
    (port / "crates" / "pkg" / "src" / "lib.rs").write_text(
        "// Scaffold provenance: rust-template @ b19c502b16df\npub fn x() {}\n")
    code, out = _run(monkeypatch, capsys, orig, port, state)
    assert out["scaffold_files"][0]["provenance"] == \
        ["rust-template @ b19c502b16df"]  # visible, never silently skipped


def test_empty_scaffold_header_still_fails(monkeypatch, capsys, tmp_path):
    orig, port, state = _scaffold(tmp_path)
    _port_file(port)
    (port / "crates" / "pkg" / "src" / "lib.rs").write_text(
        "// Scaffold provenance:\npub fn x() {}\n")  # cites nothing -> not attribution
    code, out = _run(monkeypatch, capsys, orig, port, state)
    assert code == 2
    assert any(f["check"] == "unattributed-file" and f["path"] == "crates/pkg/src/lib.rs"
               for f in out["failures"])
