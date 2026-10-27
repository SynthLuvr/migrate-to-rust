"""oracle.py acceptance tests — tools/README.md #1.

The big one: `self-test` must catch 5/5 injected bugs (the Stage 2
judge-first hard gate), with normalization rules documented and no LLM in
the judging path."""

from __future__ import annotations

import json

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
