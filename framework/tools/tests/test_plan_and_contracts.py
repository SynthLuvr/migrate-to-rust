"""port_plan.py + contract_extract.py acceptance tests (#2 and #3)."""

from __future__ import annotations

import json

from conftest import run_tool


def _project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "src" / "pkg").mkdir(parents=True)
    (proj / "src" / "pkg" / "leaf.py").write_text("import re\n\n"
                                                  "def tok(s):\n    return re.split(r'\\s+', s)\n")
    (proj / "src" / "pkg" / "mid.py").write_text(
        "from pkg.leaf import tok\n\n\ndef words(s):\n    return len(tok(s))\n")
    (proj / "src" / "pkg" / "hot.py").write_text(
        "import numpy\nfrom pkg.mid import words\n\n\ndef loop(n):\n    return words('a') * n\n")
    (proj / "src" / "pkg" / "ioheavy.py").write_text(
        "import socket\n\n\ndef fetch(u):\n    return socket.getaddrinfo(u, 80)\n")
    (proj / "src" / "pkg" / "ca.py").write_text("from pkg.cb import g\n\n\ndef f(x):\n    return g(x)\n")
    (proj / "src" / "pkg" / "cb.py").write_text("from pkg.ca import f\n\n\ndef g(x):\n    return f(x)\n")
    hotspots = tmp_path / "hotspots.json"
    hotspots.write_text(json.dumps([
        {"path": "src/pkg/hot.py", "self_cpu_s": 30.0, "io_wait_s": 0.5, "calls": 9},
        {"path": "src/pkg/mid.py", "self_cpu_s": 12.0, "io_wait_s": 1.0, "calls": 5},
        {"path": "src/pkg/leaf.py", "self_cpu_s": 8.0, "io_wait_s": 0.2, "calls": 12},
        {"path": "src/pkg/ioheavy.py", "self_cpu_s": 2.0, "io_wait_s": 9.0, "calls": 1},
        {"path": "src/pkg/ca.py", "self_cpu_s": 1.0, "io_wait_s": 0.0, "calls": 1},
        {"path": "src/pkg/cb.py", "self_cpu_s": 1.0, "io_wait_s": 0.0, "calls": 1},
    ]))
    return proj, hotspots


def test_plan_ranks_io_bound_last(monkeypatch, capsys, tmp_path):
    proj, hotspots = _project(tmp_path)
    code, out = run_tool(monkeypatch, capsys, "port_plan.py", [
        "plan", "--hotspots", str(hotspots), "--project-root", str(proj)])
    assert code == 2  # cycle present -> findings
    ids = [u["id"] for u in out["units"]]
    io_units = [u for u in out["units"] if u["no_go"]]
    portable = [u for u in out["units"] if not u["no_go"]]
    assert ids[-1] == io_units[0]["id"], "I/O-bound unit must be ranked last"
    assert all(u["priority"] >= 0 for u in portable)
    priorities = [u["priority"] for u in portable]
    assert priorities == sorted(priorities, reverse=True)


def test_plan_detects_cycles_and_flags_them(monkeypatch, capsys, tmp_path):
    proj, hotspots = _project(tmp_path)
    _, out = run_tool(monkeypatch, capsys, "port_plan.py", [
        "plan", "--hotspots", str(hotspots), "--project-root", str(proj)])
    assert out["cycles"], "the ca<->cb import cycle must be detected"
    cyc_units = [u for u in out["units"] if "cycle" in u["risk_flags"]]
    assert {u["id"] for u in cyc_units} == {"u-pkg-ca", "u-pkg-cb"}
    assert all(u["order"] is None for u in cyc_units)


def test_plan_leaf_first_order(monkeypatch, capsys, tmp_path):
    proj, hotspots = _project(tmp_path)
    _, out = run_tool(monkeypatch, capsys, "port_plan.py", [
        "plan", "--hotspots", str(hotspots), "--project-root", str(proj)])
    by_id = {u["id"]: u for u in out["units"]}
    assert by_id["u-pkg-leaf"]["order"] < by_id["u-pkg-mid"]["order"] < \
        by_id["u-pkg-hot"]["order"]
    assert by_id["u-pkg-mid"]["deps"] == ["u-pkg-leaf"]
    assert by_id["u-pkg-hot"]["deps"] == ["u-pkg-mid"]


