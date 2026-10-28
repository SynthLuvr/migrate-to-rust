"""scaffold.py acceptance tests (issues #32, #48).

Covers: the scaffolded artifacts (symmetric adapters, seeded generator,
judge-ready manifest, corpus in the documented raw-value format), the
determinism contract (same seed -> byte-identical corpus; different seed ->
reviewer fresh inputs), hand-tune flagging of types the heuristic refuses
to guess, overwrite protection, and the degenerate-corpus self-check.

Module conformance (issue #48) is the default: one manifest judges the
unit's WHOLE public API through a {fn: result | __error__} envelope —
a first-public-only verdict must not green-light a unit whose other
functions diverge.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import run_tool


def _project(tmp_path, name="pkg", unit="u-bars", module="bars", body=None):
    """A one-unit project with a dataclass param (imported from a sibling
    module), an Optional param, a defaulted param, and a documented error
    path — the surface the heuristic mapping must cover."""
    proj = tmp_path / name
    (proj / "src" / name).mkdir(parents=True)
    (proj / "src" / name / "types.py").write_text(
        "from dataclasses import dataclass\n"
        "\n"
        "\n"
        "@dataclass\n"
        "class Span:\n"
        "    start: int\n"
        "    end: int\n")
    (proj / "src" / name / f"{module}.py").write_text(body or (
        '"""bars.\n'
        "\n"
        'Invariant: empty items raises ValueError (never returns [])\n'
        '"""\n'
        "from dataclasses import dataclass\n"
        "from .types import Span\n"
        "\n"
        "\n"
        "@dataclass\n"
        "class Bar:\n"
        "    width: int\n"
        '    label: str = "x"\n'
        "\n"
        "\n"
        "def bars(items: list[Bar], limit: int = 10, span: Span | None = None) -> list[str]:\n"
        '    if not items:\n'
        '        raise ValueError("empty items")\n'
        '    out = [f"{b.label}:{b.width}" for b in items if b.width <= limit]\n'
        "    if span is not None:\n"
        '        out.append(f"span={span.start}-{span.end}")\n'
        "    return out\n"
        "\n"
        "\n"
        "def unused_helper(x: int) -> int:\n"
        "    return x\n"))
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "project": {"name": name, "python_path": f"src/{name}"}, "stage": 2,
        "units": [{"id": unit, "python_path": f"src/{name}/{module}.py",
                   "rust_path": None, "deps": [], "order": 1,
                   "status": "pending"}]}))
    return proj, state


def _scaffold(monkeypatch, capsys, state, proj, out, *extra, rows=8, seed=20261028):
    return run_tool(monkeypatch, capsys, "scaffold.py", [
        "scaffold", str(state), "--root", str(proj), "--out", str(out),
        "--seed", str(seed), "--rows", str(rows), *extra])


def _strip_backend(text: str) -> str:
    head, _, rest = text.partition("# ==== BACKEND")
    _, _, tail = rest.partition("# ==== END BACKEND")
    return head + tail


# --------------------------------------------- end-to-end: judge-ready ----

