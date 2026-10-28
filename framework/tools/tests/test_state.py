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


def _seed_gates(state_file, *gates):
    """Give a mini_state fixture some gate history (it ships empty)."""
    st = _load(state_file)
    st["human_gates"] = list(gates)
    state_file.write_text(json.dumps(st))


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


def test_set_mixes_plain_and_json_fields_in_one_invocation(
        monkeypatch, capsys, mini_state):
    code, out = run_tool(monkeypatch, capsys, "state.py", [
        "set", str(mini_state),
        "--field", "project.workload=some plain string",
        "--field", '@json:go_no_go={"decision": "pending", "scope": ["u1"]}'])
    assert code == 0
    st = _load(mini_state)
    assert st["project"]["workload"] == "some plain string"
    assert st["go_no_go"] == {"decision": "pending", "scope": ["u1"]}
    assert len(st["journal"]) == 2


def test_set_json_prefix_is_exact_typing(monkeypatch, capsys, mini_state):
    code, _ = run_tool(monkeypatch, capsys, "state.py", [
        "set", str(mini_state), "--field", '@json:project.workload="4"'])
    assert code == 0
    assert _load(mini_state)["project"]["workload"] == "4"


def test_set_json_prefix_rejects_non_json_without_landing(
        monkeypatch, capsys, mini_state):
    before = _load(mini_state)
    code, out = run_tool(monkeypatch, capsys, "state.py", [
        "set", str(mini_state), "--field", "@json:project.workload=oops"])
    assert code == 2 and "not valid JSON" in out["error"]
    assert _load(mini_state) == before


def test_set_json_value_flag_keeps_whole_invocation_strictness(
        monkeypatch, capsys, mini_state):
    code, out = run_tool(monkeypatch, capsys, "state.py", [
        "set", str(mini_state), "--field", "project.workload=oops",
        "--json-value"])
    assert code == 2 and "not valid JSON" in out["error"]


def test_set_value_position_json_prefix_types_as_strict_json(
        monkeypatch, capsys, mini_state):
    """issue #52: 'path=@json:[...]' (prefix on the value, not the pair) is
    unambiguous intent — it must land typed, not silently degrade to a
    string that later fails schema validation with a confusing type error."""
    code, _ = run_tool(monkeypatch, capsys, "state.py", [
        "set", str(mini_state), "--field", 'go_no_go.scope=@json:["src/bars.py"]'])
    assert code == 0
    assert _load(mini_state)["go_no_go"]["scope"] == ["src/bars.py"]


def test_set_value_position_json_prefix_bad_payload_refuses_with_hint(
        monkeypatch, capsys, mini_state):
    before = _load(mini_state)
    code, out = run_tool(monkeypatch, capsys, "state.py", [
        "set", str(mini_state), "--field", "go_no_go.scope=@json:[oops]"])
    assert code == 2 and "not valid JSON" in out["error"]
    assert "front of the pair" in out["error"]
    assert _load(mini_state) == before


def test_set_literal_json_prefix_string_still_possible(
        monkeypatch, capsys, mini_state):
    """A value that must literally start with '@json:' is quoted JSON under
    the pair-level prefix — the escape hatch stays open."""
    code, _ = run_tool(monkeypatch, capsys, "state.py", [
        "set", str(mini_state),
        "--field", '@json:project.workload="@json:kept literal"'])
    assert code == 0
    assert _load(mini_state)["project"]["workload"] == "@json:kept literal"


def test_set_human_gates_whole_array_refused(monkeypatch, capsys, mini_state):
    """issue #52: replacing the human_gates array clobbers gate history —
    the array only grows through resolve-gate/mark (like journal[])."""
    _seed_gates(mini_state, {"gate": "go_no_go", "state": "approved",
                             "decided_by": "human"})
    before = _load(mini_state)
    code, out = run_tool(monkeypatch, capsys, "state.py", [
        "set", str(mini_state), "--field",
        '@json:human_gates=[{"gate": "redesign_approval", "state": "open"}]'])
    assert code == 2 and "append-only" in out["error"]
    assert "resolve-gate" in out["error"]
    after = _load(mini_state)
    assert after["human_gates"] == before["human_gates"]
    assert after["journal"] == before["journal"]