def test_plan_risk_flags_attached(monkeypatch, capsys, tmp_path):
    proj, hotspots = _project(tmp_path)
    _, out = run_tool(monkeypatch, capsys, "port_plan.py", [
        "plan", "--hotspots", str(hotspots), "--project-root", str(proj)])
    by_id = {u["id"]: u for u in out["units"]}
    assert "c-extension" in by_id["u-pkg-hot"]["risk_flags"]
    assert "io-bound" in by_id["u-pkg-ioheavy"]["risk_flags"]
    assert "threads" not in by_id["u-pkg-leaf"]["risk_flags"]


# ------------------------------------- issue #28: subcommands must compose --

def _quiet_project(tmp_path):
    """Cycle-free two-module project (leaf + importer)."""
    proj = tmp_path / "qproj"
    (proj / "src" / "pkg").mkdir(parents=True)
    (proj / "src" / "pkg" / "leaf.py").write_text("def g(x):\n    return x + 1\n")
    (proj / "src" / "pkg" / "hot.py").write_text(
        "from pkg.leaf import g\n\n\ndef f(n):\n    return g(n) * 2\n")
    return proj


def test_plan_accepts_from_cprofile_envelope(monkeypatch, capsys, tmp_path):
    """`plan --hotspots` must take `from-cprofile`'s output as-is: the
    standard tool envelope ({"tool", "version", "profile", "hotspots": [...]})
    unwraps to the same plan as the bare canonical list (issue #28)."""
    proj = _quiet_project(tmp_path)
    bare = [{"path": "src/pkg/hot.py", "self_cpu_s": 5.0, "calls": 7},
            {"path": "src/pkg/leaf.py", "self_cpu_s": 1.0, "calls": 3}]
    bare_file = tmp_path / "bare.json"
    bare_file.write_text(json.dumps(bare))
    envelope = tmp_path / "envelope.json"
    envelope.write_text(json.dumps({
        "tool": "port_plan.py", "version": 1,
        "profile": str(tmp_path / "run.prof"),
        "hotspots": bare,
        "summary": {"exit_code": 0, "human": "cProfile dump -> 2 file hotspots"},
    }))
    code_a, out_a = run_tool(monkeypatch, capsys, "port_plan.py", [
        "plan", "--hotspots", str(envelope), "--project-root", str(proj)])
    code_b, out_b = run_tool(monkeypatch, capsys, "port_plan.py", [
        "plan", "--hotspots", str(bare_file), "--project-root", str(proj)])
    assert code_a == code_b == 0
    assert out_a["units"] == out_b["units"]


def test_plan_rejects_bad_hotspot_shapes(monkeypatch, capsys, tmp_path):
    proj = _quiet_project(tmp_path)
    for bad in ('{"tool": "port_plan.py", "version": 1, "hotspots": []}',
                '{"tool": "port_plan.py", "version": 1}',
                '{"hotspots": {"path": "src/pkg/leaf.py"}}',
                '[]'):
        f = tmp_path / "bad.json"
        f.write_text(bad)
        code, out = run_tool(monkeypatch, capsys, "port_plan.py", [
            "plan", "--hotspots", str(f), "--project-root", str(proj)])
        assert code == 1 and out["error"]["kind"] == "usage", bad
        assert "hotspots JSON" in out["error"]["detail"]


