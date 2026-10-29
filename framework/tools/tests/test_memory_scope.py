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
import textwrap
import time
from pathlib import Path

import pytest

import _common
import delegate
import oracle
import pipeline
from conftest import run_tool

# A child that allocates ~512 MB in touched 8 MiB chunks — pages are
# written, not just reserved, so RSS actually grows past a 64 MB MemoryMax
# (bytearray alone can ride untouched zero pages).
BALLOON_LOOP = (
    "data = []\n"
    "for _ in range(64):\n"
    "    b = bytearray(8 << 20)\n"
    "    for i in range(0, len(b), 4096):\n"
    "        b[i] = 1\n"
    "    data.append(b)\n"
)
BALLOON_CHILD = BALLOON_LOOP + "print(len(data))\n"


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


class _FakePopen:
    """Deterministic subprocess.Popen stand-in recording every spawn;
    ``_FakePopen.results`` holds (returncode, stdout, stderr) triples that
    communicate() hands back (a callable entry raises on spawn — the
    spawn itself is the scripted failure)."""

    def __init__(self, cmd, **kwargs):
        _FakePopen.calls.append((list(cmd), kwargs))
        r = _FakePopen.results.pop(0)
        if callable(r):  # a poison result: spawning again IS the failure
            r()
        rc, out, err = r
        self.pid, self.returncode = 4242, rc
        self._out, self._err = out, err

    def communicate(self, input=None, timeout=None):
        return self._out, self._err

    def kill(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_scope_startup_failure_retries_unscoped_once(monkeypatch):
    """Scope never created (container CI, no user bus) -> the command
    never ran -> one unscoped retry with a warning, never a crash."""
    monkeypatch.setattr(shutil, "which",
                        lambda name: "/usr/bin/systemd-run")
    _FakePopen.calls, _FakePopen.results = [], [
        (1, "", "Failed to connect to bus: No medium found"),
        (0, "ran\n", ""),
    ]
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    rc, out, err = _common.run_child([sys.executable, "-c", "print('ran')"],
                                     mem_limit_mb=64, timeout=30)
    assert (rc, out) == (0, "ran\n")
    assert _common._SCOPE_WARN in err and "Failed to connect to bus" in err
    first, second = _FakePopen.calls[0][0], _FakePopen.calls[1][0]
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

    _FakePopen.calls, _FakePopen.results = [], [
        (1, "partial output", "Failed to connect to bus: No medium found"),
        explode,
    ]
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
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
        + textwrap.indent(BALLOON_LOOP, "    ")
        + "    return v\n")
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


# ------------------------------------------- issue #73: REPL lifecycle ---

