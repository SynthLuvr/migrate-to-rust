"""Memory containment acceptance tests — issue #74 (the enforcement gap
behind #73's overnight OOM kill loop).

Two layers, both exercised here:
- per-child scopes at the run_child choke point: `mem_limit_mb` wraps the
  command in `systemd-run --user --scope -p MemoryMax=<n>M …`, with a
  graceful unscoped fallback (never a hard failure — the harness stays
  portable);
- the forced breach: a child ballooning past its scope's ceiling dies
  ALONE (SIGKILL -> exit 137, no TimeoutExpired), the oracle maps that to
  setup_error -> verdict incomparable — the unit-level escalation ESC-1
  defers — and this process's cgroup (the stand-in for the session
  server's unit) is untouched.

The live-breach tests need a user systemd manager + cgroup v2 and skip
everywhere else; every other test is deterministic (fakes/probes only)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import _common
import delegate
import oracle
import pipeline
from conftest import run_tool

# a Python child that allocates ~512 MB in touched 8 MiB chunks — pages
# are written, not just reserved, so RSS actually grows past a 64 MB
# MemoryMax (bytearray alone can ride untouched zero pages)
BALLOON_CHILD = (
    "data = []\n"
    "for _ in range(64):\n"
    "    b = bytearray(8 << 20)\n"
    "    for i in range(0, len(b), 4096):\n"
    "        b[i] = 1\n"
    "    data.append(b)\n"
    "print(len(data))\n"
)


def _user_scope_available() -> bool:
    """True when `systemd-run --user --scope` actually works here — probed
    once with a real no-op scope, not assumed from PATH (a container CI
    can have the binary and no user manager)."""
    if shutil.which("systemd-run") is None:
        return False
    try:
        proc = subprocess.run(
            ["systemd-run", "--user", "--scope", "--collect",
             f"--unit=run-child-probe-{os.getpid()}", "--",
             sys.executable, "-c", "pass"],
            capture_output=True, text=True, timeout=60)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


requires_user_scope = pytest.mark.skipif(
    not _user_scope_available(),
    reason="forced-breach tests need systemd-run --user --scope "
           "(user manager + cgroup v2); everything else here is deterministic")


# ------------------------------------------------------------- pure ------

def test_scope_argv_builds_the_documented_prefix():
    argv, unit = _common.scope_argv(["python", "-c", "pass"], 64,
                                    systemd_run="/usr/bin/systemd-run")
    # exact, in order — the issue #74 contract
    assert argv == ["/usr/bin/systemd-run", "--user", "--scope",
                    "-p", "MemoryMax=64M", "-p", "MemorySwapMax=0",
                    f"--unit={unit}", "--", "python", "-c", "pass"]
    assert unit  # a named scope: a breach is attributable in the journal


def test_scope_argv_names_and_sanitizes_units():
    argv, unit = _common.scope_argv(["/weird dir/py thon3.11-x", "x"], 128,
                                    systemd_run="systemd-run")
    assert unit.startswith("run-child-") and " " not in unit
    assert f"--unit={unit}" in argv
    argv2, unit2 = _common.scope_argv(["python"], 128, unit="mine",
                                      systemd_run="systemd-run")
    assert unit2 == "mine" and "--unit=mine" in argv2


def test_scope_argv_unavailable_returns_command_unchanged(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    cmd = ["python", "-c", "pass"]
    argv, unit = _common.scope_argv(cmd, 64)
    assert argv is cmd and unit is None  # caller falls back to unscoped


@pytest.mark.parametrize("bad", [0, -1, None])
def test_scope_argv_rejects_nonpositive_limits(bad):
    with pytest.raises(ValueError):
        _common.scope_argv(["python"], bad, systemd_run="systemd-run")


# ------------------------------------------------------- run_child -------

def test_run_child_unscoped_path_is_unchanged():
    """mem_limit_mb=None must behave exactly as before: no systemd-run,
    no warning marker, byte-identical capture."""
    rc, out, err = _common.run_child(
        [sys.executable, "-c", "import sys; print('plain'); "
         "sys.stderr.write('noise\\n')"], timeout=60)
    assert rc == 0 and out == "plain\n" and err == "noise\n"
    assert _common._SCOPE_WARN not in err


def test_run_child_falls_back_unscoped_when_systemd_run_missing(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    rc, out, err = _common.run_child(
        [sys.executable, "-c", "print('still ran')"],
        mem_limit_mb=64, timeout=60)
    assert rc == 0 and out == "still ran\n"  # the tool never hard-fails
    assert _common._SCOPE_WARN in err and "systemd-run not on PATH" in err


class _FakeRun:
    """Deterministic subprocess.run stand-in recording every call."""

    def __init__(self, results):
        self.results, self.calls = list(results), []

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs))
        r = self.results.pop(0)
        return r(cmd) if callable(r) else r


def test_scope_startup_failure_retries_unscoped_once(monkeypatch):
    """Scope never created (container CI, no user bus) -> the command
    never ran -> one unscoped retry with a warning, never a crash."""
    monkeypatch.setattr(shutil, "which",
                        lambda name: "/usr/bin/systemd-run")
    fake = _FakeRun([
        subprocess.CompletedProcess(None, 1, "",
                                    "Failed to connect to bus: No medium found"),
        subprocess.CompletedProcess(None, 0, "ran\n", ""),
    ])
    monkeypatch.setattr(subprocess, "run", fake)
    rc, out, err = _common.run_child([sys.executable, "-c", "print('ran')"],
                                     mem_limit_mb=64, timeout=30)
    assert (rc, out) == (0, "ran\n")
    assert _common._SCOPE_WARN in err and "Failed to connect to bus" in err
    first, second = fake.calls[0][0], fake.calls[1][0]
    assert first[0] == "/usr/bin/systemd-run" and \
        "-p" in first and "MemoryMax=64M" in first
    assert second[0] == sys.executable  # the bare command, unscoped


def test_a_child_that_ran_is_never_rerun(monkeypatch):
    """The fallback must not re-execute side effects: a scoped attempt
    that produced stdout (the child RAN and failed) is returned as-is —
    even when its stderr happens to carry a startup-failure signature."""
    monkeypatch.setattr(shutil, "which",
                        lambda name: "/usr/bin/systemd-run")
    ran = {"second": False}

    def explode(*a, **k):
        ran["second"] = True
        raise AssertionError("the child must not run twice")

    fake = _FakeRun([
        subprocess.CompletedProcess(None, 1, "partial output",
                                    "Failed to connect to bus: No medium found"),
        explode,
    ])
    monkeypatch.setattr(subprocess, "run", fake)
    rc, out, err = _common.run_child([sys.executable, "x"],
                                     mem_limit_mb=64, timeout=30)
    assert (rc, out) == (1, "partial output")
    assert _common._SCOPE_WARN not in err and not ran["second"]


# -------------------------------------------------- the forced breach ----

@requires_user_scope
def test_scoped_breach_kills_the_child_alone():
    """512 MB under a 64 MB MemoryMax: the child dies (non-zero, no
    TimeoutExpired raised), the kill is attributable to its named scope
    unit, and THIS process's cgroup — the stand-in for the session
    server's unit — is untouched (issue #74 acceptance: service stays up)."""
    own_cgroup = Path("/proc/self/cgroup").read_text()
    rc, out, err = _common.run_child(
        [sys.executable, "-c", BALLOON_CHILD], mem_limit_mb=64, timeout=240)
    assert rc != 0                       # SIGKILL -> 137 via systemd-run
    assert rc in (137, -9), (rc, err)
    assert "run-child-" in err           # journal names the scope unit
    assert Path("/proc/self/cgroup").read_text() == own_cgroup