def test_plan_skips_profiler_noise_paths(monkeypatch, capsys, tmp_path):
    """Non-existent hotspot paths (console-script launchers recorded as "~",
    stale entries) are measurement noise, not port units: skip with one
    warning instead of refusing the whole plan (issue #28)."""
    proj = _quiet_project(tmp_path)
    hotspots = tmp_path / "noisy.json"
    hotspots.write_text(json.dumps([
        {"path": "~", "self_cpu_s": 9.9, "calls": 999},
        {"path": "src/pkg/hot.py", "self_cpu_s": 5.0, "calls": 7},
        {"path": "stale/gone.py", "self_cpu_s": 4.0, "calls": 2},
        {"path": "src/pkg/leaf.py", "self_cpu_s": 1.0, "calls": 3},
    ]))
    code, out = run_tool(monkeypatch, capsys, "port_plan.py", [
        "plan", "--hotspots", str(hotspots), "--project-root", str(proj)])
    assert code == 0, "noise must not fail the plan"
    assert sorted(u["id"] for u in out["units"]) == ["u-pkg-hot", "u-pkg-leaf"]
    skip_warnings = [w for w in out["warnings"] if "skipped 2 hotspot path(s)" in w]
    assert len(skip_warnings) == 1, "one summarized warning, not one per path"
    assert "~" in skip_warnings[0] and "stale/gone.py" in skip_warnings[0]
    assert "skipped 2 unresolvable hotspot path(s)" in out["summary"]["human"]


def test_plan_refuses_when_nothing_resolves(monkeypatch, capsys, tmp_path):
    proj = _quiet_project(tmp_path)
    hotspots = tmp_path / "allnoise.json"
    hotspots.write_text(json.dumps([{"path": "~", "self_cpu_s": 9.9},
                                    {"path": "nope.py", "self_cpu_s": 1.0}]))
    code, out = run_tool(monkeypatch, capsys, "port_plan.py", [
        "plan", "--hotspots", str(hotspots), "--project-root", str(proj)])
    assert code == 1 and out["error"]["kind"] == "usage"
    assert "no hotspot path resolved" in out["error"]["detail"]
    assert "~" in out["error"]["detail"] and "nope.py" in out["error"]["detail"]


def test_parse_error_is_tooling_note_not_risk_flag(monkeypatch, capsys, tmp_path):
    """A file the *tool's* interpreter can't parse (e.g. newer-than-tool
    syntax) still ran under the target interpreter — the profile proves it —
    so the old `parse-error` risk flag was a phantom portability penalty.
    It must become an unscored note plus a regex-fallback scan that still
    recovers import edges (issue #28)."""
    proj = tmp_path / "pproj"
    (proj / "src" / "pkg").mkdir(parents=True)
    (proj / "src" / "pkg" / "leaf.py").write_text("def g(x):\n    return x + 1\n")
    # syntax invalid under every Python — the suppression must not depend
    # on which interpreter runs the tool
    (proj / "src" / "pkg" / "newer.py").write_text(
        "from pkg.leaf import g\n\n\ndef broken(:\n    return g(1)\n")
    hotspots = tmp_path / "h.json"
    hotspots.write_text(json.dumps([
        {"path": "src/pkg/newer.py", "self_cpu_s": 5.0, "calls": 7},
        {"path": "src/pkg/leaf.py", "self_cpu_s": 1.0, "calls": 3},
    ]))
    code, out = run_tool(monkeypatch, capsys, "port_plan.py", [
        "plan", "--hotspots", str(hotspots), "--project-root", str(proj)])
    assert code == 0
    by_id = {u["id"]: u for u in out["units"]}
    newer = by_id["u-pkg-newer"]
    assert "parse-error" not in newer["risk_flags"]
    assert newer["risk_flags"] == []
    assert newer["portability_score"] == 100  # no phantom -10 penalty
    assert "not counted as a portability risk" in newer["parse_note"]
    assert newer["deps"] == ["u-pkg-leaf"]  # regex fallback keeps dep edges
    assert any("regex-fallback" in w and "u-pkg-newer" in w for w in out["warnings"])
    assert "1 regex-fallback scan(s)" in out["summary"]["human"]


# ------------------------------------- issue #51: system time, not units --
# Stdlib files that appear in the profile (copy.py, csv, statistics, ...)
# exist on disk, so the issue #28 "path not found" guard never fired and
# they became units — inflating the backlog and adding risk-flag noise to
# Stage-1 exit criteria. Only code under --project-root is a port unit.

