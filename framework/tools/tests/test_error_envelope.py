"""Regression tests for issue #20: every IO/usage-error path must emit the
uniform JSON error envelope on stdout (README "Uniform conventions" — a
refusal is a result, not a crash), with ``summary.exit_code`` mirroring the
process exit code. One representative failure per tool, plus subprocess-level
reproductions of the two original repros to prove the ``__main__`` wiring."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

import _common
from conftest import REPO_ROOT, TOOLS, run_tool


def _assert_envelope(code: int, out: dict, *, kind: str, exit_code: int = 1) -> str:
    """The envelope contract: error.kind/detail present, and the documented
    summary.exit_code == actual exit code, so stdout parsers never crash."""
    assert code == exit_code
    assert out["error"]["kind"] == kind
    detail = out["error"]["detail"]
    assert isinstance(detail, str) and detail
    assert out["summary"]["exit_code"] == code
    assert out["summary"]["human"] == detail
    return detail


# ------------------------------------------------- helper shape (the spec) --

def test_emit_error_envelope_shape(capsys):
    code = _common.emit_error("boom", tool="t.py", version=1)
    out = json.loads(capsys.readouterr().out)
    assert code == 1 == out["summary"]["exit_code"]
    assert out["tool"] == "t.py" and out["version"] == 1
    assert out["error"] == {"kind": "io", "detail": "boom"}
    assert out["summary"]["human"] == "boom"
    assert out["generated_at"]


def test_run_cli_genuine_bugs_still_crash():
    """The boundary converts refusals only; programming errors must still
    raise (a crash stays a crash — the convention is not a bug hider)."""
    def buggy(_argv=None):
        raise TypeError("logic bug")
    with pytest.raises(TypeError):
        _common.run_cli(buggy, tool="t.py", version=1)


# ---------------------------------------------------- the two issue repros --

def test_issue20_repro_contract_extract_missing_python_file(monkeypatch, capsys, tmp_path):
    """Repro 1: unit's python file missing -> JSON envelope, not a bare
    stderr line with empty stdout."""
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "project": {"name": "demo", "python_path": "src/demo"}, "stage": 2,
        "units": [{"id": "u1", "python_path": "src/demo/missing.py",
                   "rust_path": None, "deps": [], "order": 1,
                   "status": "pending"}]}))
    code, out = run_tool(monkeypatch, capsys, "contract_extract.py",
                         ["extract", "--state", str(state), "--root", str(tmp_path)])
    detail = _assert_envelope(code, out, kind="io")
    assert "u1" in detail and "src/demo/missing.py" in detail


def test_issue20_repro_port_plan_missing_hotspots_file(monkeypatch, capsys, tmp_path):
    """Repro 2: --hotspots points at a missing file -> JSON envelope, not an
    uncaught FileNotFoundError traceback with empty stdout."""
    missing = tmp_path / "does-not-exist.json"
    code, out = run_tool(monkeypatch, capsys, "port_plan.py",
                         ["plan", "--hotspots", str(missing),
                          "--project-root", str(tmp_path)])
    detail = _assert_envelope(code, out, kind="io")
    assert "does-not-exist.json" in detail


def test_issue20_subprocess_contract_extract(tmp_path):
    """End-to-end (python contract_extract.py ...): stdout parses as JSON,
    exit code mirrors summary.exit_code, stderr carries no bare text."""
    proc = subprocess.run(
        [sys.executable, str(TOOLS / "contract_extract.py"), "extract",
         "--state", str(REPO_ROOT / "framework/examples/migration-state.json"),
         "--out", str(tmp_path / "ct")],
        capture_output=True, text=True, cwd=str(REPO_ROOT))
    out = json.loads(proc.stdout)  # JSONDecodeError before the fix
    assert _assert_envelope(proc.returncode, out, kind="io")
    assert "python file not found" in out["error"]["detail"]
    assert proc.stderr == ""


def test_issue20_subprocess_port_plan(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(TOOLS / "port_plan.py"), "plan",
         "--hotspots", str(tmp_path / "does-not-exist.json"),
         "--project-root", str(REPO_ROOT)],
        capture_output=True, text=True, cwd=str(REPO_ROOT))
    out = json.loads(proc.stdout)  # empty stdout + traceback before the fix
    assert _assert_envelope(proc.returncode, out, kind="io")
    assert "FileNotFoundError" in out["error"]["detail"]
    assert proc.stderr == ""


# --------------------------- one representative refusal per tool (8/8) ----

def test_port_plan_hotspot_path_not_found(monkeypatch, capsys, tmp_path):
    hotspots = tmp_path / "hotspots.json"
    hotspots.write_text(json.dumps(
        [{"path": "src/ghost.py", "self_cpu_s": 1.0, "calls": 1}]))
    code, out = run_tool(monkeypatch, capsys, "port_plan.py",
                         ["plan", "--hotspots", str(hotspots),
                          "--project-root", str(tmp_path)])
    detail = _assert_envelope(code, out, kind="io")
    assert detail == "hotspot path not found: src/ghost.py"


def test_port_plan_hotspots_not_a_list(monkeypatch, capsys, tmp_path):
    hotspots = tmp_path / "hotspots.json"
    hotspots.write_text(json.dumps({"path": "x"}))
    code, out = run_tool(monkeypatch, capsys, "port_plan.py",
                         ["plan", "--hotspots", str(hotspots),
                          "--project-root", str(tmp_path)])
    _assert_envelope(code, out, kind="usage")


def test_contract_extract_missing_python_file(monkeypatch, capsys, tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "project": {"name": "demo", "python_path": "src/demo"}, "stage": 2,
        "units": [{"id": "u1", "python_path": "src/demo/missing.py",
                   "rust_path": None, "deps": [], "order": 1,
                   "status": "pending"}]}))
    code, out = run_tool(monkeypatch, capsys, "contract_extract.py",
                         ["extract", "--state", str(state), "--root", str(tmp_path)])
    detail = _assert_envelope(code, out, kind="io")
    assert "u1" in detail and "src/demo/missing.py" in detail


def test_contract_extract_state_has_no_units(monkeypatch, capsys, tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps(
        {"project": {"name": "demo", "python_path": "src/demo"}, "stage": 2,
         "units": []}))
    code, out = run_tool(monkeypatch, capsys, "contract_extract.py",
                         ["extract", "--state", str(state), "--root", str(tmp_path)])
    _assert_envelope(code, out, kind="usage")


def test_oracle_judge_manifest_missing_key(monkeypatch, capsys, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"reference": {}, "candidate": {}}))
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["judge", "--manifest", str(manifest)])
    detail = _assert_envelope(code, out, kind="usage")
    assert "corpus" in detail


def test_oracle_compare_runs_missing_list(monkeypatch, capsys, tmp_path):
    runs = tmp_path / "runs.json"
    runs.write_text(json.dumps({"reference": []}))
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["compare", "--runs", str(runs)])
    detail = _assert_envelope(code, out, kind="usage")
    assert "candidate" in detail


def test_bench_manifest_missing_key(monkeypatch, capsys, tmp_path):
    manifest = tmp_path / "bench.json"
    manifest.write_text(json.dumps({"name": "demo"}))
    code, out = run_tool(monkeypatch, capsys, "bench.py",
                         ["run", "--manifest", str(manifest)])
    detail = _assert_envelope(code, out, kind="usage")
    assert "corpus" in detail


def test_bench_empty_corpus(monkeypatch, capsys, tmp_path):
    (tmp_path / "corpus.jsonl").write_text("")
    manifest = tmp_path / "bench.json"
    manifest.write_text(json.dumps(
        {"name": "demo", "corpus": "corpus.jsonl",
         "python": {"entry": "pkg.a:run"}, "rust": {"entry": "pkg_rs:run"}}))
    code, out = run_tool(monkeypatch, capsys, "bench.py",
                         ["run", "--manifest", str(manifest)])
    detail = _assert_envelope(code, out, kind="usage")
    assert "corpus" in detail and "empty" in detail


def test_state_init_refuses_overwrite(monkeypatch, capsys, tmp_path):
    existing = tmp_path / "state.json"
    existing.write_text("{}")
    code, out = run_tool(monkeypatch, capsys, "state.py",
                         ["init", str(existing), "--name", "demo",
                          "--python-path", "src/demo"])
    detail = _assert_envelope(code, out, kind="io")
    assert "already exists" in detail


def test_attribution_tree_not_a_directory(monkeypatch, capsys, tmp_path):
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("x")
    code, out = run_tool(monkeypatch, capsys, "attribution.py",
                         ["check", "--original", str(not_a_dir),
                          "--port", str(tmp_path), "--state", str(tmp_path / "s.json")])
    detail = _assert_envelope(code, out, kind="io")
    assert "original" in detail and "not a directory" in detail


def test_pyo3_review_nothing_to_review(monkeypatch, capsys):
    code, out = run_tool(monkeypatch, capsys, "pyo3_review.py", ["review"])
    detail = _assert_envelope(code, out, kind="usage")
    assert "nothing to review" in detail


def test_upstream_watch_not_a_git_repo(monkeypatch, capsys, tmp_path, mini_state):
    code, out = run_tool(monkeypatch, capsys, "upstream_watch.py",
                         ["watch", "--state", str(mini_state),
                          "--repo", str(tmp_path)])
    detail = _assert_envelope(code, out, kind="io")
    assert "not a git repository" in detail


# ---------------------------------------------------- malformed JSON input --

def test_malformed_json_input_is_a_refusal_not_a_crash(monkeypatch, capsys, tmp_path):
    """The boundary turns OSError/ValueError from input files (missing,
    unreadable, malformed) into envelopes for every tool — shown here for
    oracle with a truncated manifest."""
    bad = tmp_path / "manifest.json"
    bad.write_text('{"reference": ')
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["judge", "--manifest", str(bad)])
    detail = _assert_envelope(code, out, kind="usage")
    assert "JSONDecodeError" in detail


def test_human_flag_echoes_error_summary_to_stderr(capsys, tmp_path):
    """--human must behave identically on error envelopes: JSON on stdout,
    the embedded summary echoed to stderr."""
    import port_plan
    from _common import run_cli
    code = run_cli(port_plan.main,
                   ["--human", "plan", "--hotspots", str(tmp_path / "no.json"),
                    "--project-root", str(tmp_path)],
                   tool=port_plan.TOOL, version=port_plan.VERSION)
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    _assert_envelope(code, out, kind="io")
    assert captured.err == out["summary"]["human"] + "\n"
