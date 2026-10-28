#!/usr/bin/env python3
"""state.py — dual-build state manager (framework/tools/README.md #4).

Stages 1–7. Owns ``migration-state.json``: it is the ONLY writer (agents
report; the orchestrator records through this tool), validates it against
``../state/migration-state.schema.json``, and journals **every** transition
into the state file's own ``journal[]`` — "every state transition journaled"
is acceptance criterion #4.

Subcommands
-----------
  validate   schema + coherence invariants (duplicates, dangling deps, order,
             parity/status coherence, out-of-band edit detection)
  init       create a minimal valid state file
  set        change fields via dotted paths — ``stage``,
             ``units[u-parse-1].status``, ``human_gates[gate=unit_escalation].state``
             (the exact field vocabulary the journal already uses); a leading
             ``@json:`` types one field as strict JSON so plain and JSON
             fields mix in a single invocation (issue #35); the prefix also
             works directly on the value (``path=@json:[...]``) and never
             silently degrades to a string (issue #52); ``human_gates`` may
             only be set wholesale while empty — after that gate history
             only grows through resolve-gate/mark (issue #52); every
             changed field appends one journal entry; atomic write; invalid
             results roll back
  mark       status transitions with the MIGRATION_PROMPT.md discipline
             encoded as hard guards (ported requires an equivalent verdict;
             escalated auto-opens the unit_escalation human gate — and at
             autonomy L2+ immediately resolves it as deferred via policy
             rule ESC-1: defer, not halt, the Python fallback stays live;
             divergent_explained requires a written explanation)
  resolve-gate  record a gate decision — decided_by 'human' or
             'policy:<rule-id>' + policy_version + evidence[] — journaled,
             validated, atomic; the policy evaluation itself lives in
             pipeline.py resolve-gates
  shim       emit the per-unit fallback switch (Python stays callable per
             unit — reversible via one env var, no extra state anywhere)
  migrate    bring an older/partial state up to the current schema shape
  journal    print the journal (read-only), optionally filtered
  show       project a unit or a summary

Exit codes: 0 ok (warnings possible) · 2 validation/transition errors ·
1 usage/IO. Anti-requirement honored: no state outside migration-state.json.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    EXIT_FINDINGS, EXIT_OK, ToolError, emit, module_name_of,
    now_iso, run_cli, write_json_atomic,
)

TOOL = "state.py"
VERSION = 1
JSON_FIELD_PREFIX = "@json:"
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "state" / "migration-state.schema.json"
POLICY_PATH = Path(__file__).resolve().parent.parent / "autonomy" / "policy.json"
STATUSES = ["pending", "ported", "divergent_explained", "escalated", "fallback_python", "retired"]
# statuses whose live side is the Rust implementation (fallback still on call
# for everything else; "retired" means the fallback was deliberately removed
# in Stage 7, so rust is the only side left)
RUST_LIVE_STATUSES = {"ported", "divergent_explained", "retired"}
GATES = ["go_no_go", "scope_change", "redesign_approval", "unit_escalation",
         "cutover", "crate_substitution", "no_equivalent_dependency"]
# autonomy levels that resolve gates by policy (autonomy/policy.json)
# instead of stopping for a human (issue #22): L2 = all gates policy-resolved,
# publish disabled; L3 = full autopilot incl. cutover
POLICY_LEVELS = {"L0", "L1", "L2", "L3"}
DRIVER_LEVELS = {"L2", "L3"}
TERMINAL_RUN_STATUSES = {"complete", "no_go_measured", "budget_exhausted", "failed_safely"}
_POLICY_DECIDED_BY_RE = re.compile(r"^policy:[A-Za-z][A-Za-z0-9]*-\d+$")


def load_policy(path: Path | None = None) -> dict | None:
    """Best-effort load of autonomy/policy.json (structural check lives in
    pipeline.py; here we only need version + rule ids). Returns None when the
    file is missing/unreadable — older checkouts run without a policy file."""
    try:
        policy = json.loads((path or POLICY_PATH).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return policy if isinstance(policy, dict) else None


def autonomy_level(state: dict) -> str:
    """Effective autonomy level; absent block = L0 (supervised, the
    pre-policy behavior — an old state file keeps its meaning)."""
    level = (state.get("autonomy") or {}).get("level")
    return level if level in POLICY_LEVELS else "L0"


def publish_preauthorized(state: dict) -> bool:
    """Cutover/publish is pre-authorized at L3 only, with the explicit flag."""
    auto = state.get("autonomy") or {}
    return auto.get("level") == "L3" and auto.get("publish_allowed") is True


def find_gate(state: dict, name: str) -> dict | None:
    """The newest open gate named `name`, else the newest gate of that name."""
    gate = None
    for g in reversed(state.get("human_gates", [])):
        if g.get("gate") == name:
            gate = g
            if g.get("state") == "open":
                break
    return gate


def merge_evidence(existing: list | None, added: list | None) -> list:
    """Union two evidence lists, first occurrence first."""
    merged = list(existing or [])
    merged.extend(e for e in added or [] if e not in merged)
    return merged


def record_gate_fields(gate: dict, updates: dict, changes: list) -> None:
    """Journal + apply field updates on one gate record — the shared write
    path for resolve-gate (humans/CLI) and the driver (pipeline.py)."""
    prefix = f"human_gates[gate={gate.get('gate')}]"
    for field, new in updates.items():
        old = gate.get(field)
        if old != new:
            changes.append((None, f"{prefix}.{field}", old, new))
            gate[field] = new

# --------------------------------------------------------------- schema ----
# A stdlib-only validator for the draft-2020-12 subset the schema uses:
# $schema/$comment/title ignored · type (incl. unions) · required ·
# properties · additionalProperties · items · enum · minimum/maximum ·
# format date-time.
# tests/test_state.py cross-checks it against the jsonschema package when
# that is installed, so the two never drift silently.

_DATE_TIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$"
)


def _check_datetime(value) -> bool:
    return bool(_DATE_TIME_RE.match(str(value)))


def _type_ok(value, type_names: list[str]) -> bool:
    for t in type_names:
        if t == "object" and isinstance(value, dict):
            return True
        if t == "array" and isinstance(value, list):
            return True
        if t == "string" and isinstance(value, str):
            return True
        if t == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if t == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if t == "boolean" and isinstance(value, bool):
            return True
        if t == "null" and value is None:
            return True
    return False


def validate_schema(instance, schema: dict, path: str = "$", errors: list | None = None) -> list[str]:
    """Validate `instance` against the (subset of the) schema. Returns a list
    of 'path: message' strings."""
    if errors is None:
        errors = []
    if "$ref" in schema or "allOf" in schema or "oneOf" in schema or "anyOf" in schema:
        errors.append(f"{path}: schema uses a keyword this validator does not implement")
        return errors
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in enum {schema['enum']!r}")
    types = schema.get("type")
    if types:
        names = types if isinstance(types, list) else [types]
        if not _type_ok(instance, names):
            errors.append(f"{path}: expected type {types}, got {type(instance).__name__}")
            return errors
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required property {key!r}")
        properties = schema.get("properties", {})
        for key, sub in properties.items():
            if key in instance:
                validate_schema(instance[key], sub, f"{path}.{key}", errors)
        addl = schema.get("additionalProperties")
        if addl is not None:
            for key, value in instance.items():
                if key in properties:
                    continue
                if addl is False:
                    errors.append(f"{path}: additional property {key!r} is not allowed")
                elif isinstance(addl, dict):
                    validate_schema(value, addl, f"{path}.{key}", errors)
    elif isinstance(instance, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(instance):
                validate_schema(item, items, f"{path}[{i}]", errors)
    if schema.get("format") == "date-time" and isinstance(instance, str) and not _check_datetime(instance):
        errors.append(f"{path}: {instance!r} is not an ISO-8601 date-time with timezone")
    if "minimum" in schema and isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if instance < schema["minimum"]:
            errors.append(f"{path}: {instance} < minimum {schema['minimum']}")
    if "maximum" in schema and isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if instance > schema["maximum"]:
            errors.append(f"{path}: {instance} > maximum {schema['maximum']}")
    return errors


# ----------------------------------------------------------- invariants ----

def _gate_discipline_errors(state: dict, policy: dict | None) -> list[str]:
    """A resolved gate records WHO decided — 'human' or 'policy:<rule-id>' —
    plus the policy version it evaluated; a policy-approved cutover is
    publish, which is pre-authorized at L3 only."""
    errors: list[str] = []
    rule_ids = ({r.get("id") for r in policy.get("rules", [])}
                if policy else None)
    auto = state.get("autonomy") or {}
    if auto.get("publish_allowed") and not publish_preauthorized(state):
        errors.append("autonomy: publish_allowed=true requires level=L3 "
                      "(publish is an L3-only pre-authorization)")
    for i, g in enumerate(state.get("human_gates", [])):
        label = f"human_gates[{i}] ({g.get('gate')})"
        if g.get("state") == "open":
            continue
        decided_by = g.get("decided_by")
        if not decided_by:
            errors.append(f"{label}: state={g.get('state')!r} requires decided_by "
                          f"('human' or 'policy:<rule-id>')")
            continue
        if decided_by != "human":
            if not _POLICY_DECIDED_BY_RE.match(str(decided_by)):
                errors.append(f"{label}: decided_by={decided_by!r} must be 'human' "
                              f"or 'policy:<rule-id>'")
                continue
            if not g.get("policy_version"):
                errors.append(f"{label}: decided_by={decided_by} requires "
                              f"policy_version")
            rid = str(decided_by).split(":", 1)[1]
            if rule_ids is not None and rid not in rule_ids:
                errors.append(f"{label}: decided_by names rule {rid!r} which "
                              f"autonomy/policy.json does not define")
        if (g.get("gate") == "cutover" and g.get("state") == "approved"
                and str(decided_by).startswith("policy:")
                and not publish_preauthorized(state)):
            errors.append(
                f"{label}: policy-approved cutover requires autonomy.level=L3 "
                f"and autonomy.publish_allowed=true (got level={auto.get('level')!r}, "
                f"publish_allowed={auto.get('publish_allowed')!r})")
    return errors


def _resumed_after_terminal_warning(state: dict) -> str | None:
    """Terminal-state discipline: once run_status reaches a terminal value,
    newer journaled transitions mean the run resumed after finishing (fine
    for budget_exhausted with a raised ceiling; verify intent).

    Issue #25: a run wrapped in a successor-session chain resumes BY
    DESIGN — session-level budget_exhausted spawns the next session
    automatically (tools/succession.py) — so a recorded succession
    suppresses the warning. Out-of-band edit detection (unit status vs.
    journal) still applies."""
    chain_sessions = (state.get("session_chain") or {}).get("sessions") or []
    if any(isinstance(s, dict) and s.get("succession_reason")
           for s in chain_sessions):
        return None
    if state.get("run_status") not in TERMINAL_RUN_STATUSES:
        return None
    entries = state.get("journal", [])
    for idx, entry in enumerate(entries):
        if (entry.get("field") == "run_status"
                and entry.get("to") in TERMINAL_RUN_STATUSES):
            later = entries[idx + 1:]
            if not later:
                return None
            return (f"run_status={state['run_status']} is terminal but "
                    f"{len(later)} newer journal entr"
                    f"{'y exists' if len(later) == 1 else 'ies exist'} — the run "
                    f"resumed after finishing (fine for budget_exhausted with a "
                    f"raised ceiling; verify intent)")
    return None


def _is_int(value) -> bool:
    """JSON booleans are ints in Python — exclude them."""
    return isinstance(value, int) and not isinstance(value, bool)


def _session_entry_errors(sessions: list) -> list[str]:
    errors: list[str] = []
    for i, s in enumerate(sessions):
        if not isinstance(s, dict):
            errors.append(f"session_chain.sessions[{i}]: must be an object")
            continue
        for key in ("index", "id", "name"):
            if not s.get(key):
                errors.append(f"session_chain.sessions[{i}]: {key} required")
        if s.get("index") != i + 1:
            errors.append(
                f"session_chain.sessions[{i}]: index {s.get('index')!r} must "
                f"be {i + 1} (sessions are contiguous from 1, oldest first)")
    return errors


def _session_chain_errors(state: dict) -> list[str]:
    """Coherence of the successor-session chain record (issue #25):
    sessions are contiguous from index 1, depth counts the successions
    (sessions - 1) and never exceeds the cap. The chain harness
    (succession.py) writes this block through save_validated, so every
    intermediate shape must satisfy these too."""
    chain = state.get("session_chain")
    if chain is None:
        return []
    if not isinstance(chain, dict):
        return ["session_chain: must be an object"]
    sessions = chain.get("sessions")
    if sessions is None:
        sessions = []
    if not isinstance(sessions, list):
        return ["session_chain.sessions: must be an array"]
    errors = _session_entry_errors(sessions)
    depth = chain.get("depth")
    if not _is_int(depth):
        errors.append("session_chain.depth: integer required")
    elif depth != max(0, len(sessions) - 1):
        errors.append(
            f"session_chain.depth: {depth} but {len(sessions)} session(s) "
            f"recorded — depth must equal sessions - 1 (one per succession)")
    max_depth = chain.get("max_depth")
    if not _is_int(max_depth) or max_depth < 1:
        errors.append("session_chain.max_depth: integer >= 1 required")
    elif _is_int(depth) and depth > max_depth:
        errors.append(f"session_chain.depth: {depth} > max_depth {max_depth}")
    return errors


def check_invariants(state: dict) -> tuple[list[str], list[str]]:
    """Coherence checks the JSON schema cannot express. Returns (errors,
    warnings) — warnings are reported, never silent, but do not fail."""
    errors, warnings = [], []
    units = state.get("units", [])
    by_id: dict[str, dict] = {}
    for u in units:
        uid = u.get("id")
        if uid in by_id:
            errors.append(f"units: duplicate unit id {uid!r}")
        by_id[uid] = u

    for u in units:
        uid = u.get("id")
        for dep in u.get("deps", []):
            if dep not in by_id:
                errors.append(f"units[{uid}].deps: unknown unit id {dep!r}")

    # dependency cycles: DFS
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {uid: WHITE for uid in by_id}
    cycle_found = []

    def visit(uid: str, stack: list[str]) -> None:
        color[uid] = GRAY
        stack.append(uid)
        for dep in by_id[uid].get("deps", []):
            if dep not in by_id:
                continue
            if color[dep] == GRAY:
                cycle = stack[stack.index(dep):] + [dep]
                cycle_found.append(" -> ".join(cycle))
            elif color[dep] == WHITE:
                visit(dep, stack)
        stack.pop()
        color[uid] = BLACK

    for uid in by_id:
        if color[uid] == WHITE:
            visit(uid, [])
    for c in sorted(set(cycle_found)):
        errors.append(f"units: dependency cycle {c}")
    if cycle_found:
        return errors, warnings  # ordering checks are meaningless with cycles

    for u in units:
        uid = u.get("id")
        order = u.get("order")
        if isinstance(order, int):
            for dep in u.get("deps", []):
                if dep not in by_id:
                    continue  # already reported as an unknown-dep error above
                dep_order = by_id[dep].get("order")
                if isinstance(dep_order, int) and dep_order >= order:
                    errors.append(
                        f"units[{uid}].order: {order} does not come after dependency "
                        f"{dep} (order {dep_order}) — leaf-first violated"
                    )
        parity = u.get("parity") or {}
        status = u.get("status")
        if parity.get("last_verdict") == "divergent" and not parity.get("divergence_note"):
            errors.append(
                f"units[{uid}]: parity.last_verdict=divergent requires "
                f"parity.divergence_note (every divergence carries a written explanation)"
            )
        if status == "ported" and parity.get("last_verdict") not in ("equivalent", None):
            warnings.append(
                f"units[{uid}]: status=ported but parity.last_verdict="
                f"{parity.get('last_verdict')!r} — only an equivalent verdict gates ported"
            )
        if status == "escalated":
            gates = [g for g in state.get("human_gates", [])
                     if g.get("gate") == "unit_escalation"
                     and g.get("state") in ("open", "deferred")]
            if not gates:
                warnings.append(
                    f"units[{uid}]: status=escalated but no unit_escalation gate "
                    f"(open for humans, or deferred via policy:ESC-1 at L2+)"
                )

    errors += _gate_discipline_errors(state, load_policy())
    errors += _session_chain_errors(state)

    resumed = _resumed_after_terminal_warning(state)
    if resumed:
        warnings.append(resumed)

    # out-of-band edit detection: the last journaled status transition for a
    # unit must agree with the unit's current status
    for u in units:
        uid = u.get("id")
        last = None
        for entry in state.get("journal", []):
            if entry.get("field") == f"units[{uid}].status":
                last = entry
        if last is not None and last.get("to") != u.get("status"):
            warnings.append(
                f"units[{uid}].status: current value {u.get('status')!r} disagrees with the "
                f"last journal entry ({last.get('to')!r} at {last.get('timestamp')}) — "
                f"state changed outside state.py"
            )
    return errors, warnings


# ------------------------------------------------------------ field paths --
# Same vocabulary the journal[] `field` column uses, e.g.
#   stage
#   units[u-parse-1].status
#   units[u-parse-1].parity.runs
#   human_gates[gate=unit_escalation].state

_FIELD_RE = re.compile(r"^([A-Za-z_][\w-]*)(?:\[(.+?)\])?$")


def _split_path(path: str) -> list[str]:
    return [seg for seg in path.split(".") if seg]


def _resolve_container(root: dict, segments: list[str], create: bool):
    """Walk all but the last segment; returns (container, last_key) where
    container is a dict or a list."""
    node = root
    for seg in segments[:-1]:
        node = _descend(node, seg, create)
        if node is None:
            raise KeyError(seg)
    last = segments[-1]
    m = _FIELD_RE.match(last)
    if not m:
        raise ValueError(f"malformed path segment {last!r}")
    key, index = m.group(1), m.group(2)
    return node, key, index


def _descend(node, seg: str, create: bool):
    m = _FIELD_RE.match(seg)
    if not m:
        raise ValueError(f"malformed path segment {seg!r}")
    key, index = m.group(1), m.group(2)
    if isinstance(node, dict):
        if key not in node:
            if not create:
                raise KeyError(seg)
            node[key] = [] if index else {}
        child = node[key]
    else:
        raise TypeError(f"cannot descend into {type(node).__name__} at {seg!r}")
    if index is None:
        return child
    if not isinstance(child, list):
        raise TypeError(f"{seg}: indexing a non-list")
    item = _list_select(child, index)
    if item is None:
        if not create:
            raise KeyError(seg)
        item = {}
        child.append(item)  # append semantics for units[] selectors
    return item


def _list_select(lst: list, index: str):
    if index.startswith("gate="):
        gate = index[5:]
        for item in lst:
            if isinstance(item, dict) and item.get("gate") == gate:
                return item
        return None
    if index.isdigit():
        i = int(index)
        return lst[i] if 0 <= i < len(lst) else None
    # unit id selector
    for item in lst:
        if isinstance(item, dict) and item.get("id") == index:
            return item
    return None


def get_field(state: dict, path: str):
    node, key, index = _resolve_container(state, _split_path(path), create=False)
    if index is not None:
        if not isinstance(node, dict):
            raise TypeError(f"{path}: segment mismatch")
        child = node.get(key)
        if not isinstance(child, list):
            return None
        item = _list_select(child, index)
        return item
    return node.get(key) if isinstance(node, dict) else None


def set_field(state: dict, path: str, value) -> bool:
    """Set one dotted field in-place. Returns True if the value changed.
    `journal` is append-only and rejected here, and so is a non-empty
    `human_gates` — gate history grows through resolve-gate/mark, an empty
    array may still be seeded wholesale (issue #52). Indexed segments
    (units[u-1], human_gates[gate=g]) select; the leaf key must be
    addressed one level deeper, e.g. units[u-1].status."""
    if _split_path(path)[0] == "journal":
        raise ValueError("journal[] is append-only — transitions go through set/mark")
    if path == "human_gates" and state.get("human_gates"):
        raise ValueError(
            "human_gates is append-only once it has entries — replacing the "
            "array would clobber gate history (issue #52); open/resolve "
            "gates through resolve-gate --create / mark / pipeline "
            "resolve-gates, or edit one record's fields via "
            "human_gates[gate=<name>].<leaf>")
    node, key, index = _resolve_container(state, _split_path(path), create=True)
    if index is not None:
        raise ValueError(
            f"{path}: indexed selectors address a leaf one level deeper "
            f"(e.g. units[u-1].status); replacing whole list items is not supported"
        )
    if not isinstance(node, dict):
        raise TypeError(f"{path}: cannot set into {type(node).__name__}")
    old = node.get(key)
    if old == value:
        return False
    node[key] = value
    return True


# ------------------------------------------------------------ journaling ---

def _journal_summary(value) -> str:
    """Compact, JSON-safe stand-in for large values in journal from/to."""
    if isinstance(value, list):
        return f"<list:{len(value)} items>"
    if isinstance(value, dict):
        return f"<object:{len(value)} keys>"
    return value


def append_transitions(state: dict, changes: list[tuple], actor: str) -> None:
    """changes: list of (unit_id, field_path, old, new). One journal entry per
    changed field, per tools/README.md #4."""
    ts = now_iso()
    for unit, field, old, new in changes:
        state.setdefault("journal", []).append({
            "timestamp": ts,
            "unit": unit,
            "field": field,
            "from": old if old is None or _is_small(old) else _journal_summary(old),
            "to": new if new is None or _is_small(new) else _journal_summary(new),
            "actor": actor,
        })


def _is_small(value) -> bool:
    try:
        return len(json.dumps(value)) <= 200
    except (TypeError, ValueError):
        return False


def _unit_id_of(field: str):
    m = re.match(r"^units\[([^]]+)\]", field)
    return m.group(1) if m else None


def save_validated(state: dict, path: Path, actor: str, changes: list[tuple]) -> None:
    """Validate, then atomically write. On validation errors the in-memory
    state is rolled back (the file never sees it) and an error raised."""
    schema = json.loads(SCHEMA_PATH.read_text())
    errors = validate_schema(state, schema)
    inv_errors, _ = check_invariants(state)
    if errors or inv_errors:
        raise ValueError("resulting state would be invalid:\n  " +
                         "\n  ".join(errors + inv_errors))
    append_transitions(state, changes, actor)
    write_json_atomic(path, state)


# ------------------------------------------------------------- commands ----

def _load(path: Path) -> dict:
    state = json.loads(path.read_text())
    if not isinstance(state, dict):
        raise ValueError(f"{path}: state file must contain a JSON object")
    return state


def _find_unit(state: dict, uid: str) -> dict | None:
    return next((u for u in state.get("units", []) if u.get("id") == uid), None)


def _emit_validation(state: dict, label: str, human_flag: bool) -> int:
    schema = json.loads(SCHEMA_PATH.read_text())
    schema_errors = validate_schema(state, schema)
    inv_errors, warnings = check_invariants(state)
    errors = schema_errors + inv_errors
    code = EXIT_FINDINGS if errors else EXIT_OK
    human = (f"{label}: " + ("VALID" if not errors else f"INVALID ({len(errors)} errors)")
             + (f", {len(warnings)} warning(s)" if warnings else ""))
    emit({
        "tool": TOOL, "version": VERSION, "target": label,
        "valid": not errors, "schema_errors": schema_errors,
        "invariant_errors": inv_errors, "warnings": warnings,
        "summary": {"exit_code": code, "human": human},
    }, human_flag=human_flag)
    return code


def cmd_validate(args) -> int:
    state = _load(Path(args.file))
    return _emit_validation(state, args.file, args.human)


def autonomy_block(level: str) -> dict:
    """The pre-authorized autonomy block (issue #22) for `level`, filled from
    autonomy/policy.json defaults: the policy version in force, the run-level
    budget ceilings, and the level's publish flag. L0 keeps the supervised
    meaning of pre-policy state files."""
    policy = load_policy() or {}
    block = {"level": level if level in POLICY_LEVELS else "L0"}
    if policy.get("version"):
        block["policy_version"] = policy["version"]
    budget = policy.get("budget") or {}
    block["budget"] = {
        "max_tokens": budget.get("max_tokens", 5_000_000),
        "max_wall_clock_minutes": budget.get("max_wall_clock_minutes", 720),
        "max_chain_depth": budget.get("max_chain_depth", 10),
    }
    block["publish_allowed"] = bool(
        (policy.get("levels") or {}).get(block["level"], {}).get("publish_allowed"))
    return block


def cmd_init(args) -> int:
    path = Path(args.file)
    if path.exists() and not args.force:
        raise ToolError(f"{path} already exists (use --force to overwrite)")
    state = {
        "project": {"name": args.name, "python_path": args.python_path},
        "stage": args.stage,
        "units": [],
        "human_gates": [],
        "journal": [],
        "budget_ledger": {"tokens_total": 0, "hours_human_total": 0.0,
                          "regression_triage_hours": 0.0},
        "autonomy": autonomy_block(args.autonomy_level),
        "run_status": "pending",
    }
    schema = json.loads(SCHEMA_PATH.read_text())
    errs = validate_schema(state, schema)
    assert not errs, errs
    append_transitions(state, [(None, "project", None, {"name": args.name,
                                                        "python_path": args.python_path}),
                               (None, "stage", None, args.stage),
                               (None, "autonomy", None, state["autonomy"]),
                               (None, "run_status", None, "pending")], actor="state.py")
    write_json_atomic(path, state)
    human = (f"initialized {path} (project={args.name}, stage={args.stage}, "
             f"autonomy={args.autonomy_level})")
    emit({"tool": TOOL, "version": VERSION, "file": str(path),
          "summary": {"exit_code": EXIT_OK, "human": human}}, human_flag=args.human)
    return EXIT_OK


def _refusal(reason: str, human_flag: bool, **extra) -> int:
    """Errors are machine-readable too: JSON out, non-zero exit."""
    payload = {"tool": TOOL, "version": VERSION, "error": reason,
               "summary": {"exit_code": EXIT_FINDINGS, "human": reason}}
    payload.update(extra)
    emit(payload, human_flag=human_flag)
    return EXIT_FINDINGS


def _split_pair(spec: str) -> tuple[str, str] | None:
    """Split 'path=value' on the first '=' outside [...] selectors, so gate
    selectors survive: human_gates[gate=go_no_go].state=approved. Returns
    None when the pair has no top-level '=' at all."""
    depth = 0
    for i, ch in enumerate(spec):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif ch == "=" and depth == 0:
            return spec[:i], spec[i + 1:]
    return None


def _parse_pair(pair: str, strict_all: bool) -> tuple[str | None, object, str | None]:
    """Decode one --field pair into (field, value, refusal); refusal is
    None when the pair decoded fine.

    '@json:' at the front of the pair types its value as strict JSON
    (issue #35); on the value itself it is honored too — unambiguous
    intent must not silently degrade to a string that later fails schema
    validation with a confusing type error (issue #52). Plain pairs
    decode JSON leniently (numbers, true/false, null, ...) and fall back
    to the raw string unless strict_all (--json-value) makes the whole
    invocation strict."""
    strict = pair.startswith(JSON_FIELD_PREFIX)
    spec = pair.removeprefix(JSON_FIELD_PREFIX)
    split = _split_pair(spec)
    if split is None:
        return None, None, f"--field expects path=value, got {pair!r}"
    field, raw = split
    field = field.strip()
    value_prefix = not strict and raw.startswith(JSON_FIELD_PREFIX)
    if value_prefix:
        raw = raw.removeprefix(JSON_FIELD_PREFIX)
    try:
        return field, json.loads(raw), None
    except json.JSONDecodeError as e:
        if not (strict or value_prefix or strict_all):
            return field, raw, None
        hint = (" — the '@json:' prefix types one value as strict "
                "JSON; put it at the front of the pair "
                "('@json:path=value') or quote the payload"
                if value_prefix else "")
        return field, None, f"--field {field}: value is not valid JSON: {e}{hint}"


def cmd_set(args) -> int:
    path = Path(args.file)
    state = _load(path)
    changes = []
    for pair in args.field:
        field, value, refusal = _parse_pair(pair, args.json_value)
        if refusal:
            return _refusal(refusal, args.human)
        try:
            old = get_field(state, field)
        except (KeyError, TypeError, ValueError):
            old = None  # nested parent created by this very set
        try:
            changed = set_field(state, field, value)
        except (KeyError, ValueError, TypeError) as e:
            return _refusal(f"{field}: {e}", args.human, file=str(path))
        if changed:
            changes.append((_unit_id_of(field), field, old, value))
    if not changes:
        human = f"no changes ({path} untouched)"
        emit({"tool": TOOL, "version": VERSION, "file": str(path), "changes": [],
              "summary": {"exit_code": EXIT_OK, "human": human}}, human_flag=args.human)
        return EXIT_OK
    try:
        save_validated(state, path, actor=args.actor, changes=changes)
    except ValueError as e:
        return _refusal(str(e), args.human, file=str(path), rolled_back=True)
    human = (f"applied {len(changes)} change(s), {len(changes)} journal "
             f"entries appended -> {path}")
    emit({"tool": TOOL, "version": VERSION, "file": str(path),
          "changes": [{"unit": u, "field": f, "from": _journal_summary(o) if o is not None else None,
                       "to": _journal_summary(n) if n is not None else n} for u, f, o, n in changes],
          "journal_len": len(state["journal"]),
          "summary": {"exit_code": EXIT_OK, "human": human}}, human_flag=args.human)
    return EXIT_OK


def _escalation_gate(state: dict, unit: str, note: str | None, actor: str,
                     journal_change) -> dict:
    """The unit's escalation gate: reuse the open one, else open it."""
    gate = next((g for g in state.get("human_gates", [])
                 if g.get("gate") == "unit_escalation" and g.get("state") == "open"),
                None)
    if gate is None:
        gate = {"gate": "unit_escalation", "state": "open",
                "note": note or f"{unit}: escalated by {actor}"}
        state.setdefault("human_gates", []).append(gate)
        journal_change("human_gates[gate=unit_escalation].state", None, "open")
    return gate


def _defer_escalation_via_policy(gate: dict, unit: str, journal_change) -> None:
    """At L2+ escalation is defer, not halt: resolve the gate via ESC-1 —
    the unit keeps its Python fallback live and drains into the end-of-run
    escalation dossier while the backlog continues."""
    version = (load_policy() or {}).get("version")
    gate["state"] = "deferred"
    gate["decided_by"] = "policy:ESC-1"
    if version:
        gate["policy_version"] = version
    gate["evidence"] = sorted(set(gate.get("evidence") or []) | {
        f"unit={unit}", "fallback=python (live)",
        "deferred=drains into the end-of-run escalation dossier"})
    journal_change("human_gates[gate=unit_escalation].state", "open", "deferred")
    journal_change("human_gates[gate=unit_escalation].decided_by",
                   None, "policy:ESC-1")
    if version:
        journal_change("human_gates[gate=unit_escalation].policy_version",
                       None, version)


def cmd_mark(args) -> int:
    path = Path(args.file)
    state = _load(path)
    unit = _find_unit(state, args.unit)
    if unit is None:
        return _refusal(f"unknown unit {args.unit!r}", args.human, file=str(path))
    old_status = unit.get("status")
    parity = unit.setdefault("parity", {})
    changes = []

    def mark_field(field: str, old, new) -> None:
        if old != new:
            changes.append((args.unit, field, old, new))

    if args.status == "ported":
        if parity.get("last_verdict") != "equivalent":
            return _refusal(
                f"cannot mark {args.unit} ported: parity.last_verdict="
                f"{parity.get('last_verdict')!r}, only an equivalent oracle verdict "
                f"gates the transition (MIGRATION_PROMPT.md repair loop)",
                args.human, unit=args.unit, file=str(path))
    if args.status == "divergent_explained":
        if parity.get("last_verdict") != "divergent":
            return _refusal(f"cannot mark {args.unit} divergent_explained: "
                            f"last_verdict is {parity.get('last_verdict')!r}, "
                            f"not divergent", args.human, unit=args.unit)
        if not args.note:
            return _refusal("divergent_explained requires --note "
                            "(the written explanation)", args.human, unit=args.unit)
        mark_field(f"units[{args.unit}].parity.divergence_note",
                   parity.get("divergence_note"), args.note)
        parity["divergence_note"] = args.note

    if args.status == "escalated":
        gate = _escalation_gate(state, args.unit, args.note, args.actor, mark_field)
        if autonomy_level(state) in DRIVER_LEVELS:
            _defer_escalation_via_policy(gate, args.unit, mark_field)

    mark_field(f"units[{args.unit}].status", old_status, args.status)
    unit["status"] = args.status
    if args.note and args.status not in ("divergent_explained", "escalated"):
        old_notes = unit.get("notes")
        unit["notes"] = ((old_notes + "\n") if old_notes else "") + args.note
        mark_field(f"units[{args.unit}].notes", old_notes, unit["notes"])
    try:
        save_validated(state, path, actor=args.actor, changes=changes)
    except ValueError as e:
        return _refusal(str(e), args.human, unit=args.unit, rolled_back=True)
    n = len(changes)
    human = f"{args.unit}: {old_status} -> {args.status} ({n} journal entr{'y' if n == 1 else 'ies'})"
    emit({"tool": TOOL, "version": VERSION, "file": str(path), "unit": args.unit,
          "from": old_status, "to": args.status, "changes": len(changes),
          "summary": {"exit_code": EXIT_OK, "human": human}}, human_flag=args.human)
    return EXIT_OK


def _decider_error(args, policy: dict | None, version: str | None) -> str | None:
    """Validate --by: 'human', or 'policy:<rule-id>' the policy defines."""
    if args.by == "human":
        return None
    if not _POLICY_DECIDED_BY_RE.match(args.by):
        return "--by must be 'human' or 'policy:<rule-id>' (e.g. policy:GO-1)"
    rule_id = args.by.split(":", 1)[1]
    rule_ids = ({r.get("id") for r in policy.get("rules", [])}
                if policy else None)
    if rule_ids is not None and rule_id not in rule_ids:
        return f"--by {args.by!r}: rule id not defined in autonomy/policy.json"
    if not version:
        return (f"--by {args.by!r} requires --policy-version (or a readable "
                f"autonomy/policy.json)")
    return None


def cmd_resolve_gate(args) -> int:
    """Record a gate decision — 'human' or 'policy:<rule-id>' — journaled,
    validated, atomic. The policy evaluation itself lives in pipeline.py
    (resolve-gates); this is the recording surface both humans and the
    driver write through."""
    path = Path(args.file)
    state = _load(path)
    gate = find_gate(state, args.gate)
    changes = []
    if gate is None:
        if not args.create:
            return _refusal(f"no {args.gate!r} gate in {path} — open one first "
                            f"or pass --create", args.human, file=str(path))
        gate = {"gate": args.gate, "state": "open",
                "note": args.note or f"{args.gate}: opened for resolution"}
        state.setdefault("human_gates", []).append(gate)
        changes.append((None, f"human_gates[gate={args.gate}].state", None, "open"))
    policy = load_policy()
    version = args.policy_version or (policy or {}).get("version")
    error = _decider_error(args, policy, version)
    if error:
        return _refusal(error, args.human, file=str(path))
    if (args.gate == "cutover" and args.decision == "approved"
            and args.by.startswith("policy:")
            and not publish_preauthorized(state)):
        return _refusal("policy-approved cutover requires autonomy.level=L3 and "
                        "autonomy.publish_allowed=true — publish is an L3-only "
                        "pre-authorization (set the level BEFORE the run, never "
                        "mid-run)", args.human, file=str(path))

    old = gate.get("state")
    updates = {"state": args.decision, "decided_by": args.by}
    if args.by != "human" and version:
        updates["policy_version"] = version
    if args.evidence:
        updates["evidence"] = merge_evidence(gate.get("evidence"), args.evidence)
    if args.note:
        updates["note"] = args.note
    record_gate_fields(gate, updates, changes)
    if not changes:
        human = f"no changes ({path} untouched)"
        emit({"tool": TOOL, "version": VERSION, "file": str(path), "changes": [],
              "summary": {"exit_code": EXIT_OK, "human": human}}, human_flag=args.human)
        return EXIT_OK
    try:
        save_validated(state, path, actor=args.actor, changes=changes)
    except ValueError as e:
        return _refusal(str(e), args.human, file=str(path), rolled_back=True)
    human = (f"gate {args.gate}: {old} -> {args.decision} "
             f"(decided_by={args.by}, {len(changes)} journal entries)")
    emit({"tool": TOOL, "version": VERSION, "file": str(path), "gate": args.gate,
          "decision": args.decision, "decided_by": args.by,
          "policy_version": gate.get("policy_version"),
          "evidence": gate.get("evidence", []),
          "summary": {"exit_code": EXIT_OK, "human": human}}, human_flag=args.human)
    return EXIT_OK


def _module_from_rust_path(rust_path: str) -> str:
    p = Path(rust_path)
    parts = list(p.with_suffix("").parts)
    if "src" in parts:
        parts = parts[parts.index("src") + 1:]
    return ".".join(parts)


def fallback_env_var(state: dict, env_prefix: str | None = None) -> str:
    """The one env var every generated shim reads: <PREFIX>_FALLBACK, the
    prefix from --env-prefix or the project name (non-alnum -> _). bench_ab
    imports this so the A/B harness flips the exact var the shims read —
    the derivation cannot drift between the two tools."""
    prefix = (env_prefix or
              re.sub(r"[^0-9A-Za-z]+", "_",
                     state.get("project", {}).get("name", "migration")).upper())
    return f"{prefix}_FALLBACK"


SHIM_TEMPLATE = '''"""Auto-generated per-unit fallback switch — framework/tools/state.py shim.

Unit:  {unit}  ({python_path} -> {rust_path})
Status at generation: {status}

Dual-build discipline: the Python fallback stays callable per unit until it
is deliberately retired in Stage 7. The switch is reversible per unit with
NO extra state anywhere (anti-requirement: no state outside
migration-state.json): set {env_var} to "all" (every unit falls back), a
comma-separated list of unit ids, or "none" (default).
"""

import importlib
import os

_UNIT = {unit!r}
_ENV = {env_var!r}
_GENERATED_STATUS = {status!r}
_RUST_LIVE = _GENERATED_STATUS in ("ported", "divergent_explained", "retired")


def _use_fallback() -> bool:
    value = os.environ.get(_ENV, "none")
    if value == "all":
        return True
    selected = [x.strip() for x in value.split(",") if x.strip()]
    return _UNIT in selected


def _python():
    return importlib.import_module({python_import!r})


def _rust():
    return importlib.import_module({rust_import!r})


def impl():
    """Return the live implementation module for this unit (python or rust)."""
    if _use_fallback() or not _RUST_LIVE:
        return _python()
    return _rust()
'''


def cmd_shim(args) -> int:
    state = _load(Path(args.file))
    unit = _find_unit(state, args.unit)
    if unit is None:
        return _refusal(f"unknown unit {args.unit!r}", args.human)
    if not unit.get("rust_path"):
        return _refusal(f"unit {args.unit!r} has no rust_path — nothing to "
                        f"switch to yet", args.human, unit=args.unit)
    python_import = args.python_import or module_name_of(unit["python_path"])
    rust_import = args.rust_import or _module_from_rust_path(unit["rust_path"])
    env_var = fallback_env_var(state, args.env_prefix)
    code = SHIM_TEMPLATE.format(
        unit=unit["id"], python_path=unit["python_path"], rust_path=unit["rust_path"],
        status=unit.get("status", "pending"), env_var=env_var,
        python_import=python_import, rust_import=rust_import)
    default_side = ("rust" if unit.get("status") in RUST_LIVE_STATUSES else "python")
    written = None
    if args.write:
        target = Path(args.write)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")
        written = str(target)
    human = (f"shim for {unit['id']}: default side={default_side}, "
             f"flip via {env_var}=<{'all' if default_side == 'rust' else 'none'}|{unit['id']}|...>"
             + (f", written to {written}" if written else ""))
    emit({"tool": TOOL, "version": VERSION, "unit": unit["id"],
          "status": unit.get("status"), "default_side": default_side,
          "env_var": env_var, "python_import": python_import,
          "rust_import": rust_import, "code": code, "written_to": written,
          "summary": {"exit_code": EXIT_OK, "human": human}}, human_flag=args.human)
    return EXIT_OK


def cmd_migrate(args) -> int:
    path = Path(args.file)
    state = _load(path)
    before = json.dumps(state, sort_keys=True)
    changes = []
    migrations = []

    def ensure(obj: dict, key: str, default, field: str, unit) -> None:
        if key not in obj:
            obj[key] = default
            migrations.append({"field": field, "from": None, "to": default})
            changes.append((unit, field, None, default))

    ensure(state, "human_gates", [], "human_gates", None)
    ensure(state, "journal", [], "journal", None)
    ensure(state, "budget_ledger",
           {"tokens_total": 0, "hours_human_total": 0.0, "regression_triage_hours": 0.0},
           "budget_ledger", None)
    # issue #22 backfills: the autonomy block, run_status, and decided_by
    # for gates resolved before the policy layer existed (human decisions
    # by definition). The autonomy block backfills as L0 — never silently
    # re-authorize a pre-policy run as autonomous (new runs init at the
    # never-ask default L2).
    ensure(state, "autonomy", autonomy_block("L0"), "autonomy", None)
    ensure(state, "run_status", "pending", "run_status", None)
    for i, g in enumerate(state.get("human_gates", [])):
        if g.get("state") != "open" and not g.get("decided_by"):
            g["decided_by"] = "human"
            field = f"human_gates[{i}].decided_by"
            migrations.append({"field": field, "from": None, "to": "human"})
            changes.append((None, field, None, "human"))
    for unit in state.get("units", []):
        uid = unit.get("id")
        ensure(unit, "attempts", 0, f"units[{uid}].attempts", uid)
        ensure(unit, "budget", {"max_rounds": 5, "tokens_spent": 0, "exhausted": False},
               f"units[{uid}].budget", uid)
        ensure(unit, "risk_flags", [], f"units[{uid}].risk_flags", uid)

    if json.dumps(state, sort_keys=True) == before:
        human = f"{path} already current — no migrations needed"
        emit({"tool": TOOL, "version": VERSION, "file": str(path), "migrations": [],
              "summary": {"exit_code": EXIT_OK, "human": human}}, human_flag=args.human)
        return EXIT_OK
    schema = json.loads(SCHEMA_PATH.read_text())
    schema_errors = validate_schema(state, schema)
    if schema_errors:
        # migrations alone cannot fix structural schema violations — report
        # them; migrate only fills gaps, never rewrites shapes
        return _refusal("schema errors remain after migration: " +
                        "; ".join(schema_errors), args.human, file=str(path))
    append_transitions(state, changes, actor="state.py")
    write_json_atomic(path, state)
    human = f"migrated {path}: {len(migrations)} field(s) backfilled, journaled"
    emit({"tool": TOOL, "version": VERSION, "file": str(path),
          "migrations": migrations, "journal_len": len(state.get("journal", [])),
          "summary": {"exit_code": EXIT_OK, "human": human}}, human_flag=args.human)
    return EXIT_OK


def cmd_journal(args) -> int:
    state = _load(Path(args.file))
    entries = state.get("journal", [])
    if args.unit:
        entries = [e for e in entries if e.get("unit") == args.unit]
    if args.tail:
        entries = entries[-args.tail:]
    human = f"journal: {len(entries)} entr(ies) shown"
    emit({"tool": TOOL, "version": VERSION, "journal": entries,
          "summary": {"exit_code": EXIT_OK, "human": human}}, human_flag=args.human)
    return EXIT_OK


def cmd_show(args) -> int:
    state = _load(Path(args.file))
    if args.unit:
        unit = _find_unit(state, args.unit)
        if unit is None:
            return _refusal(f"unknown unit {args.unit!r}", args.human)
        human = (f"{args.unit}: status={unit.get('status')} "
                 f"parity={unit.get('parity', {}).get('last_verdict', 'unjudged')}")
        emit({"tool": TOOL, "version": VERSION, "unit": unit,
              "summary": {"exit_code": EXIT_OK, "human": human}}, human_flag=args.human)
        return EXIT_OK
    counts: dict[str, int] = {}
    for u in state.get("units", []):
        counts[u.get("status", "?")] = counts.get(u.get("status", "?"), 0) + 1
    human = (f"{state.get('project', {}).get('name', '?')}: stage={state.get('stage')}, "
             f"run={state.get('run_status', 'n/a')}, "
             f"autonomy={autonomy_level(state)}, "
             f"{len(state.get('units', []))} units: "
             + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    emit({"tool": TOOL, "version": VERSION, "project": state.get("project"),
          "stage": state.get("stage"), "run_status": state.get("run_status"),
          "autonomy": state.get("autonomy"), "status_counts": counts,
          "journal_len": len(state.get("journal", [])),
          "open_gates": [g for g in state.get("human_gates", []) if g.get("state") == "open"],
          "summary": {"exit_code": EXIT_OK, "human": human}}, human_flag=args.human)
    return EXIT_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=TOOL, description=__doc__.splitlines()[0])
    ap.add_argument("--human", action="store_true", help="echo human summary to stderr")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("validate", help="validate a state file")
    p.add_argument("file")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("init", help="create a minimal valid state file")
    p.add_argument("file")
    p.add_argument("--name", required=True)
    p.add_argument("--python-path", required=True)
    p.add_argument("--stage", type=int, default=0)
    p.add_argument("--autonomy-level", default="L2", choices=sorted(POLICY_LEVELS),
                   help="pre-authorized autonomy for the run (issue #22); "
                        "default L2 = all gates policy-resolved, never asks "
                        "(L0/L1 = operator-supervised: gates stay open and the "
                        "run ends with the dossier; L3 = autopilot incl. cutover)")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("set", help="set field(s) — journaled, validated, atomic")
    p.add_argument("file")
    p.add_argument("--field", action="append", required=True,
                   help="dotted path=value, e.g. units[u-1].status=ported "
                        "(repeatable); a leading '@json:' types that one "
                        "value as strict JSON (also accepted directly on "
                        "the value: path=@json:[...])")
    p.add_argument("--actor", default="orchestrator")
    p.add_argument("--json-value", action="store_true",
                   help="treat every value as strict JSON (error on "
                        "non-JSON); to mix, use the per-field '@json:' "
                        "prefix")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("mark", help="guarded unit status transition")
    p.add_argument("file")
    p.add_argument("--unit", required=True)
    p.add_argument("--status", required=True, choices=STATUSES)
    p.add_argument("--note", help="required for divergent_explained; appended to notes otherwise")
    p.add_argument("--actor", default="orchestrator")
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("resolve-gate", help="record a gate decision — human or "
                                            "policy:<rule-id> — journaled and validated")
    p.add_argument("file")
    p.add_argument("--gate", required=True, choices=GATES)
    p.add_argument("--decision", required=True, choices=["approved", "rejected", "deferred"])
    p.add_argument("--by", required=True, help="'human' or 'policy:<rule-id>' (e.g. policy:GO-1)")
    p.add_argument("--policy-version", help="defaults to autonomy/policy.json's version")
    p.add_argument("--evidence", action="append", default=[],
                   help="key=value record the decision evaluated (repeatable)")
    p.add_argument("--note")
    p.add_argument("--create", action="store_true",
                   help="open the gate first if no gate of this name exists")
    p.add_argument("--actor", default="orchestrator")
    p.set_defaults(func=cmd_resolve_gate)

    p = sub.add_parser("shim", help="emit the per-unit fallback switch shim")
    p.add_argument("file")
    p.add_argument("--unit", required=True)
    p.add_argument("--python-import", help="override the derived python module")
    p.add_argument("--rust-import", help="override the derived rust module")
    p.add_argument("--env-prefix", help="override the env-var prefix (default: project name)")
    p.add_argument("--write", help="also write the shim to this path")
    p.set_defaults(func=cmd_shim)

    p = sub.add_parser("migrate", help="backfill missing fields to current schema shape")
    p.add_argument("file")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("journal", help="print the journal (read-only)")
    p.add_argument("file")
    p.add_argument("--unit")
    p.add_argument("--tail", type=int)
    p.set_defaults(func=cmd_journal)

    p = sub.add_parser("show", help="show project summary or one unit")
    p.add_argument("file")
    p.add_argument("--unit")
    p.set_defaults(func=cmd_show)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(run_cli(main, tool=TOOL, version=VERSION))