@requires_user_scope
def test_oracle_maps_a_scoped_breach_to_incomparable(monkeypatch, capsys,
                                                     tmp_path):
    """The full #74 semantics: the ballooning candidate child is killed
    inside its scope, the oracle records setup_error, the verdict is
    incomparable (exit 3) — the escalation ESC-1 defers at L2+ — while the
    reference side and this process keep running."""
    (tmp_path / "ref.py").write_text("def run(v):\n    return v\n")
    (tmp_path / "cand.py").write_text(
        "def run(v):\n"
        "    data = []\n"
        "    for _ in range(64):\n"
        "        b = bytearray(8 << 20)\n"
        "        for i in range(0, len(b), 4096):\n"
        "            b[i] = 1\n"
        "        data.append(b)\n"
        "    return v\n")
    (tmp_path / "corpus.jsonl").write_text(
        json.dumps(1) + "\n" + json.dumps(2) + "\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "unit": "u-breach",
        "reference": {"entry": "ref:run"},
        "candidate": {"entry": "cand:run"},
        "corpus": "corpus.jsonl",
    }))
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["judge", "--manifest", str(manifest),
                          "--timeout", "60", "--mem-limit-mb", "64"])
    assert code == 3                     # incomparable, not a crash
    assert out["verdict"] == "incomparable"
    assert out["counts"]["incomparable"] == len(out["runs"])
    details = " ".join(r["detail"] or "" for r in out["runs"])
    assert "candidate setup failure" in details
    assert "-9" in details or "137" in details   # SIGKILL, either spelling


# ------------------------------------------------- choke-point routing ---

