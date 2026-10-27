#!/usr/bin/env python3
"""pipeline.py — the deterministic driver (issue #22, proposals B + C).

A new loop owns the state machine; the LLM is demoted to a *stage worker*
invoked via any CLI agent. Gates are no longer blocking human stops: each is
a machine-decidable rule in ``autonomy/policy.json`` with a conservative
default, journaled and non-blocking. The loop is code — testable, cheap,
deterministic, resumable by construction — consistent with the framework's
own doctrine: deterministic tooling for location and validation; the LLM
only for edits (judgment stays *inside* stages: tranche selection,
decomposition, repair).

    loop:
      stage = state.stage; run stage checklist via the agent CLI
      verify exit criteria  -> state.py validation + state-expressible checks
                               (+ oracle.py self-test at Stage 2)
      resolve open gates    -> evaluate autonomy/policy.json in code
      journal decisions     -> state.py (decided_by + policy_version + evidence)
      advance stage or terminal-state exit

Terminal-state discipline: a run ALWAYS ends in ``complete`` /
``no_go_measured`` / ``budget_exhausted`` / ``failed_safely`` — never
"awaiting input". Every terminal state emits the same deliverable set (run
report incl. the escalation dossier, state file, journal). Safety rests on
the existing dual-build design: per-unit Python fallback, one-env-var shim,
rollback pin — every autonomous decision stays reversible per unit.

Autonomy levels (pre-authorized BEFORE the run; zero mid-run questions):
L0 supervised (human gates are hard stops — the pre-policy behavior; the
agent-driven recipe covers it, this driver refuses to run) · L1 agent
proposes, humans decide (also refused here) · L2 all gates policy-resolved,
publish disabled · L3 full autopilot incl. cutover.

Subcommands
-----------
  run            drive stages 0 -> 7 to a terminal state (requires L2+)
  resolve-gates  evaluate open gates against the policy in code and record
                 the decisions through state.py's journaled writer (pure
                 evaluation is exercised by the tests; --dry-run shows
                 decisions without writing)
  report         emit the run report (status, gate dossier, escalation
                 dossier, budget usage) — auto-written at every terminal exit
  status         one-JSON run summary

This tool writes the state file ONLY through state.py's save_validated (the
"state.py is the ONLY writer" discipline is preserved; pipeline.py composes
it, the way bench.py composes oracle.py's comparator).

Exit codes follow tools/README.md: 0 clean (any terminal state except
failed_safely is a *result*, not a failure) · 2 findings (failed_safely, or
a refusal such as "driver requires L2+") · 1 usage/IO via the shared
envelope.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state as state_tool  # noqa: E402  — the ONLY writer, composed not duplicated
from _common import (  # noqa: E402
    EXIT_FINDINGS, EXIT_OK, ToolError, emit, run_cli,
)

TOOL = "pipeline.py"
VERSION = 1
FRAMEWORK = Path(__file__).resolve().parent.parent
DEFAULT_POLICY = FRAMEWORK / "autonomy" / "policy.json"
DEFAULT_SUBSTITUTIONS = FRAMEWORK / "knowledge" / "substitutions.json"
ORACLE = Path(__file__).resolve().parent / "oracle.py"
STAGE_FILES = {0: "0-feasibility.md", 1: "1-plan.md", 2: "2-contracts.md",
               3: "3-scaffold.md", 4: "4-translate.md", 5: "5-gates.md",
               6: "6-integrate.md", 7: "7-maintain.md"}
LAST_STAGE = 7
# gates the driver itself opens at a stage boundary when the worker did not
# (gate ownership moves to code at L2+; the worker records the *data*)
STAGE_GATES = {0: ["go_no_go"], 6: ["cutover"]}
# evaluation order: descoping (DEP-1) must clear the scope before GO-1 reads it
GATE_ORDER = {"no_equivalent_dependency": 0, "go_no_go": 1}
UNDECIDABLE = "undecidable"


# --------------------------------------------------------------- policy ----

def load_policy(path: str | Path | None = None) -> dict:
    """Load autonomy/policy.json and structurally validate it. Every gate in
    the schema's enum must be covered by exactly one rule id — an uncovered
    gate would reintroduce a blocking stop at L2+."""
    p = Path(path) if path else DEFAULT_POLICY
    try:
        policy = json.loads(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise ToolError(f"policy {p}: {e}", kind="io") from e
    except json.JSONDecodeError as e:
        raise ToolError(f"policy {p}: invalid JSON: {e}", kind="usage") from e
    errors = _policy_structure_errors(policy)
    if errors:
        raise ToolError(f"invalid policy {p}:\n  " + "\n  ".join(errors), kind="usage")
    return policy


def _policy_structure_errors(policy: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(policy.get("version"), str) or not policy["version"]:
        errors.append("version: required (every decision journals it)")
    levels = policy.get("levels")
    if not isinstance(levels, dict) or set(levels) != state_tool.POLICY_LEVELS:
        errors.append(f"levels: must define exactly "
                      f"{', '.join(sorted(state_tool.POLICY_LEVELS))}")
    elif not all(isinstance(lv.get("publish_allowed"), bool)
                 for lv in levels.values() if isinstance(lv, dict)):
        errors.append("levels.*: publish_allowed (bool) required on every level")
    rules = policy.get("rules")
    if not isinstance(rules, list) or not rules:
        errors.append("rules: non-empty list required")
    else:
        ids = [r.get("id") for r in rules if isinstance(r, dict)]
        if len(ids) != len(rules) or len(set(ids)) != len(ids):
            errors.append("rules: every rule needs a unique id")
        covered: set[str] = set()
        for r in rules:
            if not isinstance(r, dict):
                continue
            rid = r.get("id")
            if not isinstance(rid, str) or not re.fullmatch(r"[A-Z]+-\d+", rid or ""):
                errors.append(f"rule id {rid!r}: must match <AREA>-<number> (e.g. GO-1)")
            if r.get("gate") not in state_tool.GATES:
                errors.append(f"rule {rid}: gate {r.get('gate')!r} is not in the "
                              f"schema's gate enum")
            else:
                covered.add(r["gate"])
            if not isinstance(r.get("decides"), str) or not r.get("decides"):
                errors.append(f"rule {rid}: 'decides' text required")
            if not isinstance(r.get("params", {}), dict):
                errors.append(f"rule {rid}: params must be an object")
        missing = sorted(set(state_tool.GATES) - covered)
        if missing:
            errors.append(f"rules: no rule covers gate(s) {missing} — every gate "
                          f"must be machine-decidable at L2+")
    budget = policy.get("budget")
    if (not isinstance(budget, dict)
            or "max_tokens" not in budget or "max_wall_clock_minutes" not in budget):
        errors.append("budget: max_tokens and max_wall_clock_minutes required")
    return errors


def load_substitutions(path: str | Path | None = None) -> dict:
    p = Path(path) if path else DEFAULT_SUBSTITUTIONS
    try:
        subs = json.loads(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise ToolError(f"substitutions {p}: {e}", kind="io") from e
    except json.JSONDecodeError as e:
        raise ToolError(f"substitutions {p}: invalid JSON: {e}", kind="usage") from e
    if not isinstance(subs.get("substitutions"), list):
        raise ToolError(f"substitutions {p}: 'substitutions' list required",
                        kind="usage")
    return subs


def rule_for(policy: dict, gate: str) -> dict | None:
    return next((r for r in policy.get("rules", [])
                 if isinstance(r, dict) and r.get("gate") == gate), None)


# ------------------------------------------------------------- evidence ----

def parse_evidence(entries) -> dict[str, str]:
    """gate.evidence[] 'key=value' records -> dict (last wins; malformed
    entries are ignored — evidence is extracted, never invented)."""
    out: dict[str, str] = {}
    for e in entries or []:
        if isinstance(e, str) and "=" in e:
            k, v = e.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _num(value, cast=float):
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def _age_months(iso_date: str, today: date) -> float | None:
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return (today - d).days / 30.4375


def _module_parts(x: str) -> list[str]:
    return [p for p in re.split(r"[./]", x or "") if p and p != "src"]


def _under_module(path: str, module: str) -> bool:
    pp, mp = _module_parts(path), _module_parts(module)
    return bool(mp) and pp[:len(mp)] == mp


# ------------------------------------------------------------ evaluation ---
# Pure functions: (state, gate, policy, ctx) -> decision dict. A decision is
# always one of approved / rejected / deferred — the conservative default —
# except UNDECIDABLE, which leaves the gate open to drain into the run
# report (the stage-exit verifier catches the missing-input cases that
# matter, e.g. a Stage 0 without a projection never gets this far).

def _decision(gate: str, rule: dict, decision: str, note: str, evidence: list[str],
              **extra) -> dict:
    out = {"gate": gate, "rule": rule["id"], "decision": decision,
           "note": note, "evidence": evidence}
    out.update(extra)
    return out


def _eval_go_no_go(state, gate, rule, ctx):
    params = rule.get("params", {})
    bar = _num(params.get("min_end_to_end_saving_pct"), float) or 50.0
    gng = state.get("go_no_go") or {}
    projection = gng.get("projection") or {}
    saving = _num(projection.get("end_to_end_saving_pct"))
    scope = gng.get("scope") or []
    if saving is None or not scope:
        return _decision("go_no_go", rule, UNDECIDABLE,
                         "projection/scope not recorded — Stage 0 exit criteria "
                         "unmet; nothing to decide on", [])
    blocking = [d for d in (gng.get("no_equivalent_deps") or []) if d in scope]
    evidence = [f"end_to_end_saving_pct={saving} (bar >= {bar})",
                f"scoped_loc={projection.get('scoped_loc')}",
                f"scope={','.join(scope)}"]
    if blocking:
        return _decision("go_no_go", rule, "rejected",
                         f"no-equivalent dependency inside scope: {blocking} — "
                         f"descope via policy:DEP-1 first (never improvise a "
                         f"substitute)", evidence + [f"blocking={blocking}"])
    if saving >= bar:
        return _decision("go_no_go", rule, "approved",
                         f"go — projection meets the bar ({saving}% >= {bar}%); "
                         f"scope is the recorded minimal tranche", evidence,
                         go_decision="go")
    on_reject = rule.get("on_reject") or {}
    return _decision("go_no_go", rule, "rejected",
                     on_reject.get("note") or
                     "no-go — projected saving below the bar; keep/optimize the "
                     f"Python ({saving}% < {bar}%)", evidence,
                     go_decision="no-go", run_status="no_go_measured")


def _eval_crate_substitution(state, gate, rule, ctx):
    max_months = _num(rule.get("params", {}).get("max_verified_age_months"),
                      float) or 6.0
    ev = parse_evidence(gate.get("evidence"))
    name = ev.get("python")
    if not name:  # convention: the gate note names the python module first
        note_words = (gate.get("note") or "").split()
        if note_words:
            name = note_words[0]
    if not name:
        return _decision("crate_substitution", rule, UNDECIDABLE,
                         "python=<module> evidence (or a gate note naming it) "
                         "required to look the substitution up", [])
    subs = ctx.get("substitutions") or {}
    entry = next((s for s in subs.get("substitutions", [])
                  if isinstance(s, dict) and s.get("python") == name), None)
    if entry is None:
        return _decision("crate_substitution", rule, "rejected",
                         f"no entry for {name!r} in knowledge/substitutions.json — "
                         f"never improvise a substitute (hard rule); the module "
                         f"stays Python", [f"python={name}", "table=miss"])
    today: date = ctx["today"]
    for crate in entry.get("rust", []):
        verified = (entry.get("verified") or {}).get(crate)
        if verified in ("canonical", "builtin"):
            return _decision("crate_substitution", rule, "approved",
                             f"{name} -> {crate} ({verified}; the table's "
                             f"standing verification)", [f"python={name}",
                             f"crate={crate}", f"verified={verified}"])
        age = _age_months(verified or "", today)
        if age is not None and age <= max_months:
            return _decision("crate_substitution", rule, "approved",
                             f"{name} -> {crate} (verified {verified}, "
                             f"{age:.1f} months old <= {max_months:g})",
                             [f"python={name}", f"crate={crate}",
                              f"verified={verified}", f"age_months={age:.1f}"])
    return _decision("crate_substitution", rule, "rejected",
                     f"no candidate crate for {name!r} carries verification within "
                     f"{max_months:g} months — refresh the table (Stage 7 "
                     f"re-verification) and re-run; the module stays Python",
                     [f"python={name}", "verified=none-within-window"])


def _eval_redesign(state, gate, rule, ctx):
    bar = _num(rule.get("params", {}).get("min_benchmark_improvement_pct"),
               float) or 10.0
    ev = parse_evidence(gate.get("evidence"))
    spike = ev.get("spike_verdict")
    bench = ev.get("bench_improvement_pct", "unrecorded")
    bench_num = _num(ev.get("bench_improvement_pct"))
    spike_txt = f"spike_verdict={spike or 'unrecorded'}"
    bench_txt = f"bench_improvement_pct={bench}"
    evidence = [spike_txt, f"{bench_txt} (bar >= {bar:g})"]
    if spike == "equivalent" and bench_num is not None and bench_num >= bar:
        return _decision("redesign_approval", rule, "approved",
                         f"spike passes the oracle and the benchmark clears the "
                         f"bar (+{bench_num:g}% >= {bar:g}%)", evidence)
    why = []
    if spike != "equivalent":
        why.append(f"{spike_txt} (needs equivalent)")
    if bench_num is None or bench_num < bar:
        why.append(f"{bench_txt} (needs >= {bar:g})")
    return _decision("redesign_approval", rule, "rejected",
                     "keep the literal shape (safe default): " + "; ".join(why)
                     + " — correctness gates apply either way", evidence)


def _eval_no_equivalent(state, gate, rule, ctx):
    ev = parse_evidence(gate.get("evidence"))
    module = ev.get("module")
    if not module:
        return _decision("no_equivalent_dependency", rule, UNDECIDABLE,
                         "module=<python package> evidence required to descope", [])
    return _decision("no_equivalent_dependency", rule, "approved",
                     f"descope {module}: the module stays Python (dual-build "
                     f"fallback); dependents drop from the tranche transitively; "
                     f"never a substitution", [f"module={module}",
                     "action=descope", "fallback=python (live)"])


def _eval_escalation(state, gate, rule, ctx):
    unit = parse_evidence(gate.get("evidence")).get("unit")
    ev = [f"unit={unit}"] if unit else []
    return _decision("unit_escalation", rule, "deferred",
                     "defer, not halt: the unit keeps its Python fallback live "
                     "and drains into the end-of-run escalation dossier; the "
                     "backlog continues", ev + ["fallback=python (live)",
                     "deferred=drains into the end-of-run escalation dossier"])


def _eval_scope_change(state, gate, rule, ctx):
    return _decision("scope_change", rule, "rejected",
                     "mid-run scope growth is not auto-approved under any level — "
                     "recorded for the run report; re-enter Stage 0 in a fresh "
                     "run to grow scope (a new projection, not a mid-flight edit)",
                     [])


def _eval_cutover(state, gate, rule, ctx):
    params = rule.get("params", {})
    level = state_tool.autonomy_level(state)
    publish_allowed = (state.get("autonomy") or {}).get("publish_allowed") is True
    if not state_tool.publish_preauthorized(state):
        return _decision("cutover", rule, "rejected",
                         f"publish disabled at {level} — the run completes without "
                         f"cutover; the dual-build stays intact (publish is an "
                         f"L3-only pre-authorization)", [f"autonomy={level}",
                         f"publish_allowed={str(publish_allowed).lower()}"])
    ev = parse_evidence(gate.get("evidence"))
    rate = _num(ev.get("shadow_diff_rate"))
    samples = _num(ev.get("shadow_samples"), int)
    canary = _num(ev.get("canary_green_minutes"), int)
    rollback = ev.get("rollback_tested", "").lower() == "true"
    checks = [
        (rate is not None and rate <= float(params.get("max_shadow_diff_rate", 0.0)),
         f"shadow_diff_rate={ev.get('shadow_diff_rate', 'unrecorded')} "
         f"(<= {params.get('max_shadow_diff_rate', 0.0)})"),
        (samples is not None and samples >= int(params.get("min_shadow_samples", 1000)),
         f"shadow_samples={ev.get('shadow_samples', 'unrecorded')} "
         f"(>= {params.get('min_shadow_samples', 1000)})"),
        (canary is not None and canary >= int(params.get("min_canary_green_minutes", 60)),
         f"canary_green_minutes={ev.get('canary_green_minutes', 'unrecorded')} "
         f"(>= {params.get('min_canary_green_minutes', 60)})"),
        (rollback or not params.get("rollback_test_required", True),
         f"rollback_tested={ev.get('rollback_tested', 'unrecorded')} "
         f"(deploys the old pin, verifies, returns)"),
    ]
    evidence = [text for _, text in checks]
    if all(ok for ok, _ in checks):
        return _decision("cutover", rule, "approved",
                         "cutover approved: shadow diff clean over the sample "
                         "floor, canary green past the window, rollback tested "
                         "(publish pre-authorized at L3)", evidence)
    failed = [text for ok, text in checks if not ok]
    return _decision("cutover", rule, "rejected",
                     "cutover evidence below bar — no cutover, the run continues "
                     "safely on the Python fallback: " + "; ".join(failed), evidence)


EVALUATORS = {
    "go_no_go": _eval_go_no_go,
    "crate_substitution": _eval_crate_substitution,
    "redesign_approval": _eval_redesign,
    "no_equivalent_dependency": _eval_no_equivalent,
    "unit_escalation": _eval_escalation,
    "scope_change": _eval_scope_change,
    "cutover": _eval_cutover,
}


def evaluate_gate(state: dict, gate: dict, policy: dict, ctx: dict) -> dict:
    """Pure policy evaluation of one gate record. Returns a decision dict;
    decision == UNDECIDABLE means 'cannot be decided from the recorded
    evidence' — the gate stays open and drains into the run report."""
    name = gate.get("gate")
    rule = rule_for(policy, name)
    if rule is None:
        return {"gate": name, "rule": None, "decision": UNDECIDABLE,
                "note": "no policy rule covers this gate (policy.json bug)",
                "evidence": []}
    return EVALUATORS[name](state, gate, rule, ctx)


