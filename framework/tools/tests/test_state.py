"""state.py acceptance tests — tools/README.md #4: every state transition
journaled into the state file's own journal[], validation, guards, shim,
migrate."""

from __future__ import annotations

import json

import state as state_tool
from conftest import REPO_ROOT, run_tool

EXAMPLE = REPO_ROOT / "framework" / "state" / "examples" / "stage4-mid-migration.json"


def _load(path):
    return json.loads(path.read_text())


def test_example_state_validates(monkeypatch, capsys):
    code, out = run_tool(monkeypatch, capsys, "state.py", ["validate", str(EXAMPLE)])
    assert code == 0
    assert out["valid"] is True
    assert out["schema_errors"] == [] and out["invariant_errors"] == []


def test_every_transition_journaled(monkeypatch, capsys, mini_state):
    before = _load(mini_state)
    code, out = run_tool(monkeypatch, capsys, "state.py", [
        "set", str(mini_state),
        "--field", "units[u1].risk_flags=[\"regex translation\"]",
        "--field", "units[u1].attempts=2", "--actor", "orchestrator"])
    assert code == 0
    after = _load(mini_state)
    assert after["units"][0]["risk_flags"] == ["regex translation"]
    assert after["units"][0]["attempts"] == 2
    entries = after["journal"]
    assert len(entries) == len(before["journal"]) + 2
    fields = [e["field"] for e in entries[-2:]]
    assert fields == ["units[u1].risk_flags", "units[u1].attempts"]
    for e in entries[-2:]:
        assert e["unit"] == "u1" and e["actor"] == "orchestrator"
        assert set(e) >= {"timestamp", "unit", "field", "from", "to"}


def test_set_journal_directly_rejected(monkeypatch, capsys, mini_state):
    code, out = run_tool(monkeypatch, capsys, "state.py",
                         ["set", str(mini_state), "--field", "journal=[]"])
    assert code == 2
    assert "append-only" in out["error"]
    assert _load(mini_state)["journal"] == []  # untouched


def test_invalid_result_rolls_back(monkeypatch, capsys, mini_state):
    before = _load(mini_state)
    # stage out of schema range: the write must not land
    code, _ = run_tool(monkeypatch, capsys, "state.py",
                       ["set", str(mini_state), "--field", "stage=9"])
    assert code == 2
    assert _load(mini_state) == before


def test_mark_ported_requires_equivalent_verdict(monkeypatch, capsys, mini_state):
    code, _ = run_tool(monkeypatch, capsys, "state.py",
                       ["mark", str(mini_state), "--unit", "u1", "--status", "ported"])
    assert code == 2
    assert _load(mini_state)["units"][0]["status"] == "pending"

    run_tool(monkeypatch, capsys, "state.py", [
        "set", str(mini_state), "--field",
        "units[u1].parity={\"runs\":3,\"equivalent\":3,\"divergent\":0,"
        "\"incomparable\":0,\"last_verdict\":\"equivalent\"}"])
    code, out = run_tool(monkeypatch, capsys, "state.py",
                         ["mark", str(mini_state), "--unit", "u1", "--status", "ported"])
    assert code == 0
    assert _load(mini_state)["units"][0]["status"] == "ported"


def test_mark_escalated_opens_human_gate(monkeypatch, capsys, mini_state):
    code, out = run_tool(monkeypatch, capsys, "state.py",
                         ["mark", str(mini_state), "--unit", "u1",
                          "--status", "escalated", "--note", "repro attached"])
    assert code == 0
    st = _load(mini_state)
    gates = [g for g in st["human_gates"]
             if g["gate"] == "unit_escalation" and g["state"] == "open"]
    assert gates, "escalation must auto-open the unit_escalation human gate"
    assert any(e["field"] == "human_gates[gate=unit_escalation].state"
               for e in st["journal"])


def test_divergent_explained_requires_note_and_divergent_verdict(
        monkeypatch, capsys, mini_state):
    code, _ = run_tool(monkeypatch, capsys, "state.py", [
        "mark", str(mini_state), "--unit", "u1", "--status", "divergent_explained",
        "--note", "x"])
    assert code == 2  # last_verdict is not divergent
    run_tool(monkeypatch, capsys, "state.py", [
        "set", str(mini_state), "--field",
        "units[u1].parity={\"runs\":1,\"equivalent\":0,\"divergent\":1,"
        "\"incomparable\":0,\"last_verdict\":\"divergent\","
        "\"divergence_note\":\"seed\"}"])
    code, out = run_tool(monkeypatch, capsys, "state.py", [
        "mark", str(mini_state), "--unit", "u1",
        "--status", "divergent_explained"])  # no note
    assert code == 2
    assert "--note" in out["error"]
    code, out = run_tool(monkeypatch, capsys, "state.py", [
        "mark", str(mini_state), "--unit", "u1",
        "--status", "divergent_explained", "--note", "ordering-only divergence"])
    assert code == 0
    u = _load(mini_state)["units"][0]
    assert u["status"] == "divergent_explained"
    assert u["parity"]["divergence_note"] == "ordering-only divergence"


def test_shim_emits_reversible_per_unit_switch(monkeypatch, capsys, mini_state):
    code, out = run_tool(monkeypatch, capsys, "state.py",
                         ["shim", str(mini_state), "--unit", "u1"])
    assert code == 0
    code_txt = out["code"]
    assert "DEMO_FALLBACK" in code_txt and "u1" in code_txt
    assert "import_module" in code_txt
    # default side for status=pending is python; reversible without state
    assert out["default_side"] == "python"
    # the shim must be valid python
    compile(code_txt, "shim.py", "exec")