def test_delegate_invoke_agent_goes_through_run_child(monkeypatch, tmp_path):
    """Issue #74: _invoke_agent was the last spawn site bypassing
    run_child — the fixed env, captured output, hard timeout, and any
    memory scope now come from the single choke point."""
    seen = {}

    def fake_run_child(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return 0, "agent out", ""

    monkeypatch.setattr(delegate, "run_child", fake_run_child)
    rc, tail = delegate._invoke_agent(
        "translate {candidate} {rust_file}", {"candidate": "c1",
                                              "rust_file": "u1.rs"},
        cwd=tmp_path, timeout=7.5, env={"CARGO_TARGET_DIR": "/t"},
        mem_limit_mb=99)
    assert (rc, tail) == (0, "agent out")
    assert seen["cmd"] == ["translate", "c1", "u1.rs"]
    assert seen["kw"]["cwd"] == str(tmp_path)
    assert seen["kw"]["timeout"] == 7.5
    assert seen["kw"]["env_extra"] == {"CARGO_TARGET_DIR": "/t"}
    assert seen["kw"]["mem_limit_mb"] == 99


def test_delegate_invoke_agent_error_corners(monkeypatch, tmp_path):
    def raise_fnf(cmd, **kw):
        raise FileNotFoundError("no such agent")

    monkeypatch.setattr(delegate, "run_child", raise_fnf)
    rc, tail = delegate._invoke_agent("agent {unit}", {"unit": "u1"},
                                      cwd=tmp_path, timeout=5)
    assert rc == 127 and "not found" in tail

    def raise_timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(delegate, "run_child", raise_timeout)
    rc, tail = delegate._invoke_agent("agent {unit}", {"unit": "u1"},
                                      cwd=tmp_path, timeout=5)
    assert rc == 124 and "timed out" in tail


def test_pipeline_invoke_agent_goes_through_run_child(monkeypatch, tmp_path):
    seen = {}

    def fake_run_child(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        return 0, "stage done", ""

    monkeypatch.setattr(pipeline, "run_child", fake_run_child)
    rc, tail = pipeline._invoke_agent("worker {stage} {state}", 3,
                                      tmp_path / "state.json",
                                      mem_limit_mb=512)
    assert (rc, tail) == (0, "stage done")
    assert seen["cmd"][0] == "worker" and seen["cmd"][1] == "3"
    assert seen["kw"]["timeout"] == 3600
    assert seen["kw"]["mem_limit_mb"] == 512

    def raise_fnf(cmd, **kw):
        raise FileNotFoundError("no such worker")

    monkeypatch.setattr(pipeline, "run_child", raise_fnf)
    assert pipeline._invoke_agent("w", 1, tmp_path / "s.json")[0] == 127


def test_oracle_judge_forwards_the_limit_to_its_children(monkeypatch, capsys,
                                                         tmp_path):
    (tmp_path / "ref.py").write_text("def run(v):\n    return v\n")
    (tmp_path / "cand.py").write_text("def run(v):\n    return v\n")
    (tmp_path / "corpus.jsonl").write_text(json.dumps(1) + "\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "unit": "u-1", "reference": {"entry": "ref:run"},
        "candidate": {"entry": "cand:run"}, "corpus": "corpus.jsonl",
    }))
    limits = []

    def fake_run_child(cmd, **kw):
        limits.append(kw.get("mem_limit_mb"))
        return 0, json.dumps({"i": 0, "ok": True, "value": 1}), ""

    monkeypatch.setattr(oracle, "run_child", fake_run_child)
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["judge", "--manifest", str(manifest),
                          "--mem-limit-mb", "64"])
    assert code == 0
    assert limits == [64, 64]            # reference child + candidate child


def test_delegate_judge_candidate_forwards_the_limit(monkeypatch):
    seen = {}

    def fake_run_child(cmd, **kw):
        seen["cmd"] = cmd
        return 0, json.dumps({"verdict": "equivalent", "counts": {},
                              "total": 1}), ""

    monkeypatch.setattr(delegate, "run_child", fake_run_child)
    rec = delegate._judge_candidate(Path("/tmp/m.json"), 30.0,
                                    mem_limit_mb=256)
    assert rec["judged"] and rec["verdict"] == "equivalent"
    assert seen["cmd"][-2:] == ["--mem-limit-mb", "256"]


def test_shipped_slice_file_declares_the_run_level_ceilings():
    """The runbook's launch commands quote MemoryHigh/Max/SwapMax — keep
    the shipped migration.slice in sync with them (issue #74)."""
    text = (Path(__file__).resolve().parent.parent / "migration.slice") \
        .read_text(encoding="utf-8")
    assert "[Slice]" in text
    for line in ("MemoryHigh=", "MemoryMax=", "MemorySwapMax=0"):
        assert any(l.startswith(line) for l in text.splitlines()), line