def test_set_human_gates_whole_array_allowed_while_empty(
        monkeypatch, capsys, mini_state):
    """Seeding a fresh state's (still empty) human_gates stays possible —
    the pipeline tests' bootstrap pattern; the guard protects history, not
    initial seeding."""
    code, _ = run_tool(monkeypatch, capsys, "state.py", [
        "set", str(mini_state), "--json-value", "--field",
        'human_gates=[{"gate": "go_no_go", "state": "open"}]'])
    assert code == 0
    assert _load(mini_state)["human_gates"][0]["gate"] == "go_no_go"


def test_set_human_gates_leaf_field_still_works(monkeypatch, capsys, mini_state):
    """The refusal is whole-array only: per-record edits stay in the set
    vocabulary (human_gates[gate=<name>].<leaf>)."""
    _seed_gates(mini_state, {"gate": "go_no_go", "state": "open"})
    code, _ = run_tool(monkeypatch, capsys, "state.py", [
        "set", str(mini_state),
        "--field", "human_gates[gate=go_no_go].note=scope locked"])
    assert code == 0
    assert _load(mini_state)["human_gates"][0]["note"] == "scope locked"


def test_set_gate_selector_survives_equals_inside_brackets(
        monkeypatch, capsys, mini_state):
    """'[gate=<name>]' itself contains '=' — the path=value split must cut
    at the top-level '=' only, and a value may still contain '='."""
    _seed_gates(mini_state, {"gate": "go_no_go", "state": "open"})
    code, _ = run_tool(monkeypatch, capsys, "state.py", [
        "set", str(mini_state),
        "--field", "human_gates[gate=go_no_go].note=a=b"])
    assert code == 0
    assert _load(mini_state)["human_gates"][0]["note"] == "a=b"


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


def test_mark_escalated_at_l2_auto_resolves_via_policy(monkeypatch, capsys, mini_state):
    # issue #22: at L2+ escalation is defer, not halt — the gate opens and
    # immediately resolves via policy rule ESC-1 (python fallback stays live)
    run_tool(monkeypatch, capsys, "state.py", [
        "set", str(mini_state), "--field", 'autonomy={"level": "L2"}',
        "--json-value"])
    code, out = run_tool(monkeypatch, capsys, "state.py", [
        "mark", str(mini_state), "--unit", "u1", "--status", "escalated",
        "--note", "repro attached"])
    assert code == 0
    st = _load(mini_state)
    gate = [g for g in st["human_gates"] if g["gate"] == "unit_escalation"][0]
    assert gate["state"] == "deferred"
    assert gate["decided_by"] == "policy:ESC-1"
    assert gate["policy_version"]  # journaled decisions name their policy
    assert any("fallback=python" in e for e in gate["evidence"])
    fields = [e["field"] for e in st["journal"]]
    assert "human_gates[gate=unit_escalation].decided_by" in fields


def test_mark_escalated_at_l0_opens_and_waits_unchanged(monkeypatch, capsys, mini_state):
    # no autonomy block = L0 (supervised): the pre-policy behavior stands
    code, _ = run_tool(monkeypatch, capsys, "state.py", [
        "mark", str(mini_state), "--unit", "u1", "--status", "escalated"])
    assert code == 0
    gate = [g for g in _load(mini_state)["human_gates"]
            if g["gate"] == "unit_escalation"][0]
    assert gate["state"] == "open" and "decided_by" not in gate


def test_resolve_gate_records_human_and_policy_decisions(monkeypatch, capsys, mini_state):
    code, _ = run_tool(monkeypatch, capsys, "state.py", [
        "resolve-gate", str(mini_state), "--gate", "go_no_go", "--create",
        "--decision", "approved", "--by", "policy:GO-1",
        "--evidence", "end_to_end_saving_pct=58.0 (bar >= 50.0)"])
    assert code == 0
    gate = [g for g in _load(mini_state)["human_gates"]
            if g["gate"] == "go_no_go"][0]
    assert gate["state"] == "approved"
    assert gate["decided_by"] == "policy:GO-1" and gate["policy_version"]
    # a human decision is recorded just as first-class
    code, _ = run_tool(monkeypatch, capsys, "state.py", [
        "resolve-gate", str(mini_state), "--gate", "scope_change", "--create",
        "--decision", "approved", "--by", "human", "--note", "operator OK"])
    assert code == 0
    gate = [g for g in _load(mini_state)["human_gates"]
            if g["gate"] == "scope_change"][0]
    assert gate["decided_by"] == "human" and "policy_version" not in gate


