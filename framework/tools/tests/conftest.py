"""Shared test setup: make framework/tools importable and give every test a
tmp project scaffold helper."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parent.parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

REPO_ROOT = TOOLS.parent.parent


def run_tool(monkeypatch, capsys, script: str, argv: list[str]) -> tuple[int, dict]:
    """Run a tool's main() in-process through the shared CLI boundary
    (run_cli), exactly like the real command line — so refusals surface as
    JSON envelopes instead of escaping exceptions. Returns (exit_code,
    parsed_json); json.loads enforces the 'stdout is always one JSON
    object' convention even on error paths."""
    mod = __import__(script[:-3])
    monkeypatch.setattr(sys, "argv", [script] + argv)
    from _common import run_cli
    code = run_cli(mod.main, argv, tool=getattr(mod, "TOOL", script),
                   version=getattr(mod, "VERSION", None))
    out = capsys.readouterr().out
    return code, json.loads(out)


@pytest.fixture
def mini_state(tmp_path) -> Path:
    """A minimal valid state file with one unit."""
    state = {
        "project": {"name": "demo", "python_path": "src/demo"},
        "stage": 2,
        "units": [{
            "id": "u1", "python_path": "src/demo/a.py",
            "rust_path": "crates/demo/src/u1.rs", "deps": [], "order": 1,
            "status": "pending",
        }],
        "human_gates": [], "journal": [],
    }
    p = tmp_path / "state.json"
    p.write_text(json.dumps(state))
    return p