def test_scaffold_end_to_end_and_judge_ready(monkeypatch, capsys, tmp_path):
    proj, state = _project(tmp_path)
    out = tmp_path / "runs"
    code, payload = _scaffold(monkeypatch, capsys, state, proj, out)
    assert code == 0, payload["summary"]["human"]
    unit = payload["units"][0]

    # the five artifacts, in the layout the issue specifies
    files = {Path(p) for p in unit["files"].values()}
    assert files == {
        out / "adapters/u_bars_ref.py", out / "adapters/u_bars_cand.py",
        out / "corpora/gen_u_bars.py", out / "corpora/corpus-u-bars.jsonl",
        out / "manifests/u-bars.json"}
    assert all(f.is_file() for f in files)

    # module conformance is the DEFAULT (issue #48): every public function
    # is judged, not just the module-stem pick
    assert unit["mode"] == "module"
    assert unit["functions"] == ["bars", "unused_helper"]
    assert unit["function_source"] == "all-public"

    # signature -> spec mapping: dataclass param resolved through the
    # sibling-module import, optional/defaults carried (per function)
    bars = unit["functions_detail"]["bars"]
    items = bars["params"]["items"]
    assert items["kind"] == "list"
    assert items["item"]["kind"] == "dataclass"
    assert items["item"]["name"] == "Bar"
    assert items["item"]["module"] == "pkg.bars"
    assert bars["params"]["span"]["kind"] == "optional"
    assert bars["params"]["span"]["item"]["name"] == "Span"
    assert unit["hand_tune"] == []

    # corpus: one raw JSON value per line, a kwargs object over the UNION
    # of the judged functions' parameters — NEVER the {"i", "input"} stdin
    # framing (issue #29)
    union = {"items", "limit", "span", "x"}
    lines = (out / "corpora/corpus-u-bars.jsonl").read_text().splitlines()
    assert lines and all(line.strip() for line in lines)
    rows = [json.loads(line) for line in lines]
    assert all(isinstance(r, dict) for r in rows)
    assert all(set(r) <= union for r in rows)
    assert not any(set(r) == {"i", "input"} for r in rows)
    zero = rows[0]  # the all-zero edge row: the documented error path
    assert zero == {"items": [], "limit": 0, "span": None, "x": 0}
    assert any(r["items"] and json.dumps(r, sort_keys=True) for r in rows)

    # adapters symmetric by construction: identical outside the BACKEND
    ref = (out / "adapters/u_bars_ref.py").read_text()
    cand = (out / "adapters/u_bars_cand.py").read_text()
    assert _strip_backend(ref) == _strip_backend(cand)
    assert unit["adapters"]["symmetric"] is True
    assert "CANDIDATE_MODULE = None" in cand  # Stage-2 placeholder, not a guess

    # manifest is judge-ready as-is (entries are relative .py paths) and
    # judges the whole public API (issue #48)
    manifest = json.loads((out / "manifests/u-bars.json").read_text())
    assert manifest["unit"] == "u-bars"
    assert manifest["mode"] == "module"
    assert manifest["functions"] == ["bars", "unused_helper"]
    assert manifest["corpus"] == "../corpora/corpus-u-bars.jsonl"
    for side in ("reference", "candidate"):
        entry = manifest[side]["entry"]
        assert entry.endswith(".py:run") and ".." in entry
        assert (out / "manifests" / entry.split(":")[0]).is_file()

    # the scaffold dry-ran its own corpus through oracle.py's runner
    assert unit["corpus_dry_run"]["degenerate"] is False
    assert unit["corpus_dry_run"]["reference_ok"] > 0


def test_scaffolded_manifest_judges_equivalent_ref_vs_cand(monkeypatch, capsys, tmp_path):
    """The manifest must be ready for `oracle.py judge` verbatim. With the
    candidate pointed at the same module (a stand-in for the installed
    wheel), a symmetric pair must verdict EQUIVALENT — including the
    error-path rows (error==error) — through the production subprocess
    path."""
    proj, state = _project(tmp_path)
    out = tmp_path / "runs"
    code, payload = _scaffold(monkeypatch, capsys, state, proj, out,
                              "--candidate-module", "pkg.bars")
    assert code == 0, payload["summary"]["human"]
    code, out_json = run_tool(monkeypatch, capsys, "oracle.py", [
        "judge", "--manifest", str(out / "manifests/u-bars.json")])
    assert code == 0, out_json["summary"]["human"]
    assert out_json["verdict"] == "equivalent"
    assert out_json["counts"] == {"equivalent": out_json["total"],
                                  "divergent": 0, "incomparable": 0}
    assert out_json["ground_truth"]["degenerate"] is False


def test_placeholder_candidate_is_an_honest_setup_failure(monkeypatch, capsys, tmp_path):
    """Without --candidate-module the judge must refuse honestly — every
    run incomparable with the setup failure named (judge exit 3 with a
    setup detail, per the existing convention), never a hollow verdict."""
    proj, state = _project(tmp_path)
    out = tmp_path / "runs"
    _scaffold(monkeypatch, capsys, state, proj, out)
    code, out_json = run_tool(monkeypatch, capsys, "oracle.py", [
        "judge", "--manifest", str(out / "manifests/u-bars.json")])
    assert code == 3
    assert out_json["verdict"] == "incomparable"
    assert out_json["counts"]["equivalent"] == 0
    assert "NotImplementedError" in json.dumps(out_json["runs"])
    assert "candidate module not set" in out_json["runs"][0]["detail"]


