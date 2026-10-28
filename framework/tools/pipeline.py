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
Session-level ``budget_exhausted`` (the orchestrator session's context
window, not these run-level ceilings) is handled by ``succession.py``
(issue #25), which chains successor GLV sessions automatically and
bootstraps GLV itself (issue #47: serve + its own named token — no
alternate transport); this driver's own ``budget_exhausted`` exit
therefore means the run-level bound hit — the pre-authorized budget is
spent, and raising it is the operator's between-runs decision.

Autonomy levels (pre-authorized BEFORE the run; zero mid-run questions at
any level — the agent never asks): L0 supervised (gates are hard stops: the
agent-driven recipe leaves the gate open and ends the run with the dossier
for the operator; this driver refuses to run it) · L1 agent proposes into
the dossier, operator decides between runs (also refused here) · L2 all
gates policy-resolved, publish disabled · L3 full autopilot incl. cutover.

Subcommands
-----------
  preflight      run and record, in one command, everything stages 0/3
                 silently assumed (issue #31): target suite green + wall
                 time, pytest collection capture, oracle self-test 5/5,
                 rust-template reachability + HEAD SHA, toolchain versions,
                 target venv/interpreter/wheel flavor/derived PYO3_PYTHON,
                 substitutions freshness, framework-vs-target interpreter
                 match — journaled as a `preflight` block + a human report;
                 exit 2 on the hard prerequisites (suite not green,
                 self-test not 5/5, template unreachable) so a run starts
                 only when Stage 0's exit criteria are actually checkable
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
failed_safely is a *result*, not a failure) · 2 findings (failed_safely, a
refusal such as "driver requires L2+", or a preflight hard prerequisite —
suite not green / self-test not 5/5 / template unreachable) · 1 usage/IO
via the shared envelope.
"""

from __future__ import annotations

import argparse
import json
import os
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
    EXIT_FINDINGS, EXIT_OK, ToolError, emit, now_iso, python_version_string,
    run_child, run_cli,
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

# Preflight constants (issue #31): the environment checks stages 0-3 each
# re-implemented ad hoc. Three are hard prerequisites: without them Stage
# 0's exit criteria are not even checkable, so preflight fails loudly
# (exit 2) instead of letting a run start on assumptions.
TEMPLATE_URL = "https://github.com/SynthLuvr/rust-template"
HARD_CHECKS = ("suite", "oracle_selftest", "template")
SKIP_TO_CHECK = {"suite": "suite", "pytest-collect": "pytest_collect",
                 "selftest": "oracle_selftest", "template": "template",
                 "toolchain": "toolchain"}
PYTEST_NODEID_RE = re.compile(r"^\S+\.py::\S+$")
PYTEST_SUMMARY_RE = re.compile(
    r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)")
# cargo/rustc/maturin/uv feed Stage 3 directly; the extras are the
# template's gate stack
TOOLCHAIN_VERSION_CMDS = {"cargo": ["cargo", "--version"],
                          "rustc": ["rustc", "--version"],
                          "maturin": ["maturin", "--version"],
                          "uv": ["uv", "--version"]}
TOOLCHAIN_EXTRA_CMDS = {"cargo-deny": ["cargo", "deny", "--version"],
                        "cargo-llvm-cov": ["cargo", "llvm-cov", "--version"],
                        "cargo-shear": ["cargo", "shear", "--version"],
                        "pandoc": ["pandoc", "--version"]}
SUBSTITUTIONS_MAX_AGE_DAYS = 30  # the table's own rule: RE-VERIFY before each migration
SUITE_TIMEOUT = 3600.0           # matches the driver's per-stage agent timeout
COLLECT_TIMEOUT = 300.0          # matches contract_extract.py's collection timeout


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
    max_chain_depth = budget.get("max_chain_depth") if isinstance(budget, dict) else None
    if max_chain_depth is not None and (not isinstance(max_chain_depth, int)
                                        or isinstance(max_chain_depth, bool)
                                        or max_chain_depth < 1):
        errors.append("budget.max_chain_depth: integer >= 1 required "
                      "(the successor-chain depth cap, issue #25)")
    candidates = policy.get("candidates")
    if not isinstance(candidates, dict):
        errors.append("candidates: object required — K is policy, not judgment "
                      "(issue #59); without the schedule every run re-improvises "
                      "the K-vs-budget tradeoff")
    else:
        cid = candidates.get("id")
        if not isinstance(cid, str) or not re.fullmatch(r"[A-Z]+-\d+", cid or ""):
            errors.append(f"candidates.id {cid!r}: must match <AREA>-<number> "
                          f"(e.g. CAND-1) — journal/pack citations are "
                          f"'policy:<id>'")
        for key in ("k_small_unit", "k_large_unit"):
            v = candidates.get(key)
            if not isinstance(v, int) or isinstance(v, bool) or v < 1:
                errors.append(f"candidates.{key}: integer >= 1 required")
        v = candidates.get("size_threshold_loc")
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            errors.append("candidates.size_threshold_loc: integer >= 1 required "
                          "(unit size in non-blank Python LOC)")
        frac = candidates.get("budget_floor_fraction")
        if not isinstance(frac, (int, float)) or isinstance(frac, bool) \
                or not 0 < frac <= 1:
            errors.append("candidates.budget_floor_fraction: number in (0, 1] "
                          "required (the run-budget-remaining fraction under "
                          "which K drops to k_large_unit)")
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


def resolve_k(state: dict, unit: dict, policy: dict,
              unit_loc: int | None = None) -> dict:
    """K is policy, not judgment (issue #59): resolve a unit's candidate
    count from policy.json's 'candidates' schedule (id CAND-1) — K=3 below
    the size threshold, K=1 + bounded repair rounds above it or under the
    run-budget floor. Pure and shared: delegate.py run-candidates enforces
    it as its default so no orchestrator re-improvises the tradeoff, and
    the decision (with its inputs) rides the verdict pack + the journal as
    a policy id, not prose. Repair rounds count as candidates for the
    oracle ranking, so 'the oracle ranks, not the model' holds by
    construction at any K."""
    cand = policy.get("candidates") or {}
    k_small = int(cand.get("k_small_unit", 3))
    k_large = int(cand.get("k_large_unit", 1))
    threshold = int(cand.get("size_threshold_loc", 150))
    floor = float(cand.get("budget_floor_fraction", 0.25))
    citation = f"policy:{cand.get('id', 'CAND-1')}"

    spent = tokens_spent(state)
    ceiling = ((state.get("autonomy") or {}).get("budget") or {}) \
        .get("max_tokens") or (policy.get("budget") or {}).get("max_tokens")
    remaining = None
    if isinstance(ceiling, int) and ceiling > 0:
        remaining = (ceiling - spent) / ceiling
    inputs = {"unit_loc": unit_loc, "size_threshold_loc": threshold,
              "tokens_spent": spent, "max_tokens": ceiling,
              "remaining_fraction": (round(remaining, 4)
                                     if remaining is not None else None),
              "budget_floor_fraction": floor}

    if remaining is not None and remaining < floor:
        k, basis = k_large, [
            f"run budget remaining {remaining:.0%} < floor {floor:.0%} — "
            f"K={k_large} + bounded repair rounds (MAX_ROUNDS) as the "
            f"candidate pool"]
    elif unit_loc is None:
        k, basis = k_small, [
            "unit size unknown (python_path unreadable/missing) — K="
            f"{k_small}, the small-unit default; not a judgment call"]
    elif unit_loc > threshold:
        k, basis = k_large, [
            f"unit_loc {unit_loc} > size_threshold_loc {threshold} — "
            f"K={k_large} + bounded repair rounds (MAX_ROUNDS) as the "
            f"candidate pool"]
    else:
        k, basis = k_small, [
            f"unit_loc {unit_loc} <= size_threshold_loc {threshold} — cheap "
            f"to parallelize, big variance payoff"]
    return {"k": k, "citation": citation,
            "policy_version": policy.get("version"),
            "rounds_as_candidates": k < k_small, "inputs": inputs, "basis": basis}


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
    # issue #34: the projection is a *claim* until a matched A/B benchmark
    # exists — when bench_ab.py has recorded a measured end_to_end_saving_pct
    # on the gate, the measurement outranks the Stage-0 estimate
    ev = parse_evidence(gate.get("evidence"))
    measured = _num(ev.get("end_to_end_saving_pct"))
    saving = measured if measured is not None else _num(
        projection.get("end_to_end_saving_pct"))
    source = ("measured (bench_ab A/B)" if measured is not None
              else "projected (Stage 0)")
    scope = gng.get("scope") or []
    if saving is None or not scope:
        return _decision("go_no_go", rule, UNDECIDABLE,
                         "projection/scope not recorded — Stage 0 exit criteria "
                         "unmet; nothing to decide on", [])
    blocking = [d for d in (gng.get("no_equivalent_deps") or []) if d in scope]
    evidence = [f"end_to_end_saving_pct={saving} (bar >= {bar}, {source})",
                f"scoped_loc={projection.get('scoped_loc')}",
                f"scope={','.join(scope)}"]
    if blocking:
        return _decision("go_no_go", rule, "rejected",
                         f"no-equivalent dependency inside scope: {blocking} — "
                         f"descope via policy:DEP-1 first (never improvise a "
                         f"substitute)", evidence + [f"blocking={blocking}"])
    if saving >= bar:
        return _decision("go_no_go", rule, "approved",
                         f"go — {source} saving meets the bar ({saving}% >= "
                         f"{bar}%); scope is the recorded minimal tranche",
                         evidence, go_decision="go")
    on_reject = rule.get("on_reject") or {}
    return _decision("go_no_go", rule, "rejected",
                     on_reject.get("note") or
                     f"no-go — {source} saving below the bar; keep/optimize the "
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


# ------------------------------------------------------------- preflight --
# Each check is a pure-ish function returning one record; cmd_preflight
# assembles them into the journaled `preflight` block. Hard checks
# (HARD_CHECKS) failing — or being *unprovable* (no target recorded) —
# exits 2; an explicit --skip downgrades a hard check to a warning (the
# operator's choice, never silent).

def _probe(cmd: list[str], *, cwd: str | None = None,
           timeout: float = 120.0) -> tuple[int, str, str]:
    """run_child with the not-found/timeout corner normalized to a failed
    probe (code 127/124) so one code path reports every outcome."""
    try:
        return run_child(cmd, cwd=cwd, timeout=timeout)
    except OSError:
        return 127, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:g}s"


def _resolve_target_root(flag_value: str | None, state: dict) -> tuple[Path | None, str]:
    """--target-root wins; else the root a previous preflight recorded
    (preflight.target.root) — re-runs need no re-typing. Returns
    (resolved absolute path or None, note for the report)."""
    recorded = ((state.get("preflight") or {}).get("target") or {}).get("root")
    raw = flag_value or recorded
    if not raw:
        return None, ("no target root recorded — pass --target-root once; it "
                      "is journaled into the preflight block for re-runs")
    root = Path(raw).resolve()
    if not root.is_dir():
        return None, f"target root {raw} is not a directory"
    source = "--target-root" if flag_value else "recorded by a previous preflight"
    return root, f"{root} (from {source})"


def _resolve_target_python(root: Path | None) -> tuple[Path | None, str]:
    """The target's venv interpreter (`.venv` then `venv`, POSIX and Windows
    layouts). None means: fall back to this interpreter for the suite /
    self-test runs, recorded as such — an honest failure with the output
    tail beats a silent wrong-interpreter run."""
    if root is None:
        return None, "no target root recorded"
    exe = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    for name in (".venv", "venv"):
        py = root / name / exe
        if py.exists():
            return py, f"target venv {root / name}"
    return None, f"no .venv/ or venv/ under {root}"


_PY_PROBE_SRC = (
    "import json, sys, sysconfig; print(json.dumps({"
    "'version': '%d.%d.%d' % sys.version_info[:3],"
    " 'implementation': sys.implementation.name,"
    " 'gil_disabled': bool(sysconfig.get_config_var('Py_GIL_DISABLED'))}))"
)


def _probe_python(python: Path | str) -> dict:
    code, out, err = _probe([str(python), "-c", _PY_PROBE_SRC], timeout=60.0)
    if code != 0:
        return {"ok": False, "detail": (err.strip() or f"exit {code}")[:300]}
    try:
        info = json.loads(out.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"ok": False, "detail": "probe output was not the expected JSON"}
    info["ok"] = True
    return info


def _wheel_flavor(gil_disabled: bool) -> str:
    """The Stage 3 wheel decision the flavor forces (stages/3-scaffold.md):
    free-threaded targets need cp3XXt wheels; abi3t arrives with 3.15."""
    if gil_disabled:
        return "free-threaded — cp3XXt wheels required (abi3t arrives with 3.15)"
    return "standard — abi3 or version-specific wheels"


def _parse_pytest_summary(text: str) -> dict[str, int]:
    """Best-effort counts from pytest -q's summary line
    ('12 passed, 1 skipped in 3.42s'); plural keys fold to singular."""
    counts: dict[str, int] = {}
    for m in PYTEST_SUMMARY_RE.finditer(text or ""):
        key = m.group(2).removesuffix("s")
        counts[key] = counts.get(key, 0) + int(m.group(1))
    return counts


def _minor_version(version: str) -> tuple[int, int] | None:
    parts = (version or "").split(".")
    try:
        return (int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        return None


def _interpreter_mismatch(framework: str, target: str) -> str | None:
    """Warning when the framework interpreter is older than the target's:
    tools that parse target source (port_plan.py, contract_extract.py) and
    the oracle's reference side must run under the target interpreter — a
    3.10 framework python SyntaxErrors on 3.12+ syntax (hit for real)."""
    f, t = _minor_version(framework), _minor_version(target)
    if f and t and t > f:
        return (f"framework python {framework} is older than target python "
                f"{target}: run the source-parsing tools (port_plan.py, "
                f"contract_extract.py) and the oracle's reference side under "
                f"the target interpreter, or they SyntaxError on newer syntax")
    return None


def _substitutions_record(subs: dict, today: date) -> dict:
    """knowledge/substitutions.json freshness — the table's own rule is
    'RE-VERIFY before each migration'; an unparsable version counts as
    stale (never silently trusted)."""
    version = subs.get("version")
    record = {"version": version, "max_age_days": SUBSTITUTIONS_MAX_AGE_DAYS}
    try:
        record["age_days"] = (today - date.fromisoformat(str(version))).days
    except (TypeError, ValueError):
        record["age_days"] = None
    record["stale"] = (record["age_days"] is None
                       or record["age_days"] > SUBSTITUTIONS_MAX_AGE_DAYS)
    record["id"] = "substitutions_freshness"
    record["ok"] = not record["stale"]
    if record["stale"]:
        record["detail"] = (
            f"substitutions.json version {version!r} is "
            + ("unparsable (expected YYYY-MM-DD)" if record["age_days"] is None
               else f"{record['age_days']} days old (> {SUBSTITUTIONS_MAX_AGE_DAYS})")
            + " — RE-VERIFY before Stage 1 maps dependencies (ecosystems move)")
    else:
        record["detail"] = (f"substitutions.json verified {version} "
                            f"({record['age_days']} days old)")
    return record


def _check_template(url: str) -> dict:
    """rust-template reachable + HEAD SHA — Stage 3's provenance header and
    dossier, captured here so the scaffold cites the same run (issue #31).
    `git ls-remote <url> HEAD` needs no clone (a local checkout path works
    offline, which the tests rely on)."""
    code, out, err = _probe(["git", "ls-remote", url, "HEAD"], timeout=60.0)
    sha = next((ln.split()[0] for ln in out.splitlines() if ln.strip()), None)
    ok = code == 0 and bool(sha) and bool(re.fullmatch(r"[0-9a-f]{7,40}", sha))
    record = {"id": "template", "hard": True, "ok": ok, "url": url,
              "head_sha": sha if ok else None}
    record["detail"] = (f"rust-template reachable, HEAD {sha[:12]}"
                        if ok else
                        f"git ls-remote {url} failed (exit {code}): "
                        f"{((err or out).strip() or 'no output')[:200]}")
    return record


def _check_selftest(python: str) -> dict:
    """oracle.py self-test re-proven this run, under the interpreter the run
    will actually judge with — the Stage 2 hard gate is per-run, never
    inherited from an old session."""
    code, out, err = _probe([python, str(ORACLE), "self-test"],
                            timeout=COLLECT_TIMEOUT)
    caught = injected = None
    try:
        payload = json.loads(out)
        caught, injected = payload.get("caught"), payload.get("injected_bugs")
    except json.JSONDecodeError:
        pass
    ok = code == EXIT_OK and caught is not None and caught == injected
    record = {"id": "oracle_selftest", "hard": True, "ok": ok,
              "interpreter": python, "caught": caught, "injected": injected}
    if ok:
        record["detail"] = f"oracle.py self-test {caught}/{injected} under {python}"
    else:
        record["detail"] = (f"oracle.py self-test not {caught}/{injected} "
                            f"(exit {code}) — the Stage 2 judge-first gate "
                            f"cannot be proven this run: "
                            f"{((err or out).strip() or 'no output')[:200]}")
    return record


def _check_pytest_collect(python: str, root: Path, capture_path: Path) -> dict:
    """`pytest --collect-only -q` capture for contract_extract
    (--pytest-collect); written next to the state file so Stage 2 replays
    the same collection instead of re-running it."""
    code, out, err = _probe([python, "-m", "pytest", "--collect-only", "-q"],
                            cwd=str(root), timeout=COLLECT_TIMEOUT)
    nodeids = [ln.strip() for ln in out.splitlines()
               if PYTEST_NODEID_RE.match(ln.strip())]
    capture_path.write_text(out, encoding="utf-8")
    record = {"id": "pytest_collect", "ok": code in (0, 5) and bool(nodeids),
              "exit_code": code, "collected": len(nodeids),
              "errors": _parse_pytest_summary(out[-500:]).get("error", 0),
              "capture": str(capture_path)}
    if code not in (0, 5):
        record["detail"] = (f"pytest --collect-only exited {code}: "
                            f"{((err or out).strip() or 'no output')[:200]} "
                            f"— contract_extract cannot extract contracts")
    elif not nodeids:
        record["detail"] = ("collection found no tests — the suite is not "
                            "substantive enough to act as the spec (hard rule 2)")
    else:
        record["detail"] = (f"{len(nodeids)} tests collected; capture written to "
                            f"{capture_path} (contract_extract --pytest-collect)")
    return record


def _check_suite(python: str, root: Path) -> dict:
    """The target suite green + wall time — the spec baseline Stage 2
    records and every later parity claim is measured against."""
    started = time.monotonic()
    code, out, err = _probe([python, "-m", "pytest", "-q"], cwd=str(root),
                            timeout=SUITE_TIMEOUT)
    wall = round(time.monotonic() - started, 3)
    counts = _parse_pytest_summary((out or "")[-500:] + "\n" + (err or "")[-500:])
    green = code == 0 and counts.get("passed", 0) > 0
    record = {"id": "suite", "hard": True, "ok": green, "exit_code": code,
              "counts": counts, "wall_seconds": wall,
              "tail": ((out or "") + (err or ""))[-600:].strip()}
    if green:
        record["detail"] = (f"target suite green: {counts.get('passed', 0)} "
                            f"passed in {wall:g}s (the Stage 2 baseline)")
    elif code == 5:
        record["detail"] = ("pytest collected no tests (exit 5) — the suite "
                            "is not substantive enough to act as the spec")
    else:
        record["detail"] = (f"target suite NOT green (exit {code}, "
                            f"{counts.get('failed', 0)} failed, "
                            f"{counts.get('error', 0)} errors) — the spec "
                            f"itself is broken; fix it before any migration "
                            f"work (the full test suite is the spec, hard rule 2)")
    return record


def _check_toolchain() -> dict:
    """cargo/rustc/maturin/uv versions + the template gate stack
    (cargo-deny/llvm-cov/shear, pandoc): `just doctor` covers the *crate*,
    nothing checked it before scaffolding — this does (soft: missing pieces
    block Stage 3, which is exactly when they are first needed)."""
    versions, missing = {}, []
    for name, cmd in {**TOOLCHAIN_VERSION_CMDS, **TOOLCHAIN_EXTRA_CMDS}.items():
        code, out, err = _probe(cmd, timeout=60.0)
        first = next((ln for ln in (out or err).splitlines() if ln.strip()), "")
        if code == 0 and first:
            versions[name] = first.strip()
        else:
            versions[name] = None
            missing.append(name)
    core_missing = [n for n in TOOLCHAIN_VERSION_CMDS if n in missing]
    record = {"id": "toolchain", "ok": True, "versions": versions,
              "missing": missing, "core_missing": core_missing}
    present = "; ".join(f"{k}: {versions[k]}" for k in versions if versions[k])
    record["detail"] = (
        (present or "no toolchain found")
        + (f" — MISSING: {', '.join(missing)} (Stage 3 gates need them)"
           if missing else " — full toolchain present"))
    return record


def _check_target_interpreter(root: Path | None, venv_python: Path | None,
                              venv_note: str) -> dict:
    """Target venv + interpreter version + Py_GIL_DISABLED (wheel flavor) +
    the derived PYO3_PYTHON the justfile pins for dev builds."""
    if venv_python is None:
        return {"id": "target_interpreter", "ok": False,
                "root": str(root) if root else None,
                "detail": f"not resolved: {venv_note} — PYO3_PYTHON and the "
                          f"wheel flavor are unknown; Stage 3/4 dev builds "
                          f"cannot be pinned"}
    info = _probe_python(venv_python)
    if not info.get("ok"):
        return {"id": "target_interpreter", "ok": False,
                "python": str(venv_python),
                "detail": f"probe failed: {info.get('detail')}"}
    flavor = _wheel_flavor(bool(info.get("gil_disabled")))
    return {"id": "target_interpreter", "ok": True, "root": str(root),
            "python": str(venv_python), "version": info["version"],
            "implementation": info.get("implementation"),
            "gil_disabled": bool(info.get("gil_disabled")),
            "wheel_flavor": flavor, "pyo3_python": str(venv_python),
            "detail": f"{info['version']} at {venv_python} — {flavor}; "
                      f"PYO3_PYTHON={venv_python}"}


def build_preflight_report(project: str, block: dict) -> str:
    """Human report for <state>.preflight.md — the one artifact an operator
    reads before starting the run."""
    checks = block.get("checks") or {}
    lines = [
        f"# Preflight — {project} ({block.get('timestamp')})",
        "",
        f"- result: **{'ok' if block.get('ok') else 'FAILED — hard prerequisite(s) unproven'}**",
        f"- policy: v{block.get('policy_version', '?')}",
        f"- framework interpreter: {block.get('framework_python', '?')}",
    ]
    target = checks.get("target_interpreter") or {}
    if target.get("python"):
        lines.append(f"- target interpreter: {target.get('version')} at "
                     f"{target.get('python')} — {target.get('wheel_flavor')}; "
                     f"PYO3_PYTHON={target.get('pyo3_python')}")
    else:
        lines.append(f"- target interpreter: unresolved ({target.get('detail')})")
    lines += ["", "| check | status | detail |", "|---|---|---|"]
    for cid in ("template", "oracle_selftest", "pytest_collect", "suite",
                "toolchain", "target_interpreter", "substitutions_freshness",
                "interpreter_match"):
        rec = checks.get(cid)
        if not rec:
            continue
        if rec.get("skipped"):
            status = "skipped"
        else:
            status = "ok" if rec.get("ok") else "FAILED"
            if rec.get("hard"):
                status += " · hard"
        lines.append(f"| {cid} | {status} | {rec.get('detail', '')} |")
    if block.get("warnings"):
        lines += ["", "## Warnings", ""]
        lines += [f"- {w}" for w in block["warnings"]]
    if block.get("hard_failures"):
        lines += ["", "## Hard failures (a run must not start)", ""]
        lines += [f"- **{cid}**: {(checks.get(cid) or {}).get('detail', '')}"
                  for cid in block["hard_failures"]]
    lines.append("")
    return "\n".join(lines)


def _artifact_path(state_path: Path, suffix: str) -> Path:
    """Sibling artifact of the state file: <state>.json -> <state><suffix>."""
    return state_path.parent / (state_path.stem + suffix)


def _skipped_record(cid: str, hard: bool) -> dict:
    return {"id": cid, "hard": hard, "ok": None, "skipped": True,
            "detail": f"skipped via --skip {cid.replace('_', '-')}: "
                      + ("hard prerequisite NOT proven (operator choice)"
                         if hard else "not checked")}


def _unprovable_record(cid: str, hard: bool, why: str) -> dict:
    """A check that could not run at all (e.g. no target root). For a hard
    check that is a failure, never a pass — 'unproven' is not 'green'; a
    soft check just records why it did not run."""
    return {"id": cid, "hard": hard, "ok": False,
            "detail": f"not run: {why}"}


def cmd_preflight(args) -> int:
    policy = load_policy(args.policy)
    path = Path(args.state)
    state = _load_state(path)
    subs = load_substitutions(args.substitutions)
    today = date.today()
    skipped = {SKIP_TO_CHECK[s] for s in (args.skip or [])}
    warnings: list[str] = []

    # everything below depends on the target root + its venv interpreter
    root, root_note = _resolve_target_root(args.target_root, state)
    if root is None:
        warnings.append(f"target root unresolved — {root_note}")
    venv_python, venv_note = _resolve_target_python(root)
    python = str(venv_python) if venv_python else sys.executable
    if root is not None and venv_python is None:
        warnings.append(f"{venv_note} — suite/self-test fall back to "
                        f"{sys.executable}; if the suite needs the venv's "
                        f"dependencies this fails loudly with the output tail")

    def run_check(cid, hard, run, *, needs_root=False):
        """One code path for every check: --skip records an explicit skip
        (a warning, never silent); a check needing the target when none was
        resolved is unprovable (a failure when hard); else run it."""
        if cid in skipped:
            return _skipped_record(cid, hard)
        if needs_root and root is None:
            return _unprovable_record(cid, hard,
                                      f"target root unresolved — {root_note}")
        return run()

    checks = {
        "target_interpreter": _check_target_interpreter(root, venv_python,
                                                        venv_note),
        "template": run_check("template", True,
                              lambda: _check_template(args.template_url)),
        "oracle_selftest": run_check("oracle_selftest", True,
                                     lambda: _check_selftest(python)),
        "pytest_collect": run_check(
            "pytest_collect", False,
            lambda: _check_pytest_collect(
                python, root, _artifact_path(path, ".pytest-collect.txt")),
            needs_root=True),
        "suite": run_check("suite", True,
                           lambda: _check_suite(python, root), needs_root=True),
        "toolchain": run_check("toolchain", False, _check_toolchain),
    }
    if venv_python is None:
        warnings.append(f"target interpreter unresolved: {venv_note}")

    if checks["toolchain"].get("missing"):
        warnings.append("toolchain missing: "
                        + ", ".join(checks["toolchain"]["missing"])
                        + " — Stage 3 gates (just check / deny / llvm-cov / "
                          "shear / the pandoc Markdown gate) need them")
    checks["substitutions_freshness"] = _substitutions_record(subs, today)
    if not checks["substitutions_freshness"]["ok"]:
        warnings.append(checks["substitutions_freshness"]["detail"])

    framework_python = python_version_string()
    target_version = checks["target_interpreter"].get("version")
    mismatch = (_interpreter_mismatch(framework_python, target_version)
                if target_version else None)
    checks["interpreter_match"] = {
        "id": "interpreter_match",
        "ok": mismatch is None,
        "framework_python": framework_python,
        "target_python": target_version,
        "parse_tools_must_use_target": mismatch is not None,
        "detail": mismatch or (f"framework {framework_python} >= target "
                               f"{target_version or 'n/a'} — the framework "
                               f"interpreter may parse the target source"),
    }
    if mismatch:
        warnings.append(mismatch)

    # a hard check that failed or could not run (ok is not True) is a hard
    # failure (exit 2); one skipped via --skip is only a warning
    hard_failures = [cid for cid in HARD_CHECKS
                     if not checks[cid].get("skipped")
                     and checks[cid].get("ok") is not True]
    warnings.extend(f"hard prerequisite '{cid}' skipped by --skip — "
                    f"NOT proven this run"
                    for cid in HARD_CHECKS if checks[cid].get("skipped"))

    # machine-facing summary Stage 3/4 pin from; the venv interpreter IS the
    # derived PYO3_PYTHON, and root is recorded so re-runs need no flag
    target_check = checks["target_interpreter"]
    target_summary = {"root": target_check.get("root")}
    for key in ("python", "version", "gil_disabled", "wheel_flavor",
                "pyo3_python"):
        target_summary[key] = target_check.get(key)

    report_path = _artifact_path(path, ".preflight.md")
    block = {
        "timestamp": now_iso(),
        "ok": not hard_failures,
        "policy_version": policy.get("version"),
        "hard_failures": hard_failures,
        "warnings": warnings,
        "framework_python": framework_python,
        "target": target_summary,
        "checks": checks,
        "report_path": str(report_path),
    }
    old = state.get("preflight")
    state["preflight"] = block
    try:
        state_tool.save_validated(state, path, actor=TOOL,
                                  changes=[(None, "preflight", old, block)])
    except ValueError as e:
        return _emit_refusal(f"preflight block rejected — resulting state "
                             f"would be invalid: {e}", args.human,
                             file=str(path), rolled_back=True)
    report_path.write_text(
        build_preflight_report(state.get("project", {}).get("name", "?"), block),
        encoding="utf-8")

    code = EXIT_FINDINGS if hard_failures else EXIT_OK
    human = (f"preflight {'OK' if not hard_failures else 'FAILED'}: "
             f"{len(checks)} check(s), {len(hard_failures)} hard failure(s), "
             f"{len(warnings)} warning(s) -> {path} + {report_path}")
    emit({"tool": TOOL, "version": VERSION, "file": str(path),
          "ok": not hard_failures, "hard_failures": hard_failures,
          "warnings": warnings, "checks": checks,
          "report_path": str(report_path),
          "summary": {"exit_code": code, "human": human}},
         human_flag=args.human)
    return code


# ----------------------------------------------------------------- report --

def tokens_spent(state: dict) -> int:
    """Run-level budget usage: the ledger plus every unit's spend.

    Public (no underscore): succession.py composes it to enforce the
    run-level ceilings across a whole session chain — the sum over
    sessions is inherent because every session writes the one state
    file (issue #25)."""
    ledger = (state.get("budget_ledger") or {}).get("tokens_total") or 0
    per_unit = sum((u.get("budget") or {}).get("tokens_spent", 0) or 0
                   for u in state.get("units", []))
    return ledger + per_unit


def _chain_report_lines(chain: dict, tokens: int, ceiling: int | None) -> list[str]:
    """The Session chain section of the run report (issue #25): depth,
    wall clock and budget summed over every session in the chain, one
    line per session and notice. Empty when the run never chained."""
    if not chain.get("sessions"):
        return []
    lines = ["", "## Session chain (issue #25)", "",
             f"- depth: {chain.get('depth', 0)} of max "
             f"{chain.get('max_depth', '—')} successions · wall clock "
             f"across the chain: {chain.get('wall_clock_minutes', 0)} min",
             f"- budget across the chain: {tokens} tokens recorded"
             + (f" / ceiling {ceiling}" if ceiling else "")
             + " (sum over every session — successors get a fresh "
               "session budget, never a fresh run budget)"]
    for s in chain["sessions"]:
        lines.append(f"- s{s.get('index')} **{s.get('name')}** "
                     f"({s.get('id')}): {s.get('outcome') or 'in flight'}"
                     + (f" — {s.get('succession_reason')}"
                        if s.get("succession_reason") else ""))
    lines += [f"- notice: {n}" for n in chain.get("notices") or []]
    return lines


def build_report(state: dict, policy: dict) -> str:
    level = state_tool.autonomy_level(state)
    tokens = tokens_spent(state)
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
    lines += _chain_report_lines(state.get("session_chain") or {},
                                 tokens, ceiling)
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
            f"autonomy={level}: the deterministic driver runs L2+ only. At "
            f"L0/L1 the agent-driven recipe never asks either — it leaves the "
            f"gate open and ends the run with the dossier for the operator "
            f"(decided between runs). Pre-authorize autonomy BEFORE the run: "
            f"--autonomy L2|L3 here, or state.py init --autonomy-level "
            f"(default L2).", args.human, file=str(path))
    run_status = state.get("run_status")
    if run_status in ("complete", "no_go_measured"):
        return _emit_refusal(
            f"run_status={run_status} is a finished run — start a fresh run "
            f"(state.py init) to migrate again; resume is only for "
            f"budget_exhausted/failed_safely after the operator raises the "
            f"ceiling between runs (tools/succession.py chains successor "
            f"sessions automatically and bootstraps GLV itself — issues "
            f"#25/#47)",
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
            tokens = tokens_spent(state)
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
             + f"; report: {rp}"
             + ("; the run-level budget is spent: session-level exhaustion "
                "never appears as a run result (tools/succession.py chains "
                "GLV successor sessions automatically and bootstraps GLV "
                "itself, issues #25/#47); raising autonomy.budget is an "
                "operator decision between runs, never a mid-run ask"
                if run_status == "budget_exhausted" else ""))
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
            f"autonomy={level}: gates resolve by policy at L2+ only. At L0/L1 "
            f"the operator decides between runs — record that decision with "
            f"`state.py resolve-gate --by human` outside the run, or "
            f"pre-authorize L2+ (nothing asks mid-run; --human-override forces "
            f"evaluation here; the decision is still journaled policy:<rule>). "
            f"For a fresh run, `state.py init --autonomy-level L2` is the "
            f"documented default; a hand-written state file without an "
            f"autonomy block reads as L0", args.human, file=str(path))
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
    tokens = tokens_spent(state)
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

    p = sub.add_parser("preflight",
                       help="run + record everything stages 0/3 assumed "
                            "(issue #31): suite green, template SHA, "
                            "toolchains, target interpreter, oracle self-test")
    p.add_argument("state")
    p.add_argument("--target-root",
                   help="the repo being migrated (absolute or relative); "
                        "recorded into the preflight block, so later re-runs "
                        "omit the flag")
    p.add_argument("--template-url", default=TEMPLATE_URL,
                   help="rust-template provenance source (a local checkout "
                        "path works offline; default: the GitHub URL from "
                        "stages/3-scaffold.md)")
    p.add_argument("--policy", help="override autonomy/policy.json")
    p.add_argument("--substitutions",
                   help="override knowledge/substitutions.json")
    p.add_argument("--skip", action="append", default=[],
                   choices=sorted(SKIP_TO_CHECK),
                   help="skip a check (repeatable); skipping a hard "
                        "prerequisite downgrades it to an explicit warning — "
                        "the offline corner, never silent")
    p.set_defaults(func=cmd_preflight)

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