# ------------------------------------------------------------- applying ----

def _open_gate(state: dict, name: str, note: str, changes: list) -> dict:
    gate = {"gate": name, "state": "open", "note": note}
    state.setdefault("human_gates", []).append(gate)
    changes.append((None, f"human_gates[gate={name}].state", None, "open"))
    return gate


def _descope_module(state: dict, module: str, changes: list) -> list[str]:
    """DEP-1 side effect: units inside `module` and their transitive
    dependents drop to fallback_python; the module leaves go_no_go scope.
    Returns the affected unit ids (evidence for the journal)."""
    units = state.get("units", [])
    hit = {u.get("id") for u in units
           if _under_module(u.get("python_path") or "", module)}
    grown = True
    while grown:  # transitive dependents join the descope
        grown = False
        for u in units:
            if u.get("id") in hit:
                continue
            if any(d in hit for d in u.get("deps", [])):
                hit.add(u.get("id"))
                grown = True
    for u in units:
        uid = u.get("id")
        if uid in hit and u.get("status") in ("pending",):
            changes.append((uid, f"units[{uid}].status", u.get("status"),
                            "fallback_python"))
            u["status"] = "fallback_python"
    gng = state.get("go_no_go")
    if isinstance(gng, dict):
        new_scope = [s for s in (gng.get("scope") or []) if not _under_module(s, module)]
        if new_scope != gng.get("scope"):
            changes.append((None, "go_no_go.scope", gng.get("scope"), new_scope))
            gng["scope"] = new_scope
        new_no_eq = [d for d in (gng.get("no_equivalent_deps") or []) if d != module]
        if new_no_eq != gng.get("no_equivalent_deps"):
            changes.append((None, "go_no_go.no_equivalent_deps",
                            gng.get("no_equivalent_deps"), new_no_eq))
            gng["no_equivalent_deps"] = new_no_eq
    return sorted(x for x in hit if x)


