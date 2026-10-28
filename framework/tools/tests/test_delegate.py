"""delegate.py acceptance tests (issue #33).

Covers the stage-4 candidate runner's contract: per-candidate partitioned
files and assignment briefs, the bounded repair loop with VERBATIM gate
feedback ({feedback_file}), the mechanical import gate, compile-then-oracle
ranking (a real oracle.py judge run per gate-green candidate, through a
scaffold.py manifest whose candidate adapter is retargeted per candidate),
the ranked table + winner, and — the role-separation rule — that the tool
NEVER writes the state file while emitting the verdict pack + ready-to-run
state.py commands the orchestrator records with.

No cargo/maturin in CI: gates and the wheel build are stubbed with
deterministic scripts (the gate emits a cargo-style ``-->`` span so error
attribution is exercised); the "wheel" modules are plain python files the
scaffolded candidate adapter imports — the oracle path is fully real.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

from conftest import TOOLS, run_tool

PORTED = "// PORTED: ready"
GATE_ERROR = "error[E0999]: PORTED marker missing"

AGENT_SCRIPT = """\
import sys
from pathlib import Path

mode, cand, rust, feedback, round_no = sys.argv[1:6]
round_no = int(round_no)
good = "use serde_json::Value;\\n// Ported from src/demo/foo.py (MIT)\\n"
good += %r + "\\n"
bad = good.replace(%r + "\\n", "")
if round_no > 0:  # the repair signal must arrive verbatim, or refuse
    fb = Path(feedback).read_text()
    assert %r in fb, "gate output was not fed back VERBATIM"
if mode == "repair":  # per-candidate behaviors for the e2e scenario
    selector = {"c1": "repair", "c2": "hopeless", "c3": "always"}[cand]
else:
    selector = mode
body = {"repair": bad if round_no == 0 else good,
        "always": good,
        "hopeless": bad}[selector]
Path(rust).write_text(body)
""" % (PORTED, PORTED, GATE_ERROR)

GATE_SCRIPT = """\
import sys
from pathlib import Path

rust, rel = sys.argv[1], sys.argv[2]
try:
    text = Path(rust).read_text()
except OSError:
    print("{}: candidate file not written".format(%r))
    print("  --> {}:1:1".format(rel))
    sys.exit(1)
if %r not in text:
    print(%r)
    print("  --> {}:1:1".format(rel))
    sys.exit(1)
