"""succession.py acceptance tests (issue #25).

The chain harness wraps a run in GLV orchestrator sessions so that
session-level budget_exhausted (the orchestrator's context window — the
motivating stall of run 20261028_4) becomes a scheduling event in code:
the successor spawns automatically with full resume context, guardrails
bound the chain, and only genuine terminals — or the run-level ceilings —
end it. A scripted fake GLV client stands in for the transport (the real
one is HTTP per tools/glv-subagents.md); the "agent work" each fake
session performs is a journaled mutation of the state file through
state.py, exactly what a real session does.

Issue #47: GLV is the unconditional transport — the harness bootstraps
it itself (probe/start `glv serve`, mint run-<project>-s<n> per
session, re-mint on 401, revive on connection loss); no alternate
operating mode exists anywhere in the framework (the grep-clean test
below pins that)."""

from __future__ import annotations

import json
import os
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
    assert out["run_status"] == "complete"
    assert out["chain"]["depth"] == 1 and len(out["chain"]["sessions"]) == 2

    # two GLV sessions, successor named per convention, both in the target
    assert [s["name"] for s in fake.started] == \
        ["run-demo-s1", "run-demo-s2"]
    assert all(s["working_dir"] == str(target) for s in fake.started)

    # session 1 rode the operator's single prompt + chain context
    first = fake.started[0]["prompt"]
    assert "Migrate demo per the framework prompt" in first
    assert str(p) in first and "successor session" in first.lower()
    assert "GLV bootstrap" in first        # issue #47: bootstrap first act

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
    assert "GLV bootstrap" in brief             # re-bootstrap from cold start

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


# ---------------------------------------------- GLV self-bootstrap (#47) ----

def _wire_selfbootstrap(monkeypatch, fake, *, ensure=None):
    """Wire the chain for the issue-#47 cold-start path: NO GLV_* env,
    nothing assumed — the harness must bootstrap itself. The transport
    seams (_ensure_server / _mint_token / GlvClient) are faked; returns
    the minted token names (the `glv token list` audit trail)."""
    monkeypatch.delenv("GLV_BASE_URL", raising=False)
    monkeypatch.delenv("GLV_API_TOKEN", raising=False)
    monkeypatch.setattr(succession_tool, "GlvClient",
                        lambda url, token: fake)
    monkeypatch.setattr(succession_tool, "_ensure_server",
                        ensure or (lambda url, **kw: "up"))
    minted = []

    def fake_mint(base_url, name):
        minted.append(name)
        return f"glv_at_fake_{name}"

    monkeypatch.setattr(succession_tool, "_mint_token", fake_mint)
    return minted


def test_chain_bootstraps_glv_and_mints_a_token_per_session(
        monkeypatch, capsys, tmp_path):
    """Cold start (issue #47 acceptance): no server configured, no env —
    the harness itself ensures the server, mints run-<project>-s<n> per
    chain session (the audit trail), exports the env vars, and the run
    still reaches complete from the one prompt."""
    p = _midrun_state(tmp_path)
    fake = FakeGlv(p, [
        _session_exhausts(stage=4, unit_status={"u1": "ported"}),
        _session_completes(unit_status={"u2": "ported", "u3": "ported"}),
    ])
    minted = _wire_selfbootstrap(monkeypatch, fake)

    code, out = run_tool(monkeypatch, capsys, "succession.py",
                         ["chain", str(p), "--prompt-file", "/dev/null"])
    assert code == 0, out
    assert out["run_status"] == "complete"
    # one named token per chain session — the `glv token list` trail
    assert minted == ["run-demo-s1", "run-demo-s2"]
    assert out["glv"] == {"base_url": "http://localhost:3000",
                          "token_source": "self-minted",
                          "tokens_minted": ["run-demo-s1", "run-demo-s2"]}
    boot = [e for e in out["events"] if e["event"] == "glv-bootstrap"]
    assert boot and boot[0]["mode"] == "self"
    assert boot[0]["token_name"] == "run-demo-s1"
    assert {"event": "glv-token-minted",
            "token_name": "run-demo-s2"} in out["events"]
    # the harness exported the transport it provisioned itself
    assert os.environ["GLV_BASE_URL"] == "http://localhost:3000"
    assert os.environ["GLV_API_TOKEN"].startswith("glv_at_fake_run-demo-s2")
    # a raw token never leaks into the tool output
    assert "glv_at_fake" not in json.dumps(out)


def test_chain_starts_the_server_when_nothing_answers(monkeypatch, capsys,
                                                      tmp_path):
    """Probe says down → the harness starts `glv serve` itself and waits
    for the probe to turn green before the first session (issue #47)."""
    p = _midrun_state(tmp_path)
    fake = FakeGlv(p, [_session_completes()])
    started = []

    def ensure(url, **kw):
        started.append(url)
        return "started"

    _wire_selfbootstrap(monkeypatch, fake, ensure=ensure)
    code, out = run_tool(monkeypatch, capsys, "succession.py",
                         ["chain", str(p), "--prompt-file", "/dev/null"])
    assert code == 0, out
    assert started == ["http://localhost:3000"]
    boot = next(e for e in out["events"] if e["event"] == "glv-bootstrap")
    assert boot["server"] == "started"