def apply_gate_decision(state: dict, decision: dict, policy: dict,
                        changes: list) -> None:
    """Record one policy decision on its gate (and its side effects) as
    journaled field changes; the caller persists via state_tool.save_validated."""
    name = decision["gate"]
    gate = state_tool.find_gate(state, name)
    if gate is None:
        gate = _open_gate(state, name,
                          f"{name}: opened by the driver for policy resolution",
                          changes)
    note = (gate.get("note") + " | " if gate.get("note") else "") + decision["note"]
    state_tool.record_gate_fields(gate, {
        "state": decision["decision"],
        "decided_by": f"policy:{decision['rule']}",
        "policy_version": policy.get("version"),
        "evidence": state_tool.merge_evidence(gate.get("evidence"),
                                              decision.get("evidence")),
        "note": note,
    }, changes)

    if name == "no_equivalent_dependency" and decision["decision"] == "approved":
        module = parse_evidence(decision.get("evidence")).get("module")
        if module:
            dropped = _descope_module(state, module, changes)
            if dropped:
                state_tool.record_gate_fields(gate, {
                    "note": gate["note"] + f" [descoped units: {', '.join(dropped)}]",
                }, changes)
    if name == "go_no_go":
        gng = state.get("go_no_go")
        if isinstance(gng, dict) and decision.get("go_decision"):
            approver = f"policy:{decision['rule']}"
            if gng.get("decision") != decision["go_decision"]:
                changes.append((None, "go_no_go.decision", gng.get("decision"),
                                decision["go_decision"]))
                gng["decision"] = decision["go_decision"]
            if gng.get("approved_by") != approver:
                changes.append((None, "go_no_go.approved_by",
                                gng.get("approved_by"), approver))
                gng["approved_by"] = approver
    if decision.get("run_status") and state.get("run_status") != decision["run_status"]:
        changes.append((None, "run_status", state.get("run_status"),
                        decision["run_status"]))
        state["run_status"] = decision["run_status"]