# --------------------------------------------- seeded fresh-inputs (#32) --

def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_generator_same_seed_is_byte_identical(monkeypatch, capsys, tmp_path):
    proj, state = _project(tmp_path)
    out = tmp_path / "runs"
    _, payload = _scaffold(monkeypatch, capsys, state, proj, out)
    gen = _load_module(Path(payload["units"][0]["files"]["generator"]))
    a = gen.build_rows(20261028, 8)
    b = gen.build_rows(20261028, 8)
    assert a == b
    corpus = Path(payload["units"][0]["files"]["corpus"]).read_bytes()
    assert hashlib.sha256(corpus).hexdigest() == payload["units"][0]["corpus"]["sha256"]


def test_generator_different_seed_is_fresh_inputs(monkeypatch, capsys, tmp_path):
    """The reviewer's re-judgment (Stage 4 step 8): a different seed must
    produce a different corpus via the generated CLI, in the documented
    raw-value format."""
    proj, state = _project(tmp_path)
    out = tmp_path / "runs"
    _, payload = _scaffold(monkeypatch, capsys, state, proj, out)
    gen_path = Path(payload["units"][0]["files"]["generator"])
    fresh = tmp_path / "corpus-fresh.jsonl"
    code = subprocess.run(
        [sys.executable, str(gen_path), "--seed", "424242", "--rows", "24",
         "--out", str(fresh)], capture_output=True, text=True, timeout=120)
    assert code.returncode == 0, code.stderr
    summary = json.loads(code.stdout)
    assert summary["seed"] == 424242 and summary["corpus"]["rows"] >= 24
    original = Path(payload["units"][0]["files"]["corpus"]).read_text()
    fresh_text = fresh.read_text()
    assert fresh_text != original, "a different seed must yield fresh inputs"
    for line in fresh_text.splitlines():  # format guard, issue #29
        row = json.loads(line)
        assert isinstance(row, dict) and set(row) <= {"items", "limit", "span", "x"}


# --------------------------------------------- flagged, not guessed -------

def test_unknown_types_are_flagged_for_hand_completion(monkeypatch, capsys, tmp_path):
    """The heuristic must not guess exotic types: the parameter is flagged,
    the scaffold exits with findings, and the HAND-TUNE block names it in
    BOTH adapters (issue #32: 'unknown/complex types flagged for
    hand-completion rather than guessed')."""
    body = ("class Frame:  # a real class, but NOT a @dataclass — no mechanical\n"
            "    pass      # JSON-field constructor exists; must not be guessed\n"
            "\n"
            "\n"
            "def score(frame: Frame, n: int) -> int:\n"
            "    return len(frame) + n\n")
    proj, state = _project(tmp_path, name="q", unit="u-score", module="score", body=body)
    out = tmp_path / "runs"
    code, payload = _scaffold(monkeypatch, capsys, state, proj, out,
                              "--mode", "function")
    assert code == 2, "hand-tune flags must be findings, not silence"
    unit = payload["units"][0]
    assert any(f["where"] == "param frame" for f in unit["hand_tune"])
    ref = (out / "adapters/u_score_ref.py").read_text()
    cand = (out / "adapters/u_score_cand.py").read_text()
    assert "HAND-TUNE (1 item(s))" in ref and "param frame" in ref
    assert "HAND-TUNE (1 item(s))" in cand and "param frame" in cand
    assert unit["adapters"]["symmetric"] is True
    # rows for the flagged param carry JSON-any stand-ins: most error, the
    # ok fraction lands below the oracle's thin-ground-truth threshold —
    # surfaced as a warning (the always-raising degenerate case is covered
    # by test_always_raising_reference_is_a_degenerate_finding)
    assert any("thin ground truth" in w for w in payload["warnings"])