def test_ensure_server_starts_serve_then_waits_for_the_probe(monkeypatch):
    probes, started = [], []

    def fake_probe(url, **kw):
        probes.append(url)
        return len(probes) > 1           # down first, up after the start

    monkeypatch.setattr(succession_tool, "_probe_server", fake_probe)
    monkeypatch.setattr(succession_tool, "_serve_start",
                        lambda: started.append(1) or True)
    monkeypatch.setattr(succession_tool.time, "sleep", lambda s: None)
    assert succession_tool._ensure_server("http://x:1") == "started"
    assert started == [1] and len(probes) == 2
    # already serving: the probe answers on the first call, no start
    monkeypatch.setattr(succession_tool, "_probe_server",
                        lambda url, **kw: probes.append(url) or True)
    n_probes, n_started = len(probes), len(started)
    assert succession_tool._ensure_server("http://x:1") == "up"
    assert len(probes) == n_probes + 1 and len(started) == n_started


def test_ensure_server_without_the_cli_is_an_environment_error(monkeypatch):
    """Nothing answering + no glv CLI to start one: an environment
    failure naming what is missing — never a fallback mode, never a
    human-resume instruction."""
    monkeypatch.setattr(succession_tool, "_probe_server",
                        lambda url, **kw: False)
    monkeypatch.setattr(succession_tool, "_glv_cli_path", lambda: None)
    with pytest.raises(state_tool.ToolError) as exc:
        succession_tool._ensure_server("http://localhost:3000")
    msg = str(exc.value)
    assert "glv" in msg and "install" in msg
    assert "degraded" not in msg and "fallback" not in msg