def test_resolve_gate_refuses_unknown_rules_and_non_policy_ids(
        monkeypatch, capsys, mini_state):
    code, out = run_tool(monkeypatch, capsys, "state.py", [
        "resolve-gate", str(mini_state), "--gate", "redesign_approval",
        "--create", "--decision", "approved", "--by", "policy:NOPE-9"])
    assert code == 2 and "not defined" in out["error"]
    code, out = run_tool(monkeypatch, capsys, "state.py", [
        "resolve-gate", str(mini_state), "--gate", "redesign_approval",
        "--create", "--decision", "approved", "--by", "the-agent-itself"])
    assert code == 2 and "human" in out["error"]


def test_policy_cutover_approval_requires_l3_publish_allowed(
        monkeypatch, capsys, mini_state):
    run_tool(monkeypatch, capsys, "state.py", [
        "set", str(mini_state), "--field",
        'autonomy={"level": "L2", "publish_allowed": false}', "--json-value"])
    code, out = run_tool(monkeypatch, capsys, "state.py", [
        "resolve-gate", str(mini_state), "--gate", "cutover", "--create",
        "--decision", "approved", "--by", "policy:CUT-1"])
    assert code == 2 and "L3" in out["error"]


def test_validate_enforces_gate_resolution_discipline(monkeypatch, capsys, tmp_path):
    def mutated(mutator):
        st = json.loads(EXAMPLE.read_text())
        mutator(st)
        p = tmp_path / "g.json"
        p.write_text(json.dumps(st))
        return run_tool(monkeypatch, capsys, "state.py", ["validate", str(p)])

    # resolved gate without decided_by
    code, out = mutated(lambda s: s["human_gates"][0].pop("decided_by"))
    assert code == 2 and any("decided_by" in e for e in out["invariant_errors"])
    # decided_by naming a well-formed rule id the policy does not define
    code, out = mutated(lambda s: s["human_gates"][0].update(
        decided_by="policy:ZZ-99"))
    assert code == 2 and any("does not define" in e for e in out["invariant_errors"])
    # policy version required on policy decisions (this example's resolved
    # gates are human decisions; make one a policy decision sans version)
    def as_policy_sans_version(s):
        s["human_gates"][0]["decided_by"] = "policy:GO-1"
        s["human_gates"][0].pop("policy_version", None)
    code, out = mutated(as_policy_sans_version)
    assert code == 2 and any("policy_version" in e for e in out["invariant_errors"])
    # publish_allowed without L3 (this example is L1/publish=false; flip both)
    code, out = mutated(lambda s: s["autonomy"].update(
        level="L2", publish_allowed=True))
    assert code == 2 and any("L3" in e for e in out["invariant_errors"])


def test_validate_warns_on_resumed_terminal_run(monkeypatch, capsys, tmp_path):
    st = json.loads(EXAMPLE.read_text())
    st["run_status"] = "complete"
    st["journal"].append({  # the terminal transition itself
        "timestamp": "2026-10-31T10:00:00Z", "unit": None,
        "field": "run_status", "from": "running", "to": "complete",
        "actor": "pipeline.py"})
    st["journal"].append({  # work continued after run_status=complete
        "timestamp": "2026-11-01T10:00:00Z", "unit": None,
        "field": "stage", "from": 4, "to": 5, "actor": "orchestrator"})
    p = tmp_path / "resumed.json"
    p.write_text(json.dumps(st))
    code, out = run_tool(monkeypatch, capsys, "state.py", ["validate", str(p)])
    assert code == 0  # warning, not error — resume can be legitimate
    assert any("resumed after finishing" in w for w in out["warnings"])


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


def test_shim_derives_both_src_layout_spellings(monkeypatch, capsys, mini_state):
    """Issue #53: src-layout targets import under BOTH spellings — the
    shim's python import must try each in turn like the adapters do
    (issue #26), not commit to the bare module name."""
    code, out = run_tool(monkeypatch, capsys, "state.py",
                         ["shim", str(mini_state), "--unit", "u1"])
    assert code == 0
    # src/demo/a.py -> demo.a (canonical) AND src.demo.a
    assert out["python_imports"] == ["demo.a", "src.demo.a"]
    assert out["python_import"] == "demo.a"  # canonical first, back-compat
    for spelling in ("demo.a", "src.demo.a"):
        assert spelling in out["code"]
    compile(out["code"], "shim.py", "exec")
    # an explicit override replaces the derivation wholesale (issue #53)
    code, out = run_tool(monkeypatch, capsys, "state.py", [
        "shim", str(mini_state), "--unit", "u1",
        "--python-import", "demo.a,other.demo.a"])
    assert code == 0
    assert out["python_imports"] == ["demo.a", "other.demo.a"]