def test_variadic_params_are_flagged(monkeypatch, capsys, tmp_path):
    body = ("def tally(*args: int) -> int:\n"
            "    return sum(args)\n")
    proj, state = _project(tmp_path, name="v", unit="u-tally", module="tally", body=body)
    code, payload = _scaffold(monkeypatch, capsys, state, proj, tmp_path / "runs")
    assert code == 2
    assert any("*args" in f["reason"] for f in payload["units"][0]["hand_tune"])


# --------------------------------------------- guard rails ----------------

def test_refuses_to_overwrite_without_force(monkeypatch, capsys, tmp_path):
    proj, state = _project(tmp_path)
    out = tmp_path / "runs"
    assert _scaffold(monkeypatch, capsys, state, proj, out)[0] == 0
    code, payload = _scaffold(monkeypatch, capsys, state, proj, out, seed=1)
    assert code == 1 and payload["error"]["kind"] == "usage"
    assert "--force" in payload["error"]["detail"]
    assert _scaffold(monkeypatch, capsys, state, proj, out, "--force")[0] == 0


def test_outside_runs_is_allowed_but_warned(monkeypatch, capsys, tmp_path):
    """runs/ is gitignored so private names never reach the repo; another
    --out still works but says why that matters."""
    proj, state = _project(tmp_path)
    code, payload = _scaffold(monkeypatch, capsys, state, proj, tmp_path / "contracts")
    assert code == 0
    assert any("not under a runs/ directory" in w for w in payload["warnings"])


def test_always_raising_reference_is_a_degenerate_finding(monkeypatch, capsys, tmp_path):
    body = ("def boom(x: int) -> int:\n"
            "    raise RuntimeError('always')\n")
    proj, state = _project(tmp_path, name="b", unit="u-boom", module="boom", body=body)
    code, payload = _scaffold(monkeypatch, capsys, state, proj, tmp_path / "runs")
    assert code == 2
    unit = payload["units"][0]
    assert unit["corpus_dry_run"]["degenerate"] is True
    assert any(f["kind"] == "degenerate_corpus" for f in payload["findings"])


def test_unknown_unit_id_is_a_usage_error(monkeypatch, capsys, tmp_path):
    proj, state = _project(tmp_path)
    code, payload = _scaffold(monkeypatch, capsys, state, proj, tmp_path / "runs",
                              "--unit", "u-nope")
    assert code == 1 and payload["error"]["kind"] == "usage"
    assert "u-nope" in payload["error"]["detail"]


def test_missing_state_path_is_a_usage_error(monkeypatch, capsys):
    code, payload = run_tool(monkeypatch, capsys, "scaffold.py", ["scaffold"])
    assert code == 1 and payload["error"]["kind"] == "usage"
    assert "migration-state" in payload["error"]["detail"]


# --------------------------------------------- canonical adapters ---------

def test_set_return_value_is_canonicalized_not_unserializable(monkeypatch, capsys, tmp_path):
    """A unit returning set[str] (unserializable per oracle.py) gets a
    canonical serialize() in the scaffolded adapters: sorted list — the
    verdict must be EQUIVALENT, not incomparable."""
    body = ("def tags(words: list[str]) -> set[str]:\n"
            "    return set(w for w in words if w)\n")
    proj, state = _project(tmp_path, name="t", unit="u-tags", module="tags", body=body)
    out = tmp_path / "runs"
    code, payload = _scaffold(monkeypatch, capsys, state, proj, out,
                              "--candidate-module", "t.tags")
    assert code == 0, payload["summary"]["human"]
    code, out_json = run_tool(monkeypatch, capsys, "oracle.py", [
        "judge", "--manifest", str(out / "manifests/u-tags.json")])
    assert code == 0, out_json["summary"]["human"]
    assert out_json["counts"]["incomparable"] == 0
    assert out_json["verdict"] == "equivalent"


# --------------------------------------------- module conformance (#48) ---

