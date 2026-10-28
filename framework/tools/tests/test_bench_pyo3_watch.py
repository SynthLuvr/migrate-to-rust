"""bench.py + pyo3_review.py + upstream_watch.py acceptance tests (#5, #6, #8)."""

from __future__ import annotations

import json
import subprocess

from conftest import run_tool


# ------------------------------------------------------------------ bench --

def _bench_scaffold(tmp_path, committed=True, break_parity=False):
    root = tmp_path / "bench"
    (root / "corpora").mkdir(parents=True)
    (root / "impl").mkdir(parents=True)
    (root / "impl" / "py_impl.py").write_text(
        "def run(v):\n    return {'total': sum(v['nums'])}\n")
    (root / "impl" / "rs_impl.py").write_text(
        "def run(v):\n    return {'total': sum(v['nums'])"
        + (" + 1" if break_parity else "") + "}\n")
    (root / "corpora" / "nums.jsonl").write_text(
        "\n".join(json.dumps({"nums": [1, 2, 3, i]}) for i in range(20)) + "\n")
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({
        "name": "sum", "corpus": "corpora/nums.jsonl",
        "python": {"entry": "impl.py_impl:run"},
        "rust": {"entry": "impl.rs_impl:run"},
        "warmup": 0, "repeats": 2}))
    if committed:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "bench"], cwd=root, check=True)
    return manifest


def test_bench_produces_verdict_and_repro(monkeypatch, capsys, tmp_path):
    manifest = _bench_scaffold(tmp_path)
    code, out = run_tool(monkeypatch, capsys, "bench.py",
                         ["run", "--manifest", str(manifest)])
    assert code == 0
    assert out["parity"]["counts"]["equivalent"] == 20
    assert out["corpus"]["commit"] and not out["corpus"].get("dirty")
    assert out["matched_inputs"] == {"count": 20, "identical_order": True,
                                     "digest": out["matched_inputs"]["digest"]}
    assert out["speedup"]["median_speedup"] > 0
    repro = tmp_path / "bench" / "bench-sum.repro.sh"
    assert repro.exists()
    text = repro.read_text()
    assert out["corpus"]["commit"][:8] in text and "set -euo pipefail" in text


def test_bench_refuses_dirty_corpus(monkeypatch, capsys, tmp_path):
    manifest = _bench_scaffold(tmp_path)
    with open(tmp_path / "bench" / "corpora" / "nums.jsonl", "a") as fh:
        fh.write(json.dumps({"nums": [9]}) + "\n")
    code, out = run_tool(monkeypatch, capsys, "bench.py",
                         ["run", "--manifest", str(manifest)])
    assert code == 1
    assert "dirty" in out["summary"]["human"]
    assert "REFUSED" in out["summary"]["human"]


def test_bench_divergent_outputs_abort_without_timing(monkeypatch, capsys, tmp_path):
    manifest = _bench_scaffold(tmp_path, break_parity=True)
    code, out = run_tool(monkeypatch, capsys, "bench.py",
                         ["run", "--manifest", str(manifest)])
    assert code == 2
    assert out["parity"]["counts"]["divergent"] == 20
    assert "speedup" not in out, "no number without (parity +) repro"
    assert "ABORTED" in out["summary"]["human"]


def test_bench_degenerate_corpus_aborts_without_timing(monkeypatch, capsys, tmp_path):
    # issue #30: the reference side rejects every row and the candidate
    # errors identically — per-run error==error pairs are not ground truth
    # to hang a timing claim on
    manifest = _bench_scaffold(tmp_path)
    for name in ("py_impl.py", "rs_impl.py"):
        (tmp_path / "bench" / "impl" / name).write_text(
            "def run(v):\n    raise TypeError('corpus is not what the reference expects')\n")
    code, out = run_tool(monkeypatch, capsys, "bench.py",
                         ["run", "--manifest", str(manifest)])
    assert code == 2
    assert out["parity"]["verdict"] == "incomparable"
    assert out["parity"]["counts"]["equivalent"] == 20  # per-run error==error
    assert out["ground_truth"]["degenerate"] is True
    assert "speedup" not in out, "no number without ground truth"
    assert "no ground truth" in out["summary"]["human"]
    assert "ABORTED" in out["summary"]["human"]


