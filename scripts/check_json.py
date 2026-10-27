#!/usr/bin/env python3
"""Parse-check every JSON file tracked in the repo.

The minimum bar from the CI issue: ``framework/knowledge/substitutions.json``
and ``framework/state/migration-state.schema.json`` must parse. Rather than
whitelist two files, this walks every ``*.json`` in the repo (schema- and
semantics-checks live elsewhere: ``framework/state/check_examples.py`` for the
schema, ``framework/tools/tests`` for the tools) and fails on the first file
that is not valid JSON, so a stray trailing comma can never land unnoticed.

Stdlib only, no third-party imports. Non-zero exit on any failure.

Run locally:  python scripts/check_json.py
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Must exist and parse even if a future rename removes them from the walk.
REQUIRED = [
    Path("framework/knowledge/substitutions.json"),
    Path("framework/state/migration-state.schema.json"),
]


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    for rel in REQUIRED:
        if not (REPO_ROOT / rel).is_file():
            fail(f"required JSON file missing: {rel}")

    json_files = sorted(
        p for p in REPO_ROOT.rglob("*.json") if ".git" not in p.parts
    )
    # rglob should find both REQUIRED files; assert rather than assume.
    walked = {p.relative_to(REPO_ROOT) for p in json_files}
    for rel in REQUIRED:
        if rel not in walked:
            fail(f"{rel} exists but was not walked — check the glob")

    bad = 0
    for path in json_files:
        rel = path.relative_to(REPO_ROOT)
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f"FAIL: {rel}: {exc}", file=sys.stderr)
            bad += 1
        else:
            print(f"ok: {rel} parses")
    if bad:
        fail(f"{bad} file(s) failed to parse")
    print(f"parsed {len(json_files)} JSON file(s)")


if __name__ == "__main__":
    main()
