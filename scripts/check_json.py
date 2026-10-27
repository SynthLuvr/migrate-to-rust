#!/usr/bin/env python3
"""Parse-check every JSON file in the repo.

The CI issue names two files that must parse (substitutions.json, the state
schema); rather than whitelist them, this parses every ``*.json`` in the
repo, so a stray trailing comma anywhere fails CI. Semantic checks live
elsewhere: ``framework/state/check_examples.py`` for the schema,
``framework/tools/tests`` for the tools.

Stdlib only. Run locally: python scripts/check_json.py
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Must parse even if a future rename drops them out of the walk.
REQUIRED = [
    Path("framework/knowledge/substitutions.json"),
    Path("framework/state/migration-state.schema.json"),
]


def main() -> None:
    json_files = sorted(p for p in REPO_ROOT.rglob("*.json") if ".git" not in p.parts)
    walked = {p.relative_to(REPO_ROOT) for p in json_files}
    for rel in REQUIRED:
        if rel not in walked:
            print(f"FAIL: required JSON file not found: {rel}", file=sys.stderr)
            sys.exit(1)

    broken = 0
    for path in json_files:
        rel = path.relative_to(REPO_ROOT)
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f"FAIL: {rel}: {exc}", file=sys.stderr)
            broken += 1
        else:
            print(f"ok: {rel} parses")
    if broken:
        sys.exit(1)
    print(f"parsed {len(json_files)} JSON file(s)")


if __name__ == "__main__":
    main()