def _pid_gone(pid: int) -> bool:
    """True when pid no longer exists as a live process. A zombie (state
    Z in /proc) counts as gone: it holds no memory — the #73 orphan held
    hundreds of MB while blocked."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    try:  # /proc stat: state is the token after the (comm) field
        state = Path(f"/proc/{pid}/stat").read_text() \
            .rsplit(")", 1)[1].split()[0]
        return state == "Z"
    except (OSError, IndexError):
        return False


def _wait_gone(pid: int, deadline_s: float = 10.0) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if _pid_gone(pid):
            return True
        time.sleep(0.1)
    return False


def test_run_child_timeout_kills_the_whole_process_group(tmp_path):
    """#73 fix 3, the parent half: a run_child timeout SIGKILLs the child
    SESSION, not just the direct pid — pre-fix, a grandchild blocked on an
    inherited pipe survived the kill as an orphan holding RSS."""
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(textwrap.dedent("""
        import os, time
        print(os.getpid(), flush=True)
        time.sleep(120)
    """))
    middle = tmp_path / "middle.py"
    middle.write_text(textwrap.dedent(f"""
        import subprocess, sys, time
        p = subprocess.Popen([sys.executable, {str(grandchild)!r}],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        print(p.pid, flush=True)
        time.sleep(120)
    """))
    exc = None
    try:
        _common.run_child([sys.executable, str(middle)], timeout=2.0)
    except subprocess.TimeoutExpired as e:
        exc = e
    assert exc is not None
    grandchildren = [int(l) for l in (exc.stdout or "").split()
                     if l.isdigit()]
    assert len(grandchildren) == 1
    # the pre-fix orphan: a grandchild sleeping 120 s died with the group
    assert _wait_gone(grandchildren[0]), \
        f"grandchild pid {grandchildren[0]} outlived the timeout"


@pytest.mark.skipif(not Path("/proc").exists(), reason="/proc needed")
def test_repl_child_exits_when_its_judge_dies(tmp_path):
    """#73 fix 3, the child half: SIGKILL the judge and its REPL child —
    whose stdin write end is still held open by a THIRD process, the
    historic blocked-forever orphan — must exit on its ppid watchdog
    instead of living on holding RSS."""
    runner = tmp_path / "repl.py"
    runner.write_text(oracle.RUNNER_SRC)
    (tmp_path / "ref.py").write_text("def run(v):\n    return v\n")
    err_file = tmp_path / "repl.err"
    pid_file = tmp_path / "repl.pid"
    entry = str(tmp_path / "ref.py") + ":run"

    r, w = os.pipe()          # the REPL child's stdin; a keeper process
    os.set_inheritable(w, True)   # holds the write end open -> NO EOF,
    os.set_inheritable(r, True)   # only the watchdog can end the child
    keeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, pass_fds=(w,))
    os.close(w)

    judge = tmp_path / "judge.py"
    judge.write_text(textwrap.dedent(f"""
        import os, subprocess, sys, time
        err = open({str(err_file)!r}, "wb")
        p = subprocess.Popen([sys.executable, {str(runner)!r}, {entry!r}],
                             stdin=int(sys.argv[1]),
                             stdout=subprocess.DEVNULL, stderr=err)
        with open({str(pid_file)!r}, "w") as fh:
            fh.write(str(p.pid))
        time.sleep(1.5)     # the child arms its watchdog, blocks on stdin
        os._exit(0)         # the judge dies; the child must NOT survive
    """))
    mid = subprocess.Popen([sys.executable, str(judge), str(r)],
                           stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, pass_fds=(r,))
    os.close(r)
    try:
        assert mid.wait(timeout=30) == 0
        pid = int(pid_file.read_text())
        assert _wait_gone(pid, deadline_s=15.0), \
            f"REPL child pid {pid} outlived its judge — the #73 orphan"
        err = err_file.read_bytes()
        assert b"oracle-repl" in err and b"is gone" in err
    finally:
        keeper.kill()
        keeper.wait()


def test_repl_collects_reference_cycles_between_rows(tmp_path):
    """#73 fix 2: the REPL loop runs gc.collect() every GC_EVERY_N_ROWS
    rows. The judged entry spies on gc.collect and reports the call count
    as its output value — row GC_EVERY_N_ROWS+ must see >= 1 call."""
    n = oracle.GC_EVERY_N_ROWS
    (tmp_path / "spy.py").write_text(textwrap.dedent("""
        import gc
        _calls = []
        _real = gc.collect
        gc.collect = lambda *a, **k: (_calls.append(1), _real())[1]
        def run(v):
            cycle = set(); cycle.add(id(cycle))  # uncollectable by refcount
            return [len(_calls), v]
    """))
    rows = [(i, i) for i in range(n + 6)]
    records, terminal, _peak = oracle._run_entry_chunk(
        "spy.py:run", rows, cwd=tmp_path, timeout=60.0, mem_limit_mb=None)
    assert terminal is None
    assert records[n + 5]["ok"] is True
    assert records[n + 5]["value"][0] >= 1   # collected mid-run, on cadence
    assert records[0]["value"][0] == 0       # ...but not before the cadence


def test_repl_child_self_reports_peak_rss(tmp_path):
    """#73 fix 4: each REPL child's FINAL stdout line is the
    {"peak_rss_kb": N} meta record; _run_entry_chunk surfaces it."""
    (tmp_path / "ref.py").write_text("def run(v):\n    return v\n")
    records, terminal, peak = oracle._run_entry_chunk(
        "ref.py:run", [(0, {"x": 1}), (1, {"x": 2})], cwd=tmp_path,
        timeout=60.0, mem_limit_mb=None)
    assert terminal is None
    assert [r["ok"] for r in records] == [True, True]
    assert peak >= 1024  # a live CPython easily exceeds 1 MB high-water


def _tiny_judge_scaffold(tmp_path):
    (tmp_path / "ref.py").write_text("def run(v):\n    return v\n")
    (tmp_path / "cand.py").write_text("def run(v):\n    return v\n")
    (tmp_path / "corpus.jsonl").write_text(json.dumps(1) + "\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "unit": "u-73", "reference": {"entry": "ref:run"},
        "candidate": {"entry": "cand:run"}, "corpus": "corpus.jsonl",
    }))
    return manifest


def test_judge_bounds_children_by_default(monkeypatch, capsys, tmp_path):
    """#73 fix 1: a BARE judge run — exactly the issue's repro command —
    bounds every REPL child without any operator flag; --mem-limit-mb 0
    is the explicit opt-out."""
    manifest = _tiny_judge_scaffold(tmp_path)
    limits = []

    def fake_run_child(cmd, **kw):
        limits.append(kw.get("mem_limit_mb"))
        return 0, json.dumps({"i": 0, "ok": True, "value": 1}), ""

    monkeypatch.setattr(oracle, "run_child", fake_run_child)
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["judge", "--manifest", str(manifest)])
    assert code == 0
    assert limits == [oracle.DEFAULT_MEM_LIMIT_MB] * 2
    assert out["resources"]["mem_limit_mb"] == oracle.DEFAULT_MEM_LIMIT_MB
    assert out["resources"]["peak_rss_mb"] == {"reference": None,
                                               "candidate": None}

    limits.clear()
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["judge", "--manifest", str(manifest),
                          "--mem-limit-mb", "0"])
    assert code == 0
    assert limits == [None, None]
    assert out["resources"]["mem_limit_mb"] is None


def test_judge_reports_per_side_peak_rss(monkeypatch, capsys, tmp_path):
    """#73 fix 4: each side's children self-report peak RSS; the judge
    folds the max into the verdict payload's resources.peak_rss_mb."""
    manifest = _tiny_judge_scaffold(tmp_path)

    def fake_run_child(cmd, **kw):
        entry = cmd[-1]
        kb = 4096 if entry.startswith("ref") else 8192
        stdout = (json.dumps({"i": 0, "ok": True, "value": 1}) + "\n"
                  + json.dumps({"peak_rss_kb": kb}) + "\n")
        return 0, stdout, ""

    monkeypatch.setattr(oracle, "run_child", fake_run_child)
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["judge", "--manifest", str(manifest)])
    assert code == 0
    assert out["resources"]["peak_rss_mb"] == {"reference": 4.0,
                                               "candidate": 8.0}