def _write_flat_target(tmp_path):
    """A fake src-layout target importable ONLY under the src. spelling,
    plus a flat wheel module exposing the unit's function — the two real
    layouts that broke the shim (issue #53)."""
    pkg = tmp_path / "target"
    demo = pkg / "src" / "demo"
    demo.mkdir(parents=True)
    (demo / "__init__.py").write_text("")
    (demo / "a.py").write_text("def f():\n    return 'py'\n")
    (pkg / "wicksmith_core.py").write_text("def f():\n    return 'rust'\n")
    return pkg


def test_shim_flat_wheel_reexports_and_switches_sides(
        monkeypatch, capsys, tmp_path):
    """Issue #53 flat PyO3 wheels: no per-unit rust module exists — the shim
    must re-export the unit's names from the flat wheel, keeping the env-var
    switching semantics, and fall back across python import spellings."""
    pkg = _write_flat_target(tmp_path)
    monkeypatch.syspath_prepend(str(pkg))
    st = tmp_path / "state.json"
    st.write_text(json.dumps({
        "project": {"name": "demo", "python_path": "src/demo"},
        "stage": 6, "units": [{
            "id": "u1", "python_path": "src/demo/a.py",
            "rust_path": "crates/demo/src/u1.rs", "deps": [], "order": 1,
            "status": "ported",
        }], "human_gates": [], "journal": []}))
    code, out = run_tool(monkeypatch, capsys, "state.py", [
        "shim", str(st), "--unit", "u1", "--flat",
        "--rust-import", "wicksmith_core", "--names", "f"])
    assert code == 0 and out["flat"] is True and out["names"] == ["f"]
    assert out["default_side"] == "rust"
    ns = {}
    exec(compile(out["code"], "shim.py", "exec"), ns)
    monkeypatch.delenv("DEMO_FALLBACK", raising=False)
    # ported + none -> flat rust wheel; PEP 562 dispatch serves `from shim import f`
    assert ns["impl"]().f() == "rust"
    assert ns["__getattr__"]("f")() == "rust"
    # per-unit and global flips stay reversible with no extra state
    monkeypatch.setenv("DEMO_FALLBACK", "u1")
    assert ns["impl"]().f() == "py"   # only src.demo.a exists -> 2nd spelling
    monkeypatch.setenv("DEMO_FALLBACK", "all")
    assert ns["impl"]().f() == "py"
    # unknown names do not silently resolve
    try:
        ns["__getattr__"]("nope")
    except AttributeError:
        pass
    else:
        raise AssertionError("unknown name must raise AttributeError")


def test_shim_flat_wheel_flags_name_drift(monkeypatch, capsys, tmp_path):
    """A name the wheel does not expose is rust-side drift vs the recorded
    rust_names — loud, not a silent fallback (issue #53)."""
    pkg = _write_flat_target(tmp_path)
    monkeypatch.syspath_prepend(str(pkg))
    st = tmp_path / "state.json"
    st.write_text(json.dumps({
        "project": {"name": "demo", "python_path": "src/demo"},
        "stage": 6, "units": [{
            "id": "u1", "python_path": "src/demo/a.py",
            "rust_path": "crates/demo/src/u1.rs", "deps": [], "order": 1,
            "status": "ported",
        }], "human_gates": [], "journal": []}))
    code, out = run_tool(monkeypatch, capsys, "state.py", [
        "shim", str(st), "--unit", "u1", "--flat",
        "--rust-import", "wicksmith_core", "--names", "f,g"])
    assert code == 0
    ns = {}
    exec(compile(out["code"], "shim.py", "exec"), ns)
    monkeypatch.delenv("DEMO_FALLBACK", raising=False)
    try:
        ns["impl"]()
    except AttributeError as e:
        assert "g" in str(e) and "wicksmith_core" in str(e)
    else:
        raise AssertionError("missing wheel name must raise, not fall back")


def test_shim_flat_refuses_without_names_or_module(
        monkeypatch, capsys, mini_state):
    code, out = run_tool(monkeypatch, capsys, "state.py",
                         ["shim", str(mini_state), "--unit", "u1", "--flat"])
    assert code == 2 and "rust_names" in out["error"]
    code, out = run_tool(monkeypatch, capsys, "state.py", [
        "shim", str(mini_state), "--unit", "u1", "--flat", "--names", "f"])
    assert code == 2 and "rust_module" in out["error"]


