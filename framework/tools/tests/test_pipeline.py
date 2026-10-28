"""pipeline.py + autonomy/policy.json acceptance tests (issue #22).

Proposal B: the deterministic driver owns the loop — budget guard, terminal
states, stage verification, resumability — with a stub CLI agent standing in
for the stage worker (any real agent shells out identically).
Proposal C: every gate resolves by an evaluable rule with a conservative
default; decisions are journaled (decided_by=policy:<rule-id>), never
LLM self-approval; escalation defers instead of halting."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

import pipeline as pipeline_tool
import state as state_tool
from conftest import REPO_ROOT, TOOLS, run_tool

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


def test_go_rule_prefers_measured_evidence_over_the_projection():
    # issue #34: the ≥50% bar is a claim until a matched A/B benchmark
    # exists — bench_ab.py records the measurement on the gate, and GO-1
    # reads it INSTEAD of the Stage-0 projection (honest reporting)
    state = {"go_no_go": {"decision": "go", "scope": ["demo.parse"],
                          "projection": {"end_to_end_saving_pct": 80.0},
                          "no_equivalent_deps": []}}
    gate = _gate("go_no_go", evidence=["end_to_end_saving_pct=42.5",
                                       "measured_by=bench_ab"])
    d = _decide(state, gate)
    assert d["decision"] == "rejected"
    assert d["run_status"] == "no_go_measured"
    assert any("42.5" in e and "measured" in e for e in d["evidence"])
    # without the measurement the same projection still approves
    d = _decide(state, _gate("go_no_go"))
    assert d["decision"] == "approved"


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


def test_resolve_gates_refuses_supervised_levels(monkeypatch, capsys, tmp_path):
    p = tmp_path / "state.json"
    run_tool(monkeypatch, capsys, "state.py", [
        "init", str(p), "--name", "demo", "--python-path", "src/demo",
        "--autonomy-level", "L0"])  # operator chose supervision explicitly
    code, out = run_tool(monkeypatch, capsys, "pipeline.py",
                         ["resolve-gates", str(p)])
    assert code == 2 and "L2" in out["error"]
    assert "nothing asks mid-run" in out["error"]
    # issue #27: the refusal names the concrete fresh-run remediation
    assert "state.py init --autonomy-level L2" in out["error"]
    assert _load(p)["human_gates"] == []  # nothing self-approved


def test_resolve_gates_refusal_explains_schema_only_state(monkeypatch, capsys,
                                                          mini_state):
    """issue #27: autonomy is not schema-required, so a hand-written state
    file (mini_state has no autonomy block) reads as L0 — the refusal must
    say so and name the init remediation instead of leaving the operator
    guessing."""
    code, out = run_tool(monkeypatch, capsys, "pipeline.py",
                         ["resolve-gates", str(mini_state)])
    assert code == 2 and out["error"].startswith("autonomy=L0:")
    assert "state.py init --autonomy-level L2" in out["error"]
    assert "autonomy block reads as L0" in out["error"]


def test_recipe_prompt_inits_state_via_state_py():
    """issue #27: the recipe used to tell the orchestrator to hand-write the
    state file from the schema — autonomy is not schema-required, so that
    file read as L0 and the run's first resolve-gates failed. The prompt
    must initialize through state.py init (autonomy journaled, default L2)."""
    recipe = (REPO_ROOT / "framework" / "recipe.yaml").read_text()
    assert "state.py init framework/state/migration-state.json" in recipe
    # the hand-init phrasing that produced autonomy-less (L0) state is gone
    assert "migration-state.json from" not in recipe


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
        "init", str(p), "--name", "demo", "--python-path", "src/demo",
        "--autonomy-level", "L0"])  # operator chose supervision explicitly
    code, out = run_tool(monkeypatch, capsys, "pipeline.py",
                         ["run", str(p), "--dry-run"])
    assert code == 2 and "L2" in out["error"]
    assert "never asks" in out["error"]
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


# ------------------------------------------------- preflight (issue #31) --
# e2e against a throwaway target repo; the template check runs offline
# against this repo's own git checkout (--template-url REPO_ROOT).

def _target_project(tmp_path, *, failing=False):
    """A throwaway target repo: a tests/ suite (green or red) plus a fake
    `.venv` whose python exec-wraps the running interpreter (a symlink is
    NOT enough: venv resolution follows the resolved binary, so a venv
    python reached through a symlink silently loses its site-packages)."""
    root = tmp_path / "target"
    (root / "tests").mkdir(parents=True)
    body = ("def test_add():\n    assert 1 + 1 == 2\n\n"
            "def test_mul():\n    assert 2 * 3 == 6\n")
    if failing:
        body += "\n\ndef test_broken():\n    assert 1 + 1 == 3\n"
    (root / "tests" / "test_math.py").write_text(body)
    if os.name == "nt":
        pytest.skip("the exec-wrapper venv fixture is POSIX-only")
    vbin = root / ".venv" / "bin"
    vbin.mkdir(parents=True)
    vpy = vbin / "python"
    vpy.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$@\"\n")
    vpy.chmod(0o755)
    return root


def _init_state(monkeypatch, capsys, tmp_path):
    p = tmp_path / "state.json"
    run_tool(monkeypatch, capsys, "state.py", [
        "init", str(p), "--name", "demo", "--python-path", "src/demo"])
    return p


def _preflight(monkeypatch, capsys, p, root=None, *extra):
    argv = ["preflight", str(p), "--template-url", str(REPO_ROOT)]
    if root is not None:
        argv += ["--target-root", str(root)]
    argv += list(extra)
    return run_tool(monkeypatch, capsys, "pipeline.py", argv)


def test_preflight_green_writes_block_report_and_journal(monkeypatch, capsys,
                                                         tmp_path):
    p = _init_state(monkeypatch, capsys, tmp_path)
    root = _target_project(tmp_path)
    code, out = _preflight(monkeypatch, capsys, p, root)
    assert code == 0, out
    assert out["ok"] is True and out["hard_failures"] == []
    st = _load(p)
    pf = st["preflight"]
    assert pf["ok"] is True and pf["hard_failures"] == []
    head = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                          capture_output=True, text=True,
                          check=True).stdout.strip()
    assert pf["checks"]["template"] == {
        "id": "template", "hard": True, "ok": True, "url": str(REPO_ROOT),
        "head_sha": head,
        "detail": f"rust-template reachable, HEAD {head[:12]}"}
    selftest = pf["checks"]["oracle_selftest"]
    assert selftest["ok"] and (selftest["caught"], selftest["injected"]) == (5, 5)
    # the spec baseline: green + wall time, recorded for Stage 2
    suite = pf["checks"]["suite"]
    assert suite["ok"] and suite["counts"]["passed"] == 2
    assert suite["wall_seconds"] >= 0
    # collection capture feeds contract_extract --pytest-collect
    collect = pf["checks"]["pytest_collect"]
    assert collect["ok"] and collect["collected"] == 2
    capture = Path(collect["capture"])
    assert capture == tmp_path / "state.pytest-collect.txt"
    assert capture.read_text().count("::") == 2
    # the target interpreter summary Stage 3/4 pin from
    ti = pf["checks"]["target_interpreter"]
    assert ti["ok"] and ti["pyo3_python"] == ti["python"]
    assert ti["python"].endswith("/.venv/bin/python")
    assert ti["gil_disabled"] is False and "abi3" in ti["wheel_flavor"]
    assert pf["target"]["root"] == str(root)
    assert pf["target"]["pyo3_python"] == ti["python"]
    # journaled through state.py, one preflight transition per run
    assert [e["field"] for e in st["journal"]].count("preflight") == 1
    # deliverables: the human report; and the state stays coherent
    report = tmp_path / "state.preflight.md"
    assert report.exists() and "target suite green" in report.read_text()
    assert "PYO3_PYTHON" in report.read_text()
    code, v = run_tool(monkeypatch, capsys, "state.py", ["validate", str(p)])
    assert code == 0, v["invariant_errors"]


def test_preflight_fails_loudly_on_a_red_target_suite(monkeypatch, capsys,
                                                      tmp_path):
    p = _init_state(monkeypatch, capsys, tmp_path)
    root = _target_project(tmp_path, failing=True)
    code, out = _preflight(monkeypatch, capsys, p, root)
    assert code == 2
    assert out["ok"] is False and out["hard_failures"] == ["suite"]
    assert out["checks"]["suite"]["counts"]["failed"] == 1
    # a hard failure is still journaled — auditability, never a silent skip
    pf = _load(p)["preflight"]
    assert pf["ok"] is False and pf["hard_failures"] == ["suite"]
    assert "NOT green" in (tmp_path / "state.preflight.md").read_text()


def test_preflight_fails_loudly_when_the_template_is_unreachable(
        monkeypatch, capsys, tmp_path):
    p = _init_state(monkeypatch, capsys, tmp_path)
    root = _target_project(tmp_path)
    code, out = _preflight(monkeypatch, capsys, p, root,
                           "--template-url", str(tmp_path / "no-such-repo"))
    assert code == 2
    assert "template" in out["hard_failures"]
    assert out["checks"]["template"]["head_sha"] is None
    assert _load(p)["preflight"]["checks"]["template"]["ok"] is False


def test_preflight_fails_loudly_when_the_selftest_cannot_run(
        monkeypatch, capsys, tmp_path):
    broken = tmp_path / "broken_oracle.py"
    broken.write_text("import sys\nsys.stderr.write('judge broken')\nsys.exit(2)\n")
    monkeypatch.setattr(pipeline_tool, "ORACLE", broken)
    p = _init_state(monkeypatch, capsys, tmp_path)
    root = _target_project(tmp_path)
    code, out = _preflight(monkeypatch, capsys, p, root)
    assert code == 2 and "oracle_selftest" in out["hard_failures"]
    rec = out["checks"]["oracle_selftest"]
    assert rec["caught"] is None and "judge-first" in rec["detail"]


def test_preflight_without_a_target_root_fails_the_unprovable_hard_checks(
        monkeypatch, capsys, tmp_path):
    p = _init_state(monkeypatch, capsys, tmp_path)
    code, out = _preflight(monkeypatch, capsys, p)  # no --target-root anywhere
    assert code == 2
    assert out["hard_failures"] == ["suite"]  # 'unproven' is not 'green'
    assert any("--target-root" in w for w in out["warnings"])


def test_preflight_skip_downgrades_a_hard_check_to_an_explicit_warning(
        monkeypatch, capsys, tmp_path):
    p = _init_state(monkeypatch, capsys, tmp_path)
    code, out = _preflight(monkeypatch, capsys, p, None,
                           "--skip", "suite", "--skip", "pytest-collect")
    assert code == 0 and out["hard_failures"] == []
    assert out["checks"]["suite"]["skipped"] is True
    assert any("'suite' skipped" in w for w in out["warnings"])
    assert any("NOT proven" in w for w in out["warnings"])


def test_preflight_records_the_target_root_for_re_runs(monkeypatch, capsys,
                                                       tmp_path):
    p = _init_state(monkeypatch, capsys, tmp_path)
    root = _target_project(tmp_path)
    code, _ = _preflight(monkeypatch, capsys, p, root)
    assert code == 0
    # re-run with no flag: the recorded root drives the suite check again
    code, out = _preflight(monkeypatch, capsys, p)
    assert code == 0
    assert out["checks"]["suite"]["ok"] is True
    assert _load(p)["preflight"]["target"]["root"] == str(root)


def test_preflight_warns_when_no_target_venv_is_found(monkeypatch, capsys,
                                                      tmp_path):
    p = _init_state(monkeypatch, capsys, tmp_path)
    root = _target_project(tmp_path)
    shutil.rmtree(root / ".venv")
    code, out = _preflight(monkeypatch, capsys, p, root)
    # soft: the suite/self-test ran under the framework interpreter instead
    assert code == 0
    assert out["checks"]["target_interpreter"]["ok"] is False
    assert out["checks"]["oracle_selftest"]["interpreter"] == sys.executable
    assert any(".venv" in w for w in out["warnings"])


# ------------------------------------------------------- preflight pure ---

def test_pytest_summary_counts_fold_plurals_and_errors():
    counts = pipeline_tool._parse_pytest_summary(
        "2 passed, 1 failed, 3 errors, 4 skipped in 0.10s")
    assert counts == {"passed": 2, "failed": 1, "error": 3, "skipped": 4}
    assert pipeline_tool._parse_pytest_summary("") == {}


def test_interpreter_mismatch_only_flags_an_older_framework():
    warn = pipeline_tool._interpreter_mismatch("3.10.14", "3.12.4")
    assert warn and "3.12" in warn and "SyntaxError" in warn
    assert pipeline_tool._interpreter_mismatch("3.12.1", "3.12.4") is None
    assert pipeline_tool._interpreter_mismatch("3.13.0", "3.12.4") is None
    assert pipeline_tool._interpreter_mismatch("3.12", "garbage") is None


def test_wheel_flavor_names_the_free_threaded_pin():
    assert "cp3XXt" in pipeline_tool._wheel_flavor(True)
    assert "abi3" in pipeline_tool._wheel_flavor(False)


def test_substitutions_freshness_flags_stale_and_unparsable_versions():
    stale = pipeline_tool._substitutions_record({"version": "2020-01-01"},
                                                date(2026, 10, 28))
    assert stale["stale"] is True and stale["age_days"] > 30
    fresh = pipeline_tool._substitutions_record({"version": "2026-10-27"},
                                                date(2026, 10, 28))
    assert fresh["ok"] is True and fresh["age_days"] == 1
    unparsable = pipeline_tool._substitutions_record({"version": "recent"},
                                                     date(2026, 10, 28))
    assert unparsable["stale"] is True and unparsable["age_days"] is None
