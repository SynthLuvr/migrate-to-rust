#!/usr/bin/env python3
"""Validate state-file examples against migration-state.schema.json.

Every example instance must validate against the schema (draft 2020-12).
Across the corpus, at least one instance must exercise ``journal[]``, the
``scope_change`` gate, and per-unit ``notes``/``sessions[]`` together; at
least one unit in each of the six statuses must appear (the worked example,
``framework/examples/migration-state.json``, exists precisely to cover
them); and the schema must *constrain* those fields — malformed journal
entries, session entries, and gate values are rejected rather than merely
tolerated by the schema's permissive default of allowing unspecified
properties.

Non-zero exit on any failure, so CI fails loudly.

Run locally:  pip install jsonschema && python framework/state/check_examples.py

Optional arguments override the default instance set (files or directories):
    python framework/state/check_examples.py framework/examples/migration-state.json
"""

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE / "migration-state.schema.json"
EXAMPLES_DIR = HERE / "examples"
WORKED_EXAMPLE = HERE.parent / "examples" / "migration-state.json"
DEFAULT_INSTANCE_PATHS = [EXAMPLES_DIR, WORKED_EXAMPLE]


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def resolve_instance_paths(args: list[str]) -> list[Path]:
    """CLI args (files or dirs) override the defaults; dirs expand to *.json."""
    roots = [Path(a) for a in args] if args else DEFAULT_INSTANCE_PATHS
    paths = []
    for root in roots:
        if root.is_dir():
            paths.extend(sorted(root.glob("*.json")))
        else:
            paths.append(root)
    for path in paths:
        if not path.is_file():
            fail(f"example instance {path} not found")
    if not paths:
        fail("no example instances found")
    return paths


def validate_examples(validator: Draft202012Validator, paths: list[Path]) -> list[dict]:
    instances = []
    for path in paths:
        instance = json.loads(path.read_text())
        errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
        for err in errors:
            print(
                f"{path.name}: {'/'.join(map(str, err.absolute_path)) or '<root>'}: {err.message}"
            )
        if errors:
            fail(f"{path.name} does not validate against migration-state.schema.json")
        instances.append(instance)
        print(f"ok: {path.name} validates against migration-state.schema.json")
    return instances


def require_fields_exercised(instances: list[dict]) -> None:
    """At least one instance must exercise the guarded fields, or the
    example corpus stops guarding them."""
    exercised = any(
        inst.get("journal")
        and any(gate.get("gate") == "scope_change" for gate in inst.get("human_gates", []))
        and any("notes" in unit and "sessions" in unit for unit in inst["units"])
        for inst in instances
    )
    if not exercised:
        fail(
            "no example exercises journal[] + human_gates.gate=scope_change + "
            "unit notes/sessions together"
        )
    print("ok: an example instance exercises journal[], scope_change, and unit notes/sessions")


def require_all_statuses_exercised(instances: list[dict], schema: dict) -> None:
    """Every status the schema's enum permits must appear on some unit in
    the example corpus (the worked example exists to cover them all)."""
    statuses = schema["properties"]["units"]["items"]["properties"]["status"]["enum"]
    seen = {u.get("status") for inst in instances for u in inst.get("units", [])}
    missing = [s for s in statuses if s not in seen]
    if missing:
        fail(
            "no example instance shows a unit with status: "
            + ", ".join(missing)
            + " (see framework/examples/migration-state.json)"
        )
    print(f"ok: example instances show a unit in each of the {len(statuses)} statuses")


def must_accept(label: str, subschema: dict, fragment: object) -> None:
    if list(Draft202012Validator(subschema).iter_errors(fragment)):
        fail(f"schema rejected {label}")


def must_reject(label: str, subschema: dict, fragment: object) -> None:
    if not list(Draft202012Validator(subschema).iter_errors(fragment)):
        fail(f"schema accepted {label} — the constraint does not bite")


def require_fields_constrained(schema: dict) -> None:
    journal_entry_schema = schema["properties"]["journal"]["items"]
    session_schema = schema["properties"]["units"]["items"]["properties"]["sessions"]["items"]
    gate_schema = schema["properties"]["human_gates"]["items"]

    valid_journal_entry = {
        "timestamp": "2026-10-12T11:05:00Z",
        "unit": None,
        "field": "stage",
        "from": 3,
        "to": 4,
        "actor": "orchestrator",
    }
    must_accept("a well-formed journal entry", journal_entry_schema, valid_journal_entry)
    must_reject(
        "a journal entry missing 'to'",
        journal_entry_schema,
        {k: v for k, v in valid_journal_entry.items() if k != "to"},
    )
    must_reject(
        "a journal entry with a non-string timestamp",
        journal_entry_schema,
        {**valid_journal_entry, "timestamp": 1760264700},
    )
    must_reject(
        "a session entry missing 'role'",
        session_schema,
        {"id": "sess_01hazr4q8m", "name": "unit-u-parse-1-translator-c1"},
    )
    must_reject(
        "a bogus gate value ('scope-change' typo)",
        gate_schema,
        {"gate": "scope-change", "state": "open"},
    )
    must_accept(
        "the mandated gate value scope_change",
        gate_schema,
        {"gate": "scope_change", "state": "open"},
    )
    print("ok: journal/sessions required fields and gate enum are enforced")


def main() -> None:
    schema = json.loads(SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    instances = validate_examples(validator, resolve_instance_paths(sys.argv[1:]))
    require_fields_exercised(instances)
    require_all_statuses_exercised(instances, schema)
    require_fields_constrained(schema)
    print(f"validated {len(instances)} example instance(s) against the schema")


if __name__ == "__main__":
    main()
