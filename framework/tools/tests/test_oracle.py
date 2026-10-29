"""oracle.py acceptance tests — tools/README.md #1.

The big one: `self-test` must catch 5/5 injected bugs (the Stage 2
judge-first hard gate), with normalization rules documented and no LLM in
the judging path."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import oracle
from conftest import run_tool


def test_self_test_catches_5_of_5(monkeypatch, capsys):
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["self-test"])
    assert code == 0
    assert out["caught"] == 5 and out["injected_bugs"] == 5
    kinds = {r["bug"] for r in out["results"]}
    assert kinds == {"off-by-one", "dropped-branch", "swapped-args",
                     "wrong-type-coercion", "swallowed-error"}
    # every caught bug carries diff evidence
    assert all(r["caught"] and r["divergent_runs"] >= 1 for r in out["results"])
    assert all(r["evidence"] for r in out["results"])
    assert out["llm_in_judging_path"] is False


def test_self_test_proves_rules_do_not_over_forgive(monkeypatch, capsys):
    _, out = run_tool(monkeypatch, capsys, "oracle.py", ["self-test"])
    for case in out["benign_normalization_cases"]:
        assert case["verdict"] == "equivalent", case


def test_rules_are_documented(monkeypatch, capsys):
    _, out = run_tool(monkeypatch, capsys, "oracle.py", ["rules"])
    for key in out["defaults"]:
        assert key in out["docs"], f"rule {key} lacks documentation"
    assert set(out["verdicts"]) == {"equivalent", "divergent", "incomparable"}


def test_classify_int_float_distinct():
    rules = oracle.merge_rules({})
    v = oracle.classify_pair(0, {"ok": True, "value": 1},
                             {"ok": True, "value": 1.0}, rules)
    assert v["verdict"] == "divergent"
    assert v["diffs"][0]["kind"] == "numeric-type"


def test_classify_float_tolerance_and_dict_order():
    rules = oracle.merge_rules({})
    v = oracle.classify_pair(0, {"ok": True, "value": 0.1 + 0.2},
                             {"ok": True, "value": 0.3}, rules)
    assert v["verdict"] == "equivalent"
    v = oracle.classify_pair(1, {"ok": True, "value": {"b": 1, "a": 2}},
                             {"ok": True, "value": {"a": 2, "b": 1}}, rules)
    assert v["verdict"] == "equivalent"


def test_classify_error_type_mismatch_and_message_mode():
    rules = oracle.merge_rules({})
    v = oracle.classify_pair(0, {"ok": False, "error": {"type": "ValueError", "message": "x"}},
                             {"ok": False, "error": {"type": "TypeError", "message": "x"}}, rules)
    assert v["verdict"] == "divergent"
    rules_type_only = oracle.merge_rules({"normalization": {"error_compare": "type-only"}})
    v = oracle.classify_pair(0, {"ok": False, "error": {"type": "ValueError", "message": "x"}},
                             {"ok": False, "error": {"type": "ValueError", "message": "y"}},
                             rules_type_only)
    assert v["verdict"] == "equivalent"


def test_incomparable_when_reference_lacks_ground_truth():
    rules = oracle.merge_rules({})
    v = oracle.classify_pair(0, {"ok": False, "error": {"type": "Boom", "message": "ref broke"}},
                             None, rules)
    assert v["verdict"] == "incomparable"
    v = oracle.classify_pair(1, {"unserializable": True}, {"ok": True, "value": 1}, rules)
    assert v["verdict"] == "incomparable"


def test_compare_command_exit_codes(monkeypatch, capsys, tmp_path):
    runs = tmp_path / "runs.json"
    runs.write_text(json.dumps({
        "unit": "u-test",
        "reference": [{"i": 0, "ok": True, "value": {"a": 1}},
                      {"i": 1, "ok": False, "error": {"type": "ValueError", "message": "m"}}],
        "candidate": [{"i": 0, "ok": True, "value": {"a": 2}},
                      {"i": 1, "ok": True, "value": None}],
    }))
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["compare", "--runs", str(runs)])
    assert code == 2  # divergent dominates
    assert out["verdict"] == "divergent"
    assert out["counts"] == {"equivalent": 0, "divergent": 2, "incomparable": 0}
    kinds = {d["kind"] for r in out["runs"] for d in r["diffs"]}
    assert "int" in kinds and "swallowed-error" in kinds
    # deterministic digest: same inputs -> same verdicts digest
    code2, out2 = run_tool(monkeypatch, capsys, "oracle.py",
                           ["compare", "--runs", str(runs)])
    assert out2["verdicts_digest"] == out["verdicts_digest"]


def test_judge_subprocess_end_to_end(monkeypatch, capsys, tmp_path):
    (tmp_path / "impl.py").write_text(
        "def run(v):\n"
        "    if v['bad']:\n"
        "        raise ValueError('bad input')\n"
        "    return {'n': len(v['xs']), 'total': sum(v['xs'])}\n")
    (tmp_path / "cand.py").write_text(
        "def run(v):\n"
        "    if v['bad']:\n"
        "        return {'n': 0, 'total': 0}   # swallowed error\n"
        "    return {'n': len(v['xs']), 'total': sum(v['xs'])}\n")
    (tmp_path / "corpus.jsonl").write_text("\n".join([
        json.dumps({"xs": [1, 2], "bad": False}),
        json.dumps({"xs": [], "bad": True}),
    ]) + "\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "unit": "u-1",
        "reference": {"entry": "impl:run"},
        "candidate": {"entry": "cand:run"},
        "corpus": "corpus.jsonl",
    }))
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["judge", "--manifest", str(manifest)])
    assert code == 2
    assert out["counts"] == {"equivalent": 1, "divergent": 1, "incomparable": 0}
    assert out["runs"][1]["diffs"][0]["kind"] == "swallowed-error"
    assert out["corpus"]["lines"] == 2


# ---------------------------------------------------------- issue #32 ----
# Entries may also be relative *.py file paths, resolved against the
# manifest's directory (the child's cwd) — scaffold.py emits adapters that
# live in their own directory next to the manifest, and the manifest must
# stay judge-ready verbatim.

def test_judge_accepts_file_path_entries(monkeypatch, capsys, tmp_path):
    (tmp_path / "adapters").mkdir()
    (tmp_path / "manifests").mkdir()
    (tmp_path / "adapters" / "impl.py").write_text(
        "def run(v):\n"
        "    if v['bad']:\n"
        "        raise ValueError('bad input')\n"
        "    return {'n': len(v['xs'])}\n")
    (tmp_path / "adapters" / "cand.py").write_text(
        "def run(v):\n"
        "    if v['bad']:\n"
        "        return {'n': 0}   # swallowed error\n"
        "    return {'n': len(v['xs'])}\n")
    (tmp_path / "corpus.jsonl").write_text("\n".join([
        json.dumps({"xs": [1, 2], "bad": False}),
        json.dumps({"xs": [], "bad": True}),
    ]) + "\n")
    manifest = tmp_path / "manifests" / "m.json"
    manifest.write_text(json.dumps({
        "unit": "u-1",
        "reference": {"entry": "../adapters/impl.py:run"},
        "candidate": {"entry": "../adapters/cand.py:run"},
        "corpus": "../corpus.jsonl",
    }))
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["judge", "--manifest", str(manifest)])
    assert code == 2
    assert out["counts"] == {"equivalent": 1, "divergent": 1, "incomparable": 0}
    assert out["runs"][1]["diffs"][0]["kind"] == "swallowed-error"


def test_judge_file_path_entry_missing_is_setup_failure(monkeypatch, capsys, tmp_path):
    (tmp_path / "impl.py").write_text("def run(v):\n    return v\n")
    (tmp_path / "corpus.jsonl").write_text(json.dumps({"x": 1}) + "\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "unit": "u-1",
        "reference": {"entry": "./impl.py:run"},
        "candidate": {"entry": "./not_there.py:run"},
        "corpus": "corpus.jsonl",
    }))
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["judge", "--manifest", str(manifest)])
    assert code == 3  # a side that cannot run at all: incomparable, detail names it
    assert "not_there.py" in out["runs"][0]["detail"]
    assert "entry file not found" in out["runs"][0]["detail"]


def test_local_runner_resolves_file_path_entries(tmp_path):
    entry_mod = tmp_path / "entry.py"
    entry_mod.write_text("def double(v):\n    return v * 2\n")
    fn = oracle._load_entry_callable(f"{entry_mod}:double")
    assert fn(21) == 42
    entry_mod.write_text("def run(v):\n    return v + 1\n")
    fn = oracle._load_entry_callable(str(entry_mod))  # attr defaults to run
    assert fn(1) == 2


def test_corpus_validate_accepts_file_path_entry(monkeypatch, capsys, tmp_path):
    (tmp_path / "adapters").mkdir()
    (tmp_path / "adapters" / "impl.py").write_text("def run(v):\n    return v['x'] * 2\n")
    (tmp_path / "corpus.jsonl").write_text("\n".join([
        json.dumps({"x": 1}), json.dumps({"x": 2})]) + "\n")
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["corpus", "--validate", str(tmp_path / "corpus.jsonl"),
                          "--entry", "adapters/impl.py:run"])
    assert code == 0
    assert out["ok"] == out["total"] == 2


# ---------------------------------------------------------- issue #29 ----
# The module docstring used to imply the corpus file itself was JSONL
# {"i": ..., "input": ...}; following it double-wraps every input, both
# sides raise the same TypeError, and the judge reports a hollow 40/40
# EQUIVALENT. The corpus file is one raw JSON value per line; the framing
# is internal stdin protocol.

def test_docs_pin_the_corpus_file_format():
    doc = oracle.__doc__
    assert "one raw JSON value per" in doc
    assert '{"xs": [1, 2], "bad": false}' in doc
    assert '{"xs": [], "bad": true}' in doc
    assert "internal stdin framing" in doc
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    assert "one raw" in readme and "internal stdin framing" in readme
    assert '{"xs": [1, 2], "bad": false}' in readme


def test_judge_refuses_prewrapped_corpus(monkeypatch, capsys, tmp_path):
    (tmp_path / "impl.py").write_text("def run(v):\n    return v\n")
    (tmp_path / "corpus.jsonl").write_text("\n".join([
        json.dumps({"i": 0, "input": {"xs": [1, 2], "bad": False}}),
        json.dumps({"i": 1, "input": {"xs": [], "bad": True}}),
    ]) + "\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "unit": "u-1",
        "reference": {"entry": "impl:run"},
        "candidate": {"entry": "impl:run"},
        "corpus": "corpus.jsonl",
    }))
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["judge", "--manifest", str(manifest)])
    assert code == 1
    assert out["error"]["kind"] == "usage"
    assert "pre-wrapped" in out["error"]["detail"]
    assert "raw JSON values" in out["error"]["detail"]


def test_prewrap_guard_tolerates_legitimate_keys(tmp_path):
    # each value defeats one clause of the every-line-is-framing guard
    values = [
        {"input": "a"},                           # missing "i"
        {"i": 2, "input": {"x": 1}, "extra": 0},  # extra key
        {"i": "not-an-int", "input": [1]},        # i not an int
        [1, 2, 3],                                # not even a dict
    ]
    path = tmp_path / "corpus.jsonl"
    path.write_text("\n".join(json.dumps(v) for v in values) + "\n")
    assert oracle._load_corpus(path) == values


def test_corpus_validate_reports_per_row_status(monkeypatch, capsys, tmp_path):
    (tmp_path / "impl.py").write_text(
        "def run(v):\n"
        "    if v['bad']:\n"
        "        raise ValueError('bad input')\n"
        "    return {'n': len(v['xs']), 'total': sum(v['xs'])}\n")
    (tmp_path / "corpus.jsonl").write_text("\n".join([
        json.dumps({"xs": [1, 2], "bad": False}),
        json.dumps({"xs": [], "bad": True}),
    ]) + "\n")
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["corpus", "--validate", str(tmp_path / "corpus.jsonl"),
                          "--entry", "impl:run"])
    assert code == 2  # one error row -> findings
    assert out["rows"][0]["status"] == "ok"
    assert out["rows"][1]["status"] == "error"
    assert "ValueError" in out["rows"][1]["detail"]
    assert out["counts"] == {"ok": 1, "error": 1}
    assert out["hollow_equivalent_risk"] is False
    assert out["corpus"]["lines"] == 2


def test_corpus_validate_flags_the_hollow_signature(monkeypatch, capsys, tmp_path):
    # an entry that errors on every input is the hollow-verdict signature
    (tmp_path / "impl.py").write_text(
        "def run(v):\n"
        "    return {'n': len(v['payload']['xs'])}\n")  # KeyError on every row
    (tmp_path / "corpus.jsonl").write_text("\n".join([
        json.dumps({"xs": [1, 2]}),
        json.dumps({"xs": []}),
    ]) + "\n")
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["corpus", "--validate", str(tmp_path / "corpus.jsonl"),
                          "--entry", "impl:run"])
    assert code == 2
    assert all(r["status"] == "error" for r in out["rows"])
    assert out["hollow_equivalent_risk"] is True
    assert "hollow" in out["warning"]


def test_corpus_validate_all_ok_exits_zero(monkeypatch, capsys, tmp_path):
    (tmp_path / "impl.py").write_text("def run(v):\n    return v['x'] * 2\n")
    (tmp_path / "corpus.jsonl").write_text("\n".join([
        json.dumps({"x": 1}), json.dumps({"x": 2})]) + "\n")
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["corpus", "--validate", str(tmp_path / "corpus.jsonl"),
                          "--entry", "impl:run"])
    assert code == 0
    assert out["ok"] == out["total"] == 2
    assert out["hollow_equivalent_risk"] is False


def test_corpus_validate_setup_failure_exits_four(monkeypatch, capsys, tmp_path):
    (tmp_path / "corpus.jsonl").write_text(json.dumps({"x": 1}) + "\n")
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["corpus", "--validate", str(tmp_path / "corpus.jsonl"),
                          "--entry", "no_such_module:run"])
    assert code == 4
    assert "setup_error" in out


def test_corpus_validate_also_refuses_prewrapped(monkeypatch, capsys, tmp_path):
    (tmp_path / "corpus.jsonl").write_text("\n".join([
        json.dumps({"i": 0, "input": {"x": 1}}),
        json.dumps({"i": 1, "input": {"x": 2}}),
    ]) + "\n")
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["corpus", "--validate", str(tmp_path / "corpus.jsonl"),
                          "--entry", "impl:run"])
    assert code == 1
    assert out["error"]["kind"] == "usage"
    assert "pre-wrapped" in out["error"]["detail"]


# ---------------------------------------------------------- issue #30 ----
# A corpus whose every row the reference implementation rejects used to
# judge 40/40 EQUIVALENT (compare_error calls error==error equivalent per
# run) — a hollow pass: not one row had ground truth. The summary verdict
# must be INCOMPARABLE, never EQUIVALENT; error-path mismatches on such a
# corpus are corpus artifacts, not candidate defects.

def _degenerate_scaffold(tmp_path, cand_src=None):
    """Manifest whose reference rejects every corpus row; the candidate
    errors identically unless ``cand_src`` says otherwise."""
    ref_src = "def run(v):\n    raise TypeError('rows are not what the reference expects')\n"
    (tmp_path / "impl.py").write_text(ref_src)
    (tmp_path / "cand.py").write_text(cand_src or ref_src)
    (tmp_path / "corpus.jsonl").write_text("\n".join(
        json.dumps({"x": i}) for i in range(4)) + "\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "unit": "u-30",
        "reference": {"entry": "impl:run"},
        "candidate": {"entry": "cand:run"},
        "corpus": "corpus.jsonl",
    }))
    return manifest


def test_judge_all_error_reference_corpus_is_incomparable(monkeypatch, capsys, tmp_path):
    # the exact Stage-2 scenario: both sides raise the SAME exception on
    # every row -> 4/4 per-run equivalent, zero ground truth
    manifest = _degenerate_scaffold(tmp_path)
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["judge", "--manifest", str(manifest)])
    assert code == 3
    assert out["verdict"] == "incomparable"
    assert out["counts"] == {"equivalent": 4, "divergent": 0, "incomparable": 0}
    assert out["ground_truth"] == {
        "reference_ok": 0, "runs": 4, "reference_ok_fraction": 0.0,
        "min_reference_ok_fraction": 0.5, "degenerate": True,
        "detail": "reference errored on every corpus row — corpus is "
                  "degenerate for this entry",
        "warning": None,
    }
    assert "INCOMPARABLE" in out["summary"]["human"]
    assert "degenerate" in out["summary"]["human"]
    assert "not parity evidence" in out["summary"]["human"]


def test_degenerate_overrides_divergent_error_pairs(monkeypatch, capsys, tmp_path):
    # candidate raises a DIFFERENT exception on every row: per-run divergent,
    # but with zero reference ground truth the summary stays INCOMPARABLE —
    # the error-path evidence is kept for the operator, it just cannot gate
    manifest = _degenerate_scaffold(
        tmp_path, cand_src="def run(v):\n    raise KeyError('different')\n")
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["judge", "--manifest", str(manifest)])
    assert code == 3
    assert out["verdict"] == "incomparable"
    assert out["counts"] == {"equivalent": 0, "divergent": 4, "incomparable": 0}
    assert out["ground_truth"]["degenerate"] is True
    assert out["diff_kind_histogram"] == {"error-type": 4}
    assert "corpus artifacts" in out["summary"]["human"]


def test_judge_setup_failure_still_incomparable_with_detail(monkeypatch, capsys, tmp_path):
    # a reference that cannot even be imported: no ground truth on any row
    manifest = _degenerate_scaffold(tmp_path)
    m = json.loads((tmp_path / "manifest.json").read_text())
    m["reference"] = {"entry": "no_such_module:run"}
    (tmp_path / "manifest.json").write_text(json.dumps(m))
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["judge", "--manifest", str(manifest)])
    assert code == 3
    assert out["verdict"] == "incomparable"
    assert out["counts"]["incomparable"] == 4
    assert out["ground_truth"]["degenerate"] is True
    assert "failed to run" in out["ground_truth"]["detail"]
    assert "no ground truth on any row" in out["ground_truth"]["detail"]


def test_compare_all_error_reference_runs_is_incomparable(monkeypatch, capsys, tmp_path):
    # shadow-mode (Stage 6) pre-captured runs get the same guard
    runs = tmp_path / "runs.json"
    err = {"type": "TypeError", "message": "m"}
    runs.write_text(json.dumps({
        "unit": "u-30",
        "reference": [{"i": i, "ok": False, "error": err} for i in range(2)],
        "candidate": [{"i": i, "ok": False, "error": err} for i in range(2)],
    }))
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["compare", "--runs", str(runs)])
    assert code == 3
    assert out["verdict"] == "incomparable"
    assert out["counts"]["equivalent"] == 2
    assert out["ground_truth"]["degenerate"] is True


def test_thin_ground_truth_warns_but_can_stay_equivalent(monkeypatch, capsys, tmp_path):
    # 1 of 4 reference rows ok (25% < the default 50%): the verdict can
    # still be equivalent, but the warning fires — nobody should believe a
    # green verdict built on a mostly-rejected corpus without seeing it
    src = ("def run(v):\n"
           "    if v['bad']:\n"
           "        raise ValueError('bad input')\n"
           "    return {'n': len(v['xs'])}\n")
    (tmp_path / "impl.py").write_text(src)
    (tmp_path / "cand.py").write_text(src)
    (tmp_path / "corpus.jsonl").write_text("\n".join(
        json.dumps({"xs": [i], "bad": i > 0}) for i in range(4)) + "\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "unit": "u-thin", "reference": {"entry": "impl:run"},
        "candidate": {"entry": "cand:run"}, "corpus": "corpus.jsonl",
    }))
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["judge", "--manifest", str(manifest)])
    assert code == 0
    assert out["verdict"] == "equivalent"
    assert out["counts"]["equivalent"] == 4
    gt = out["ground_truth"]
    assert gt["degenerate"] is False
    assert gt["reference_ok"] == 1 and gt["reference_ok_fraction"] == 0.25
    assert "1/4" in gt["warning"] and "min_reference_ok_fraction" in gt["warning"]
    assert "WARNING" in out["summary"]["human"]


def test_min_reference_ok_fraction_is_configurable(monkeypatch, capsys, tmp_path):
    # same 1/4-ok runs: 0.2 admits 25%, 0 silences the warning entirely
    ok = {"i": 0, "ok": True, "value": {"n": 1}}
    bad = [{"i": i, "ok": False, "error": {"type": "ValueError", "message": "bad"}}
           for i in (1, 2, 3)]
    for frac, warns in ((0.2, False), (0.0, False), (0.5, True)):
        runs = tmp_path / f"runs-{frac}.json"
        runs.write_text(json.dumps({
            "unit": "u-thin",
            "normalization": {"min_reference_ok_fraction": frac},
            "reference": [ok] + bad, "candidate": [ok] + bad,
        }))
        code, out = run_tool(monkeypatch, capsys, "oracle.py",
                             ["compare", "--runs", str(runs)])
        assert code == 0 and out["verdict"] == "equivalent"
        assert out["ground_truth"]["min_reference_ok_fraction"] == frac
        assert (out["ground_truth"]["warning"] is not None) is warns


def test_zero_ok_guard_cannot_be_disabled(monkeypatch, capsys, tmp_path):
    # min_reference_ok_fraction=0 only silences the WARNING; the zero-ok
    # INCOMPARABLE guard itself is not optional (the guard belongs in the
    # judge, not in the operator's discipline)
    runs = tmp_path / "runs.json"
    err = {"type": "TypeError", "message": "m"}
    runs.write_text(json.dumps({
        "unit": "u-30",
        "normalization": {"min_reference_ok_fraction": 0},
        "reference": [{"i": i, "ok": False, "error": err} for i in range(2)],
        "candidate": [{"i": i, "ok": False, "error": err} for i in range(2)],
    }))
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["compare", "--runs", str(runs)])
    assert code == 3
    assert out["verdict"] == "incomparable"
    assert out["ground_truth"]["degenerate"] is True
    assert out["ground_truth"]["warning"] is None


def test_bad_min_reference_ok_fraction_is_refused(monkeypatch, capsys, tmp_path):
    for bad in (1.5, True, "half"):
        runs = tmp_path / "runs.json"
        runs.write_text(json.dumps({
            "unit": "u", "normalization": {"min_reference_ok_fraction": bad},
            "reference": [], "candidate": [],
        }))
        code, out = run_tool(monkeypatch, capsys, "oracle.py",
                             ["compare", "--runs", str(runs)])
        assert code == 1
        assert out["error"]["kind"] == "usage"
        assert "min_reference_ok_fraction" in out["error"]["detail"]


def test_rules_document_min_reference_ok_fraction(monkeypatch, capsys):
    _, out = run_tool(monkeypatch, capsys, "oracle.py", ["rules"])
    assert out["defaults"]["min_reference_ok_fraction"] == 0.5
    assert "INCOMPARABLE" in out["docs"]["min_reference_ok_fraction"]
    assert "zero ok reference runs" in out["docs"]["min_reference_ok_fraction"]


def test_self_test_proves_the_degenerate_corpus_guard(monkeypatch, capsys):
    _, out = run_tool(monkeypatch, capsys, "oracle.py", ["self-test"])
    guard = out["degenerate_corpus_guard"]
    assert guard["held"] is True
    assert guard["summary_verdict"] == "incomparable"
    assert guard["per_run_equivalent"] == 2
    assert guard["reference_ok"] == 0
    assert "guard held" in out["summary"]["human"]


def test_corpus_validate_warns_on_thin_ground_truth(monkeypatch, capsys, tmp_path):
    # 1 of 4 rows ok: not hollow (a value was produced) but below the
    # judge's default threshold — the dry-run warns before a judge run
    (tmp_path / "impl.py").write_text(
        "def run(v):\n"
        "    if v['bad']:\n"
        "        raise ValueError('bad input')\n"
        "    return {'n': len(v['xs'])}\n")
    (tmp_path / "corpus.jsonl").write_text("\n".join(
        json.dumps({"xs": [i], "bad": i > 0}) for i in range(4)) + "\n")
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["corpus", "--validate", str(tmp_path / "corpus.jsonl"),
                          "--entry", "impl:run"])
    assert code == 2  # three error rows
    assert out["hollow_equivalent_risk"] is False
    assert out["reference_ok_fraction"] == 0.25
    assert out["warning"] and "1/4" in out["warning"]
    assert "THIN" in out["summary"]["human"]


# ---------------------------------------------------------- issue #75 ----
# The judge streams the corpus instead of materializing it: pre-flight
# refusals before any child spawns, rows judged chunk by chunk (each chunk
# its own child per side with its own timeout), verdicts folded as chunks
# complete with a verbatim-retention cap.

def _stream_scaffold(tmp_path, rows=10, ref_src=None, cand_src=None):
    (tmp_path / "impl.py").write_text(ref_src or "def run(v):\n    return {'x': v['x'] * 2}\n")
    (tmp_path / "cand.py").write_text(cand_src or "def run(v):\n    return {'x': v['x'] * 2}\n")
    (tmp_path / "corpus.jsonl").write_text("\n".join(
        json.dumps({"x": i}) for i in range(rows)) + "\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "unit": "u-75",
        "reference": {"entry": "impl:run"},
        "candidate": {"entry": "cand.py:run"},
        "corpus": "corpus.jsonl",
    }))
    return manifest


def _oversized_scaffold(tmp_path):
    # ~300 KB of corpus: over a 1 MB x4 footprint estimate, under it with --runs 5
    (tmp_path / "impl.py").write_text("def run(v):\n    return len(v['pad'])\n")
    (tmp_path / "corpus.jsonl").write_text("\n".join(
        json.dumps({"i": i, "pad": "z" * 1000}) for i in range(300)) + "\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "unit": "u-big", "reference": {"entry": "impl:run"},
        "candidate": {"entry": "impl:run"}, "corpus": "corpus.jsonl"}))
    return manifest


def test_judge_streams_stdin_in_chunks(monkeypatch, capsys, tmp_path):
    # with --chunk-rows 3 a 10-row corpus spawns 4 children per side, each
    # receiving at most 3 framed rows — never one bulk stdin
    manifest = _stream_scaffold(tmp_path, rows=10)
    stdins = []
    real_run_child = oracle.run_child

    def spy_run_child(cmd, **kwargs):
        stdins.append(kwargs.get("input_text") or "")
        return real_run_child(cmd, **kwargs)

    monkeypatch.setattr(oracle, "run_child", spy_run_child)
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["judge", "--manifest", str(manifest), "--chunk-rows", "3"])
    assert code == 0
    assert out["counts"] == {"equivalent": 10, "divergent": 0, "incomparable": 0}
    assert len(stdins) == 8  # ceil(10/3) = 4 chunks x 2 sides
    corpus_bytes = (tmp_path / "corpus.jsonl").stat().st_size
    assert all(s.count("\n") <= 3 for s in stdins)
    assert max(len(s) for s in stdins) < corpus_bytes
    # the 10 framed inputs all reached the children exactly once
    framed = [json.loads(line) for s in stdins for line in s.splitlines()]
    assert sorted(f["i"] for f in framed) == sorted(list(range(10)) * 2)


def test_verdict_payload_is_chunk_size_invariant(monkeypatch, capsys, tmp_path):
    # counts, digests, histograms, ground truth and every run entry are a
    # pure function of (records, rules) — chunking must not leak into them
    manifest = _stream_scaffold(tmp_path, rows=7, cand_src=(
        "def run(v):\n"
        "    if v['x'] % 2:\n"
        "        return {'x': v['x'] * 3}   # divergent on odd rows\n"
        "    return {'x': v['x'] * 2}\n"))
    payloads = []
    for chunk_rows in ("1", "3", "500"):
        _, out = run_tool(monkeypatch, capsys, "oracle.py",
                          ["judge", "--manifest", str(manifest),
                           "--chunk-rows", chunk_rows])
        out.pop("generated_at")
        payloads.append(out)
    assert payloads[0] == payloads[1] == payloads[2]
    assert payloads[0]["counts"] == {"equivalent": 4, "divergent": 3, "incomparable": 0}


def test_slow_row_times_out_its_chunk_only(monkeypatch, capsys, tmp_path):
    # the adjacent failure behind #73's secondary observation: one slow
    # reference function used to blow the whole-run timeout. The slow chunk
    # times out alone; every other chunk still produces verdicts.
    slow = ("import time\n"
            "def run(v):\n"
            "    if v['slow']:\n"
            "        time.sleep(60)\n"
            "    return {'x': v['x'] * 2}\n")
    (tmp_path / "impl.py").write_text(slow)
    (tmp_path / "cand.py").write_text(slow)
    (tmp_path / "corpus.jsonl").write_text("\n".join(
        json.dumps({"x": i, "slow": i in (4, 5)}) for i in range(10)) + "\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "unit": "u-slow", "reference": {"entry": "impl:run"},
        "candidate": {"entry": "cand:run"}, "corpus": "corpus.jsonl"}))
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["judge", "--manifest", str(manifest),
                          "--chunk-rows", "2", "--timeout", "2"])
    assert code == 3  # incomparable rows, no divergence
    assert out["counts"] == {"equivalent": 8, "divergent": 0, "incomparable": 2}
    assert out["runs"][4]["verdict"] == "incomparable"
    assert "chunk timed out after 2.0s" in out["runs"][4]["detail"]
    assert out["runs"][6]["verdict"] == "equivalent"  # chunks after the slow one judged


def test_oversized_corpus_refused_before_any_child_spawns(monkeypatch, capsys, tmp_path):
    # estimated footprint (bytes x 4) over budget -> the standard error
    # envelope, and run_child is never called at all
    manifest = _oversized_scaffold(tmp_path)

    def no_spawn(cmd, **kwargs):
        raise AssertionError("a child was spawned for an oversized corpus")

    monkeypatch.setattr(oracle, "run_child", no_spawn)
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["judge", "--manifest", str(manifest),
                          "--max-corpus-mb", "1"])
    assert code == 1
    assert out["error"]["kind"] == "usage"
    assert "too large to judge" in out["error"]["detail"]
    assert "--runs" in out["error"]["detail"]  # the documented repair


def test_runs_cap_admits_an_oversized_corpus(monkeypatch, capsys, tmp_path):
    # an explicit --runs cap scales the estimated footprint to the rows
    # that will actually be judged
    manifest = _oversized_scaffold(tmp_path)
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["judge", "--manifest", str(manifest),
                          "--max-corpus-mb", "1", "--runs", "5"])
    assert code == 0
    assert out["counts"] == {"equivalent": 5, "divergent": 0, "incomparable": 0}
    assert out["corpus"]["lines"] == 5  # judged rows, not file rows


def test_retention_cap_summarizes_runs_beyond_the_first_n(monkeypatch, capsys, tmp_path):
    # the first RETAIN_VERBATIM_RUNS run entries stay verbatim; beyond
    # that entries are summarized — while counts, histogram and
    # verdicts_digest still cover every run
    monkeypatch.setattr(oracle, "RETAIN_VERBATIM_RUNS", 4)
    manifest = _stream_scaffold(tmp_path, rows=12, cand_src=(
        "def run(v):\n    return {'x': v['x'] * 2 + 1}\n"))  # every row divergent
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["judge", "--manifest", str(manifest)])
    assert code == 2
    assert out["counts"] == {"equivalent": 0, "divergent": 12, "incomparable": 0}
    assert out["diff_kind_histogram"] == {"int": 12}  # full corpus, not the cap
    assert out["runs_summarized"] == 8
    verbatim = out["runs"][:4]
    assert all("diffs" in r and "summarized" not in r for r in verbatim)
    summarized = out["runs"][4:]
    assert all(r["summarized"] is True and r["verdict"] == "divergent"
               and r["diff_digest"] for r in summarized)
    assert [r["run"] for r in out["runs"]] == list(range(12))  # still indexable


def test_retention_cap_covers_all_runs_in_the_digest(monkeypatch, capsys, tmp_path):
    # the digest is folded incrementally but must equal the list-era
    # sha256("\n".join(f"{run}:{verdict}"))
    manifest = _stream_scaffold(tmp_path, rows=9)
    monkeypatch.setattr(oracle, "RETAIN_VERBATIM_RUNS", 2)
    _, out = run_tool(monkeypatch, capsys, "oracle.py",
                      ["judge", "--manifest", str(manifest)])
    expected = hashlib.sha256(
        "\n".join(f"{r['run']}:{r['verdict']}" for r in out["runs"]).encode()
    ).hexdigest()[:16]
    assert out["verdicts_digest"] == expected


def test_corpus_dry_run_truncates_beyond_retention(monkeypatch, capsys, tmp_path):
    # the dry-run keeps per-row status verbatim only up to
    # RETAIN_VERBATIM_ROWS; counts/total always cover every judged row
    monkeypatch.setattr(oracle, "RETAIN_VERBATIM_ROWS", 2)
    (tmp_path / "impl.py").write_text("def run(v):\n    return {'x': v['x'] * 2}\n")
    (tmp_path / "corpus.jsonl").write_text("\n".join(
        json.dumps({"x": i}) for i in range(5)) + "\n")
    code, out = run_tool(monkeypatch, capsys, "oracle.py",
                         ["corpus", "--validate", str(tmp_path / "corpus.jsonl"),
                          "--entry", "impl:run"])
    assert code == 0
    assert len(out["rows"]) == 2
    assert out["rows_truncated"] == 3
    assert out["ok"] == out["total"] == 5  # full corpus, not the cap
    assert out["counts"] == {"ok": 5}


def test_streamed_refusals_keep_file_line_context(tmp_path):
    # malformed lines still refuse with file:line context from the
    # streaming loader — same shape read_jsonl gave
    path = tmp_path / "corpus.jsonl"
    path.write_text('{"x": 1}\nnot json\n')
    try:
        list(oracle._iter_corpus_rows(path))
        raise AssertionError("malformed line was not refused")
    except ValueError as e:
        assert f"{path}:2: invalid JSON line" in str(e)
    # the pre-wrapped and empty refusals fire in the pre-flight pass,
    # before any row is yielded
    prewrapped = tmp_path / "pre.jsonl"
    prewrapped.write_text("\n".join(
        json.dumps({"i": i, "input": {"x": i}}) for i in range(2)) + "\n")
    try:
        oracle._corpus_preflight(prewrapped)
        raise AssertionError("pre-wrapped corpus was not refused")
    except ValueError as e:
        assert "pre-wrapped" in str(e)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n\n")
    try:
        oracle._corpus_preflight(empty)
        raise AssertionError("empty corpus was not refused")
    except ValueError as e:
        assert "is empty" in str(e)