def test_plan_skips_stdlib_hotspots_outside_root(monkeypatch, capsys, tmp_path):
    """Hotspot files that exist but live outside --project-root (stdlib,
    site-packages, the interpreter) are system time, never port units —
    the crate replaces them with Rust idioms, SUB-1 doesn't apply (issue
    #51). Their time must stay visible as signal, not backlog."""
    import copy
    import csv
    from pathlib import Path
    proj = _quiet_project(tmp_path)
    stdlib_copy = str(Path(copy.__file__).resolve())
    stdlib_csv = str(Path(csv.__file__).resolve())
    hotspots = tmp_path / "sys.json"
    hotspots.write_text(json.dumps([
        {"path": stdlib_copy, "self_cpu_s": 9.0, "calls": 400},  # deepcopy cost
        {"path": stdlib_csv, "self_cpu_s": 3.5, "calls": 90},
        {"path": "src/pkg/hot.py", "self_cpu_s": 5.0, "calls": 7},
        {"path": "src/pkg/leaf.py", "self_cpu_s": 1.0, "calls": 3},
    ]))
    code, out = run_tool(monkeypatch, capsys, "port_plan.py", [
        "plan", "--hotspots", str(hotspots), "--project-root", str(proj)])
    assert code == 0, out["summary"]["human"]
    assert sorted(u["id"] for u in out["units"]) == ["u-pkg-hot", "u-pkg-leaf"], \
        "stdlib hotspots must not become units"
    sys_warnings = [w for w in out["warnings"] if "outside the project root" in w]
    assert len(sys_warnings) == 1, "one summarized warning, not one per file"
    assert "system time" in sys_warnings[0]
    assert "copy.py" in sys_warnings[0] and "csv.py" in sys_warnings[0]
    st = out["system_time"]
    assert st["self_cpu_s"] == 12.5  # 9.0 + 3.5 kept as Stage-0 signal
    assert [m["module"] for m in st["modules"]] == ["copy", "csv"]  # cpu-desc
    assert st["modules"][0]["self_cpu_s"] == 9.0
    assert "2 system-time file(s)" in out["summary"]["human"]
    assert "12.500s self-CPU" in out["summary"]["human"]


def test_plan_absolute_paths_inside_root_still_units(monkeypatch, capsys, tmp_path):
    """The root-containment rule must not over-match: a profiler recording
    ABSOLUTE paths inside the project still yields units for them."""
    proj = _quiet_project(tmp_path)
    hotspots = tmp_path / "abs.json"
    hotspots.write_text(json.dumps([
        {"path": str(proj / "src" / "pkg" / "hot.py"), "self_cpu_s": 5.0, "calls": 7},
        {"path": str(proj / "src" / "pkg" / "leaf.py"), "self_cpu_s": 1.0, "calls": 3},
    ]))
    code, out = run_tool(monkeypatch, capsys, "port_plan.py", [
        "plan", "--hotspots", str(hotspots), "--project-root", str(proj)])
    assert code == 0
    assert sorted(u["id"] for u in out["units"]) == ["u-pkg-hot", "u-pkg-leaf"]
    assert "system_time" not in out
    assert not any("outside the project root" in w for w in out["warnings"])


def test_plan_vendored_venv_inside_root_is_system_time(monkeypatch, capsys, tmp_path):
    """A venv checked into the repo sits under the project root but the
    project doesn't own its code: site-packages/venv hotspot files are
    system time too (issue #51 names site-packages explicitly)."""
    proj = _quiet_project(tmp_path)
    dep = proj / ".venv" / "lib" / "python3.12" / "site-packages" / "dep.py"
    dep.parent.mkdir(parents=True)
    dep.write_text("def f():\n    return 1\n")
    hotspots = tmp_path / "venv.json"
    hotspots.write_text(json.dumps([
        {"path": str(dep), "self_cpu_s": 7.0, "calls": 50},
        {"path": "src/pkg/hot.py", "self_cpu_s": 5.0, "calls": 7},
    ]))
    code, out = run_tool(monkeypatch, capsys, "port_plan.py", [
        "plan", "--hotspots", str(hotspots), "--project-root", str(proj)])
    assert code == 0
    assert [u["id"] for u in out["units"]] == ["u-pkg-hot"]
    assert [m["module"] for m in out["system_time"]["modules"]] == ["dep"]
    assert out["system_time"]["self_cpu_s"] == 7.0