def test_bench_thin_ground_truth_warns_but_times(monkeypatch, capsys, tmp_path):
    # reference ok on only 5 of 20 rows (25% < 50%): parity still
    # equivalent per run, timings proceed, but the warning surfaces
    manifest = _bench_scaffold(tmp_path)
    src = ("def run(v):\n"
           "    if v['nums'][3] % 4:\n"
           "        raise ValueError('three quarters rejected')\n"
           "    return {'total': sum(v['nums'])}\n")
    (tmp_path / "bench" / "impl" / "py_impl.py").write_text(src)
    (tmp_path / "bench" / "impl" / "rs_impl.py").write_text(src)
    code, out = run_tool(monkeypatch, capsys, "bench.py",
                         ["run", "--manifest", str(manifest)])
    assert code == 0
    assert out["parity"]["verdict"] == "equivalent"
    assert out["ground_truth"]["reference_ok"] == 5
    assert out["ground_truth"]["reference_ok_fraction"] == 0.25
    assert any("thin ground truth" in w for w in out["warnings"])
    assert "WARNING" in out["summary"]["human"]
    assert "speedup" in out


# ------------------------------------------------------------ pyo3_review --

BAD_RS = """use pyo3::prelude::*;
use std::rc::Rc;

#[pyfunction]
fn sum_all(py: Python, values: Vec<i64>) -> PyResult<i64> {
    if values.is_empty() { panic!("empty"); }
    let _ = py.eval("1+1", None, None);
    Ok(values.iter().sum())
}

#[pyfunction]
fn leak(obj: &Bound<'_, PyAny>) -> usize {
    let raw = obj.clone().into_raw();
    unsafe { pyo3::ffi::Py_DECREF(raw) };
    0
}

#[pyclass]
struct Gadget { inner: Rc<Vec<u8>> }

#[pymethods]
impl Gadget {
    fn detach_bad(&self, py: Python) -> usize {
        py.detach(|| {
            let n = py.len0();
            n
        })
    }
}

#[pyfunction]
fn old_api(py: Python) -> usize { py.allow_threads(|| 1) }

#[pyfunction]
fn new_api(py: Python) -> usize { py.detach(|| 2) }

#[pyfunction]
#[pyo3(signature = (value, default=None))]
fn to_float(py: Python, value: &Bound<'_, PyAny>, default: Option<Py<PyAny>>) -> Py<PyAny> {
    default.unwrap_or_else(|| py.None())
}
"""

GOOD_RS = """use pyo3::prelude::*;

#[pyfunction]
fn sum_all(_py: Python, values: Vec<i64>) -> PyResult<i64> {
    Ok(values.iter().sum())
}

#[pyclass]
struct Gadget { inner: Vec<u8> }

#[pymethods]
impl Gadget {
    fn len(&self) -> usize { self.inner.len() }
}
"""


def test_pyo3_review_all_rules_fire(monkeypatch, capsys, tmp_path):
    bad = tmp_path / "bad.rs"
    bad.write_text(BAD_RS)
    code, out = run_tool(monkeypatch, capsys, "pyo3_review.py",
                         ["review", "--files", str(bad)])
    assert code == 2
    rules = {f["rule"] for f in out["findings"]}
    assert "PYO3-UNWRAP" in rules       # panic! in pyfunction
    assert "PYO3-SWALLOW" in rules      # let _ = py.eval
    assert "PYO3-REFCOUNT" in rules     # into_raw + Py_DECREF, no SAFETY
    assert "PYO3-DETACH" in rules       # py use inside detach closure
    assert "PYO3-GEN" in rules          # allow_threads + detach mixed
    assert "PYO3-THREADSAFE" in rules   # Rc in pyclass field
    assert "PYO3-OPTION" in rules       # Option<Py<PyAny>> param (issue #56)
    for f in out["findings"]:
        assert f["checklist"].endswith("reviewer.md#2-pyo3-ffi-hazards")
        assert f["file"] and f["line"] >= 1


