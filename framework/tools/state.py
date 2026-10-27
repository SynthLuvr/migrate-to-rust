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
             (the exact field vocabulary the journal already uses); every
             changed field appends one journal entry; atomic write; invalid
             results roll back
  mark       status transitions with the MIGRATION_PROMPT.md discipline
             encoded as hard guards (ported requires an equivalent verdict;
             escalated auto-opens the unit_escalation human gate;
             divergent_explained requires a written explanation)
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
    EXIT_FINDINGS, EXIT_OK, emit, emit_error, module_name_of,
    now_iso, run_cli, write_json_atomic,
)

TOOL = "state.py"
VERSION = 1
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "state" / "migration-state.schema.json"
STATUSES = ["pending", "ported", "divergent_explained", "escalated", "fallback_python", "retired"]
# statuses whose live side is the Rust implementation (fallback still on call
# for everything else; "retired" means the fallback was deliberately removed
# in Stage 7, so rust is the only side left)
RUST_LIVE_STATUSES = {"ported", "divergent_explained", "retired"}

# --------------------------------------------------------------- schema ----
# A stdlib-only validator for the draft-2020-12 subset the schema uses:
# $schema/$comment/title ignored · type (incl. unions) · required ·
# properties · items · enum · minimum/maximum · format date-time.
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
        for key, sub in schema.get("properties", {}).items():
            if key in instance:
                validate_schema(instance[key], sub, f"{path}.{key}", errors)
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
                     if g.get("gate") == "unit_escalation" and g.get("state") == "open"]
            if not gates:
                warnings.append(
                    f"units[{uid}]: status=escalated but no open unit_escalation human gate"
                )

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
    `journal` itself is append-only and rejected here. Indexed segments
    (units[u-1], human_gates[gate=g]) SELECT; the leaf key must be addressed
    one level deeper, e.g. units[u-1].status."""
    if _split_path(path)[0] == "journal":
        raise ValueError("journal[] is append-only — transitions go through set/mark")
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


def cmd_init(args) -> int:
    path = Path(args.file)
    if path.exists() and not args.force:
        return emit_error(f"{path} already exists (use --force to overwrite)",
                          tool=TOOL, version=VERSION, human_flag=args.human)
    state = {
        "project": {"name": args.name, "python_path": args.python_path},
        "stage": args.stage,
        "units": [],
        "human_gates": [],
        "journal": [],
        "budget_ledger": {"tokens_total": 0, "hours_human_total": 0.0,
                          "regression_triage_hours": 0.0},
    }
    schema = json.loads(SCHEMA_PATH.read_text())
    errs = validate_schema(state, schema)
    assert not errs, errs
    append_transitions(state, [(None, "project", None, {"name": args.name,
                                                        "python_path": args.python_path}),
                               (None, "stage", None, args.stage)], actor="state.py")
    write_json_atomic(path, state)
    human = f"initialized {path} (project={args.name}, stage={args.stage})"
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


def cmd_set(args) -> int:
    path = Path(args.file)
    state = _load(path)
    changes = []
    for pair in args.field:
        if "=" not in pair:
            return _refusal(f"--field expects path=value, got {pair!r}", args.human)
        field, raw = pair.split("=", 1)
        field = field.strip()
        try:
            value = json.loads(raw)  # numbers, true/false, null, lists...
        except json.JSONDecodeError as e:
            if args.json_value:
                return _refusal(f"--field {field}: value is not valid JSON: {e}",
                                args.human)
            value = raw  # fall back to raw string
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
        gates = state.setdefault("human_gates", [])
        has_open = any(g.get("gate") == "unit_escalation" and g.get("state") == "open"
                       for g in gates)
        if not has_open:
            gates.append({"gate": "unit_escalation", "state": "open",
                          "note": args.note or f"{args.unit}: escalated by {args.actor}"})
            mark_field("human_gates[gate=unit_escalation].state", None, "open")

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


def _module_from_rust_path(rust_path: str) -> str:
    p = Path(rust_path)
    parts = list(p.with_suffix("").parts)
    if "src" in parts:
        parts = parts[parts.index("src") + 1:]
    return ".".join(parts)


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
    prefix = (args.env_prefix or
              re.sub(r"[^0-9A-Za-z]+", "_", state.get("project", {}).get("name", "migration")).upper())
    env_var = f"{prefix}_FALLBACK"
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
             f"{len(state.get('units', []))} units: "
             + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    emit({"tool": TOOL, "version": VERSION, "project": state.get("project"),
          "stage": state.get("stage"), "status_counts": counts,
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
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("set", help="set field(s) — journaled, validated, atomic")
    p.add_argument("file")
    p.add_argument("--field", action="append", required=True,
                   help="dotted path=value, e.g. units[u-1].status=ported (repeatable)")
    p.add_argument("--actor", default="orchestrator")
    p.add_argument("--json-value", action="store_true",
                   help="treat values as strict JSON (error on non-JSON)")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("mark", help="guarded unit status transition")
    p.add_argument("file")
    p.add_argument("--unit", required=True)
    p.add_argument("--status", required=True, choices=STATUSES)
    p.add_argument("--note", help="required for divergent_explained; appended to notes otherwise")
    p.add_argument("--actor", default="orchestrator")
    p.set_defaults(func=cmd_mark)

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