def test_plan_refuses_when_only_system_time_resolves(monkeypatch, capsys, tmp_path):
    """Every hotspot being outside the root (or unresolvable noise) still
    refuses: there is nothing under the project root to port."""
    import copy
    from pathlib import Path
    proj = _quiet_project(tmp_path)
    hotspots = tmp_path / "onlysys.json"
    hotspots.write_text(json.dumps([
        {"path": str(Path(copy.__file__).resolve()), "self_cpu_s": 9.0, "calls": 1},
        {"path": "~", "self_cpu_s": 0.9, "calls": 99},
    ]))
    code, out = run_tool(monkeypatch, capsys, "port_plan.py", [
        "plan", "--hotspots", str(hotspots), "--project-root", str(proj)])
    assert code == 1 and out["error"]["kind"] == "usage"
    assert "no hotspot path resolved" in out["error"]["detail"]
    assert "system time" in out["error"]["detail"]
    assert "copy.py" in out["error"]["detail"] and "~" in out["error"]["detail"]


# ------------------------------------------------------ contract_extract ---

def _cproj(tmp_path):
    proj = tmp_path / "cproj"
    (proj / "src" / "pkg").mkdir(parents=True)
    (proj / "tests").mkdir(parents=True)
    (proj / "src" / "pkg" / "parse.py").write_text(
        '"""Tokens.\n\nInvariant: len(out) <= len(text.split(sep))\n"""\n'
        "\n\ndef tokenize(text: str, sep: str = ',') -> list:\n"
        '    """Split.\n\n    Postcondition: no empty tokens\n    """\n'
        "    return [t for t in text.split(sep) if t]\n")
    (proj / "src" / "pkg" / "rank.py").write_text("def rank(items):\n    return sorted(items)\n")
    (proj / "tests" / "test_parse.py").write_text(
        "from pkg.parse import tokenize\n\n\ndef test_roundtrip():\n    assert tokenize('a,b') == ['a', 'b']\n")
    (proj / "tests" / "test_orphan.py").write_text(
        "import os\n\n\ndef test_env():\n    assert os.name\n")
    state = tmp_path / "cstate.json"
    state.write_text(json.dumps({
        "project": {"name": "pkg", "python_path": "src/pkg"}, "stage": 2,
        "units": [
            {"id": "u-parse", "python_path": "src/pkg/parse.py", "rust_path": None,
             "deps": [], "order": 1, "status": "pending"},
            {"id": "u-rank", "python_path": "src/pkg/rank.py", "rust_path": None,
             "deps": [], "order": 2, "status": "pending"},
        ]}))
    collect = tmp_path / "collect.txt"
    collect.write_text("tests/test_parse.py::test_roundtrip\n"
                       "tests/test_orphan.py::test_env\n\n2 tests collected in 0.01s\n")
    return proj, state, collect


def test_contracts_flag_uncovered_and_dangling(monkeypatch, capsys, tmp_path):
    proj, state, collect = _cproj(tmp_path)
    code, out = run_tool(monkeypatch, capsys, "contract_extract.py", [
        "extract", "--state", str(state), "--root", str(proj),
        "--pytest-collect", str(collect)])
    assert code == 2, "uncovered unit + dangling test must fail the run"
    assert out["uncovered_units"] == ["u-rank"]  # no test imports pkg.rank
    assert out["coverage_pct"] == 50.0
    by_unit = {c["unit"]: c for c in out["contracts"]}
    assert by_unit["u-parse"]["covering_tests"] == ["tests/test_parse.py::test_roundtrip"]


