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
# Some projects' import root IS the top-level src/ directory: tests do
# ``from src.parse import ...`` and entry points read ``src.cli:main``.
# module_name_of() strips the src/ prefix (-> "parse"), so matching only
# the stripped spelling reported EVERY unit uncovered and EVERY test
# dangling (issue #26).

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
    assert out["uncovered_units"] == [] and out["dangling_tests"] == []
    by_unit = {c["unit"]: c for c in out["contracts"]}
    assert by_unit["u-parse"]["covering_tests"] == \
        ["tests/test_parse.py::test_roundtrip"]
    assert by_unit["u-rank"]["covering_tests"] == \
        ["tests/test_rank.py::test_sorted"]
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
    assert [d["nodeid"] for d in out["dangling_tests"]] == \
        ["tests/test_engine.py::test_engine"]
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