IND_PY = ('"""indicators — several public series functions sharing a domain\n'
          "vocabulary (the issue #48 shape: first-public judged only 1 of N).\n"
          "\n"
          'Invariant: empty values raises ValueError; period must be 1..64\n"""\n'
          "\n"
          "\n"
          "def sma_series(values: list[float], period: int = 3) -> list[float]:\n"
          "    if not values:\n"
          '        raise ValueError("empty values")\n'
          "    if not 1 <= period <= 64:\n"
          '        raise ValueError("period out of range")\n'
          "    return [sum(values[i:i + period]) / period\n"
          "            for i in range(len(values) - period + 1)]\n"
          "\n"
          "\n"
          "def rsi_series(values: list[float], period: int = 14) -> list[float]:\n"
          "    if not values:\n"
          '        raise ValueError("empty values")\n'
          "    if not 1 <= period <= 64:\n"
          '        raise ValueError("period out of range")\n'
          "    gains = [max(values[i] - values[i - 1], 0.0)\n"
          "             for i in range(1, len(values))]\n"
          "    return [round(sum(gains[:i + 1]) / (i + 1), 9)\n"
          "            for i in range(min(period, len(gains)))]\n"
          "\n"
          "\n"
          "def floor_div(n: int, d: int = 2) -> int:\n"
          "    return n // d\n")

# the wheel-side twin: identical EXCEPT rsi_series averages over i + 2 —
# exactly the kind of sibling-function divergence first-public never sees
IND_RUST_BUG = ('def sma_series(values: list[float], period: int = 3) -> list[float]:\n'
                "    if not values:\n"
                '        raise ValueError("empty values")\n'
                "    if not 1 <= period <= 64:\n"
                '        raise ValueError("period out of range")\n'
                "    return [sum(values[i:i + period]) / period\n"
                "            for i in range(len(values) - period + 1)]\n"
                "\n"
                "\n"
                "def rsi_series(values: list[float], period: int = 14) -> list[float]:\n"
                "    if not values:\n"
                '        raise ValueError("empty values")\n'
                "    if not 1 <= period <= 64:\n"
                '        raise ValueError("period out of range")\n'
                "    gains = [max(values[i] - values[i - 1], 0.0)\n"
                "             for i in range(1, len(values))]\n"
                "    return [round(sum(gains[:i + 1]) / (i + 2), 9)\n"  # the bug
                "            for i in range(min(period, len(gains)))]\n"
                "\n"
                "\n"
                "def floor_div(n: int, d: int = 2) -> int:\n"
                "    return n // d\n")


def _ind_project(tmp_path):
    """A unit module with THREE public functions (none named after the
    module, so the legacy pick was first-public) plus a buggy wheel twin."""
    proj, state = _project(tmp_path, name="ind", unit="u-ind",
                           module="indicators", body=IND_PY)
    (proj / "src" / "ind" / "indicators_rust.py").write_text(IND_RUST_BUG)
    return proj, state


def test_module_mode_catches_sibling_divergence_first_public_misses(
        monkeypatch, capsys, tmp_path):
    """The issue #48 acceptance case: a candidate whose FIRST function is
    faithful but a sibling diverges. The default (module) scaffold must
    verdict DIVERGENT with the diff naming the diverging function — while
    the legacy --mode function scaffold over the same unit and the same
    buggy candidate verdicts EQUIVALENT (the green light the issue
    describes)."""
    proj, state = _ind_project(tmp_path)
    out = tmp_path / "runs"
    code, payload = _scaffold(monkeypatch, capsys, state, proj, out,
                              "--candidate-module", "ind.indicators_rust")
    assert code == 0, payload["summary"]["human"]
    unit = payload["units"][0]
    assert unit["mode"] == "module"
    assert unit["functions"] == ["sma_series", "rsi_series", "floor_div"]
    assert sorted(unit["union_parameters"]) == ["d", "n", "period", "values"]

    code, out_json = run_tool(monkeypatch, capsys, "oracle.py", [
        "judge", "--manifest", str(out / "manifests/u-ind.json")])
    assert code == 2 and out_json["verdict"] == "divergent"
    divergent = [r for r in out_json["runs"] if r["verdict"] == "divergent"]
    assert divergent, "the sibling divergence must surface"
    assert all(d["diffs"][0]["path"].startswith("$.rsi_series")
               for d in divergent), "the diff must name the function"
    # the error-path rows compare equivalent through the envelope: both
    # sides raise the same ValueError per function
    equiv = [r for r in out_json["runs"] if r["verdict"] == "equivalent"]
    assert equiv and out_json["counts"]["incomparable"] == 0

    # the legacy single-entry scaffold over the SAME unit + candidate:
    # sma_series is first-public, it is faithful, so it verdicts EQUIVALENT
    # — the exact green light module mode exists to prevent
    out2 = tmp_path / "runs-fn"
    code, payload = _scaffold(monkeypatch, capsys, state, proj, out2,
                              "--mode", "function",
                              "--candidate-module", "ind.indicators_rust")
    assert code == 0
    assert payload["units"][0]["function"] == "sma_series"
    assert payload["units"][0]["function_source"] == "first-public"
    code, legacy = run_tool(monkeypatch, capsys, "oracle.py", [
        "judge", "--manifest", str(out2 / "manifests/u-ind.json")])
    assert code == 0 and legacy["verdict"] == "equivalent"