def test_full_coverage_passes_when_no_dangling(monkeypatch, capsys, tmp_path):
    proj, state, collect = _cproj(tmp_path)
    (proj / "tests" / "test_rank.py").write_text(
        "from pkg.rank import rank\n\n\ndef test_sorted():\n    assert rank([2, 1]) == [1, 2]\n")
    collect.write_text("tests/test_parse.py::test_roundtrip\n"
                       "tests/test_rank.py::test_sorted\n\n2 tests collected\n")
    code, out = run_tool(monkeypatch, capsys, "contract_extract.py", [
        "extract", "--state", str(state), "--root", str(proj),
        "--pytest-collect", str(collect)])
    assert code == 0
    assert out["coverage_pct"] == 100.0
    assert out["uncovered_units"] == [] and out["dangling_tests"] == []


def test_dangling_tests_always_flagged(monkeypatch, capsys, tmp_path):
    proj, state, collect = _cproj(tmp_path)
    code, out = run_tool(monkeypatch, capsys, "contract_extract.py", [
        "extract", "--state", str(state), "--root", str(proj),
        "--pytest-collect", str(collect), "--allow-uncovered"])
    assert any(d["nodeid"] == "tests/test_orphan.py::test_env" for d in out["dangling_tests"])
    assert code == 2  # dangling still fails even with allow-uncovered


def test_invariants_and_signatures_extracted(monkeypatch, capsys, tmp_path):
    proj, state, collect = _cproj(tmp_path)
    _, out = run_tool(monkeypatch, capsys, "contract_extract.py", [
        "extract", "--state", str(state), "--root", str(proj),
        "--pytest-collect", str(collect), "--allow-uncovered"])
    parse = next(c for c in out["contracts"] if c["unit"] == "u-parse")
    assert "Invariant: len(out) <= len(text.split(sep))" in parse["invariants"]
    assert "Postcondition: no empty tokens" in parse["invariants"]
    assert any(s.startswith("def tokenize(text: str") for s in parse["type_signatures"])
    rank = next(c for c in out["contracts"] if c["unit"] == "u-rank")
    assert rank["untyped"] is True  # fully unannotated def renders Any


def test_contract_files_written(monkeypatch, capsys, tmp_path):
    proj, state, collect = _cproj(tmp_path)
    out_dir = tmp_path / "contracts"
    _, out = run_tool(monkeypatch, capsys, "contract_extract.py", [
        "extract", "--state", str(state), "--root", str(proj),
        "--pytest-collect", str(collect), "--allow-uncovered",
        "--out", str(out_dir)])
    assert (out_dir / "u-parse.contract.json").exists()
    assert (out_dir / "u-rank.contract.json").exists()


# ------------------------------------------- src-as-package layout (#26) ---
# These projects' import root IS the top-level src/ directory, so tests
# import ``src.<module>`` — the spelling module_name_of() strips away.

def _srcpkg(tmp_path):
    proj = tmp_path / "srcproj"
    (proj / "src").mkdir(parents=True)
    (proj / "tests").mkdir(parents=True)
    (proj / "src" / "parse.py").write_text(
        '"""Tokens.\n\nInvariant: no empty tokens\n"""\n\n\n'
        "def tokenize(text: str, sep: str = ',') -> list:\n"
        "    return [t for t in text.split(sep) if t]\n")
    (proj / "src" / "rank.py").write_text("def rank(items):\n    return sorted(items)\n")
    state = tmp_path / "srcstate.json"
    state.write_text(json.dumps({
        "project": {"name": "srcproj", "python_path": "src"}, "stage": 2,
        "units": [
            {"id": "u-parse", "python_path": "src/parse.py", "rust_path": None,
             "deps": [], "order": 1, "status": "pending"},
            {"id": "u-rank", "python_path": "src/rank.py", "rust_path": None,
             "deps": [], "order": 2, "status": "pending"},
        ]}))
    return proj, state