def test_pyo3_review_clean_file_passes(monkeypatch, capsys, tmp_path):
    good = tmp_path / "good.rs"
    good.write_text(GOOD_RS)
    code, out = run_tool(monkeypatch, capsys, "pyo3_review.py",
                         ["review", "--files", str(good)])
    assert code == 0
    assert out["counts"] == {"errors": 0, "warnings": 0}


def test_pyo3_review_diff_mode(monkeypatch, capsys, tmp_path):
    diff = tmp_path / "d.diff"
    diff.write_text(
        "diff --git a/src/lib.rs b/src/lib.rs\n"
        "--- a/src/lib.rs\n+++ b/src/lib.rs\n"
        "@@ -5,3 +5,6 @@ fn x() {\n"
        "     let a = 1;\n"
        "+    let p = obj.into_raw();\n"
        "+    let _ = py.call0(\"f\");\n"
        "+    let y = v.unwrap();\n"
        "     let b = 2;\n")
    code, out = run_tool(monkeypatch, capsys, "pyo3_review.py",
                         ["review", "--diff", str(diff)])
    assert code == 2
    rules = {f["rule"] for f in out["findings"]}
    assert rules == {"PYO3-REFCOUNT", "PYO3-SWALLOW", "PYO3-UNWRAP"}
    assert all(f["context"] == "diff" for f in out["findings"])
    assert [f["line"] for f in out["findings"]] == [6, 7, 8]


OPTION_RS = """use pyo3::prelude::*;
use pyo3::types::{PyDict, PyTuple};

#[pyfunction]
#[pyo3(signature = (value, default=None))]
fn to_float(
    value: &Bound<'_, PyAny>,
    default: Option<Py<PyAny>>,
) -> Option<Py<PyAny>> {
    default
}

#[pyfunction]
#[pyo3(signature = (*args))]
fn to_float_manual(py: Python<'_>, args: &Bound<'_, PyTuple>) -> Py<PyAny> {
    py.None()
}

#[pyfunction]
fn kwargs_ok(kwds: Option<&Bound<'_, PyDict>>) -> usize {
    kwds.map_or(0, |dict| dict.len())
}

#[pyfunction]
fn opt_int(period: Option<i64>) -> i64 {
    period.unwrap_or(14)
}
"""


def test_pyo3_review_option_arg_conflation(monkeypatch, capsys, tmp_path):
    """PYO3-OPTION (issue #56): Option<Py-typed> params on exported fns warn;
    the *args manual-binding pattern, **kwargs idiom, numeric Options, and
    Option-typed RETURN types must not fire."""
    rs = tmp_path / "option.rs"
    rs.write_text(OPTION_RS)
    code, out = run_tool(monkeypatch, capsys, "pyo3_review.py",
                         ["review", "--files", str(rs)])
    assert code == 0  # warning severity alone does not fail
    fires = [f for f in out["findings"] if f["rule"] == "PYO3-OPTION"]
    assert len(fires) == 1
    first = fires[0]
    assert first["severity"] == "warning"
    assert first["line"] == 8                      # the param line, not the fn line
    assert "to_float" in first["why"]
    assert "*args" in first["why"]
    assert {f["rule"] for f in out["findings"]} == {"PYO3-OPTION"}
    # --strict promotes the warning to a failure
    code, out = run_tool(monkeypatch, capsys, "pyo3_review.py",
                         ["review", "--files", str(rs), "--strict"])
    assert code == 2


def test_pyo3_review_option_diff_mode(monkeypatch, capsys, tmp_path):
    diff = tmp_path / "d.diff"
    diff.write_text(
        "diff --git a/src/lib.rs b/src/lib.rs\n"
        "--- a/src/lib.rs\n+++ b/src/lib.rs\n"
        "@@ -3,2 +3,3 @@ fn x() {\n"
        "     let a = 1;\n"
        "+    default: Option<Py<PyAny>>,\n"
        "     let b = 2;\n")
    code, out = run_tool(monkeypatch, capsys, "pyo3_review.py",
                         ["review", "--diff", str(diff)])
    assert code == 0  # warning only
    rules = {f["rule"]: f for f in out["findings"]}
    assert set(rules) == {"PYO3-OPTION"}
    assert rules["PYO3-OPTION"]["severity"] == "warning"
    assert rules["PYO3-OPTION"]["context"] == "diff"
    assert rules["PYO3-OPTION"]["line"] == 4