def test_migrate_backfills_and_journals(monkeypatch, capsys, tmp_path):
    st = json.loads(EXAMPLE.read_text())
    for u in st["units"]:
        u.pop("budget", None)
    st.pop("budget_ledger", None)
    p = tmp_path / "old.json"
    p.write_text(json.dumps(st))
    journal_len_before = len(st.get("journal", []))
    code, out = run_tool(monkeypatch, capsys, "state.py", ["migrate", str(p)])
    assert code == 0
    after = _load(p)
    assert all("budget" in u for u in after["units"])
    assert "budget_ledger" in after
    assert len(after["journal"]) > journal_len_before
    code, _ = run_tool(monkeypatch, capsys, "state.py", ["validate", str(p)])
    assert code == 0


def test_validate_catches_invariant_violations(monkeypatch, capsys, tmp_path):
    st = json.loads(EXAMPLE.read_text())
    st["units"][0]["deps"] = ["no-such-unit"]
    p = tmp_path / "broken.json"
    p.write_text(json.dumps(st))
    code, out = run_tool(monkeypatch, capsys, "state.py", ["validate", str(p)])
    assert code == 2
    assert any("unknown unit id" in e for e in out["invariant_errors"])


def test_validate_detects_out_of_band_edit(monkeypatch, capsys, tmp_path):
    st = json.loads(EXAMPLE.read_text())
    # edit status without journaling — the journal coherence check must warn
    st["units"][0]["status"] = "fallback_python"
    p = tmp_path / "oob.json"
    p.write_text(json.dumps(st))
    code, out = run_tool(monkeypatch, capsys, "state.py", ["validate", str(p)])
    assert code == 0  # warning, not error
    assert any("outside state.py" in w for w in out["warnings"])


def test_init_creates_valid_journaled_state(monkeypatch, capsys, tmp_path):
    p = tmp_path / "new.json"
    code, out = run_tool(monkeypatch, capsys, "state.py", [
        "init", str(p), "--name", "proj", "--python-path", "src/proj"])
    assert code == 0
    st = _load(p)
    assert st["stage"] == 0 and st["units"] == []
    assert len(st["journal"]) == 2  # project + stage creation entries
    code, _ = run_tool(monkeypatch, capsys, "state.py", ["validate", str(p)])
    assert code == 0


# -- internal validator vs the jsonschema reference implementation ----------

def test_validator_agrees_with_jsonschema_package():
    pytest = __import__("pytest")
    pytest.importorskip("jsonschema")
    from jsonschema import Draft202012Validator
    schema = json.loads((REPO_ROOT / "framework" / "state" /
                         "migration-state.schema.json").read_text())
    ref = Draft202012Validator(schema)

    good = json.loads(EXAMPLE.read_text())
    assert list(ref.iter_errors(good)) == []
    assert state_tool.validate_schema(good, schema) == []

    mutations = [
        ("bad stage", lambda s: s.update(stage=9)),
        ("missing project", lambda s: s.pop("project")),
        ("bad unit status", lambda s: s["units"][0].update(status="shipped")),
        ("bad gate name", lambda s: s["human_gates"].append(
            {"gate": "scope-change", "state": "open"})),
        ("journal entry missing to", lambda s: s["journal"].append(
            {"timestamp": "2026-10-12T11:05:00Z", "unit": None, "field": "x",
             "from": 1})),
        ("journal non-datetime timestamp", lambda s: s["journal"].append(
            {"timestamp": 1760264700, "unit": None, "field": "x", "from": 1, "to": 2})),
        ("session missing role", lambda s: s["units"][0]["sessions"].append(
            {"id": "s1", "name": "n"})),
    ]
    for label, mutate in mutations:
        instance = json.loads(EXAMPLE.read_text())
        mutate(instance)
        ref_errors = list(ref.iter_errors(instance))
        ours = state_tool.validate_schema(instance, schema)
        # when jsonschema (with format checking deps absent) stays silent on
        # the timestamp case we are intentionally stricter — both must agree
        # that *something* is wrong wherever the reference says so
        if ref_errors:
            assert ours, f"{label}: internal validator missed what jsonschema caught"
        else:
            # stricter-but-correct is fine only for the documented date-time
            # format check; assert nothing else diverges silently
            if label == "journal non-datetime timestamp":
                assert ours, "date-time format must be checked internally"
            else:
                assert not ours, f"{label}: internal validator flags a valid instance"


def test_validator_schema_subset_covers_the_schema():
    """The internal validator must implement every keyword the schema
    actually uses — a schema keyword outside the subset is a bug."""
    schema = json.loads((REPO_ROOT / "framework" / "state" /
                         "migration-state.schema.json").read_text())
    supported = {"$schema", "$comment", "title", "type", "required",
                 "properties", "items", "enum", "minimum", "maximum", "format",
                 "default"}  # 'default' is informational — ignored, not validated

    def walk(node, path):
        if isinstance(node, dict):
            for key in node:
                if key in ("properties", "items", "enum", "required", "type",
                           "minimum", "maximum", "format", "$schema", "$comment",
                           "title"):
                    pass
                if key not in supported and not key.startswith("$"):
                    raise AssertionError(f"unsupported schema keyword {key!r} at {path}")
            for key, value in node.items():
                if key in ("properties",):
                    for k2, v2 in value.items():
                        walk(v2, f"{path}.{k2}")
                elif key == "items":
                    walk(value, f"{path}[]")
        return

    walk(schema, "$")