def test_module_mode_envelope_shape_and_row_validation(
        monkeypatch, capsys, tmp_path):
    """run() returns {fn: value | {"__error__": ...}} for every judged
    function; a row key no function declares, or a missing required union
    parameter, is a corpus bug and raises — never silently judged."""
    proj, state = _ind_project(tmp_path)
    out = tmp_path / "runs"
    _, payload = _scaffold(monkeypatch, capsys, state, proj, out)
    adapter = _load_module(Path(payload["units"][0]["files"]["ref_adapter"]))
    row = {"values": [1.0, 2.0, 3.0, 4.0], "period": 2, "n": 7}
    envelope = adapter.run(row)
    assert set(envelope) == {"sma_series", "rsi_series", "floor_div"}
    assert envelope["sma_series"] == [1.5, 2.5, 3.5]
    assert envelope["floor_div"] == 3
    # the documented error path is per-function evidence, not a failed row
    err = adapter.run({"values": [], "n": 0})
    assert err["sma_series"] == {"__error__": {"type": "ValueError",
                                               "message": "empty values"}}
    assert err["rsi_series"] == {"__error__": {"type": "ValueError",
                                               "message": "empty values"}}
    assert err["floor_div"] == 0
    # corpus-format guards raise row-level
    with pytest.raises(TypeError, match="not parameters of any judged function"):
        adapter.run({"values": [1.0], "bogus": 1})
    with pytest.raises(TypeError, match="missing required parameter"):
        adapter.run({"period": 3})  # values is required (sma/rsi take it)
    with pytest.raises(TypeError, match=r"\$\.period: expected int"):
        adapter.run({"values": [1.0], "period": 1.5, "n": 0})  # union spec guard


def test_module_mode_function_subset(monkeypatch, capsys, tmp_path):
    proj, state = _ind_project(tmp_path)
    out = tmp_path / "runs"
    code, payload = _scaffold(monkeypatch, capsys, state, proj, out,
                              "--function", "rsi_series,floor_div")
    assert code == 0, payload["summary"]["human"]
    unit = payload["units"][0]
    assert unit["functions"] == ["rsi_series", "floor_div"]
    assert unit["function_source"] == "flag"
    assert sorted(unit["union_parameters"]) == ["d", "n", "period", "values"]
    manifest = json.loads((out / "manifests/u-ind.json").read_text())
    assert manifest["functions"] == ["rsi_series", "floor_div"]

    code, payload = _scaffold(monkeypatch, capsys, state, proj, out,
                              "--function", "nope", "--force")
    assert code == 1 and payload["error"]["kind"] == "usage"
    assert "nope" in payload["error"]["detail"]
    assert "rsi_series" in payload["error"]["detail"]  # lists what exists


def test_function_mode_rejects_multiple_functions(monkeypatch, capsys, tmp_path):
    proj, state = _ind_project(tmp_path)
    code, payload = _scaffold(monkeypatch, capsys, state, proj, tmp_path / "runs",
                              "--mode", "function",
                              "--function", "sma_series", "--function", "rsi_series")
    assert code == 1 and payload["error"]["kind"] == "usage"
    assert "--mode module" in payload["error"]["detail"]