# ---------------------------------------------------------- upstream_watch --

def _watch_repo(tmp_path):
    repo = tmp_path / "up"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    for name in ("parse", "rank", "io"):
        (repo / "src" / "pkg" / f"{name}.py").write_text(f"def {name}():\n    pass\n")
    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True,
                       capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-qm", "v1")
    (repo / "src" / "pkg" / "parse.py").write_text(
        "def parse():\n    return 'changed'\n")
    (repo / "tests" / "test_parse.py").write_text("def test_parse():\n    assert 1\n")
    git("add", "-A")
    git("commit", "-qm", "upstream change")
    return repo


WATCH_STATE = {
    "project": {"name": "pkg", "python_path": "src/pkg"}, "stage": 7,
    "units": [
        {"id": "u-parse", "python_path": "src/pkg/parse.py",
         "rust_path": "crates/pkg/src/u_parse.rs", "deps": [], "order": 1,
         "status": "ported",
         "contract": {"covering_tests": ["tests/test_parse.py"],
                      "golden_corpus": "corpora/parse.jsonl"}},
        {"id": "u-rank", "python_path": "src/pkg/rank.py", "rust_path": None,
         "deps": ["u-parse"], "order": 2, "status": "pending"},
        {"id": "u-io", "python_path": "src/pkg/io.py", "rust_path": None,
         "deps": ["u-rank"], "order": 3, "status": "escalated"},
    ],
}


def test_watch_maps_changes_and_dependents(monkeypatch, capsys, tmp_path):
    repo = _watch_repo(tmp_path)
    state = tmp_path / "state.json"
    state.write_text(json.dumps(WATCH_STATE))
    code, out = run_tool(monkeypatch, capsys, "upstream_watch.py", [
        "watch", "--state", str(state), "--repo", str(repo),
        "--base", "HEAD~1", "--head", "HEAD"])
    assert code == 2  # u-io is escalated -> human decision
    assert out["mapping"] == {"src/pkg/parse.py": ["u-parse"]}
    plan = {p["unit"]: p for p in out["affected_units"]}
    assert set(plan) == {"u-parse", "u-rank", "u-io"}
    assert plan["u-parse"]["why"] == "upstream-changed"
    assert plan["u-rank"]["why"] == "depends-on u-parse"
    assert "oracle.py judge" in plan["u-parse"]["parity_rerun"]
    assert "u-io" in out["needing_human"]


def test_watch_no_silent_skips_on_unmapped(monkeypatch, capsys, tmp_path):
    repo = _watch_repo(tmp_path)
    state = tmp_path / "state.json"
    state.write_text(json.dumps(WATCH_STATE))
    _, out = run_tool(monkeypatch, capsys, "upstream_watch.py", [
        "watch", "--state", str(state), "--repo", str(repo),
        "--base", "HEAD~1", "--head", "HEAD"])
    unmapped = {u["file"] for u in out["unmapped"]}
    assert "tests/test_parse.py" in unmapped  # listed with a reason
    changed = {f for f in out["changed_files"]}
    assert changed == unmapped | set(out["mapping"])
    # every changed file accounted for exactly once
    assert len(out["changed_files"]) == 2


def test_watch_writes_pr_proposal(monkeypatch, capsys, tmp_path):
    repo = _watch_repo(tmp_path)
    state = tmp_path / "state.json"
    state.write_text(json.dumps(WATCH_STATE))
    pr = tmp_path / "pr.md"
    _, out = run_tool(monkeypatch, capsys, "upstream_watch.py", [
        "watch", "--state", str(state), "--repo", str(repo),
        "--base", "HEAD~1", "--head", "HEAD", "--write", str(pr)])
    text = pr.read_text()
    assert "Parity re-run plan" in text
    assert "u-parse" in text and "tests/test_parse.py" in text
    assert "human decision" in text.lower() or "human decision" in text