def resolve_open_gates(state: dict, policy: dict, ctx: dict,
                       only: str | None = None) -> tuple[list[dict], list[tuple]]:
    """Evaluate + apply every open gate (DEP-1 before GO-1 so descoping
    clears the scope first). Undecidable gates stay open and are reported.
    Mutates `state`; returns (decision records, journaled changes)."""
    open_gates = [g for g in state.get("human_gates", [])
                  if g.get("state") == "open" and (only is None or g.get("gate") == only)]
    open_gates.sort(key=lambda g: GATE_ORDER.get(g.get("gate", ""), 2))
    decisions, changes = [], []
    for gate in open_gates:
        decision = evaluate_gate(state, gate, policy, ctx)
        if decision["decision"] == UNDECIDABLE:
            decisions.append(decision)
            continue
        apply_gate_decision(state, decision, policy, changes)
        decisions.append(decision)
    return decisions, changes


# ------------------------------------------------------ stage verification -

def _validate_state(state: dict) -> list[str]:
    schema = json.loads(state_tool.SCHEMA_PATH.read_text(encoding="utf-8"))
    errors = state_tool.validate_schema(state, schema)
    inv_errors, _ = state_tool.check_invariants(state)
    return errors + inv_errors


def verify_stage_exit(state: dict, stage: int, *, run_selftest: bool = True) -> list[str]:
    """The deterministic, state-expressible subset of the stage checklists
    (repo-level gates — just check, miri, wheel builds — remain the stage
    worker's job inside the stage, enforced by the scaffold's CI)."""
    failures: list[str] = []
    units = state.get("units", [])
    if stage == 0:
        gng = state.get("go_no_go") or {}
        if gng.get("decision") not in ("go", "no-go"):
            failures.append("go_no_go.decision must be recorded (go or no-go) — "
                            "NO-GO is a legitimate, successful outcome")
        if _num((gng.get("projection") or {}).get("end_to_end_saving_pct")) is None:
            failures.append("go_no_go.projection.end_to_end_saving_pct missing — "
                            "profile before porting (hard rule 1)")
        if not gng.get("scope"):
            failures.append("go_no_go.scope empty — scope is the minimal tranche, "
                            "never 'the repo'")
    elif stage == 1:
        if not units:
            failures.append("units[] empty — Stage 1 records the dependency-"
                            "ordered backlog")
        for u in units:
            uid = u.get("id")
            if not isinstance(u.get("deps"), list):
                failures.append(f"units[{uid}].deps missing")
            if not isinstance(u.get("order"), int):
                failures.append(f"units[{uid}].order missing")
            if not ((u.get("contract") or {}).get("covering_tests")):
                failures.append(f"units[{uid}].contract.covering_tests empty — "
                                f"the full test suite is the spec")
    elif stage == 2:
        jv = state.get("judge_validation") or {}
        injected, caught = jv.get("injected_bugs"), jv.get("caught")
        if not isinstance(injected, int) or injected < 5 or caught != injected:
            failures.append(f"judge_validation: judge-first hard gate requires "
                            f"5/5 injected bugs caught (recorded "
                            f"{caught}/{injected})")
        if not jv.get("normalization_rules"):
            failures.append("judge_validation.normalization_rules missing")
        if run_selftest:
            try:
                proc = subprocess.run([sys.executable, str(ORACLE), "self-test"],
                                      capture_output=True, text=True, timeout=300)
            except subprocess.TimeoutExpired:
                failures.append("oracle.py self-test timed out — the parity "
                                "judge is not validated (Stage 2 hard gate)")
            else:
                if proc.returncode != 0:
                    failures.append("oracle.py self-test failed — the parity "
                                    "judge is not validated (Stage 2 hard gate)")
    elif stage == 3:
        missing = [u.get("id") for u in units if not u.get("rust_path")]
        if missing:
            failures.append(f"units without rust_path after scaffold: {missing}")
    elif stage == 4:
        unfinished = [u.get("id") for u in units if u.get("status") == "pending"]
        if unfinished:
            failures.append(f"units still pending (every unit must end the loop "
                            f"ported/divergent_explained/escalated/fallback_python): "
                            f"{unfinished}")
    elif stage == 5:
        bad = [u.get("id") for u in units
               if u.get("status") in ("ported", "divergent_explained")
               and not ((u.get("attribution") or {}).get("header_preserved")
                        and (u.get("attribution") or {}).get("notice_ok"))]
        if bad:
            failures.append(f"attribution gate not green for: {bad}")
    elif stage == 6:
        if state_tool.find_gate(state, "cutover") is None:
            failures.append("cutover gate absent — Stage 6 must surface the "
                            "cutover decision (approved OR rejected; either is "
                            "a decision, open is not)")
    return failures


