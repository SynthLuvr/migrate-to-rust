"""bench_ab.py acceptance tests (issue #34) — the matched-corpus A/B harness
driving the state.py shim (python fallback vs ported): identity-gated timing,
vendor-rot-proof repro, and gate evidence that resolve-gates consumes."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pipeline as pipeline_tool
from conftest import run_tool

CTX = {"today": date(2026, 10, 27), "substitutions": {}}

# payload that prints the shim value each side actually ran under — used by
# the leak/divergence/refusal tests to prove sides apart
ECHO_SIDE = 'print("side", os.environ["DEMO_FALLBACK"])'


def _script(slow_n: int, fast_n: int, payload: str = 'print("payload ok")',
            tail: str = "") -> str:
    """A fake target bench command: identical payload both sides, but the
    python (fallback) side does slow_n iterations vs the ported side's
    fast_n — a measurable, deterministic gap."""
    return ("import os, sys\n"
            'fallback = os.environ.get("DEMO_FALLBACK", "none") '
            'not in ("none", "")\n'
            f"n = {slow_n} if fallback else {fast_n}\n"
            "s = 0\n"
            "for i in range(n):\n"
            "    s += i\n"
            f"{payload}\n{tail}")


def _target(root: Path, script: str, name: str = "bench_cmd.py") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(script)
    return root


def _state(tmp_path: Path, target: Path | None, unit_status: str = "ported",
           gates: list | None = None, go_no_go: dict | None = None) -> Path:
    state = {
        "project": {"name": "demo", "python_path": "src/demo"},
        "stage": 6, "run_status": "running",
        "units": [{
            "id": "u1", "python_path": "src/demo/a.py",
            "rust_path": "crates/demo/src/u1.rs", "deps": [], "order": 1,
            "status": unit_status,
        }],
        "human_gates": gates or [], "journal": [],
    }
    if target is not None:
        state["preflight"] = {"target": {"root": str(target)}}
    if go_no_go:
        state["go_no_go"] = go_no_go
    p = tmp_path / "state.json"
    p.write_text(json.dumps(state))
    return p


def _run(monkeypatch, capsys, state: Path, *, cmd: str = "bench_cmd.py",
         out: str | None = None, extra: tuple = ()):
    argv = ["--state", str(state)]
    if out:
        argv += ["--out", str(state.parent / out)]
    argv += list(extra) + ["--", sys.executable, cmd]
    return run_tool(monkeypatch, capsys, "bench_ab.py", argv)


# --------------------------------------------------------------- the A/B --

def test_matched_ab_produces_identity_gated_artifact(monkeypatch, capsys, tmp_path):
    target = _target(tmp_path / "target", _script(slow_n=2_000_000, fast_n=100))
    state = _state(tmp_path, target)
    code, out = _run(monkeypatch, capsys, state)
    assert code == 0
    # the env var is derived exactly as state.py shim derives it
    assert out["env_var"] == "DEMO_FALLBACK"
    assert out["sides"]["python"]["env"] == {"DEMO_FALLBACK": "u1"}
    assert out["sides"]["ported"]["env"] == {"DEMO_FALLBACK": "none"}
    assert out["units"]["rust_live"] == {"u1": "ported"}
    assert out["units"]["fallback_list"] == ["u1"]
    # identity first: no ratio without it
    assert out["identity"]["stdout"]["verdict"] == "identical"
    assert out["ratio"]["median_speedup"] > 1
    assert out["improvement_pct"] > 50      # python side does 2000x the work
    assert out["timings"]["python"]["wall_s"]["median_s"] > \
        out["timings"]["ported"]["wall_s"]["median_s"]
    # artifacts on disk: per-side outputs, the artifact, the repro
    outdir = Path(out["artifact"]).parent
    assert (outdir / "python.0.stdout").read_text() == "payload ok\n"
    assert (outdir / "ported.0.stdout").read_text() == "payload ok\n"
    repro = (outdir / "bench-ab.repro.sh").read_text()
    assert "DEMO_FALLBACK=u1" in repro and "DEMO_FALLBACK=none" in repro
    assert f"{sys.executable} bench_cmd.py" in repro
    artifact = json.loads((outdir / "bench-ab.json").read_text())
    assert artifact["ratio"] == out["ratio"]  # disk == stdout, verbatim


def test_env_var_matches_state_py_shim(monkeypatch, capsys, tmp_path):
    target = _target(tmp_path / "target", _script(2_000_000, 100))
    state = _state(tmp_path, target)
    _, out = _run(monkeypatch, capsys, state)
    _, shim = run_tool(monkeypatch, capsys, "state.py",
                       ["shim", str(state), "--unit", "u1"])
    assert out["env_var"] == shim["env_var"] == "DEMO_FALLBACK"
    # and the shim hint tells the operator where the switch gets installed
    assert any("state.py shim" in h and "--unit u1" in h
               for h in out["units"]["shim_hint"])


def test_ambient_env_var_does_not_leak_into_the_ported_side(
        monkeypatch, capsys, tmp_path):
    # an inherited DEMO_FALLBACK=all must not flip the ported side: the
    # harness sets the var explicitly on both sides (the command echoes it,
    # so the artifacts prove which value each side saw)
    target = _target(tmp_path / "target", _script(10, 10, payload=ECHO_SIDE))
    state = _state(tmp_path, target)
    monkeypatch.setenv("DEMO_FALLBACK", "all")
    code, out = _run(monkeypatch, capsys, state)
    assert code == 2  # the sides legitimately print different values here
    outdir = Path(out["artifact"]).parent
    assert (outdir / "python.0.stdout").read_text() == "side u1\n"
    assert (outdir / "ported.0.stdout").read_text() == "side none\n"


def test_divergent_outputs_abort_without_ratio(monkeypatch, capsys, tmp_path):
    target = _target(tmp_path / "target", _script(10, 10, payload=ECHO_SIDE))
    state = _state(tmp_path, target)
    code, out = _run(monkeypatch, capsys, state)
    assert code == 2
    assert out["identity"]["stdout"]["verdict"] == "divergent"
    assert out["ratio"] is None and out["improvement_pct"] is None
    assert out["first_divergence"][0]["python"] == "side u1"
    assert out["first_divergence"][0]["ported"] == "side none"
    assert "NO ratio claim" in out["summary"]["human"]


def test_masked_identity_is_recorded_as_masked_never_identical(
        monkeypatch, capsys, tmp_path):
    # a bench that prints its own duration: volatile field, masked explicitly
    script = _script(2_000_000, 100,
                     payload='import time\nprint(f"payload ok; took '
                             '{time.perf_counter() % 1:.6f}s")')
    target = _target(tmp_path / "target", script, name="bench_timings.py")
    state = _state(tmp_path, target)
    code, out = _run(monkeypatch, capsys, state, cmd="bench_timings.py")
    assert code == 2  # unmasked: the duration line differs -> divergent
    code, out = _run(monkeypatch, capsys, state, cmd="bench_timings.py",
                     out="bench-mask", extra=("--mask", "took [0-9.]+s"))
    assert code == 0
    assert out["identity"]["stdout"]["verdict"] == "masked_identical"
    assert any("masking" in w for w in out["warnings"])
    assert out["ratio"]["median_speedup"] > 1


def test_failed_run_is_findings_not_a_bench(monkeypatch, capsys, tmp_path):
    target = _target(tmp_path / "target",
                     _script(100, 100, tail="sys.exit(3)"))
    state = _state(tmp_path, target)
    code, out = _run(monkeypatch, capsys, state)
    assert code == 2
    assert out["ratio"] is None
    assert any("exited 3" in w for w in out["warnings"])


def test_timeout_is_recorded_honestly(monkeypatch, capsys, tmp_path):
    target = _target(tmp_path / "target",
                     "import time\ntime.sleep(30)\nprint('payload ok')\n")
    state = _state(tmp_path, target)
    code, out = _run(monkeypatch, capsys, state, extra=("--timeout", "1"))
    assert code == 2
    assert all(r["timeout"] for side in ("python", "ported")
               for r in out["runs"][side])
    assert out["ratio"] is None
    # no captured payload on a timeout is not_evaluable — never 'identical'
    assert out["identity"]["stdout"]["verdict"] == "not_evaluable"


def test_repeat_and_warmup_runs_are_all_recorded(monkeypatch, capsys, tmp_path):
    target = _target(tmp_path / "target", _script(200_000, 100))
    state = _state(tmp_path, target)
    code, out = _run(monkeypatch, capsys, state, out="bench-r2",
                     extra=("--repeat", "2", "--warmup", "1"))
    assert code == 0
    assert [r["label"] for r in out["runs"]["python"]] == \
        ["python.w0", "python.0", "python.1"]
    assert [r["timed"] for r in out["runs"]["ported"]] == [False, True, True]
    assert out["timings"]["ported"]["wall_s"]["n"] == 2  # warmup excluded


def test_refusals(monkeypatch, capsys, tmp_path):
    target = _target(tmp_path / "target", _script(100, 100))
    # nothing ported: the shim env var would flip nothing
    state = _state(tmp_path, target, unit_status="pending")
    code, out = _run(monkeypatch, capsys, state)
    assert code == 1 and "no unit is live on the rust side" \
        in out["error"]["detail"]
    # retired units have no callable fallback either
    state = _state(tmp_path, target, unit_status="retired")
    code, out = _run(monkeypatch, capsys, state)
    assert code == 1
    # no bench command after --
    state = _state(tmp_path, target)
    code, out = run_tool(monkeypatch, capsys, "bench_ab.py",
                         ["--state", str(state)])
    assert code == 1 and "'--'" in out["error"]["detail"]
    # no cwd: nothing recorded to run in
    state = _state(tmp_path, None)
    code, out = _run(monkeypatch, capsys, state)
    assert code == 1 and "--cwd" in out["error"]["detail"]


# ----------------------------------------------------------- gate writes --

def test_gate_redesign_approval_evidence_is_consumed_by_resolve_gates(
        monkeypatch, capsys, tmp_path):
    target = _target(tmp_path / "target", _script(2_000_000, 100))
    state = _state(tmp_path, target, gates=[{
        "gate": "redesign_approval", "state": "open",
        "note": "u1 spike: dict -> flat vec",
        "evidence": ["spike_verdict=equivalent"],  # the oracle's half
    }])
    code, out = _run(monkeypatch, capsys, state,
                     extra=("--gate", "redesign_approval"))
    assert code == 0
    assert out["gate"]["created"] is False
    saved = json.loads(state.read_text())
    gate = saved["human_gates"][0]
    assert any(e.startswith("bench_improvement_pct=") for e in gate["evidence"])
    assert any(e.startswith("spike_verdict=") for e in gate["evidence"])
    assert saved["journal"][-1]["field"] == \
        "human_gates[gate=redesign_approval].evidence"
    # resolve-gates consumes it: RED-1 approves from the measured number
    policy = pipeline_tool.load_policy()
    decision = pipeline_tool.evaluate_gate(saved, gate, policy, CTX)
    assert decision["rule"] == "RED-1" and decision["decision"] == "approved"
    # and the state file is still valid for state.py
    code, v = run_tool(monkeypatch, capsys, "state.py",
                       ["validate", str(state)])
    assert code == 0 and "VALID" in v["summary"]["human"]


def test_gate_go_no_go_measurement_outranks_the_projection(
        monkeypatch, capsys, tmp_path):
    # Stage-0 projected 80%; the post-integration measurement (~5%) is what
    # GO-1 must read — "the projection bar is a claim until a matched
    # benchmark exists" (issue #34)
    target = _target(tmp_path / "target", _script(slow_n=20_000, fast_n=19_000))
    state = _state(tmp_path, target, go_no_go={
        "decision": "go", "scope": ["demo.parse"],
        "projection": {"end_to_end_saving_pct": 80.0, "scoped_loc": 900},
        "no_equivalent_deps": []})
    code, out = _run(monkeypatch, capsys, state,
                     extra=("--gate", "go_no_go"))
    assert code == 0
    assert out["gate"]["created"] is True  # no gate existed: opened for it
    saved = json.loads(state.read_text())
    gate = saved["human_gates"][0]
    assert gate["gate"] == "go_no_go" and gate["state"] == "open"
    assert any(e.startswith("end_to_end_saving_pct=") for e in gate["evidence"])
    policy = pipeline_tool.load_policy()
    decision = pipeline_tool.evaluate_gate(saved, gate, policy, CTX)
    # honest reporting: GO-1 reads the MEASUREMENT, not the rosy projection
    assert decision["decision"] == "rejected"
    assert decision["run_status"] == "no_go_measured"
    assert any("measured" in e for e in decision["evidence"])
    assert decision["go_decision"] == "no-go"


def test_gate_refuses_to_write_when_outputs_diverge(monkeypatch, capsys, tmp_path):
    target = _target(tmp_path / "target", _script(10, 10, payload=ECHO_SIDE))
    state = _state(tmp_path, target, gates=[{
        "gate": "redesign_approval", "state": "open",
        "evidence": ["spike_verdict=equivalent"]}])
    code, out = _run(monkeypatch, capsys, state,
                     extra=("--gate", "redesign_approval"))
    assert code == 2
    assert out["gate"] is None
    assert any("NOT written" in w for w in out["warnings"])
    saved = json.loads(state.read_text())
    assert saved["human_gates"][0]["evidence"] == ["spike_verdict=equivalent"]
    assert saved["journal"] == []