def test_src_layout_imports_cover_units(monkeypatch, capsys, tmp_path):
    """tests/test_<module>.py importing ``src.<module>`` must cover the unit
    whose python_path is ``src/<module>.py`` — the issue #26 regression."""
    proj, state = _srcpkg(tmp_path)
    (proj / "tests" / "test_parse.py").write_text(
        "from src.parse import tokenize\n\n\ndef test_roundtrip():\n"
        "    assert tokenize('a,b') == ['a', 'b']\n")
    (proj / "tests" / "test_rank.py").write_text(
        "from src.rank import rank\n\n\ndef test_sorted():\n"
        "    assert rank([2, 1]) == [1, 2]\n")
    collect = tmp_path / "srccollect.txt"
    collect.write_text("tests/test_parse.py::test_roundtrip\n"
                       "tests/test_rank.py::test_sorted\n\n2 tests collected\n")
    code, out = run_tool(monkeypatch, capsys, "contract_extract.py", [
        "extract", "--state", str(state), "--root", str(proj),
        "--pytest-collect", str(collect)])
    assert code == 0, out["summary"]["human"]
    assert out["coverage_pct"] == 100.0
    assert out["uncovered_units"] == []
    assert out["dangling_tests"] == []
    by_unit = {c["unit"]: c for c in out["contracts"]}
    assert by_unit["u-parse"]["covering_tests"] == ["tests/test_parse.py::test_roundtrip"]
    assert by_unit["u-rank"]["covering_tests"] == ["tests/test_rank.py::test_sorted"]
    assert out["layout_mismatch_suspected"] is False


def test_src_layout_still_flags_true_uncovered_and_dangling(monkeypatch, capsys, tmp_path):
    """The fix must not over-match: a genuinely untested unit stays uncovered
    and an out-of-scope engine test stays dangling."""
    proj, state = _srcpkg(tmp_path)
    (proj / "tests" / "test_parse.py").write_text(
        "from src.parse import tokenize\n\n\ndef test_roundtrip():\n"
        "    assert tokenize('a,b') == ['a', 'b']\n")
    (proj / "tests" / "test_engine.py").write_text(
        "import zlib\n\n\ndef test_engine():\n    assert zlib.compress(b'x')\n")
    collect = tmp_path / "srccollect.txt"
    collect.write_text("tests/test_parse.py::test_roundtrip\n"
                       "tests/test_engine.py::test_engine\n\n2 tests collected\n")
    code, out = run_tool(monkeypatch, capsys, "contract_extract.py", [
        "extract", "--state", str(state), "--root", str(proj),
        "--pytest-collect", str(collect)])
    assert code == 2
    assert out["uncovered_units"] == ["u-rank"]  # no test imports src.rank
    assert out["coverage_pct"] == 50.0
    assert [d["nodeid"] for d in out["dangling_tests"]] == ["tests/test_engine.py::test_engine"]
    by_unit = {c["unit"]: c for c in out["contracts"]}
    assert by_unit["u-parse"]["covering_tests"] == ["tests/test_parse.py::test_roundtrip"]
    assert out["layout_mismatch_suspected"] is False  # partial coverage: not a mismatch


def test_all_uncovered_plus_import_mismatch_hints_layout(monkeypatch, capsys, tmp_path):
    """When EVERY unit is uncovered while the dangling tests did import
    something (matching no unit), the failure must say 'likely layout
    mismatch' instead of reading like a weak suite (issue #26)."""
    proj, state = _srcpkg(tmp_path)
    (proj / "tests" / "test_parse.py").write_text(
        "from appmod.parse import tokenize\n\n\ndef test_roundtrip():\n"
        "    assert True\n")
    collect = tmp_path / "srccollect.txt"
    collect.write_text("tests/test_parse.py::test_roundtrip\n\n1 test collected\n")
    code, out = run_tool(monkeypatch, capsys, "contract_extract.py", [
        "extract", "--state", str(state), "--root", str(proj),
        "--pytest-collect", str(collect)])
    assert code == 2
    assert out["coverage_pct"] == 0.0
    assert out["layout_mismatch_suspected"] is True
    assert "likely layout mismatch" in out["summary"]["human"]