def test_mint_token_cli_and_loopback_paths(monkeypatch):
    # CLI path: glv token create --name <label>, raw token parsed once.
    # (Popen is the interception seam since run_child session-leads its
    # children to group-kill them on timeout — issue #73.)
    monkeypatch.setattr(succession_tool, "_glv_cli_path",
                        lambda: "/usr/bin/glv")
    calls = []

    class _FakePopen:
        returncode = 0

        def __init__(self, cmd, **kw):
            calls.append(cmd)

        def communicate(self, input=None, timeout=None):
            return "created run-demo-s1\nglv_at_Ab3_-z9\n", ""

        def kill(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(succession_tool.subprocess, "Popen", _FakePopen)
    assert succession_tool._mint_token("http://x:1", "run-demo-s1") == \
        "glv_at_Ab3_-z9"
    assert calls[0][1:] == ["token", "create", "--name", "run-demo-s1"]
    # loopback path: POST /api/tokens (no CLI on the host)
    monkeypatch.setattr(succession_tool, "_glv_cli_path", lambda: None)
    reqs = []

    class _Resp:
        def read(self):
            return b'{"token": "glv_at_loop1", "name": "run-demo-s1"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        reqs.append(req)
        return _Resp()

    monkeypatch.setattr(succession_tool.urllib.request, "urlopen",
                        fake_urlopen)
    assert succession_tool._mint_token("http://x:1", "run-demo-s1") == \
        "glv_at_loop1"
    assert reqs[0].method == "POST"
    assert reqs[0].full_url.endswith("/api/tokens")


def test_chain_re_mints_on_401_without_ending_the_chain(
        monkeypatch, capsys, tmp_path):
    """A sudden 401 run (the 30-day expiry) re-mints and continues —
    re-auth is never a stop and never a human ask (issue #47)."""

    class AuthOnceGlv(FakeGlv):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.fortied_once = False

        def start_session(self, **kw):
            # the successor spawn 401s exactly once, then the re-minted
            # retry goes through
            if len(self.started) == 1 and not self.fortied_once:
                self.fortied_once = True
                raise succession_tool.GlvAuthError(
                    "GLV POST /api/sessions: HTTP 401: expired")
            return super().start_session(**kw)

    p = _midrun_state(tmp_path)
    fake = AuthOnceGlv(p, [
        _session_exhausts(stage=4, unit_status={"u1": "ported"}),
        _session_completes(unit_status={"u2": "ported", "u3": "ported"}),
    ])
    minted = _wire_selfbootstrap(monkeypatch, fake)
    code, out = run_tool(monkeypatch, capsys, "succession.py",
                         ["chain", str(p), "--prompt-file", "/dev/null"])
    assert code == 0, out
    assert out["run_status"] == "complete"    # the chain never ended
    assert minted.count("run-demo-s2") == 2   # bootstrap mint + re-mint
    assert any(e["event"] == "glv-reauth" and
               e["token_name"] == "run-demo-s2" for e in out["events"])


def test_chain_revives_a_dead_server_mid_chain(monkeypatch, capsys,
                                               tmp_path):
    """Connection loss mid-chain: the harness owns the server lifecycle —
    it revives the server and retries the call; the chain continues."""

    class DeadOnceGlv(FakeGlv):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.died_once = False

        def wait_settled(self, session_id, **kw):
            # the first wait loses the connection exactly once; after the
            # revive the retry goes through
            if not self.died_once:
                self.died_once = True
                raise succession_tool.GlvUnreachableError(
                    "GLV GET /api/sessions/x/wait: no answer")
            return super().wait_settled(session_id, **kw)

    p = _midrun_state(tmp_path)
    fake = DeadOnceGlv(p, [_session_completes()])
    revives = []
    _wire_selfbootstrap(monkeypatch, fake,
                        ensure=lambda url, **kw:
                        revives.append(url) or "up")
    code, out = run_tool(monkeypatch, capsys, "succession.py",
                         ["chain", str(p), "--prompt-file", "/dev/null"])
    assert code == 0, out
    assert out["run_status"] == "complete"
    # two ensure calls: the bootstrap at session 1 + the mid-chain revive
    assert revives == ["http://localhost:3000",
                       "http://localhost:3000"]
    assert any(e["event"] == "glv-server-revived" for e in out["events"])


def test_chain_ends_failed_safely_when_the_transport_cannot_be_revived(
        monkeypatch, capsys, tmp_path):
    """An unrevivable transport (server dead, nothing can start it) ends
    the chain failed_safely with a journaled stop-notice — a genuine
    terminal with the dossier, never a park in budget_exhausted."""

    class AlwaysDeadGlv(FakeGlv):
        def start_session(self, **kw):
            raise succession_tool.GlvUnreachableError(
                "GLV POST /api/sessions: no answer")

    p = _midrun_state(tmp_path)
    fake = AlwaysDeadGlv(p, [])

    def dead_ensure(url, **kw):
        raise state_tool.ToolError(
            "nothing is serving and the glv CLI is not on PATH (fake)",
            kind="io")

    _wire_selfbootstrap(monkeypatch, fake, ensure=dead_ensure)
    code, out = run_tool(monkeypatch, capsys, "succession.py",
                         ["chain", str(p), "--prompt-file", "/dev/null"])
    assert code == 2
    assert out["run_status"] == "failed_safely"
    st = _load(p)
    assert any("could not be revived" in n
               for n in st["session_chain"]["notices"])
    assert (tmp_path / "state.run-report.md").exists()
    assert "degraded" not in json.dumps(out)


def test_chain_without_cli_or_server_reports_the_environment_gap(
        monkeypatch, capsys, tmp_path):
    """Cold start with nothing installed: an environment failure (exit 2,
    install glv) — the state still gets the honest terminal + notice."""
    p = _midrun_state(tmp_path)
    fake = FakeGlv(p, [])

    def dead_ensure(url, **kw):
        raise state_tool.ToolError(
            "nothing is serving at http://localhost:3000 and the glv CLI "
            "is not on PATH, so the server cannot be started — install "
            "glv; GLV is the only transport (issue #47)", kind="io")

    _wire_selfbootstrap(monkeypatch, fake, ensure=dead_ensure)
    code, out = run_tool(monkeypatch, capsys, "succession.py",
                         ["chain", str(p), "--prompt-file", "/dev/null"])
    assert code == 2 and out["run_status"] == "failed_safely"
    assert "install glv" in out["summary"]["human"]
    assert "degraded" not in json.dumps(out)
    assert "fallback" not in json.dumps(out)


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
    summary names it as the only thing that can stop a healthy run (ADR
    design/0001 — contexts are recycled per unit chunk, so a ceiling stop
    is a budget decision, never a resume path) and never presents a human
    resume as the expected outcome (issue #47)."""
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
    assert "run-level budget is spent" in out["summary"]["human"]
    assert "design/0001" in out["summary"]["human"]
    assert "manual" not in out["summary"]["human"]


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


# ------------------------------------------- issue #47: no conditionals ----

def test_no_degraded_mode_or_fallback_wording_remains():
    """Issue #47 acceptance: grep-clean across framework/ — no degraded
    mode, no pre-#25 fallback, no 'GLV reachable/unreachable'
    conditional, no manual-resume framing may survive anywhere in the
    runbook, the tools, the policy, or the state schema."""
    banned = ["degraded", "pre-#25", "glv unreachable",
              "glv is reachable", "glv reachable", "manual resume"]
    files = [
        TOOLS / "succession.py", TOOLS / "pipeline.py", TOOLS / "README.md",
        TOOLS / "glv-subagents.md", TOOLS / "tests" / "conftest.py",
        TOOLS.parent / "MIGRATION_PROMPT.md",
        TOOLS.parent / "autonomy" / "policy.json",
        TOOLS.parent / "state" / "migration-state.schema.json",
    ]
    for path in files:
        text = path.read_text(encoding="utf-8").lower()
        for phrase in banned:
            assert phrase not in text, f"{phrase!r} still in {path}"
