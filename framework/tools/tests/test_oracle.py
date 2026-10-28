"""oracle.py acceptance tests — tools/README.md #1.

The big one: `self-test` must catch 5/5 injected bugs (the Stage 2
judge-first hard gate), with normalization rules documented and no LLM in
the judging path."""

from __future__ import annotations

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