""" % (GATE_ERROR, PORTED, GATE_ERROR)

DUP_PY = ('"""foo.\n\nInvariant: negative n raises ValueError\n"""\n'
          "\n"
          "\n"
          "def dup(n: int) -> list[int]:\n"
          '    if n < 0:\n'
          '        raise ValueError("negative")\n'
          "    return [n, n]\n")


def _world(tmp_path, *, max_rounds=1):
    """A one-unit target project + state file, scaffolded for judging:
    reference adapter over the live python, candidate adapter with the
    CANDIDATE_MODULE placeholder, seeded corpus (the real issue-#32 path).
    Returns (proj, state, runs)."""
    proj = tmp_path / "proj"
    (proj / "src" / "demo").mkdir(parents=True)
    (proj / "src" / "demo" / "foo.py").write_text(DUP_PY)
    (proj / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n\n'
        "[dependencies]\nserde_json = \"1\"\npyo3 = \"0.22\"\n")
    # stand-in wheel modules: the candidates' frozen APIs as importable
    # modules under PROJECT_ROOT/src (what the scaffolded adapter imports)
    (proj / "src" / "wheel_c1.py").write_text(
        "def dup(n: int) -> list[int]:\n"
        '    if n < 0:\n        raise ValueError("negative")\n'
        "    return [n, n]\n")
    (proj / "src" / "wheel_c3.py").write_text(   # the divergent candidate
        "def dup(n: int) -> list[int]:\n    return [n]\n")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "project": {"name": "demo", "python_path": "src/demo"}, "stage": 4,
        "units": [{"id": "u-foo", "python_path": "src/demo/foo.py",
                   "rust_path": "src/u_foo.rs", "deps": [], "order": 1,
                   "status": "pending",
                   "budget": {"max_rounds": max_rounds}}],
        "human_gates": [], "journal": []}))
    runs = tmp_path / "runs"
    return proj, state, runs


def _scripts(tmp_path):
    agent = tmp_path / "agent.py"
    agent.write_text(AGENT_SCRIPT)
    gate = tmp_path / "gate.py"
    gate.write_text(GATE_SCRIPT)
    agent_cmd = f"{sys.executable} {agent} {{mode}} {{candidate}} " \
                f"{{rust_file}} {{feedback_file}} {{round}}"
    gate_cmd = f"{sys.executable} {gate} {{rust_file}} {{rust_file_rel}}"
    return agent_cmd, gate_cmd


def _scaffold(monkeypatch, capsys, state, proj, runs):
    code, out = run_tool(monkeypatch, capsys, "scaffold.py", [
        "scaffold", str(state), "--unit", "u-foo", "--root", str(proj),
        "--out", str(runs), "--seed", "7", "--rows", "6"])
    assert code == 0, out["summary"]["human"]
    return runs / "manifests" / "u-foo.json"


def _run(monkeypatch, capsys, state, proj, agent_cmd, gate_cmd, manifest,
         *extra, mode="repair", no_build=True):
    argv = ["run-candidates", "--state", str(state), "--unit", "u-foo",
            "--agent-cmd", agent_cmd.replace("{mode}", mode),
            "--target-root", str(proj), "--gate", gate_cmd,
            "--max-rounds", "1", "--agent-timeout", "60",
            "--gate-timeout", "30", "--judge-timeout", "60"]
    if manifest is not None:
        argv += ["--manifest", str(manifest),
                 "--candidate-module", "wheel_{candidate}"]
    if no_build:
        argv.append("--no-build")
    return run_tool(monkeypatch, capsys, "delegate.py", argv + list(extra))


# ------------------------------------------------------------- the loop ----

def test_end_to_end_scaffold_to_verdict_pack(monkeypatch, capsys, tmp_path):
    proj, state, runs = _world(tmp_path)
    manifest = _scaffold(monkeypatch, capsys, state, proj, runs)
    agent_cmd, gate_cmd = _scripts(tmp_path)
    before = state.read_bytes()

    code, out = _run(monkeypatch, capsys, state, proj, agent_cmd, gate_cmd,
                     manifest, "--k", "3")

    assert code == 0, out["summary"]["human"]
    assert out["winner"] == "c1" and out["winner_verdict"] == "equivalent"
    assert out["state_written"] is False
    assert state.read_bytes() == before          # role separation: read-only

    by_cand = {c["candidate"]: c for c in out["candidates"]}
    # c1: round 0 fails the gate, repair round reads the VERBATIM feedback
    # (the fake agent asserts it, and would exit 1 otherwise) and goes green
    c1 = by_cand["c1"]
    assert c1["gate_green"] and c1["rounds_used"] == 1
    assert c1["agent_invocations"] == 2
    assert c1["rounds"][0]["gate_green"] is False
    assert c1["rounds"][0]["own_error_spans"] == 1    # `--> src/u_foo_c1.rs`
    assert c1["judge"]["judged"] and c1["judge"]["verdict"] == "equivalent"
    assert c1["judge"]["counts"]["equivalent"] == c1["judge"]["total"]
    assert c1["judge"]["verdicts_digest"]               # the real oracle ran
    assert c1["judge"]["wheel_module"] == "wheel_c1"
    # c2: never green within MAX_ROUNDS=1 -> bounded stall, never judged
    c2 = by_cand["c2"]
    assert not c2["gate_green"] and c2["agent_invocations"] == 2
    assert c2["rounds_used"] == 1 and "judge" not in c2
    assert GATE_ERROR in c2["stall"]["gate_output"]     # exact failing diff
    # c3: green immediately, judged divergent by the oracle (not the model)
    c3 = by_cand["c3"]
    assert c3["gate_green"] and c3["rounds_used"] == 0
    assert c3["judge"]["verdict"] == "divergent"
    assert c3["judge"]["counts"]["divergent"] == c3["judge"]["total"]
    assert c3["judge"]["diff_kind_histogram"]
    # ranking: equivalent > divergent > red — c1, c3, c2
    assert [c["candidate"] for c in out["candidates"]] == ["c1", "c3", "c2"]
    assert "WINNER" in out["summary"]["human"]

    # partitioning: per-candidate files + briefs; a brief hands the delegate
    # only its own file + contract note + python source + the role sheet
    for cand, rust in (("c1", "src/u_foo_c1.rs"), ("c2", "src/u_foo_c2.rs"),
                       ("c3", "src/u_foo_c3.rs")):
        assert (proj / rust).exists()
        assert (tmp_path / "delegates" / "u-foo" /
                f"brief-{cand}.md").exists()
    brief_c1 = (tmp_path / "delegates" / "u-foo" /
                "brief-c1.md").read_text()
    assert "roles/translator.md" in brief_c1
    assert "src/u_foo_c1.rs" in brief_c1 and "u_foo_c2" not in brief_c1
    assert "src/demo/foo.py" in brief_c1
    assert "MAX_ROUNDS = 1" in brief_c1

    # derived judge artifacts land next to the scaffolded manifest and
    # retarget the adapter at each candidate's wheel module
    assert (runs / "manifests" / "u-foo.c1.json").exists()
    assert "wheel_c1" in (runs / "manifests" / "u_foo_cand_c1.py").read_text()

    # the verdict pack lands on disk (restartable, issue #25)
    pack = tmp_path / "delegates" / "u-foo" / "verdict-pack.json"
    assert json.loads(pack.read_text())["winner"] == "c1"

    # sessions[] entries follow the schema's naming convention
    sessions = {s["name"]: s for s in out["state_sessions"]}
    assert "unit-u-foo-translator-c1" in sessions
    assert all(s["role"] == "translator" for s in sessions.values())

    # and the suggested state.py commands actually work — the orchestrator
    # records the pack through the only writer (parity, rust_path, mark)
    cmds = out["state_commands"]
    assert any("parity=" in c for c in cmds)
    assert any("--status ported" in c for c in cmds)
    state_py = str(TOOLS / "state.py")
    for c in cmds:
        argv = shlex.split(c)
        argv[argv.index("framework/tools/state.py")] = state_py
        rc, res = run_tool(monkeypatch, capsys, "state.py",
                           argv[argv.index(state_py) + 1:])
        assert rc == 0, res
    recorded = json.loads(state.read_text())
    unit = next(u for u in recorded["units"] if u["id"] == "u-foo")
    assert unit["status"] == "ported"
    assert unit["parity"]["last_verdict"] == "equivalent"
    assert unit["parity"]["equivalent"] == unit["parity"]["runs"]
    assert unit["rust_path"].endswith("u_foo_c1.rs")


def test_divergent_winner_is_findings_and_suggests_escalation(
        monkeypatch, capsys, tmp_path):
    proj, state, runs = _world(tmp_path)
    manifest = _scaffold(monkeypatch, capsys, state, proj, runs)
    agent_cmd, gate_cmd = _scripts(tmp_path)
    # c3's wheel module diverges and it is the only candidate RUN
    code, out = _run(monkeypatch, capsys, state, proj, agent_cmd, gate_cmd,
                     manifest, "--k", "3", "--only", "c3")
    assert code == 2                                   # divergence = findings
    assert out["winner"] == "c3"
    assert out["winner_verdict"] == "divergent"
    assert any("--status escalated" in c for c in out["state_commands"])
    assert not any("--status ported" in c for c in out["state_commands"])


def test_bounded_rounds_exhaustion_records_the_stall(monkeypatch, capsys,
                                                     tmp_path):
    proj, state, runs = _world(tmp_path, max_rounds=2)
    agent_cmd, gate_cmd = _scripts(tmp_path)
    code, out = _run(monkeypatch, capsys, state, proj, agent_cmd, gate_cmd,
                     None, "--max-rounds", "2", mode="hopeless")
    assert code == 2 and out["winner"] is None
    assert out["max_rounds"] == 2                      # from the unit budget
    c1 = out["candidates"][0]
    assert c1["agent_invocations"] == 3                # initial + 2 repairs
    assert c1["rounds_used"] == 2 and not c1["gate_green"]
    assert GATE_ERROR in c1["stall"]["gate_output"]
    assert any("--status escalated" in c for c in out["state_commands"])


# ---------------------------------------------------------- import gate ----

def test_import_gate_flags_unknown_crates_only(monkeypatch, capsys, tmp_path):
    proj, state, runs = _world(tmp_path)
    agent = tmp_path / "agent_imports.py"
    agent.write_text(
        "import sys\nfrom pathlib import Path\n"
        "rust = sys.argv[2]\n"
        'Path(rust).write_text("use serde_json::Value;\\n'
        'use mystery_box::Thing;\\n'
        '" + %r + "\\n")\n' % PORTED)
    agent_cmd = f"{sys.executable} {agent} x {{rust_file}} {{feedback_file}}"
    code, out = _run(monkeypatch, capsys, state, proj, agent_cmd, "true", None)
    assert code == 2                                    # findings: gate red
    c1 = out["candidates"][0]
    findings = " ".join(c1["import_findings"])
    assert "mystery_box" in findings                    # unknown -> flagged
    assert "serde_json" not in findings                 # Cargo.toml dep -> ok
    assert "pyo3" not in findings                       # (also a dep)
    # the finding is repair signal too: it rides the feedback file verbatim
    fb = tmp_path / "delegates" / "u-foo" / "feedback-c1.txt"
    assert "mystery_box" in fb.read_text()
    assert c1["rounds"][0]["gates"][0]["cmd"] == "<mechanical import gate>"


def test_import_gate_allows_approved_substitutions(monkeypatch, capsys,
                                                   tmp_path):
    proj, state, runs = _world(tmp_path)
    subs = tmp_path / "subs.json"
    subs.write_text(json.dumps({"substitutions": [
        {"python": "requests", "rust": ["reqwest"],
         "verified": {"reqwest": "2026-09-08"}}]}))
    agent = tmp_path / "agent_subs.py"
    agent.write_text(
        "import sys\nfrom pathlib import Path\n"
        "rust = sys.argv[2]\n"
        'Path(rust).write_text("use reqwest::Client;\\n" + %r + "\\n")\n'
        % PORTED)
    agent_cmd = f"{sys.executable} {agent} x {{rust_file}} {{feedback_file}}"
    code, out = _run(monkeypatch, capsys, state, proj, agent_cmd, "true",
                     None, "--substitutions", str(subs))
    # gate green (import gate passes on the approved substitution) but no
    # oracle evidence was produced -> findings-class exit, winner unjudged
    assert code == 2 and out["winner"] == "c1"
    assert out["candidates"][0]["gate_green"] is True
    assert not out["candidates"][0]["judge"]["judged"]


# ------------------------------------------------------ refusals/usage ----

def test_agent_cmd_must_wire_the_feedback_file(monkeypatch, capsys, tmp_path):
    proj, state, runs = _world(tmp_path)
    code, out = _run(monkeypatch, capsys, state, proj, "true", "true", None,
                     "--agent-cmd", "agent-without-placeholder")
    assert code == 1
    assert out["error"]["kind"] == "usage"
    assert "{feedback_file}" in out["error"]["detail"]


def test_unknown_placeholder_is_a_usage_envelope(monkeypatch, capsys, tmp_path):
    proj, state, runs = _world(tmp_path)
    code, out = _run(monkeypatch, capsys, state, proj,
                     "agent {rustfile} {feedback_file}", "true", None)
    assert code == 1 and out["error"]["kind"] == "usage"
    assert "{rust_file}" in out["error"]["detail"]


def test_unknown_unit_is_a_usage_envelope(monkeypatch, capsys, tmp_path):
    proj, state, runs = _world(tmp_path)
    code, out = run_tool(monkeypatch, capsys, "delegate.py", [
        "run-candidates", "--state", str(state), "--unit", "u-nope",
        "--agent-cmd", "x {feedback_file}", "--target-root", str(proj)])
    assert code == 1 and out["error"]["kind"] == "usage"
    assert "u-nope" in out["error"]["detail"]


def test_budget_exhausted_refuses_to_run_over_budget(monkeypatch, capsys,
                                                     tmp_path):
    proj, state, runs = _world(tmp_path)
    st = json.loads(state.read_text())
    st["units"][0]["budget"]["exhausted"] = True
    state.write_text(json.dumps(st))
    code, out = run_tool(monkeypatch, capsys, "delegate.py", [
        "run-candidates", "--state", str(state), "--unit", "u-foo",
        "--agent-cmd", "x {feedback_file}", "--target-root", str(proj)])
    assert code == 2 and "exhausted" in out["error"]


# ------------------------------------------------------- restartability ----

def test_dry_run_derives_the_plan_without_writes(monkeypatch, capsys, tmp_path):
    proj, state, runs = _world(tmp_path)
    code, out = run_tool(monkeypatch, capsys, "delegate.py", [
        "run-candidates", "--state", str(state), "--unit", "u-foo",
        "--target-root", str(proj), "--dry-run"])
    assert code == 0 and out["dry_run"] is True
    assert out["max_rounds"] == 1                       # from the unit budget
    assert out["gates"][0] == "<mechanical import gate>"
    assert [c["candidate"] for c in out["candidates"]] == ["c1", "c2", "c3"]
    assert out["candidates"][0]["rust_file_rel"] == "src/u_foo_c1.rs"
    # no agent needed, and nothing written anywhere
    assert not (tmp_path / "delegates").exists()


def test_only_restarts_a_subset_mid_loop(monkeypatch, capsys, tmp_path):
    proj, state, runs = _world(tmp_path)
    agent_cmd, gate_cmd = _scripts(tmp_path)
    code, out = _run(monkeypatch, capsys, state, proj, agent_cmd, gate_cmd,
                     None, "--k", "3", "--only", "c2", mode="always")
    assert code == 2 and out["winner"] == "c2"          # green but unjudged
    assert [c["candidate"] for c in out["candidates"]] == ["c2"]
    assert out["candidates"][0]["agent_invocations"] == 1
    plan = {p["candidate"]: p["run"] for p in out["plan"]}
    assert plan == {"c1": False, "c2": True, "c3": False}
    # only c2's brief exists — c1/c3 were not touched
    delegates = tmp_path / "delegates" / "u-foo"
    assert (delegates / "brief-c2.md").exists()
    assert not (delegates / "brief-c1.md").exists()
    skip = out["candidates"][0]["judge"]["skip_reason"]
    assert "manifest" in skip or "candidate-module" in skip


def test_build_failure_is_a_setup_outcome_never_judged(monkeypatch, capsys,
                                                       tmp_path):
    proj, state, runs = _world(tmp_path)
    manifest = _scaffold(monkeypatch, capsys, state, proj, runs)
    agent_cmd, gate_cmd = _scripts(tmp_path)
    code, out = _run(monkeypatch, capsys, state, proj, agent_cmd, gate_cmd,
                     manifest, "--k", "1", "--only", "c1", "--build-cmd",
                     "false", "--build-timeout", "30", mode="always",
                     no_build=False)
    assert code == 4                                    # setup: no wheel
    assert out["build"]["failed"] and out["build"]["exit_code"] == 1
    assert out["winner"] == "c1"
    assert "judge" not in out["candidates"][0]          # never judged


def test_candidate_paths_follow_the_recorded_rust_dir(monkeypatch, capsys,
                                                      tmp_path):
    proj, state, runs = _world(tmp_path)
    st = json.loads(state.read_text())
    st["units"][0]["rust_path"] = "crates/demo-core/src/units/u_foo.rs"
    state.write_text(json.dumps(st))
    code, out = run_tool(monkeypatch, capsys, "delegate.py", [
        "run-candidates", "--state", str(state), "--unit", "u-foo",
        "--target-root", str(proj), "--dry-run"])
    assert code == 0
    assert out["candidates"][0]["rust_file_rel"] == \
        "crates/demo-core/src/units/u_foo_c1.rs"


def test_cargo_dep_parser_handles_target_tables_and_dashes(tmp_path):
    """The import gate's allowlist must see deps declared anywhere in
    Cargo.toml — inline tables, [dependencies.x] tables (dashed names
    normalize to the rust identifier spelling), target-gated tables —
    or real dependencies would gate-fail as 'hallucinated'."""
    import delegate
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "my-crate"\n\n[dependencies]\nserde_json = "1"\n'
        "\n[target.'cfg(unix)'.dependencies]\nnix = \"0.28\"\n\n"
        '[dev-dependencies]\npytest-style = "1"\n\n'
        '[dependencies.nested-table]\nversion = "2"\n')
    names, note = delegate._cargo_dep_names(tmp_path)
    assert note is None
    assert {"my_crate", "serde_json", "nix", "pytest_style",
            "nested_table"} <= names
    allow = {"std", "core", "alloc"} | names
    (tmp_path / "s.rs").write_text(
        "use nested_table::N;\nuse nix::unistd;\nuse made_up::X;\n")
    findings = delegate._import_findings(tmp_path / "s.rs", allow)
    assert len(findings) == 1 and "made_up" in findings[0]


def test_state_sessions_follow_the_naming_convention(monkeypatch, capsys,
                                                     tmp_path):
    proj, state, runs = _world(tmp_path)
    agent_cmd, gate_cmd = _scripts(tmp_path)
    code, out = _run(monkeypatch, capsys, state, proj, agent_cmd, gate_cmd,
                     None, mode="always")
    assert out["state_sessions"] == [
        {"id": f"local-u-foo-c{i}",
         "name": f"unit-u-foo-translator-c{i}",
         "role": "translator",
         "outcome": "gate_green=True rounds=0"} for i in (1, 2, 3)]


# -------------------------------------------------- build isolation (#55) --

ISO_GATE_SCRIPT = """\
import os
import sys
from pathlib import Path

rust = Path(sys.argv[1]).resolve()
root = Path(sys.argv[2]).resolve()
assert Path.cwd().resolve() == root, "gate cwd != the crate root handed in"
lib_lines = [l.strip() for l in
             (rust.parent / "lib.rs").read_text().splitlines()]
if os.environ.get("FAIL_FOR") and rust.name.startswith(
        "u_foo_" + os.environ["FAIL_FOR"]):
    print("error[E0555]: injected failure for " + rust.name)
    print("  --> {}:1:1".format(rust.name))
    sys.exit(1)
if os.environ.get("ISO_EXPECT") == "1":
    assert os.environ.get("CARGO_TARGET_DIR"), \\
        "isolated gate lost its CARGO_TARGET_DIR"
    siblings = sorted(p.name for p in rust.parent.glob("u_foo_c*.rs")
                      if p != rust)
    assert not siblings, "sibling candidates leaked into the sandbox: %r" % siblings
    own = "mod {};".format(rust.stem)
    assert own in lib_lines, "kept candidate's wiring was stripped"
    leftover = [l for l in lib_lines
                if "mod u_foo_c" in l and l != own]
    assert not leftover, "sibling mod lines leaked: %r" % leftover
else:
    assert not os.environ.get("CARGO_TARGET_DIR"), \\
        "shared mode must not set CARGO_TARGET_DIR"
    siblings = sorted(p.name for p in rust.parent.glob("u_foo_c*.rs")
                      if p != rust)
    assert siblings == ["u_foo_c2.rs", "u_foo_c3.rs"], \
        "shared mode must see the real crate as-is: %r" % siblings
    assert "mod u_foo_c2" in " ".join(lib_lines)
    assert "mod u_foo_c3" in " ".join(lib_lines)
print("iso-gate ok")
"""

ISO_BUILD_SCRIPT = """\
import json
import os
import sys
from pathlib import Path

src = Path.cwd() / "src"
Path(sys.argv[1]).write_text(json.dumps({
    "cwd": str(Path.cwd().resolve()),
    "cargo_target_dir": os.environ.get("CARGO_TARGET_DIR"),
    "candidates": sorted(p.name for p in src.glob("u_foo_c*.rs")),
    "lib_rs": (src / "lib.rs").read_text(),
}))
"""


def _contended_world(tmp_path):
    """A crate whose wiring already declares all K candidates, with two
    STALLED siblings left behind by an earlier run (half-written files) —
    the issue #55 contention state isolation must hide from the gates."""
    proj, state, runs = _world(tmp_path)
    (proj / "src" / "lib.rs").write_text(
        "mod u_foo_c1;\nmod u_foo_c2;\nmod u_foo_c3;\n")
    (proj / "src" / "u_foo_c2.rs").write_text("pub fn broken( {  // stalled\n")
    (proj / "src" / "u_foo_c3.rs").write_text("stalled sibling, not even rust\n")
    (proj / "target" / "debug").mkdir(parents=True)
    (proj / "target" / "debug" / "leak.bin").write_bytes(b"x")
    return proj, state, runs


def test_gates_run_in_a_per_candidate_sandbox(monkeypatch, capsys, tmp_path):
    """The default: each candidate's gates run in a sandbox copy of the
    crate carrying ONLY that candidate's module (file + mod line), under
    the unit's own CARGO_TARGET_DIR — a stalled sibling's half-written
    file cannot fail this candidate's verify loop, and the REAL crate is
    never mutated (it stays the delegates' authoritative tree)."""
    proj, state, runs = _contended_world(tmp_path)
    agent = tmp_path / "agent.py"
    agent.write_text(AGENT_SCRIPT)
    gate = tmp_path / "iso_gate.py"
    gate.write_text(ISO_GATE_SCRIPT)
    agent_cmd = f"{sys.executable} {agent} always {{candidate}} " \
                f"{{rust_file}} {{feedback_file}} {{round}}"
    gate_cmd = f"{sys.executable} {gate} {{rust_file}} {{target_root}}"
    monkeypatch.setenv("ISO_EXPECT", "1")

    code, out = _run(monkeypatch, capsys, state, proj, agent_cmd, gate_cmd,
                     None, "--only", "c1", mode="always")
    assert code == 2 and out["winner"] == "c1"   # green but unjudged
    c1 = out["candidates"][0]
    assert c1["gate_green"] and c1["rounds_used"] == 0
    assert out["build_isolation"]["mode"] == "sandbox"

    # the sandbox: outside the crate, siblings + their mod lines stripped,
    # the kept candidate's wiring intact, heavy dirs (target/) not copied
    sandbox = Path(c1["sandbox"]["root"])
    assert proj not in sandbox.parents and sandbox.exists()
    assert c1["sandbox"]["kept"] == ["c1"]
    assert c1["sandbox"]["stripped"] == ["c2", "c3"]
    assert sorted(p.name for p in (sandbox / "src").glob("u_foo_c*.rs")) \
        == ["u_foo_c1.rs"]
    assert not (sandbox / "target").exists()
    lib = (sandbox / "src" / "lib.rs").read_text()
    assert "mod u_foo_c1;" in lib
    assert "mod u_foo_c2" not in lib and "mod u_foo_c3" not in lib
    cargo_target = Path(out["build_isolation"]["cargo_target_dir"])
    assert cargo_target.is_dir() and proj not in cargo_target.parents

    # the REAL crate is untouched: stalled siblings and their wiring remain
    assert "mod u_foo_c2;" in (proj / "src" / "lib.rs").read_text()
    assert (proj / "src" / "u_foo_c2.rs").exists()
    assert (proj / "target" / "debug" / "leak.bin").exists()
    # the sandbox belongs to the round record (evidence in the pack)
    assert c1["rounds"][0]["sandbox"]["root"] == c1["sandbox"]["root"]


def test_wheel_build_sandbox_carries_only_green_candidates(
        monkeypatch, capsys, tmp_path):
    """The wheel build gets its own sandbox: only this run's gate-green
    candidates ride it — a stalled candidate's half-written module cannot
    fail the winners' wheel build."""
    proj, state, runs = _contended_world(tmp_path)
    agent = tmp_path / "agent.py"
    agent.write_text(AGENT_SCRIPT)
    gate = tmp_path / "iso_gate.py"
    gate.write_text(ISO_GATE_SCRIPT)
    build_rec = tmp_path / "build-rec.json"
    build = tmp_path / "iso_build.py"
    build.write_text(ISO_BUILD_SCRIPT)
    agent_cmd = f"{sys.executable} {agent} always {{candidate}} " \
                f"{{rust_file}} {{feedback_file}} {{round}}"
    gate_cmd = f"{sys.executable} {gate} {{rust_file}} {{target_root}}"
    build_cmd = f"{sys.executable} {build} {build_rec}"
    monkeypatch.setenv("ISO_EXPECT", "1")
    monkeypatch.setenv("FAIL_FOR", "c2")        # c2 stalls red, c1/c3 green

    code, out = _run(monkeypatch, capsys, state, proj, agent_cmd, gate_cmd,
                     None, "--build-cmd", build_cmd, "--build-timeout", "30",
                     mode="always", no_build=False)
    assert code == 2 and out["winner"] == "c1"
    assert not out["build"]["failed"]
    sandbox = out["build"]["sandbox"]
    assert sandbox["kept"] == ["c1", "c3"] and sandbox["stripped"] == ["c2"]
    rec = json.loads(build_rec.read_text())
    assert rec["cwd"] == sandbox["root"]        # build ran IN the sandbox
    assert rec["candidates"] == ["u_foo_c1.rs", "u_foo_c3.rs"]
    assert "mod u_foo_c2" not in rec["lib_rs"]
    assert "mod u_foo_c1;" in rec["lib_rs"] and "mod u_foo_c3;" in rec["lib_rs"]
    assert rec["cargo_target_dir"] == out["build"]["cargo_target_dir"]
    # and the real crate keeps every candidate exactly as the delegates
    # left them — isolation is read-only over the authoritative tree
    assert "mod u_foo_c2;" in (proj / "src" / "lib.rs").read_text()
    assert (proj / "src" / "u_foo_c2.rs").exists()


def test_no_isolate_builds_shares_the_real_crate(monkeypatch, capsys,
                                                 tmp_path):
    """--no-isolate-builds restores the pre-#55 behavior: gates run in the
    REAL crate with the ambient target dir — siblings and all."""
    proj, state, runs = _contended_world(tmp_path)
    agent = tmp_path / "agent.py"
    agent.write_text(AGENT_SCRIPT)
    gate = tmp_path / "iso_gate.py"
    gate.write_text(ISO_GATE_SCRIPT)
    agent_cmd = f"{sys.executable} {agent} always {{candidate}} " \
                f"{{rust_file}} {{feedback_file}} {{round}}"
    gate_cmd = f"{sys.executable} {gate} {{rust_file}} {{target_root}}"
    monkeypatch.delenv("ISO_EXPECT", raising=False)

    code, out = _run(monkeypatch, capsys, state, proj, agent_cmd, gate_cmd,
                     None, "--only", "c1", "--no-isolate-builds",
                     mode="always")
    assert code == 2 and out["winner"] == "c1"
    assert out["build_isolation"] == {"mode": "shared"}
    assert "sandbox" not in out["candidates"][0]   # nothing was copied
    assert not (tmp_path / "delegates" / "u-foo" / "sandboxes").exists()


def test_briefs_and_feedback_carry_the_not_yours_rule(monkeypatch, capsys,
                                                      tmp_path):
    """The issue #55 ask, captured in the artifacts: the brief documents
    sandboxed gates, and repair feedback repeats translator rule 9 —
    compile errors in files that are not yours are not yours to fix."""
    proj, state, runs = _contended_world(tmp_path)
    agent_cmd, gate_cmd = _scripts(tmp_path)
    code, out = _run(monkeypatch, capsys, state, proj, agent_cmd, gate_cmd,
                     None, mode="repair")
    assert code == 2
    brief = (tmp_path / "delegates" / "u-foo" / "brief-c1.md").read_text()
    assert "Rules 1-9" in brief
    assert "sandbox" in brief and "CARGO_TARGET_DIR" in brief
    fb = (tmp_path / "delegates" / "u-foo" / "feedback-c1.txt").read_text()
    assert "not yours to fix" in fb and "report and wait" in fb


def test_strip_mod_lines_variants():
    """The sandbox's mod-line stripper: any visibility spelling, indented,
    with attributes above (a dangling #[..] is itself an error), and it
    must NOT touch other modules' lines or attribute-less kept code."""
    import delegate
    text = ("use x;\n"
            "#[cfg(feature = \"c2\")]\n"
            "pub(crate) mod u_foo_c2;\n"
            "pub mod u_foo_c3;\n"
            "mod u_foo_c1;\n"
            "fn main() {}\n")
    out = delegate._strip_mod_lines(text, {"u_foo_c2", "u_foo_c3"})
    assert out == "use x;\nmod u_foo_c1;\nfn main() {}\n"
