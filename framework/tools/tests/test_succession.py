"""succession.py acceptance tests (issue #25).

The chain harness wraps a run in GLV orchestrator sessions so that
session-level budget_exhausted (the orchestrator's context window — the
motivating stall of run 20261028_4) becomes a scheduling event in code:
the successor spawns automatically with full resume context, guardrails
bound the chain, and only genuine terminals — or the run-level ceilings —
end it. A scripted fake GLV client stands in for the transport (the real
one is HTTP per tools/glv-subagents.md); the "agent work" each fake
session performs is a journaled mutation of the state file through
state.py, exactly what a real session does."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import succession as succession_tool
import pipeline as pipeline_tool
import state as state_tool
from conftest import TOOLS, run_tool


# ------------------------------------------------------------ fake GLV ----

class FakeGlv:
    """Scripted transport: start_session records the brief; wait_settled
    applies the next scripted mutation to the state file (the session's
    "work") and reports the session completed."""

    def __init__(self, state_path, script):
        self.state_path = Path(state_path)
        self.script = list(script)   # consumed by wait_settled, in order
        self.started = []            # [{id, name, prompt, working_dir}]
        self.stopped = []
        self.waits = []

    def start_session(self, *, name, prompt, working_dir=None, model=None):
        self.started.append({"id": f"sess_fake_s{len(self.started) + 1}",
                             "name": name, "prompt": prompt,
                             "working_dir": working_dir, "model": model})
        return {"id": self.started[-1]["id"], "status": "started"}

    def wait_settled(self, session_id, **_):
        self.waits.append(session_id)
        mutate = self.script.pop(0)
        state = json.loads(self.state_path.read_text())
        changes = mutate(state) or []
        state_tool.save_validated(state, self.state_path,
                                  actor="fake-glv-session", changes=changes)
        return {"status": "completed"}

    def stop(self, session_id):
        self.stopped.append(session_id)
        return {"status": "stopped"}


class FailingStartGlv(FakeGlv):
    """Session 1 runs; every later start_session fails (GLV died)."""

    def __init__(self, state_path, script, fail_from=2):
        super().__init__(state_path, script)
        self.fail_from = fail_from

    def start_session(self, **kw):
        if len(self.started) + 1 >= self.fail_from:
            raise succession_tool.GlvError("connection refused (fake)")
        return super().start_session(**kw)


def _wire_fake(monkeypatch, fake):
    monkeypatch.setattr(succession_tool, "GlvClient",
                        lambda url, token: fake)
    monkeypatch.setenv("GLV_BASE_URL", "http://glv.test")
    monkeypatch.setenv("GLV_API_TOKEN", "glv_at_test")


# --------------------------------------------------------- state helpers --

def _unit(uid, status="pending", rust_path=None, python_path=None):
    return {"id": uid, "python_path": python_path or f"src/demo/{uid}.py",
            "rust_path": rust_path, "deps": [], "order": 1,
            "status": status}


def _write_state(path: Path, state: dict) -> Path:
    path.write_text(json.dumps(state, indent=2))
    return path


def _midrun_state(tmp_path, *, units=None, budget=None, run_status="pending",
                  stage=0, extra=None):
    state = {
        "project": {"name": "demo", "python_path": "src/demo"},
        "stage": stage,
        "units": units if units is not None else
        [_unit("u1"), _unit("u2"), _unit("u3")],
        "human_gates": [], "journal": [],
        "budget_ledger": {"tokens_total": 0, "hours_human_total": 0.0,
                          "regression_triage_hours": 0.0},
        "autonomy": {"level": "L2", "policy_version": "1.1.0",
                     "budget": budget or {"max_tokens": 5_000_000,
                                          "max_wall_clock_minutes": 720,
                                          "max_chain_depth": 10},
                     "publish_allowed": False},
        "run_status": run_status,
    }
    if extra:
        state.update(extra)
    return _write_state(tmp_path / "state.json", state)


# scripted session outcomes (mutate state through state.py, journaled) --

def _set(field, unit, old, new):
    return unit, field, old, new


def _session_run(final_status, *, stage=None, ledger=None, unit_status=None,
                 parity_for=()):
    """A scripted session: real work (stage/units/ledger) journaled through
    state.py, then run_status left at final_status — the budget_exhausted
    shape is the motivating 20261028_4 stall."""
    def mutate(state):
        changes = []
        if stage is not None and state.get("stage") != stage:
            changes.append(_set("stage", None, state.get("stage"), stage))
            state["stage"] = stage
        for uid, status in (unit_status or {}).items():
            unit = next((u for u in state.get("units", [])
                         if u["id"] == uid), None)
            if unit is None or unit.get("status") == status:
                continue
            changes.append(_set(f"units[{uid}].status", uid,
                                unit.get("status"), status))
            unit["status"] = status
            if status in state_tool.RUST_LIVE_STATUSES and uid in parity_for:
                unit["parity"] = {"runs": 30, "equivalent": 30,
                                  "divergent": 0, "incomparable": 0,
                                  "last_verdict": "equivalent"}
                changes.append(_set(f"units[{uid}].parity", uid, None,
                                    unit["parity"]))
        if ledger is not None:
            old = (state.get("budget_ledger") or {}).get("tokens_total", 0)
            state.setdefault("budget_ledger", {})["tokens_total"] = ledger
            changes.append(_set("budget_ledger.tokens_total", None, old,
                                ledger))
        changes.append(_set("run_status", None, state.get("run_status"),
                            final_status))
        state["run_status"] = final_status
        return changes
    return mutate


def _session_exhausts(**kwargs):
    return _session_run("budget_exhausted", **kwargs)


def _session_completes(**kwargs):
    kwargs.setdefault("stage", 7)
    return _session_run("complete", **kwargs)


def _session_noop_exhausts():
    """A successor that changes nothing and re-exhausts — the loop shape
    the monotonic-progress guard exists to kill."""
    def mutate(state):
        return [_set("run_status", None, state.get("run_status"),
                     "budget_exhausted")]
    return mutate


def _load(path):
    return json.loads(Path(path).read_text())


# ------------------------------------------------------------- classify ----

def _one_session_state(run_status="budget_exhausted", *, tokens=600_000,
                       stage=4, decided=1):
    units = ([_unit("u1", "ported"), _unit("u2")]
             if decided else [_unit("u1"), _unit("u2")])
    return {"run_status": run_status, "stage": stage, "units": units,
            "budget_ledger": {"tokens_total": tokens},
            "session_chain": {"depth": 0, "max_depth": 10, "sessions": [{
                "index": 1, "id": "s1", "name": "run-demo-s1",
                "progress_snapshot": {"stage": 0, "unit_status": {
                    "pending": 2}, "tokens_spent": 0}}]}}


def test_classify_session_level_exhaustion_under_ceilings_succeeds():
    d = succession_tool._classify(_one_session_state(), max_tokens=5_000_000,
                                  max_minutes=720, max_depth=10)
    assert d["kind"] == "succeed"
    assert "session-level exhaustion" in d["reason"]


def test_classify_run_level_token_ceiling_is_genuine_exhaustion():
    d = succession_tool._classify(_one_session_state(tokens=6_000_000),
                                  max_tokens=5_000_000, max_minutes=720,
                                  max_depth=10)
    assert d["kind"] == "run_level_exhausted"
    assert "6000000 > 5000000" in d["reason"]


def test_classify_run_level_wall_clock_ceiling_across_the_chain():
    state = _one_session_state()
    state["session_chain"]["wall_clock_minutes"] = 800.0
    d = succession_tool._classify(state, max_tokens=5_000_000,
                                  max_minutes=720, max_depth=10)
    assert d["kind"] == "run_level_exhausted" and "wall-clock" in d["reason"]


def test_classify_genuine_terminals_end_the_chain():
    for status in ("complete", "no_go_measured", "failed_safely"):
        d = succession_tool._classify(_one_session_state(status),
                                      max_tokens=5_000_000, max_minutes=720,
                                      max_depth=10)
        assert d["kind"] == "terminal" and d["run_status"] == status


def test_classify_crashed_session_without_terminal_state_also_succeeds():
    # a session killed by its context window before recording anything:
    # running is as much a scheduling event as budget_exhausted
    d = succession_tool._classify(_one_session_state("running"),
                                  max_tokens=5_000_000, max_minutes=720,
                                  max_depth=10)
    assert d["kind"] == "succeed"


def test_progress_guard_and_depth_cap_are_pure_state_functions():
    # no progress in the successor (still 1 decided unit, same stage):
    # two sessions recorded, the successor (s2) last with its spawn snapshot
    state = _one_session_state()
    state["session_chain"]["sessions"] = [
        {"index": 1, "id": "s1", "name": "run-demo-s1",
         "outcome": "budget_exhausted",
         "progress_snapshot": {"stage": 0, "unit_status": {
             "pending": 2}, "tokens_spent": 0}},
        {"index": 2, "id": "s2", "name": "run-demo-s2",
         "succession_reason": "session-level exhaustion",
         "progress_snapshot": {"stage": 4, "unit_status": {
             "pending": 1, "ported": 1}, "tokens_spent": 600_000}}]
    state["session_chain"]["depth"] = 1
    d = succession_tool._classify(state, max_tokens=5_000_000,
                                  max_minutes=720, max_depth=10)
    assert d["kind"] == "no_progress" and "infinite resume loop" in d["reason"]
    # same shape but with progress and the cap reached -> depth cap
    progressed = _one_session_state()
    progressed["units"] = [_unit("u1", "ported"), _unit("u2", "ported")]
    progressed["session_chain"]["sessions"] = [
        {"index": 1, "id": "s1", "name": "run-demo-s1",
         "outcome": "budget_exhausted"},
        {"index": 2, "id": "s2", "name": "run-demo-s2",
         "progress_snapshot": {"stage": 4, "unit_status": {
             "pending": 1, "ported": 1}, "tokens_spent": 600_000}}]
    progressed["session_chain"]["depth"] = 1
    d = succession_tool._classify(progressed, max_tokens=5_000_000,
                                  max_minutes=720, max_depth=2)
    assert d["kind"] == "depth_cap" and "depth cap 2" in d["reason"]


# ----------------------------------------------------------- full chain ----

def test_chain_reaches_complete_from_one_prompt_with_mid_run_succession(
        monkeypatch, capsys, tmp_path):
    """The acceptance run: ONE human prompt; session 1 exhausts its session
    budget mid-run (stage 4, work remaining); the harness spawns the
    successor automatically; the run finishes complete — budget_exhausted
    never survives as the final status."""
    target = tmp_path / "target"
    (target / "src").mkdir(parents=True)
    (target / "src" / "u_bars.rs").write_text("// the actual file (20261028_4: "
                                              "state said crates/…/src/u_bars.rs)\n")
    (target / "src" / "demo").mkdir(parents=True)
    (target / "src" / "demo" / "__init__.py").write_text("")
    for n in ("u1", "u2", "u3"):
        (target / "src" / "demo" / f"{n}.py").write_text("x = 1\n")

    p = _midrun_state(
        tmp_path,
        units=[_unit("u1"), _unit("u2"),
               _unit("u3", rust_path="crates/demo-core/src/u_bars.rs")],
        extra={"preflight": {"ok": True, "target": {"root": str(target)}}})
    prompt = tmp_path / "first-prompt.md"
    prompt.write_text("Migrate demo per the framework prompt. "
                      "Single human prompt; finish unattended.\n")

    fake = FakeGlv(p, [
        _session_exhausts(stage=4, ledger=600_000,
                          unit_status={"u1": "ported"}, parity_for=("u1",)),
        _session_completes(unit_status={"u2": "fallback_python",
                                        "u3": "ported"},
                           ledger=900_000, parity_for=("u3",)),
    ])
    _wire_fake(monkeypatch, fake)

    code, out = run_tool(monkeypatch, capsys, "succession.py",
                         ["chain", str(p), "--prompt-file", str(prompt),
                          "--working-dir", str(target)])
    assert code == 0, out
    assert out["run_status"] == "complete" and out["degraded"] is False
    assert out["chain"]["depth"] == 1 and len(out["chain"]["sessions"]) == 2

    # two GLV sessions, successor named per convention, both in the target
    assert [s["name"] for s in fake.started] == \
        ["run-demo-s1", "run-demo-s2"]
    assert all(s["working_dir"] == str(target) for s in fake.started)

    # session 1 rode the operator's single prompt + chain context
    first = fake.started[0]["prompt"]
    assert "Migrate demo per the framework prompt" in first
    assert str(p) in first and "successor session" in first.lower()

    # the successor's brief is the full resume context (issue #25 list)
    brief = fake.started[1]["prompt"]
    assert "Successor session 2" in brief
    assert str(p) in brief                      # state file path
    assert "Escalation dossier" in brief        # dossier
    assert "Journal tail" in brief              # journal
    assert "chain depth 1/" in brief            # chain history
    assert "4400000 remaining" in brief         # run-level budget remaining
    assert "never asks a human" in brief        # resume instructions
    assert "crates/demo-core/src/u_bars.rs" in brief  # hygiene finding

    # resume hygiene repaired the mis-recorded rust path (journaled)
    st = _load(p)
    units = {u["id"]: u for u in st["units"]}
    assert units["u3"]["rust_path"] == "src/u_bars.rs"
    assert any(e["field"] == "units[u3].rust_path" and e["to"] == "src/u_bars.rs"
               for e in st["journal"])

    # journal continuity across sessions: the full run_status history of a
    # chained run — start reset, session-1 exhaustion, succession reset,
    # successor completion (all through state.py, one journal)
    statuses = [(e["to"], e.get("actor")) for e in st["journal"]
                if e["field"] == "run_status"]
    assert statuses == [("running", "succession.py"),
                        ("budget_exhausted", "fake-glv-session"),
                        ("running", "succession.py"),
                        ("complete", "fake-glv-session")]

    # the chain record: outcomes, succession reason, stop-notice
    assert [s["outcome"] for s in st["session_chain"]["sessions"]] == \
        ["budget_exhausted", "complete"]
    assert st["session_chain"]["sessions"][1]["succession_reason"]
    assert len(st["session_chain"]["notices"]) == 1
    assert "stop-notice" in st["session_chain"]["notices"][0]
    assert any(e["event"] == "succession" for e in out["events"])

    # zero pending units at complete; deliverable set written
    assert all(u["status"] != "pending" for u in st["units"])
    report = tmp_path / "state.run-report.md"
    assert report.exists()
    text = report.read_text()
    assert "Session chain" in text and "run-demo-s2" in text

    # the finished chained state is coherent — and the mid-chain resume is
    # BY DESIGN, so the resumed-after-terminal warning is suppressed
    code, out = run_tool(monkeypatch, capsys, "state.py", ["validate", str(p)])
    assert code == 0, out["invariant_errors"]
    assert not [w for w in out["warnings"] if "resumed" in w]


def test_chain_enforces_run_level_ceilings_across_the_whole_chain(
        monkeypatch, capsys, tmp_path):
    """Successors get fresh SESSION budgets, but the run-level token ceiling
    sums across every session: past it, budget_exhausted is the honest
    terminal and no further successor spawns."""
    p = _midrun_state(tmp_path, budget={"max_tokens": 5000,
                                        "max_wall_clock_minutes": 720,
                                        "max_chain_depth": 10})
    fake = FakeGlv(p, [
        _session_exhausts(stage=4, ledger=3000, unit_status={"u1": "ported"}),
        _session_exhausts(ledger=6000, unit_status={"u2": "ported"}),
    ])
    _wire_fake(monkeypatch, fake)
    code, out = run_tool(monkeypatch, capsys, "succession.py",
                         ["chain", str(p), "--prompt-file", "/dev/null"])
    assert code == 0  # budget_exhausted is a result, not a failure
    assert out["run_status"] == "budget_exhausted"
    assert len(fake.started) == 2  # no third session: the run-level bound hit
    kinds = [e["kind"] for e in out["events"] if e["event"] == "classified"]
    assert kinds == ["succeed", "run_level_exhausted"]
    assert "6000 > 5000" in out["summary"]["human"]


def test_chain_depth_cap_ends_failed_safely_with_the_dossier(
        monkeypatch, capsys, tmp_path):
    p = _midrun_state(tmp_path)
    fake = FakeGlv(p, [
        _session_exhausts(stage=4, unit_status={"u1": "ported"}),
        _session_exhausts(stage=5, unit_status={"u2": "ported"}),
    ])
    _wire_fake(monkeypatch, fake)
    code, out = run_tool(monkeypatch, capsys, "succession.py",
                         ["chain", str(p), "--max-depth", "2",
                          "--prompt-file", "/dev/null"])
    assert code == 2
    assert out["run_status"] == "failed_safely"
    assert len(fake.started) == 2
    assert "depth cap 2" in out["summary"]["human"]
    st = _load(p)
    assert st["session_chain"]["max_depth"] == 2
    assert (tmp_path / "state.run-report.md").exists()


def test_chain_monotonic_progress_guard_kills_resume_loops(
        monkeypatch, capsys, tmp_path):
    p = _midrun_state(tmp_path)
    fake = FakeGlv(p, [
        _session_exhausts(stage=4, unit_status={"u1": "ported"}),
        _session_noop_exhausts(),
    ])
    _wire_fake(monkeypatch, fake)
    code, out = run_tool(monkeypatch, capsys, "succession.py",
                         ["chain", str(p), "--prompt-file", "/dev/null"])
    assert code == 2 and out["run_status"] == "failed_safely"
    assert len(fake.started) == 2  # no third session without progress
    assert "no observable state" in out["summary"]["human"]


# -------------------------------------------------------- degraded mode ----

def test_chain_refuses_to_start_without_glv_degraded_mode_documented(
        monkeypatch, capsys, tmp_path):
    p = _midrun_state(tmp_path)
    monkeypatch.delenv("GLV_BASE_URL", raising=False)
    monkeypatch.delenv("GLV_API_TOKEN", raising=False)
    code, out = run_tool(monkeypatch, capsys, "succession.py",
                         ["chain", str(p), "--prompt-file", "/dev/null"])
    assert code == 2 and "GLV" in out["error"]
    assert "degraded" in out["error"]
    # the state file is untouched: no chain recorded, nothing spawned
    assert "session_chain" not in _load(p)


def test_chain_mid_run_glv_loss_parks_degraded_in_budget_exhausted(
        monkeypatch, capsys, tmp_path):
    p = _midrun_state(tmp_path)
    fake = FailingStartGlv(p, [
        _session_exhausts(stage=4, unit_status={"u1": "ported"}),
    ])
    _wire_fake(monkeypatch, fake)
    code, out = run_tool(monkeypatch, capsys, "succession.py",
                         ["chain", str(p), "--prompt-file", "/dev/null"])
    assert code == 0  # a terminal state, never a hang
    assert out["run_status"] == "budget_exhausted" and out["degraded"] is True
    st = _load(p)
    assert any("succession FAILED" in n
               for n in st["session_chain"]["notices"])


def test_chain_adopts_a_live_session_after_a_harness_restart(
        monkeypatch, capsys, tmp_path):
    """The harness itself can die: a re-invocation waits out the recorded
    (unsettlled) session instead of abandoning or duplicating it."""
    p = _midrun_state(tmp_path, run_status="running", extra={
        "session_chain": {"depth": 0, "max_depth": 10, "sessions": [{
            "index": 1, "id": "sess_live_1", "name": "run-demo-s1",
            "started_at": "2026-10-28T14:00:00Z",
            "succession_reason": None,
            "progress_snapshot": {"stage": 0, "unit_status": {
                "pending": 3}, "tokens_spent": 0}}]}})
    fake = FakeGlv(p, [_session_completes(
        unit_status={"u1": "ported", "u2": "ported", "u3": "ported"})])
    _wire_fake(monkeypatch, fake)
    code, out = run_tool(monkeypatch, capsys, "succession.py",
                         ["chain", str(p)])
    assert code == 0 and out["run_status"] == "complete"
    assert fake.waits == ["sess_live_1"]        # adopted, not restarted
    assert fake.started == []                   # no duplicate session
    assert out["chain"]["sessions"][0]["id"] == "sess_live_1"


def test_chain_refuses_finished_runs(monkeypatch, capsys, tmp_path):
    p = _midrun_state(tmp_path, run_status="complete")
    _wire_fake(monkeypatch, FakeGlv(p, []))
    code, out = run_tool(monkeypatch, capsys, "succession.py",
                         ["chain", str(p), "--prompt-file", "/dev/null"])
    assert code == 2 and "finished run" in out["error"]


# ------------------------------------------------------- resume hygiene ----

def test_hygiene_repair_and_unresolved(tmp_path, monkeypatch, capsys):
    target = tmp_path / "target"
    (target / "src").mkdir(parents=True)
    (target / "src" / "u_bars.rs").write_text("pub fn bars() {}\n")
    (target / "src" / "demo").mkdir(parents=True)
    (target / "src" / "demo" / "u1.py").write_text("x = 1\n")
    p = _midrun_state(
        tmp_path,
        units=[_unit("u-bars", rust_path="crates/wicksmith-core/src/u_bars.rs",
                     python_path="src/demo/u1.py"),
               _unit("u-missing", rust_path="crates/core/src/gone.rs")],
        extra={"preflight": {"ok": True, "target": {"root": str(target)}}})

    code, out = run_tool(monkeypatch, capsys, "succession.py",
                         ["hygiene", str(p)])
    assert code == 2  # u-missing (and the project dir) unresolved
    by_check = {f["check"]: f for f in out["findings"]}
    assert by_check["units[u-bars].rust_path"]["action"] == "repaired"
    assert by_check["units[u-bars].rust_path"]["found"].endswith(
        "src/u_bars.rs")
    assert by_check["units[u-missing].rust_path"]["action"] == "unresolved"

    st = _load(p)
    units = {u["id"]: u for u in st["units"]}
    assert units["u-bars"]["rust_path"] == "src/u_bars.rs"
    assert units["u-missing"]["rust_path"] == "crates/core/src/gone.rs"
    assert any(e["field"] == "units[u-bars].rust_path"
               and e["to"] == "src/u_bars.rs" for e in st["journal"])
    code, out = run_tool(monkeypatch, capsys, "state.py", ["validate", str(p)])
    assert code == 0, out["invariant_errors"]


def test_hygiene_never_guesses_an_ambiguous_repair(tmp_path, monkeypatch,
                                                   capsys):
    target = tmp_path / "target"
    for sub in ("alt1", "alt2"):
        (target / sub).mkdir(parents=True)
        (target / sub / "u_bars.rs").write_text("// one of two\n")
    p = _midrun_state(
        tmp_path, units=[_unit("u-bars", rust_path="crates/core/src/u_bars.rs")],
        extra={"preflight": {"ok": True, "target": {"root": str(target)}}})
    code, out = run_tool(monkeypatch, capsys, "succession.py",
                         ["hygiene", str(p), "--root", str(target)])
    assert code == 2
    f = next(f for f in out["findings"]
             if f["check"] == "units[u-bars].rust_path")
    assert f["action"] == "unresolved" and "2 candidate" in f["detail"]
    assert _load(p)["units"][0]["rust_path"] == "crates/core/src/u_bars.rs"


# --------------------------------------------------------------- brief ----

def test_brief_carries_the_complete_resume_context(tmp_path, monkeypatch,
                                                   capsys):
    p = _midrun_state(
        tmp_path, run_status="budget_exhausted", stage=4,
        units=[_unit("u1", "escalated"), _unit("u2"), _unit("u3")],
        extra={
            "budget_ledger": {"tokens_total": 600_000,
                              "hours_human_total": 0.0,
                              "regression_triage_hours": 0.0},
            "human_gates": [{"gate": "unit_escalation", "state": "deferred",
                             "note": "deferred via ESC-1",
                             "decided_by": "human"}],
            "journal": [{"timestamp": "2026-10-28T13:00:00Z", "unit": None,
                         "field": "stage", "from": 3, "to": 4,
                         "actor": "state.py"}],
            "session_chain": {"depth": 0, "max_depth": 10, "wall_clock_minutes":
                              104.0, "sessions": [{
                                  "index": 1, "id": "sess_1",
                                  "name": "run-demo-s1",
                                  "started_at": "2026-10-28T12:00:00Z",
                                  "settled_at": "2026-10-28T13:44:00Z",
                                  "glv_status": "completed",
                                  "outcome": "budget_exhausted",
                                  "succession_reason": None}]}})
    code, out = run_tool(monkeypatch, capsys, "succession.py",
                         ["brief", str(p)])
    assert code == 0 and out["next_session"] == 2
    brief = out["brief"]
    assert "Successor session 2" in brief
    assert str(p) in brief
    assert "u1" in brief and "unit_escalation" in brief  # dossier
    assert "stage" in brief and "2026-10-28T13:00:00Z" in brief  # journal tail
    assert "104.0 / 720" in brief
    assert "4400000 remaining" in brief


# ------------------------------------------------- policy / state wiring --

def test_policy_carries_the_chain_depth_cap():
    policy = pipeline_tool.load_policy()
    assert policy["budget"]["max_chain_depth"] == 10
    assert policy["version"]  # journaled everywhere


def test_policy_validation_rejects_a_bad_chain_depth(tmp_path):
    policy = pipeline_tool.load_policy()
    policy["budget"]["max_chain_depth"] = 0
    bad = tmp_path / "policy.json"
    bad.write_text(json.dumps(policy))
    with pytest.raises(state_tool.ToolError) as exc:
        pipeline_tool.load_policy(bad)
    assert "max_chain_depth" in str(exc.value)


def test_init_copies_the_chain_depth_into_the_autonomy_block(
        monkeypatch, capsys, tmp_path):
    p = tmp_path / "state.json"
    code, out = run_tool(monkeypatch, capsys, "state.py",
                         ["init", str(p), "--name", "demo",
                          "--python-path", "src/demo",
                          "--autonomy-level", "L2"])
    assert code == 0, out
    assert _load(p)["autonomy"]["budget"]["max_chain_depth"] == 10


def test_session_chain_invariants(tmp_path, monkeypatch, capsys):
    base = json.loads(_midrun_state(tmp_path).read_text())

    def with_chain(**chain):
        state = json.loads(json.dumps(base))
        state["session_chain"] = chain
        return _write_state(tmp_path / "state.json", state)

    # index gap
    p = with_chain(depth=1, max_depth=10, sessions=[
        {"index": 1, "id": "a", "name": "run-demo-s1"},
        {"index": 3, "id": "b", "name": "run-demo-s3"}])
    code, out = run_tool(monkeypatch, capsys, "state.py", ["validate", str(p)])
    assert code == 2
    assert any("index 3" in e for e in out["invariant_errors"])

    # depth disagrees with the session count
    p = with_chain(depth=0, max_depth=10, sessions=[
        {"index": 1, "id": "a", "name": "run-demo-s1"},
        {"index": 2, "id": "b", "name": "run-demo-s2"}])
    code, out = run_tool(monkeypatch, capsys, "state.py", ["validate", str(p)])
    assert code == 2
    assert any("depth" in e for e in out["invariant_errors"])

    # depth above the cap
    p = with_chain(depth=5, max_depth=3, sessions=[
        {"index": 1, "id": "a", "name": "run-demo-s1"},
        {"index": 2, "id": "b", "name": "run-demo-s2"},
        {"index": 3, "id": "c", "name": "run-demo-s3"},
        {"index": 4, "id": "d", "name": "run-demo-s4"},
        {"index": 5, "id": "e", "name": "run-demo-s5"},
        {"index": 6, "id": "f", "name": "run-demo-s6"}])
    code, out = run_tool(monkeypatch, capsys, "state.py", ["validate", str(p)])
    assert code == 2
    assert any("> max_depth" in e for e in out["invariant_errors"])


def test_chained_resume_does_not_warn_but_plain_resume_still_does(
        tmp_path, monkeypatch, capsys):
    base = {
        "project": {"name": "demo", "python_path": "src/demo"},
        "stage": 7, "units": [], "human_gates": [],
        "journal": [
            {"timestamp": "2026-10-01T10:00:00Z", "unit": None,
             "field": "run_status", "from": "running",
             "to": "budget_exhausted", "actor": "pipeline.py"},
            {"timestamp": "2026-10-01T11:00:00Z", "unit": None,
             "field": "run_status", "from": "budget_exhausted",
             "to": "complete", "actor": "succession.py"}],
        "run_status": "complete"}

    # without a chain: resuming after a terminal still asks for intent
    p = _write_state(tmp_path / "plain.json", json.loads(json.dumps(base)))
    code, out = run_tool(monkeypatch, capsys, "state.py", ["validate", str(p)])
    assert code == 0
    assert any("resumed" in w for w in out["warnings"])

    # with a recorded succession the resume is by design (issue #25)
    chained = json.loads(json.dumps(base))
    chained["session_chain"] = {
        "depth": 1, "max_depth": 10,
        "sessions": [
            {"index": 1, "id": "a", "name": "run-demo-s1"},
            {"index": 2, "id": "b", "name": "run-demo-s2",
             "succession_reason": "session-level budget_exhausted"}]}
    p = _write_state(tmp_path / "chained.json", chained)
    code, out = run_tool(monkeypatch, capsys, "state.py", ["validate", str(p)])
    assert code == 0
    assert not [w for w in out["warnings"] if "resumed" in w]


def test_pipeline_budget_exhausted_points_at_the_chain(monkeypatch, capsys,
                                                       tmp_path):
    """pipeline.py's own budget_exhausted exit is the RUN-level bound; its
    summary now says the session-level case chains automatically."""
    p = _midrun_state(tmp_path, run_status="pending",
                      budget={"max_tokens": 5000,
                              "max_wall_clock_minutes": 720,
                              "max_chain_depth": 10},
                      extra={"budget_ledger": {
                          "tokens_total": 9000, "hours_human_total": 0.0,
                          "regression_triage_hours": 0.0}})
    code, out = run_tool(monkeypatch, capsys, "pipeline.py",
                         ["run", str(p), "--dry-run"])
    assert code == 0 and out["run_status"] == "budget_exhausted"
    assert "succession.py" in out["summary"]["human"]
    assert "session-level" in out["summary"]["human"]


def test_run_report_includes_the_session_chain_section(tmp_path, monkeypatch,
                                                        capsys):
    p = _midrun_state(tmp_path, run_status="complete", extra={
        "session_chain": {"depth": 1, "max_depth": 10,
                          "wall_clock_minutes": 104.0,
                          "sessions": [
                              {"index": 1, "id": "a", "name": "run-demo-s1",
                               "outcome": "budget_exhausted"},
                              {"index": 2, "id": "b", "name": "run-demo-s2",
                               "outcome": "complete",
                               "succession_reason": "session-level"}],
                          "notices": ["stop-notice: s1 settled — s2 spawned"]}})
    code, out = run_tool(monkeypatch, capsys, "pipeline.py",
                         ["report", str(p)])
    assert code == 0
    text = out["report"]
    assert "Session chain" in text
    assert "run-demo-s2" in text and "stop-notice" in text
    assert "104" in text  # wall clock summed across the chain
