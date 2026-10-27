"""pipeline.py + autonomy/policy.json acceptance tests (issue #22).

Proposal B: the deterministic driver owns the loop — budget guard, terminal
states, stage verification, resumability — with a stub CLI agent standing in
for the stage worker (any real agent shells out identically).
Proposal C: every gate resolves by an evaluable rule with a conservative
default; decisions are journaled (decided_by=policy:<rule-id>), never
LLM self-approval; escalation defers instead of halting."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pipeline as pipeline_tool
import state as state_tool
from conftest import TOOLS, run_tool

TODAY = date(2026, 10, 27)  # pinned: freshness rules are evaluated against a fixed clock

SUBS = {"substitutions": [
    {"python": "freshlib", "rust": ["fresh-crate", "stale-crate"],
     "verified": {"fresh-crate": "2026-09-01", "stale-crate": "2024-01-01"}},
    {"python": "canonlib", "rust": ["serde_json"],
     "verified": {"serde_json": "canonical"}},
    {"python": "stalelib", "rust": ["old-crate"],
     "verified": {"old-crate": "2023-01-01"}},
]}


def ctx(**over):
    base = {"today": TODAY, "substitutions": SUBS}
    base.update(over)
    return base


def _gate(name, state_="open", evidence=None, note=None):
    g = {"gate": name, "state": state_}
    if evidence:
        g["evidence"] = evidence
    if note:
        g["note"] = note
    return g


def _load(path):
    return json.loads(Path(path).read_text())


# ------------------------------------------------------------ policy.json --

def test_repo_policy_loads_and_covers_every_gate():
    policy = pipeline_tool.load_policy()
    assert policy["version"]
    assert set(policy["levels"]) == {"L0", "L1", "L2", "L3"}
    covered = {r["gate"] for r in policy["rules"]}
    assert covered == set(state_tool.GATES), "every gate must be machine-decidable at L2+"
    assert len({r["id"] for r in policy["rules"]}) == len(policy["rules"])
    # publish is an L3-only pre-authorization
    assert policy["levels"]["L2"]["publish_allowed"] is False
    assert policy["levels"]["L3"]["publish_allowed"] is True


def test_policy_validation_rejects_an_uncovered_gate(tmp_path):
    policy = pipeline_tool.load_policy()
    policy["rules"] = [r for r in policy["rules"] if r["gate"] != "cutover"]
    p = tmp_path / "policy.json"
    p.write_text(json.dumps(policy))
    try:
        pipeline_tool.load_policy(p)
    except state_tool.ToolError as e:
        assert "cutover" in str(e)
    else:
        raise AssertionError("uncovered gate must be rejected")


# --------------------------------------------------- pure rule evaluation --

def _decide(state, gate, policy=None):
    policy = policy or pipeline_tool.load_policy()
    return pipeline_tool.evaluate_gate(state, gate, policy, ctx())


def test_go_rule_approves_from_the_recorded_projection():
    state = {"go_no_go": {"decision": "go", "scope": ["demo.parse"],
                          "projection": {"end_to_end_saving_pct": 63.5,
                                         "scoped_loc": 900},
                          "no_equivalent_deps": []}}
    d = _decide(state, _gate("go_no_go"))
    assert d["decision"] == "approved" and d["rule"] == "GO-1"
    assert any("63.5" in e for e in d["evidence"])


def test_go_rule_below_bar_is_a_successful_no_go_terminal_state():
    state = {"go_no_go": {"decision": "go", "scope": ["demo.parse"],
                          "projection": {"end_to_end_saving_pct": 30},
                          "no_equivalent_deps": []}}
    d = _decide(state, _gate("go_no_go"))
    assert d["decision"] == "rejected"
    assert d["run_status"] == "no_go_measured"
    assert d["go_decision"] == "no-go"


def test_go_rule_blocked_by_no_equivalent_dep_still_in_scope():
    state = {"go_no_go": {"decision": "go", "scope": ["demo.tasks"],
                          "projection": {"end_to_end_saving_pct": 80},
                          "no_equivalent_deps": ["demo.tasks"]}}
    d = _decide(state, _gate("go_no_go"))
    assert d["decision"] == "rejected" and "descope" in d["note"]


def test_go_rule_undecidable_without_projection():
    d = _decide({"go_no_go": {}}, _gate("go_no_go"))
    assert d["decision"] == pipeline_tool.UNDECIDABLE


def test_substitution_rule_picks_highest_ranked_freshly_verified_crate():
    d = _decide({}, _gate("crate_substitution", evidence=["python=freshlib"]))
    assert d["decision"] == "approved"
    assert "crate=fresh-crate" in " ".join(d["evidence"])


def test_substitution_rule_accepts_canonical_and_rejects_stale_or_missing():
    assert _decide({}, _gate("crate_substitution",
                             evidence=["python=canonlib"]))["decision"] == "approved"
    d = _decide({}, _gate("crate_substitution", evidence=["python=stalelib"]))
    assert d["decision"] == "rejected" and "stays Python" in d["note"]
    d = _decide({}, _gate("crate_substitution", evidence=["python=nosuchlib"]))
    assert d["decision"] == "rejected" and "never improvise" in d["note"]
    d = _decide({}, _gate("crate_substitution"))
    assert d["decision"] == pipeline_tool.UNDECIDABLE


def test_redesign_rule_requires_oracle_pass_and_bench_win():
    ok = _decide({}, _gate("redesign_approval",
                           evidence=["spike_verdict=equivalent",
                                     "bench_improvement_pct=12.5"]))
    assert ok["decision"] == "approved"
    weak = _decide({}, _gate("redesign_approval",
                             evidence=["spike_verdict=equivalent",
                                       "bench_improvement_pct=4"]))
    assert weak["decision"] == "rejected" and "literal shape" in weak["note"]
    missing = _decide({}, _gate("redesign_approval", evidence=[]))
    assert missing["decision"] == "rejected"  # safe default, not undecidable


def test_escalation_rule_defers_instead_of_halting():
    d = _decide({}, _gate("unit_escalation", evidence=["unit=u-index-4"]))
    assert d["decision"] == "deferred"
    assert "fallback" in d["note"]


def test_scope_change_rule_conservatively_rejects():
    d = _decide({}, _gate("scope_change"))
    assert d["decision"] == "rejected" and "fresh run" in d["note"]


def test_cutover_rule_publish_disabled_below_l3():
    d = _decide({"autonomy": {"level": "L2", "publish_allowed": False}},
                _gate("cutover"))
    assert d["decision"] == "rejected" and "publish disabled" in d["note"]


def test_cutover_rule_l3_green_evidence_approves_weak_rejects():
    green = ["shadow_diff_rate=0.0", "shadow_samples=12000",
             "canary_green_minutes=95", "rollback_tested=true"]
    l3 = {"autonomy": {"level": "L3", "publish_allowed": True}}
    assert _decide(l3, _gate("cutover", evidence=green))["decision"] == "approved"
    no_rollback = green[:3] + ["rollback_tested=false"]
    d = _decide(l3, _gate("cutover", evidence=no_rollback))
    assert d["decision"] == "rejected" and "rollback_tested" in d["note"]
    d = _decide(l3, _gate("cutover", evidence=[]))
    assert d["decision"] == "rejected"  # missing evidence: no cutover, keep fallback


# ----------------------------------------------- DEP-1 descope side effect --

def _descope_state():
    return {
        "go_no_go": {"decision": "go", "scope": ["demo.parse", "demo.tasks"],
                     "projection": {"end_to_end_saving_pct": 60},
                     "no_equivalent_deps": ["demo.tasks"]},
        "units": [
            {"id": "u-parse-1", "python_path": "src/demo/parse.py", "deps": [],
             "order": 1, "status": "pending"},
            {"id": "u-tasks-2", "python_path": "src/demo/tasks/worker.py",
             "deps": [], "order": 2, "status": "pending"},
            {"id": "u-rank-3", "python_path": "src/demo/rank.py",
             "deps": ["u-tasks-2"], "order": 3, "status": "pending"},
        ],
        "human_gates": [_gate("no_equivalent_dependency",
                              evidence=["module=demo.tasks"])],
        "journal": [],
    }


def test_dep_rule_descopes_module_and_transitive_dependents():
    policy = pipeline_tool.load_policy()
    state = _descope_state()
    decisions, changes = pipeline_tool.resolve_open_gates(state, policy, ctx())
    assert decisions[0]["decision"] == "approved"
    by_id = {u["id"]: u for u in state["units"]}
    assert by_id["u-tasks-2"]["status"] == "fallback_python"
    assert by_id["u-rank-3"]["status"] == "fallback_python"  # dependent dropped too
    assert by_id["u-parse-1"]["status"] == "pending"          # untouched
    assert state["go_no_go"]["scope"] == ["demo.parse"]
    assert state["go_no_go"]["no_equivalent_deps"] == []
    fields = [c[1] for c in changes]
    assert "units[u-tasks-2].status" in fields and "units[u-rank-3].status" in fields
    assert "go_no_go.scope" in fields


# ------------------------------------------------- resolve-gates subcommand -

def _l2_state(tmp_path, monkeypatch, capsys, gates=None, **fields):
    p = tmp_path / "state.json"
    run_tool(monkeypatch, capsys, "state.py", [
        "init", str(p), "--name", "demo", "--python-path", "src/demo",
        "--autonomy-level", "L2"])
    for k, v in fields.items():
        run_tool(monkeypatch, capsys, "state.py", [
            "set", str(p), "--field", f"{k}={json.dumps(v)}", "--json-value"])
    if gates:
        run_tool(monkeypatch, capsys, "state.py", [
            "set", str(p), "--field", f"human_gates={json.dumps(gates)}",
            "--json-value"])
    return p


def test_resolve_gates_journals_policy_decisions(monkeypatch, capsys, tmp_path):
    p = _l2_state(tmp_path, monkeypatch, capsys,
                  go_no_go={"decision": "go", "scope": ["demo.parse"],
                            "projection": {"end_to_end_saving_pct": 58.0,
                                           "scoped_loc": 100},
                            "no_equivalent_deps": []})
    code, out = run_tool(monkeypatch, capsys, "pipeline.py",
                         ["resolve-gates", str(p)])
    assert code == 0
    assert [d["decision"] for d in out["decisions"]] == ["approved"]
    st = _load(p)
    g = st["human_gates"][0]
    assert g["gate"] == "go_no_go" and g["state"] == "approved"
    assert g["decided_by"] == "policy:GO-1"
    assert g["policy_version"] and g["evidence"]
    assert st["go_no_go"]["approved_by"] == "policy:GO-1"
    assert any(e["field"] == "human_gates[gate=go_no_go].decided_by"
               for e in st["journal"])


def test_resolve_gates_refuses_below_l2_gates_are_human_stops(monkeypatch, capsys, tmp_path):
    p = tmp_path / "state.json"
    run_tool(monkeypatch, capsys, "state.py", [
        "init", str(p), "--name", "demo", "--python-path", "src/demo"])  # L0
    code, out = run_tool(monkeypatch, capsys, "pipeline.py",
                         ["resolve-gates", str(p)])
    assert code == 2 and "L2" in out["error"]
    assert _load(p)["human_gates"] == []  # nothing self-approved


def test_resolve_gates_dry_run_writes_nothing(monkeypatch, capsys, tmp_path):
    p = _l2_state(tmp_path, monkeypatch, capsys,
                  human_gates=[_gate("unit_escalation", evidence=["unit=u1"])])
    before = _load(p)
    code, out = run_tool(monkeypatch, capsys, "pipeline.py",
                         ["resolve-gates", str(p), "--dry-run"])
    assert code == 0
    by_gate = {d["gate"]: d for d in out["decisions"]}
    assert by_gate["unit_escalation"]["decision"] == "deferred"
    assert _load(p) == before  # nothing written in dry-run


# ----------------------------------------------------------- the run loop --

STUB_AGENT = '''#!/usr/bin/env python3
import json, subprocess, sys
STATE_PY, state_path, stage = sys.argv[1], sys.argv[2], int(sys.argv[3])

def sp(*a):
    r = subprocess.run([sys.executable, STATE_PY, *a], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr

def setf(field, value):
    sp("set", state_path, "--field", f"{field}={value}", "--json-value")

if stage == 0:
    setf("go_no_go", json.dumps({
        "decision": "go", "scope": ["demo.parse", "demo.rank"],
        "projection": {"scoped_loc": 900, "token_cost_usd": 153.0,
                       "end_to_end_saving_pct": 58.0},
        "no_equivalent_deps": []}))
elif stage == 1:
    setf("units", json.dumps([
        {"id": "u-parse-1", "python_path": "src/demo/parse.py", "deps": [],
         "order": 1, "status": "pending",
         "contract": {"covering_tests": ["tests/test_parse.py::test_roundtrip"]}},
        {"id": "u-rank-2", "python_path": "src/demo/rank.py",
         "deps": ["u-parse-1"], "order": 2, "status": "pending",
         "contract": {"covering_tests": ["tests/test_rank.py::test_order"]}}]))
elif stage == 2:
    setf("judge_validation", json.dumps({
        "injected_bugs": 5, "caught": 5, "mutation_sample_killed": 90,
        "normalization_rules": "contracts/normalization.md", "date": "2026-10-27"}))
elif stage == 3:
    setf("units[u-parse-1].rust_path", json.dumps("crates/demo/src/u_parse_1.rs"))
    setf("units[u-rank-2].rust_path", json.dumps("crates/demo/src/u_rank_2.rs"))
elif stage == 4:
    setf("units[u-parse-1].parity", json.dumps({
        "runs": 30, "equivalent": 30, "divergent": 0, "incomparable": 0,
        "last_verdict": "equivalent"}))
    sp("mark", state_path, "--unit", "u-parse-1", "--status", "ported")
    sp("mark", state_path, "--unit", "u-rank-2", "--status", "fallback_python",
       "--note", "descoped: outside the hot tranche")
elif stage == 5:
    setf("units[u-parse-1].attribution",
         json.dumps({"header_preserved": True, "notice_ok": True}))
# stages 6/7: the driver owns the cutover gate; at L2 CUT-1 rejects (publish
# disabled) and Stage 7 maintenance completes the run
print(f"stub stage {stage} done")
'''


def _agent_cmd(tmp_path):
    stub = tmp_path / "stub_agent.py"
    stub.write_text(STUB_AGENT)
    return f"{sys.executable} {stub} {TOOLS / 'state.py'} {{state}} {{stage}}"


def test_full_l2_run_reaches_complete_terminal_state(monkeypatch, capsys, tmp_path):
    p = tmp_path / "state.json"
    run_tool(monkeypatch, capsys, "state.py", [
        "init", str(p), "--name", "demo", "--python-path", "src/demo",
        "--autonomy-level", "L2"])
    code, out = run_tool(monkeypatch, capsys, "pipeline.py",
                         ["run", str(p), "--agent-cmd", _agent_cmd(tmp_path)])
    assert code == 0, out
    assert out["run_status"] == "complete" and out["terminal"] is True
    st = _load(p)
    assert st["stage"] == 7 and st["run_status"] == "complete"
    # terminal-state discipline: no gate left open (never "awaiting input")
    assert all(g["state"] != "open" for g in st["human_gates"])
    gates = {g["gate"]: g for g in st["human_gates"]}
    assert gates["go_no_go"]["decided_by"] == "policy:GO-1"
    assert gates["go_no_go"]["state"] == "approved"
    # publish is disabled at L2: cutover resolved (rejected), not skipped
    assert gates["cutover"]["state"] == "rejected"
    assert gates["cutover"]["decided_by"] == "policy:CUT-1"
    # every stage advance journaled, in order (init's None->0 first)
    stages = [e["to"] for e in st["journal"] if e["field"] == "stage"]
    assert stages == [0, 1, 2, 3, 4, 5, 6, 7]
    # the deliverable set: run report with the gate dossier
    report = tmp_path / "state.run-report.md"
    assert report.exists()
    text = report.read_text()
    assert "run_status: **complete**" in text and "cutover" in text
    # and the finished state is coherent (schema + invariants, warnings ok)
    code, out = run_tool(monkeypatch, capsys, "state.py", ["validate", str(p)])
    assert code == 0, out["invariant_errors"]
    assert not [w for w in out["warnings"] if "resumed" in w]


def test_no_go_measured_is_a_successful_terminal_state(monkeypatch, capsys, tmp_path):
    stub = tmp_path / "stub_no_go.py"
    stub.write_text(
        "import json, subprocess, sys\n"
        "subprocess.run([sys.executable, sys.argv[1], 'set', sys.argv[2], '--field',\n"
        "  'go_no_go=' + json.dumps({'decision': 'no-go', 'scope': ['demo.io'],\n"
        "    'projection': {'scoped_loc': 400, 'token_cost_usd': 68.0,\n"
        "                   'end_to_end_saving_pct': 12.0}, 'no_equivalent_deps': []}),\n"
        "  '--json-value'], check=True, capture_output=True)\n")
    p = tmp_path / "state.json"
    run_tool(monkeypatch, capsys, "state.py", [
        "init", str(p), "--name", "demo", "--python-path", "src/demo",
        "--autonomy-level", "L2"])
    cmd = f"{sys.executable} {stub} {TOOLS / 'state.py'} {{state}} {{stage}}"
    code, out = run_tool(monkeypatch, capsys, "pipeline.py",
                         ["run", str(p), "--agent-cmd", cmd])
    assert code == 0  # NO-GO is a result, not a failure
    assert out["run_status"] == "no_go_measured"
    st = _load(p)
    assert st["go_no_go"]["decision"] == "no-go"
    assert st["stage"] == 0  # never advanced past feasibility
    gate = {g["gate"]: g for g in st["human_gates"]}["go_no_go"]
    assert gate["state"] == "rejected" and gate["decided_by"] == "policy:GO-1"


def test_budget_exhausted_is_a_clean_resumable_terminal(monkeypatch, capsys, tmp_path):
    p = _l2_state(tmp_path, monkeypatch, capsys,
                  budget_ledger={"tokens_total": 9000, "hours_human_total": 0.0,
                                 "regression_triage_hours": 0.0},
                  autonomy={"level": "L2", "policy_version": "1.0.0",
                            "budget": {"max_tokens": 5000,
                                       "max_wall_clock_minutes": 720},
                            "publish_allowed": False})
    code, out = run_tool(monkeypatch, capsys, "pipeline.py",
                         ["run", str(p), "--dry-run"])
    assert code == 0  # safe stop, resumable after raising the ceiling
    assert out["run_status"] == "budget_exhausted"
    assert _load(p)["run_status"] == "budget_exhausted"
    assert (tmp_path / "state.run-report.md").exists()


def test_failed_safely_when_the_worker_cannot_meet_exit_criteria(
        monkeypatch, capsys, tmp_path):
    p = tmp_path / "state.json"
    run_tool(monkeypatch, capsys, "state.py", [
        "init", str(p), "--name", "demo", "--python-path", "src/demo",
        "--autonomy-level", "L2"])
    noop = f"{sys.executable} -c pass"  # a worker that records nothing
    code, out = run_tool(monkeypatch, capsys, "pipeline.py",
                         ["run", str(p), "--agent-cmd", noop])
    assert code == 2
    assert out["run_status"] == "failed_safely"
    assert "Stage 0" in out["reason"] or "stage 0" in out["reason"]
    st = _load(p)
    assert st["run_status"] == "failed_safely" and st["stage"] == 0


def test_driver_refuses_to_run_supervised_levels(monkeypatch, capsys, tmp_path):
    p = tmp_path / "state.json"
    run_tool(monkeypatch, capsys, "state.py", [
        "init", str(p), "--name", "demo", "--python-path", "src/demo"])  # L0
    code, out = run_tool(monkeypatch, capsys, "pipeline.py",
                         ["run", str(p), "--dry-run"])
    assert code == 2 and "L2" in out["error"]
    # pre-authorizing upfront is journaled, not silent
    code, out = run_tool(monkeypatch, capsys, "pipeline.py",
                         ["run", str(p), "--dry-run", "--autonomy", "L2"])
    assert _load(p)["autonomy"]["level"] == "L2"
    assert any(e["field"] == "autonomy" for e in _load(p)["journal"])


def test_once_runs_exactly_one_iteration_and_stays_resumable(
        monkeypatch, capsys, tmp_path):
    p = tmp_path / "state.json"
    run_tool(monkeypatch, capsys, "state.py", [
        "init", str(p), "--name", "demo", "--python-path", "src/demo",
        "--autonomy-level", "L2"])
    code, out = run_tool(monkeypatch, capsys, "pipeline.py",
                         ["run", str(p), "--agent-cmd", _agent_cmd(tmp_path),
                          "--once"])
    assert code == 0 and out["terminal"] is False
    st = _load(p)
    assert st["stage"] == 1 and st["run_status"] == "running"
    assert {g["gate"]: g for g in st["human_gates"]}["go_no_go"]["state"] == "approved"


def test_finished_run_refuses_to_restart(monkeypatch, capsys, tmp_path):
    p = _l2_state(tmp_path, monkeypatch, capsys, run_status="complete")
    code, out = run_tool(monkeypatch, capsys, "pipeline.py",
                         ["run", str(p), "--dry-run"])
    assert code == 2 and "fresh run" in out["error"]


# ----------------------------------------------------- report and status ---

def test_report_emits_gate_and_escalation_dossiers(monkeypatch, capsys, tmp_path):
    p = _l2_state(tmp_path, monkeypatch, capsys,
                  run_status="complete",
                  units=[{"id": "u-index-4", "python_path": "src/demo/index.py",
                          "deps": [], "order": 1, "status": "escalated",
                          "notes": "divergent after 5 rounds"}],
                  human_gates=[_gate("unit_escalation", "deferred",
                                     evidence=["unit=u-index-4"],
                                     note="policy:ESC-1 deferred") | {
                                         "decided_by": "policy:ESC-1",
                                         "policy_version": "1.0.0"}])
    code, out = run_tool(monkeypatch, capsys, "pipeline.py",
                         ["report", str(p)])
    assert code == 0
    text = out["report"]
    assert "run_status: **complete**" in text
    assert "u-index-4" in text and "Escalation dossier" in text
    assert "unit_escalation | deferred | policy:ESC-1" in text


def test_status_summarizes_the_run(monkeypatch, capsys, tmp_path):
    p = _l2_state(tmp_path, monkeypatch, capsys)
    code, out = run_tool(monkeypatch, capsys, "pipeline.py", ["status", str(p)])
    assert code == 0
    assert out["run_status"] == "pending" and out["autonomy"]["level"] == "L2"