# ----------------------------------------------------------------- report --

def _tokens_spent(state: dict) -> int:
    """Run-level budget usage: the ledger plus every unit's spend."""
    ledger = (state.get("budget_ledger") or {}).get("tokens_total") or 0
    per_unit = sum((u.get("budget") or {}).get("tokens_spent", 0) or 0
                   for u in state.get("units", []))
    return ledger + per_unit


def build_report(state: dict, policy: dict) -> str:
    level = state_tool.autonomy_level(state)
    tokens = _tokens_spent(state)
    ceiling = ((state.get("autonomy") or {}).get("budget") or {}).get("max_tokens")
    counts = Counter(u.get("status", "?") for u in state.get("units", []))
    lines = [
        f"# Run report — {state.get('project', {}).get('name', '?')}",
        "",
        f"- run_status: **{state.get('run_status', 'pending')}** (terminal states: "
        f"complete / no_go_measured / budget_exhausted / failed_safely — never "
        f"'awaiting input')",
        f"- stage reached: {state.get('stage')}/{LAST_STAGE}",
        f"- autonomy: {level} (policy v{(state.get('autonomy') or {}).get('policy_version', policy.get('version'))})",
        f"- budget: {tokens} tokens recorded"
        + (f" / ceiling {ceiling}" if ceiling else ""),
        f"- units: {len(state.get('units', []))} — "
        + (", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "none"),
        "",
        "## Gate dossier",
        "",
        "| gate | state | decided_by | evidence |",
        "|---|---|---|---|",
    ]
    for g in state.get("human_gates", []):
        ev = "; ".join(g.get("evidence") or []) or "—"
        lines.append(f"| {g.get('gate')} | {g.get('state')} | "
                     f"{g.get('decided_by', '—')} | {ev} |")
    if not state.get("human_gates"):
        lines.append("| _(none recorded)_ | | | |")
    escalated = [u for u in state.get("units", []) if u.get("status") == "escalated"]
    deferred = [g for g in state.get("human_gates", [])
                if g.get("state") in ("open", "deferred")]
    lines += ["", "## Escalation dossier (deferred to humans)", ""]
    if not escalated and not deferred:
        lines.append("_No escalations — the queue is empty._")
    for u in escalated:
        lines.append(f"- unit **{u.get('id')}** ({u.get('python_path')}): "
                     f"python fallback live; {u.get('notes') or 'see state file'}")
    for g in deferred:
        lines.append(f"- gate **{g.get('gate')}** [{g.get('state')}]: "
                     f"{g.get('note') or 'see state file'}")
    lines.append("")
    return "\n".join(lines)


def report_path_for(state_path: Path) -> Path:
    """<state>.json -> <state>.run-report.md (same directory)."""
    return state_path.parent / (state_path.stem + ".run-report.md")


# -------------------------------------------------------------- commands ---

def _load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ToolError(f"state {path}: {e}", kind="io") from e
    except json.JSONDecodeError as e:
        raise ToolError(f"state {path}: invalid JSON: {e}", kind="usage") from e
    if not isinstance(state, dict):
        raise ToolError(f"state {path}: must contain a JSON object", kind="usage")
    return state


def _emit_refusal(reason: str, human_flag: bool, **extra) -> int:
    payload = {"tool": TOOL, "version": VERSION, "error": reason,
               "summary": {"exit_code": EXIT_FINDINGS, "human": reason}}
    payload.update(extra)
    emit(payload, human_flag=human_flag)
    return EXIT_FINDINGS


class _Terminal(Exception):
    """Raised inside the run loop to jump to the terminal-state exit path,
    carrying the working state snapshot to journal (driver-opened gates live
    in memory until a stage verifies, so the snapshot must travel)."""

    def __init__(self, run_status: str, reason: str, state: dict | None = None):
        super().__init__(reason)
        self.run_status = run_status
        self.reason = reason
        self.state = state


def _invoke_agent(cmd_template: str, stage: int, state_path: Path) -> tuple[int, str]:
    cmd = cmd_template.format(
        stage=stage, state=str(state_path),
        stage_file=str(FRAMEWORK / "stages" / STAGE_FILES[stage]),
        framework=str(FRAMEWORK))
    try:
        proc = subprocess.run(shlex.split(cmd), capture_output=True, text=True,
                              timeout=3600)
    except FileNotFoundError as e:
        return 127, f"agent command not found: {e}"
    except subprocess.TimeoutExpired:
        return 124, "agent timed out after 3600s"
    tail = ((proc.stdout or "") + (proc.stderr or ""))[-2000:]
    return proc.returncode, tail


def _open_stage_gates(state: dict, stage: int, only: str | None = None) -> list:
    """Gate ownership at L2+: the driver (not the worker) surfaces this
    stage's decision gates — before verification, so the checklist can rely
    on them existing (in-memory until the stage verifies)."""
    changes: list = []
    for name in STAGE_GATES.get(stage, []):
        if only is not None and name != only:
            continue
        if state_tool.find_gate(state, name) is None:
            _open_gate(state, name,
                       f"{name}: opened by the driver at stage {stage} boundary",
                       changes)
    return changes


def _execute_stage(args, path: Path, stage: int, events: list,
                   max_retries: int) -> tuple[dict, list]:
    """Run the stage worker (any CLI agent), then verify the stage's exit
    criteria, retrying within max_retries. Returns (state, changes for gates
    the driver opened); raises _Terminal when attempts run out."""
    attempts = 0
    state: dict | None = None
    while True:
        if not args.dry_run:
            rc, tail = _invoke_agent(args.agent_cmd, stage, path)
            if rc != 0:
                attempts += 1
                events.append({"stage": stage, "event": "agent-failed",
                               "rc": rc, "tail": tail[-400:]})
                if attempts > max_retries:
                    raise _Terminal("failed_safely",
                                    f"stage {stage}: agent exited {rc} after "
                                    f"{attempts} attempt(s): {tail[-400:]}",
                                    state=state)
                continue
        try:
            state = _load_state(path)
        except ToolError as e:
            raise _Terminal("failed_safely",
                            f"stage {stage}: state unreadable after agent run "
                            f"({e.detail}); file left as-is", state=state) from e
        open_changes = _open_stage_gates(state, stage)
        failures = _validate_state(state) + verify_stage_exit(
            state, stage, run_selftest=not args.no_selftest)
        if not failures:
            events.append({"stage": stage, "event": "verified"})
            return state, open_changes
        if args.dry_run:
            raise _Terminal("failed_safely",
                            f"stage {stage} exit criteria unmet (dry-run, no "
                            f"agent to repair): {failures}", state=state)
        attempts += 1
        events.append({"stage": stage, "event": "verify-failed",
                       "failures": failures})
        if attempts > max_retries:
            raise _Terminal("failed_safely",
                            f"stage {stage}: exit criteria unmet after "
                            f"{attempts} attempt(s): {failures}", state=state)


def _enter_terminal_status(state: dict, path: Path, terminal: _Terminal,
                           events: list) -> dict:
    """Journal the terminal run_status; on an invalid resulting state keep
    the file as-is and record the failure (the report still goes out)."""
    old = state.get("run_status")
    state["run_status"] = terminal.run_status  # mutate BEFORE journaling:
    try:                                       # save_validated writes `state`
        state_tool.save_validated(state, path, actor=TOOL,
                                  changes=[(None, "run_status", old,
                                            terminal.run_status)])
    except (ValueError, ToolError):
        state["run_status"] = old
        events.append({"event": "terminal-journal-failed",
                       "detail": terminal.reason})
    else:
        state = _load_state(path)
    return state


def cmd_run(args) -> int:
    policy = load_policy(args.policy)
    path = Path(args.state)
    state = _load_state(path)
    if args.autonomy:
        block = state.get("autonomy") or {}
        if block.get("level") != args.autonomy:
            new_block = state_tool.autonomy_block(args.autonomy)
            changes = [(None, "autonomy", block or None, new_block)]
            state["autonomy"] = new_block
            state_tool.save_validated(state, path, actor=TOOL, changes=changes)
    level = state_tool.autonomy_level(state)
    if level not in state_tool.DRIVER_LEVELS:
        return _emit_refusal(
            f"autonomy={level}: the deterministic driver runs L2+ only. L0/L1 "
            f"stay with the agent-driven recipe (gates are human stops there). "
            f"Pre-authorize autonomy BEFORE the run: --autonomy L2|L3 here, or "
            f"state.py init --autonomy-level.", args.human, file=str(path))
    run_status = state.get("run_status")
    if run_status in ("complete", "no_go_measured"):
        return _emit_refusal(
            f"run_status={run_status} is a finished run — start a fresh run "
            f"(state.py init) to migrate again; resume is only for "
            f"budget_exhausted/failed_safely after raising the ceiling",
            args.human, file=str(path))
    if not args.dry_run and not args.agent_cmd:
        return _emit_refusal(
            "--agent-cmd is required unless --dry-run: the driver shells out to "
            "a CLI agent as the stage worker (goose / Claude Code / Codex — "
            "portability is the point). Placeholders: {stage} {state} "
            "{stage_file} {framework}", args.human, file=str(path))

    ceilings = ((state.get("autonomy") or {}).get("budget")
                or policy.get("budget") or {})
    max_tokens = ceilings.get("max_tokens")
    max_minutes = ceilings.get("max_wall_clock_minutes")
    max_retries = int((policy.get("budget") or {})
                      .get("max_agent_retries_per_stage", 2))
    ctx = {"today": date.today(), "substitutions": load_substitutions(args.substitutions)}

    if run_status in (None, "pending", "budget_exhausted", "failed_safely"):
        changes = [(None, "run_status", run_status, "running")]
        state["run_status"] = "running"
        state_tool.save_validated(state, path, actor=TOOL, changes=changes)

    started = time.monotonic()
    events: list[dict] = []
    terminal: _Terminal | None = None

    def journal(changes: list) -> None:
        state_tool.save_validated(state, path, actor=TOOL, changes=changes)

    try:
        while True:
            state = _load_state(path)
            tokens = _tokens_spent(state)
            elapsed = (time.monotonic() - started) / 60
            if max_tokens is not None and tokens > max_tokens:
                raise _Terminal("budget_exhausted",
                                f"token budget exhausted: {tokens} > {max_tokens}")
            if max_minutes is not None and elapsed > max_minutes:
                raise _Terminal("budget_exhausted",
                                f"wall-clock budget exhausted: {elapsed:.1f} min > "
                                f"{max_minutes} min")
            stage = state.get("stage", 0)
            if stage > LAST_STAGE:
                raise _Terminal("complete", f"stage {stage} beyond {LAST_STAGE}")

            # 1. stage worker (any CLI agent), verified, with bounded retries
            state, open_changes = _execute_stage(args, path, stage, events,
                                                 max_retries)

            # 2. resolve open gates via policy (defer/conservative-default)
            decisions, changes = resolve_open_gates(state, policy, ctx)
            changes = open_changes + changes
            if decisions or changes:
                try:
                    journal(changes)
                except ValueError as e:
                    raise _Terminal("failed_safely",
                                    f"stage {stage}: gate resolution would leave "
                                    f"the state invalid ({e}); rolled back") from e
                for d in decisions:
                    events.append({"stage": stage, "event": "gate-resolved",
                                   "gate": d["gate"], "rule": d.get("rule"),
                                   "decision": d["decision"]})
            for d in decisions:
                if d.get("run_status"):
                    raise _Terminal(d["run_status"],
                                    f"stage {stage}: gate {d['gate']} resolved "
                                    f"{d['decision']} by policy:{d['rule']} — "
                                    f"{d['note']}")

            # 3. advance or finish
            if stage == LAST_STAGE:
                raise _Terminal("complete",
                                f"stages 0->{LAST_STAGE} done; Stage 7 maintenance "
                                f"is ongoing by design (upstream tracking)")
            state["stage"] = stage + 1
            journal([(None, "stage", stage, stage + 1)])
            events.append({"stage": stage, "event": "advanced", "to": stage + 1})
            if args.once:
                break
    except _Terminal as t:
        terminal = t
        if t.state is not None:
            state = t.state

    if terminal is not None and state.get("run_status") != terminal.run_status:
        state = _enter_terminal_status(state, path, terminal, events)
    report = build_report(state, policy)
    rp = report_path_for(path)
    rp.write_text(report, encoding="utf-8")
    run_status = state.get("run_status", "pending")
    code = EXIT_FINDINGS if run_status == "failed_safely" else EXIT_OK
    human = (f"run {'finished' if terminal is not None else 'paused'}: "
             f"run_status={run_status}, stage={state.get('stage')}"
             + (f" — {terminal.reason}" if terminal is not None else
                f" (--once: resume with another pipeline.py run)")
             + f"; report: {rp}")
    emit({"tool": TOOL, "version": VERSION, "file": str(path),
          "run_status": run_status, "stage": state.get("stage"),
          "autonomy": state.get("autonomy"),
          "terminal": bool(terminal is not None),
          "reason": terminal.reason if terminal is not None else None,
          "events": events, "report_path": str(rp),
          "open_gates": [g for g in state.get("human_gates", [])
                         if g.get("state") == "open"],
          "summary": {"exit_code": code, "human": human}}, human_flag=args.human)
    return code


def cmd_resolve_gates(args) -> int:
    policy = load_policy(args.policy)
    path = Path(args.state)
    state = _load_state(path)
    level = state_tool.autonomy_level(state)
    if level not in state_tool.DRIVER_LEVELS and not args.human_override:
        return _emit_refusal(
            f"autonomy={level}: gates resolve by policy at L2+ only. At L0/L1 a "
            f"human decides — ask, then record with "
            f"`state.py resolve-gate --by human` (--human-override forces "
            f"evaluation here; the decision is still journaled policy:<rule>)",
            args.human, file=str(path))
    ctx = {"today": date.today(), "substitutions": load_substitutions(args.substitutions)}
    # the driver, not the stage worker, surfaces the gates the current stage
    # must decide — same rule the run loop uses
    open_changes = _open_stage_gates(state, state.get("stage", 0), only=args.gate)
    decisions, changes = resolve_open_gates(state, policy, ctx, only=args.gate)
    changes = open_changes + changes
    if not decisions:
        human = f"no open gates to resolve ({path})"
        emit({"tool": TOOL, "version": VERSION, "file": str(path),
              "decisions": [],
              "summary": {"exit_code": EXIT_OK, "human": human}},
             human_flag=args.human)
        return EXIT_OK
    applied = [d for d in decisions if d["decision"] != UNDECIDABLE]
    undecidable = [d for d in decisions if d["decision"] == UNDECIDABLE]
    if changes and not args.dry_run:  # persist driver-opened gates too
        try:
            state_tool.save_validated(state, path, actor=TOOL, changes=changes)
        except ValueError as e:
            return _emit_refusal(f"gate resolution rolled back — resulting state "
                                 f"would be invalid: {e}", args.human,
                                 file=str(path))
    human = (f"{len(applied)} gate(s) resolved by policy, "
             f"{len(undecidable)} undecidable (left open, in the report)"
             + (" [dry-run: nothing written]" if args.dry_run else ""))
    emit({"tool": TOOL, "version": VERSION, "file": str(path),
          "decisions": decisions, "policy_version": policy.get("version"),
          "summary": {"exit_code": EXIT_OK, "human": human}}, human_flag=args.human)
    return EXIT_OK


def cmd_report(args) -> int:
    policy = load_policy(args.policy)
    path = Path(args.state)
    state = _load_state(path)
    report = build_report(state, policy)
    out = Path(args.out) if args.out else report_path_for(path)
    out.write_text(report, encoding="utf-8")
    human = f"report written to {out} (run_status={state.get('run_status', 'pending')})"
    emit({"tool": TOOL, "version": VERSION, "file": str(path),
          "report_path": str(out), "report": report,
          "summary": {"exit_code": EXIT_OK, "human": human}}, human_flag=args.human)
    return EXIT_OK


def cmd_status(args) -> int:
    load_policy(args.policy)  # validate --policy even though status needs no rules
    state = _load_state(Path(args.state))
    tokens = _tokens_spent(state)
    ceiling = ((state.get("autonomy") or {}).get("budget") or {}).get("max_tokens")
    open_gates = [g for g in state.get("human_gates", []) if g.get("state") == "open"]
    human = (f"run={state.get('run_status', 'pending')} stage={state.get('stage')}/"
             f"{LAST_STAGE} autonomy={state_tool.autonomy_level(state)} "
             f"tokens={tokens}/{ceiling or '∞'} open_gates="
             f"{[g.get('gate') for g in open_gates] or 'none'}")
    emit({"tool": TOOL, "version": VERSION, "file": args.state,
          "run_status": state.get("run_status", "pending"),
          "stage": state.get("stage"), "units": len(state.get("units", [])),
          "tokens_spent": tokens, "token_ceiling": ceiling,
          "autonomy": state.get("autonomy"), "open_gates": open_gates,
          "summary": {"exit_code": EXIT_OK, "human": human}}, human_flag=args.human)
    return EXIT_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=TOOL, description=__doc__.splitlines()[0])
    ap.add_argument("--human", action="store_true", help="echo human summary to stderr")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="drive stages 0->7 to a terminal state (L2+)")
    p.add_argument("state")
    p.add_argument("--agent-cmd",
                   help="CLI agent invoked per stage; placeholders {stage} {state} "
                        "{stage_file} {framework}, e.g. "
                        "'goose run --recipe {framework}/recipe.yaml'")
    p.add_argument("--autonomy", choices=sorted(state_tool.POLICY_LEVELS),
                   help="set the pre-authorized level before starting (journaled)")
    p.add_argument("--policy", help="override autonomy/policy.json")
    p.add_argument("--substitutions", help="override knowledge/substitutions.json")
    p.add_argument("--dry-run", action="store_true",
                   help="no agent invocation: verify/resolve/advance on the "
                        "recorded state only (resume/replay)")
    p.add_argument("--once", action="store_true",
                   help="one iteration (agent -> verify -> resolve -> advance), "
                        "then pause resumable")
    p.add_argument("--no-selftest", action="store_true",
                   help="skip the oracle.py self-test at Stage 2 (offline corner)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("resolve-gates",
                       help="evaluate open gates against the policy, in code")
    p.add_argument("state")
    p.add_argument("--gate", choices=state_tool.GATES,
                   help="resolve only this gate (default: every open gate)")
    p.add_argument("--policy")
    p.add_argument("--substitutions")
    p.add_argument("--dry-run", action="store_true",
                   help="show decisions without writing")
    p.add_argument("--human-override", action="store_true",
                   help="evaluate even below L2 (a human is driving this command)")
    p.set_defaults(func=cmd_resolve_gates)

    p = sub.add_parser("report", help="emit the run report markdown")
    p.add_argument("state")
    p.add_argument("--policy")
    p.add_argument("--out", help="override the output path")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("status", help="one-JSON run summary")
    p.add_argument("state")
    p.add_argument("--policy")
    p.set_defaults(func=cmd_status)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(run_cli(main, tool=TOOL, version=VERSION))