def test_module_mode_conflicting_param_annotations_are_flagged(
        monkeypatch, capsys, tmp_path):
    """A name annotated differently across functions cannot be fed by one
    JSON row mechanically — flagged for hand-completion, never guessed."""
    body = ("def take(x: int) -> int:\n"
            "    return x\n"
            "\n"
            "\n"
            "def give(x: str) -> str:\n"
            "    return x\n")
    proj, state = _project(tmp_path, name="c", unit="u-conf", module="conf", body=body)
    out = tmp_path / "runs"
    code, payload = _scaffold(monkeypatch, capsys, state, proj, out)
    assert code == 2, "a param conflict is a finding, not a guess"
    unit = payload["units"][0]
    flag = next(f for f in unit["hand_tune"] if f["where"] == "union param x")
    assert "take: int" in flag["reason"] and "give: str" in flag["reason"]
    assert unit["union_parameters"]["x"]["spec"]["kind"] == "unknown"
    gen = _load_module(Path(unit["files"]["generator"]))
    assert gen.flagged_params() == ["x"]
    ref = (out / "adapters/u_conf_ref.py").read_text()
    assert "conflicting" in ref or "union param x" in ref  # HAND-TUNE block


def test_module_mode_all_error_functions_is_a_degenerate_finding(
        monkeypatch, capsys, tmp_path):
    """Envelopes make rows 'ok' even when every function raised — so the
    module-mode dry-run must tally per-function errors and refuse a corpus
    on which NO function ever succeeds (the module face of issue #30)."""
    body = ("def a(x: int) -> int:\n"
            "    raise RuntimeError('always a')\n"
            "\n"
            "\n"
            "def b(y: int) -> int:\n"
            "    raise RuntimeError('always b')\n")
    proj, state = _project(tmp_path, name="z", unit="u-zall", module="zall", body=body)
    code, payload = _scaffold(monkeypatch, capsys, state, proj, tmp_path / "runs")
    assert code == 2
    unit = payload["units"][0]
    assert unit["corpus_dry_run"]["degenerate"] is True
    finding = next(f for f in payload["findings"] if f["kind"] == "degenerate_corpus")
    assert "every judged function" in finding["detail"]


def test_module_mode_single_all_error_function_warns(monkeypatch, capsys, tmp_path):
    """One always-raising sibling next to healthy functions: the verdict
    for that function rests on nothing — a warning naming it, exit 0 (the
    corpus still judges the rest)."""
    body = ("def ok_fn(x: int) -> int:\n"
            "    return x + 1\n"
            "\n"
            "\n"
            "def boom_fn(x: int) -> int:\n"
            "    raise RuntimeError('nope')\n")
    proj, state = _project(tmp_path, name="y", unit="u-y", module="ymod", body=body)
    code, payload = _scaffold(monkeypatch, capsys, state, proj, tmp_path / "runs")
    assert code == 0, payload["summary"]["human"]
    assert any("module-mode function 'boom_fn' errored on every reference dry-run row"
               in w for w in payload["warnings"])
    stats = payload["units"][0]["corpus_dry_run"]["functions"]
    assert stats["boom_fn"]["ok"] == 0 and stats["ok_fn"]["ok"] > 0

    """The adapter must not silently widen types (int_float_distinct is a
    divergence rule, not a normalization): a float row for an int param is
    a corpus bug and must raise, not coerce."""
    body = ("def twice(n: int) -> int:\n"
            "    return n * 2\n")
    proj, state = _project(tmp_path, name="w", unit="u-twice", module="twice", body=body)
    out = tmp_path / "runs"
    _, payload = _scaffold(monkeypatch, capsys, state, proj, out)
    adapter = _load_module(Path(payload["units"][0]["files"]["ref_adapter"]))
    with pytest.raises(TypeError, match=r"\$\.n: expected int"):  # never silently coerce
        adapter.run({"n": 1.0})
    # module envelope: the judged function's result under its own key
    assert adapter.run({"n": 21}) == {"twice": 42}