def test_shim_flat_from_schema_recorded_names(monkeypatch, capsys, mini_state):
    """The schema-recorded rust_module + rust_names drive the flat shim
    with zero extra flags — one source of truth for shim + API-freeze
    check (issue #53)."""
    code, _ = run_tool(monkeypatch, capsys, "state.py", [
        "set", str(mini_state),
        "--field", "units[u1].rust_module=wicksmith_core",
        "--field", '@json:units[u1].rust_names=["f", "g"]'])
    assert code == 0
    code, out = run_tool(monkeypatch, capsys, "state.py",
                         ["shim", str(mini_state), "--unit", "u1"])
    assert code == 0
    assert out["flat"] is True
    assert out["rust_import"] == "wicksmith_core"
    assert out["names"] == ["f", "g"]
    compile(out["code"], "shim.py", "exec")
    # --names / --rust-import still win over the schema (ad-hoc overrides)
    code, out = run_tool(monkeypatch, capsys, "state.py", [
        "shim", str(mini_state), "--unit", "u1",
        "--names", "h", "--rust-import", "other_core"])
    assert code == 0 and out["names"] == ["h"] and out["rust_import"] == "other_core"


def test_migrate_backfills_and_journals(monkeypatch, capsys, tmp_path):
    st = json.loads(EXAMPLE.read_text())
    for u in st["units"]:
        u.pop("budget", None)
    st.pop("budget_ledger", None)
    st.pop("autonomy", None)          # issue #22 backfills
    st.pop("run_status", None)
    for g in st["human_gates"]:
        g.pop("decided_by", None)
    p = tmp_path / "old.json"
    p.write_text(json.dumps(st))
    journal_len_before = len(st.get("journal", []))
    code, out = run_tool(monkeypatch, capsys, "state.py", ["migrate", str(p)])
    assert code == 0
    after = _load(p)
    assert all("budget" in u for u in after["units"])
    assert "budget_ledger" in after
    assert after["autonomy"]["level"] == "L0"   # supervised meaning preserved
    assert after["run_status"] == "pending"
    resolved = [g for g in after["human_gates"] if g["state"] != "open"]
    assert resolved and all(g["decided_by"] == "human" for g in resolved)
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
    # project + stage + autonomy + run_status creation entries
    assert len(st["journal"]) == 4
    # the never-ask default: L2 policy-resolved, publish still disabled
    assert st["autonomy"]["level"] == "L2"
    assert st["autonomy"]["publish_allowed"] is False
    assert st["run_status"] == "pending"
    code, _ = run_tool(monkeypatch, capsys, "state.py", ["validate", str(p)])
    assert code == 0


# -- internal validator vs the jsonschema reference implementation ----------

def test_validator_implements_additional_properties():
    """additionalProperties (schema-per-value form, used by the preflight
    block's checks map — issue #31) constrains exactly the keys properties
    does not cover; false forbids them outright."""
    schema = {"type": "object",
              "properties": {"known": {"type": "integer"}},
              "additionalProperties": {"type": "string"}}
    assert state_tool.validate_schema({"known": 1, "extra": "ok"}, schema) == []
    errs = state_tool.validate_schema({"known": 1, "extra": 2}, schema)
    assert errs and "extra" in errs[0] and "string" in errs[0]
    closed = {"type": "object", "additionalProperties": False}
    errs = state_tool.validate_schema({"sneaky": 1}, closed)
    assert errs and "sneaky" in errs[0]
    # declared properties stay governed by their own schema, not addl
    assert state_tool.validate_schema({"known": 1}, schema) == []


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
        ("bad gate decided_by type", lambda s: s["human_gates"][0].update(
            decided_by=7)),
        ("bad gate state", lambda s: s["human_gates"][0].update(state="awaiting")),
        ("bad autonomy level", lambda s: s["autonomy"].update(level="L4")),
        ("bad run_status", lambda s: s.update(run_status="awaiting_input")),
        ("bad gate evidence item", lambda s: s["human_gates"][0].update(
            evidence=["ok=1", 5])),
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
                 "properties", "additionalProperties", "items", "enum",
                 "minimum", "maximum", "format",
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
                elif key == "additionalProperties" and isinstance(value, dict):
                    walk(value, f"{path}[+]")
        return

    walk(schema, "$")
