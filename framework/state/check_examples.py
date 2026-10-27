#!/usr/bin/env python3
"""Validate state-file examples against migration-state.schema.json.

Exit code is non-zero on any failure, so CI fails loudly. Two jobs:

1. Positive: every ``examples/*.json`` instance validates against the schema
   (draft 2020-12) and at least one instance exercises the issue-#6 fields —
   ``journal[]``, ``human_gates.gate == "scope_change"``, and per-unit
   ``notes``/``sessions[]``.

2. Negative: the new fields must be *constrained* by the schema, not merely
   tolerated by its permissive default (``additionalProperties: true``).
   Malformed journal entries, session entries, and gate values must be
   rejected.

Run locally:  pip install jsonschema && python framework/state/check_examples.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE / "migration-state.schema.json"
EXAMPLES_DIR = HERE / "examples"


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    schema = json.loads(SCHEMA_PATH.read_text())
    Draft202012Validator.check_schema(schema)  # the schema itself must be valid 2020-12
    validator = Draft202012Validator(schema)

    # --- 1. positive validation of every example instance -------------------
    examples = sorted(EXAMPLES_DIR.glob("*.json"))
    if not examples:
        fail(f"no example instances in {EXAMPLES_DIR}")

    instances = []
    for path in examples:
        instance = json.loads(path.read_text())
        errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
        for err in errors:
            print(f"{path.name}: {'/'.join(map(str, err.absolute_path)) or '<root>'}: {err.message}")
        if errors:
            fail(f"{path.name} does not validate against migration-state.schema.json")
        instances.append(instance)
        print(f"ok: {path.name} validates against migration-state.schema.json")

    # at least one instance must exercise the fields added for issue #6
    exercising = [
        inst
        for inst in instances
        if inst.get("journal")
        and any(g.get("gate") == "scope_change" for g in inst.get("human_gates", []))
        and any("notes" in u and "sessions" in u for u in inst.get("units", []))
    ]
    if not exercising:
        fail(
            "no example exercises journal[] + human_gates.gate=scope_change + "
            "unit notes/sessions together"
        )
    print("ok: an example instance exercises journal[], scope_change, and unit notes/sessions")

    # --- 2. the new fields must actually be constrained ---------------------
    unit_schema = schema["properties"]["units"]["items"]
    gate_enum = schema["properties"]["human_gates"]["items"]["properties"]["gate"]["enum"]

    if "scope_change" not in gate_enum:
        fail("human_gates.gate enum lacks scope_change")

    def must_reject(label: str, subschema: dict, fragment: object) -> None:
        if not list(Draft202012Validator(subschema).iter_errors(fragment)):
            fail(f"schema accepted {label} — the constraint does not bite")

    good_journal_entry = {
        "timestamp": "2026-10-12T11:05:00Z",
        "unit": None,
        "field": "stage",
        "from": 3,
        "to": 4,
        "actor": "orchestrator",
    }
    journal_schema = schema["properties"]["journal"]["items"]
    # sanity: the documented shape validates against the journal subschema
    if list(Draft202012Validator(journal_schema).iter_errors(good_journal_entry)):
        fail("journal subschema rejects its own documented entry shape")
    must_reject(
        "a journal entry missing 'to'",
        journal_schema,
        {k: v for k, v in good_journal_entry.items() if k != "to"},
    )
    must_reject(
        "a journal entry with a non-string timestamp",
        journal_schema,
        {**good_journal_entry, "timestamp": 1760264700},
    )
    must_reject(
        "a session entry missing 'role'",
        unit_schema["properties"]["sessions"]["items"],
        {"id": "sess_01hazr4q8m", "name": "unit-u-parse-1-translator-c1"},
    )
    must_reject(
        "a bogus gate value ('scope-change' typo)",
        schema["properties"]["human_gates"]["items"],
        {"gate": "scope-change", "state": "open"},
    )
    # and the real value must be accepted
    if list(
        Draft202012Validator(schema["properties"]["human_gates"]["items"]).iter_errors(
            {"gate": "scope_change", "state": "open"}
        )
    ):
        fail("human_gates subschema rejects the mandated gate value scope_change")

    print("ok: journal/sessions required fields and gate enum are enforced")
    print(f"validated {len(instances)} example instance(s) against the schema")


if __name__ == "__main__":
    main()
