#!/usr/bin/env python3
"""oracle.py — the equivalence oracle (framework/tools/README.md #1).

Stages 2, 4, 5, 6. Classifies every run **Equivalent / Divergent /
Incomparable** with a structured diff. Deterministic: no LLM anywhere in the
judging path (CETBench: LLM judges unreliable); the verdict is a pure
function of (reference outputs, candidate outputs, normalization rules).

How it runs code
----------------
The unit under migration ships as a PyO3 wheel whose Python API is frozen,
so *both* sides are Python callables: an "entry" is ``module:function``
(runner default function name: ``run``).

**Corpus file format (what you write)** — JSONL, **one raw JSON value per
line = one input**, exactly as the entry function should receive it.
Verbatim from the acceptance test ``test_judge_subprocess_end_to_end``::

    {"xs": [1, 2], "bad": false}
    {"xs": [], "bad": true}

Do NOT pre-wrap corpus lines in a ``{"i": ..., "input": ...}`` envelope:
that is *internal stdin framing* the oracle adds itself (below). A
pre-wrapped line reaches the entry double-wrapped, both sides raise the
same TypeError on every run, and the judge reports a hollow all-EQUIVALENT
verdict — so ``_load_corpus`` refuses a corpus in which every line carries
exactly the framing keys, and ``corpus --validate`` dry-runs an entry
per-row before judging.

**Child-process protocol (what the oracle writes)** — one child process
per side; the oracle frames each corpus value on the child's stdin as::

    {"i": <run id>, "input": <JSON value>}

and reads per-run records from stdout:

    {"i": 0, "ok": true, "value": <JSON value>}
    {"i": 0, "ok": false, "error": {"type": "ValueError", "message": "..."}}
    {"i": 0, "unserializable": true}
    {"setup_error": "ImportError: ..."}              # once, first line

Outputs must be JSON-serializable; sets and other exotic values make the run
*Incomparable* (the honest verdict — units returning sets must expose a
canonical adapter, e.g. ``sorted(list(s))``).

Verdicts
--------
- equivalent   — normalized outputs match under the documented rules
- divergent    — outputs differ, an error became a value (or vice versa),
                 error *types* differ, or a value changed beyond tolerance;
                 every divergent run carries a structured diff
- incomparable — the judge has no ground truth for the run (reference itself
                 errored/unserializable/missing, or a side failed to run).
                 Never a synonym for "close enough".

Subcommands
-----------
  judge       run both entries over a corpus manifest and classify every run
  compare     classify already-captured runs (Stage 6 shadow-mode diffing)
  corpus      dry-run ONE entry over a corpus: per-row ok / error /
              unserializable reference status before judging — surfaces
              corpus-format mistakes (e.g. pre-wrapped lines) in seconds
  self-test   Stage 2 judge-first validation: inject the 5 canonical bug
              classes and prove 5/5 are caught — the hard gate
  rules       print the normalization rules (machine-readable documentation)

judge manifest shape (relative paths resolve against the manifest's
directory)::

    {"unit": "u-1",
     "reference": {"entry": "impl:run"},
     "candidate": {"entry": "cand:run"},
     "corpus": "corpus.jsonl",
     "normalization": {...}}          # optional rule overrides

Exit codes: 0 all equivalent · 2 any divergent · 3 incomparable (no
divergence) · 1 usage/IO · 4 side failed to run at all. ``corpus`` reuses
them as: 0 every row ok · 2 some row error/unserializable/missing · 4 the
entry failed to run at all.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import re
import sys
import types
import unicodedata
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    EXIT_FINDINGS, EXIT_INCOMPARABLE, EXIT_OK, EXIT_SETUP, ToolError, emit,
    read_json, read_jsonl, run_child, run_cli, sha256_bytes,
)

TOOL = "oracle.py"
VERSION = 1

# ---------------------------------------------------------------- rules ----

DEFAULT_RULES: dict = {
    # float comparison: |a-b| <= max(abs_tol, rel_tol * max(|a|, |b|))
    "float_abs_tol": 1e-9,
    "float_rel_tol": 1e-9,
    # int and float are distinct JSON values (1 vs 1.0) — silent int<->float
    # coercion is a divergence, not a normalization (reviewer checklist §3)
    "int_float_distinct": True,
    # NaN == NaN, +inf == +inf when both sides produce them
    "nan_equal": True,
    # dict/set iteration order is not part of the contract (CPython dict
    # order vs BTreeMap): only key membership and per-key values compare.
    # NOTE: JSON round-tripping already drops insertion order; a unit whose
    # order IS the contract must emit an ordered list of pairs instead.
    "dict_order_insensitive": True,
    "set_order_insensitive": True,
    # NFC-normalize strings before comparing (NFD/NFC spelling differences)
    "unicode_nfc": True,
    # fields to ignore anywhere in the output (e.g. timing/metadata noise)
    "strip_fields": [],
    # error comparison: "type-and-message" | "type-only"
    # ("type-only" forgives message wording; wrong types never forgiven)
    "error_compare": "type-and-message",
    # regex patterns; matching substrings are blanked from error messages
    "error_message_ignore_patterns": [],
}

RULE_DOCS = {
    "float_abs_tol": "absolute tolerance for float equality",
    "float_rel_tol": "relative tolerance for float equality (math.isclose)",
    "int_float_distinct": "1 vs 1.0 is a divergence (silent coercion detection)",
    "nan_equal": "NaN equals NaN when both sides produce it",
    "dict_order_insensitive": "mapping key order is not compared (JSON drops it; order-bearing units must emit pair lists)",
    "set_order_insensitive": "set-like data is order-insensitive (canonicalize to sorted lists)",
    "unicode_nfc": "strings are NFC-normalized before comparison",
    "strip_fields": "object keys removed from both outputs before comparing",
    "error_compare": "how raised exceptions compare: type-and-message | type-only",
    "error_message_ignore_patterns": "regexes blanked from exception messages (addresses, tmp paths)",
}


def merge_rules(manifest: dict) -> dict:
    rules = copy.deepcopy(DEFAULT_RULES)
    rules.update(manifest.get("normalization") or {})
    if rules["error_compare"] not in ("type-and-message", "type-only"):
        raise ValueError(f"error_compare must be 'type-and-message' or 'type-only', got {rules['error_compare']!r}")
    rules["error_message_ignore_patterns"] = [
        re.compile(p) for p in rules["error_message_ignore_patterns"]
    ]
    return rules


# ------------------------------------------------------------ comparator ---

def _strip_fields(value, names: set[str], depth: int = 0):
    if depth > 64:
        return value
    if isinstance(value, dict):
        return {k: _strip_fields(v, names, depth + 1) for k, v in value.items() if k not in names}
    if isinstance(value, list):
        return [_strip_fields(v, names, depth + 1) for v in value]
    return value


def _collapse_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _norm_string(s: str, rules: dict) -> str:
    if rules["unicode_nfc"]:
        s = unicodedata.normalize("NFC", s)
    return s


def _norm_error_message(msg: str, rules: dict) -> str:
    for pat in rules["error_message_ignore_patterns"]:
        msg = pat.sub("<ignored>", msg)
    return _collapse_ws(msg)


def compare_values(ref, cand, rules: dict, path: str = "$", diffs: list | None = None) -> list[dict]:
    """Deterministic structural comparison. Returns the diff list (possibly
    empty). Each diff: {path, kind, detail, ref, cand}."""
    if diffs is None:
        diffs = []

    def add(kind: str, detail: str, r, c) -> None:
        diffs.append({"path": path, "kind": kind, "detail": detail,
                      "ref": _safe(r), "cand": _safe(c)})

    # bool first: bool is an int subclass in Python
    if isinstance(ref, bool) or isinstance(cand, bool):
        if isinstance(ref, bool) and isinstance(cand, bool):
            if ref is not cand:
                add("bool", f"{ref!r} != {cand!r}", ref, cand)
        else:
            add("type", f"bool vs {type(ref).__name__}/{type(cand).__name__}", ref, cand)
        return diffs

    if ref is None or cand is None:
        if ref is not cand:
            add("null", "one side is null", ref, cand)
        return diffs

    if isinstance(ref, str) and isinstance(cand, str):
        r, c = _norm_string(ref, rules), _norm_string(cand, rules)
        if r != c:
            add("string", f"string differs (len {len(r)} vs {len(c)})", r, c)
        return diffs

    # numbers (int/float; bool already handled)
    ref_num = isinstance(ref, (int, float))
    cand_num = isinstance(cand, (int, float))
    if ref_num or cand_num:
        if not (ref_num and cand_num):
            add("type", f"{type(ref).__name__} vs {type(cand).__name__}", ref, cand)
            return diffs
        if isinstance(ref, float) or isinstance(cand, float):
            if rules["int_float_distinct"] and (
                isinstance(ref, float) != isinstance(cand, float)
            ):
                add("numeric-type",
                    f"{'float' if isinstance(ref, float) else 'int'} vs "
                    f"{'float' if isinstance(cand, float) else 'int'} "
                    "(silent int<->float coercion?)", ref, cand)
                return diffs
            rf, cf = float(ref), float(cand)
            if math.isnan(rf) and math.isnan(cf):
                if not rules["nan_equal"]:
                    add("float", "NaN vs NaN with nan_equal=false", ref, cand)
                return diffs
            if not (math.isnan(rf) and math.isnan(cf)) and rf != cf and not math.isclose(
                rf, cf, rel_tol=rules["float_rel_tol"], abs_tol=rules["float_abs_tol"]
            ):
                delta = abs(rf - cf)
                rel = delta / max(abs(rf), abs(cf)) if max(abs(rf), abs(cf)) else 0.0
                add("float", f"delta {delta:.6g} (rel {rel:.6g}) exceeds tolerance", rf, cf)
            return diffs
        if ref != cand:  # both int
            add("int", f"{ref} != {cand}", ref, cand)
        return diffs

    if isinstance(ref, list) and isinstance(cand, list):
        if len(ref) != len(cand):
            add("length", f"list length {len(ref)} vs {len(cand)}", len(ref), len(cand))
        for i in range(min(len(ref), len(cand))):
            compare_values(ref[i], cand[i], rules, f"{path}[{i}]", diffs)
        return diffs

    if isinstance(ref, dict) and isinstance(cand, dict):
        missing = sorted(set(ref) - set(cand))
        extra = sorted(set(cand) - set(ref))
        for k in missing:
            diffs.append({"path": f"{path}.{k}", "kind": "dict-key",
                          "detail": "key missing in candidate", "ref": _safe(ref[k]), "cand": None})
        for k in extra:
            diffs.append({"path": f"{path}.{k}", "kind": "dict-key",
                          "detail": "key missing in reference", "ref": None, "cand": _safe(cand[k])})
        for k in sorted(set(ref) & set(cand)):
            compare_values(ref[k], cand[k], rules, f"{path}.{k}", diffs)
        return diffs

    if type(ref) is not type(cand):  # anything else (post-JSON: unreachable)
        add("type", f"{type(ref).__name__} vs {type(cand).__name__}", ref, cand)
    elif ref != cand:
        add("value", "values differ", ref, cand)
    return diffs


def _safe(v):
    """JSON-safe view of a diff operand (never raises)."""
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        return repr(v)


def compare_error(err_ref: dict, err_cand: dict, rules: dict) -> list[dict]:
    """Compare two error records {type, message} under the rules."""
    diffs = []
    t_ref = err_ref.get("type", "").rsplit(".", 1)[-1]
    t_cand = err_cand.get("type", "").rsplit(".", 1)[-1]
    if t_ref != t_cand:
        diffs.append({"path": "$.error.type", "kind": "error-type",
                      "detail": f"raised {t_cand!r}, reference raises {t_ref!r}",
                      "ref": t_ref, "cand": t_cand})
        return diffs  # messages of different exception types aren't comparable
    if rules["error_compare"] == "type-and-message":
        m_ref = _norm_error_message(str(err_ref.get("message", "")), rules)
        m_cand = _norm_error_message(str(err_cand.get("message", "")), rules)
        if m_ref != m_cand:
            diffs.append({"path": "$.error.message", "kind": "error-message",
                          "detail": "same exception type, different message",
                          "ref": m_ref, "cand": m_cand})
    return diffs


def classify_pair(run_id, ref_rec: dict | None, cand_rec: dict | None, rules: dict,
                  max_diffs: int = 20) -> dict:
    """One run: (reference record, candidate record) -> verdict + diffs."""
    out = {"run": run_id, "verdict": "incomparable", "diffs": [], "detail": None}
    for side, rec in (("reference", ref_rec), ("candidate", cand_rec)):
        if rec is None:
            out["detail"] = f"missing run record on the {side} side"
            return out
        if rec.get("unserializable"):
            out["detail"] = f"{side} output is not JSON-serializable"
            return out
        if rec.get("setup_error"):
            out["detail"] = f"{side} setup failure: {rec['setup_error']}"
            return out

    if ref_rec.get("ok") and cand_rec.get("ok"):
        strip = set(rules["strip_fields"])
        ref = _strip_fields(ref_rec.get("value"), strip) if strip else ref_rec.get("value")
        cand = _strip_fields(cand_rec.get("value"), strip) if strip else cand_rec.get("value")
        diffs = compare_values(ref, cand, rules)
        out["verdict"] = "equivalent" if not diffs else "divergent"
        out["diffs"] = diffs[:max_diffs]
        if len(diffs) > max_diffs:
            out["diff_truncated"] = len(diffs) - max_diffs
        return out

    if not ref_rec.get("ok") and not cand_rec.get("ok"):
        diffs = compare_error(ref_rec.get("error", {}), cand_rec.get("error", {}), rules)
        out["verdict"] = "equivalent" if not diffs else "divergent"
        out["diffs"] = diffs
        return out

    # one side raised where the other returned a value
    if ref_rec.get("ok"):
        out["verdict"] = "divergent"
        out["diffs"] = [{"path": "$", "kind": "spurious-error",
                         "detail": "candidate raises but reference returns a value",
                         "ref": _safe(ref_rec.get("value")),
                         "cand": {"error": cand_rec.get("error")}}]
    else:
        out["verdict"] = "divergent"
        out["diffs"] = [{"path": "$", "kind": "swallowed-error",
                         "detail": "reference raises but candidate returns a value "
                                   "(error path dropped?)",
                         "ref": {"error": ref_rec.get("error")},
                         "cand": _safe(cand_rec.get("value"))}]
    return out


# --------------------------------------------------------------- runner ----

RUNNER_SRC = r"""
import json, sys

def main(entry):
    mod, _, attr = entry.partition(":")
    attr = attr or "run"
    try:
        m = __import__(mod, fromlist=[attr])
        fn = getattr(m, attr)
    except Exception as e:  # setup failure: nothing on this side is judgeable
        print(json.dumps({"setup_error": f"{type(e).__name__}: {e}"}), flush=True)
        return
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        i, value = rec["i"], rec.get("input")
        try:
            out = fn(value)
            json.dumps(out)  # serializability gate — TypeError -> incomparable
            print(json.dumps({"i": i, "ok": True, "value": out}), flush=True)
        except TypeError as e:
            if "not JSON serializable" in str(e) or "Object of type" in str(e):
                print(json.dumps({"i": i, "unserializable": True,
                                  "detail": str(e)}), flush=True)
            else:
                print(json.dumps({"i": i, "ok": False,
                                  "error": {"type": type(e).__name__, "message": str(e)}}),
                      flush=True)
        except BaseException as e:  # errors are legitimate outputs to compare
            print(json.dumps({"i": i, "ok": False,
                              "error": {"type": type(e).__name__, "message": str(e)}}),
                  flush=True)

main(sys.argv[1])
"""


def _execute_entry_local(entry: str, inputs: list) -> list[dict]:
    """In-process execution used by self-test — the SAME record protocol the
    subprocess runner produces, so self-test judges through the identical
    path (no special-cased verdicts)."""
    mod_name, _, attr = entry.partition(":")
    attr = attr or "run"
    records = []
    try:
        module = __import__(mod_name, fromlist=[attr])
        fn = getattr(module, attr)
    except Exception as e:
        return [{"setup_error": f"{type(e).__name__}: {e}"}]
    for i, value in enumerate(inputs):
        try:
            out = fn(value)
            json.dumps(out)
            records.append({"i": i, "ok": True, "value": out})
        except TypeError as e:
            if "not JSON serializable" in str(e) or "Object of type" in str(e):
                records.append({"i": i, "unserializable": True, "detail": str(e)})
            else:
                records.append({"i": i, "ok": False,
                                "error": {"type": type(e).__name__, "message": str(e)}})
        except BaseException as e:
            records.append({"i": i, "ok": False,
                            "error": {"type": type(e).__name__, "message": str(e)}})
    return records


def _execute_entry_subprocess(entry: str, inputs: list, manifest_dir: Path,
                              timeout: float) -> list[dict]:
    stdin = "".join(json.dumps({"i": i, "input": v}) + "\n" for i, v in enumerate(inputs))
    code, stdout, stderr = run_child(
        [sys.executable, "-c", RUNNER_SRC, entry],
        cwd=str(manifest_dir), input_text=stdin, timeout=timeout,
    )
    records: dict[int, dict] = {}
    setup_errors = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # child chatter (prints) is ignored, not fatal
        if "setup_error" in rec:
            setup_errors.append(rec["setup_error"])
        elif "i" in rec:
            records[rec["i"]] = rec
    result = [records.get(i) for i in range(len(inputs))]
    if setup_errors:
        return [{"setup_error": setup_errors[0]}]
    if code != 0 and not records:
        return [{"setup_error": f"child exited {code}: {stderr.strip()[:400] or 'no stderr'}"}]
    return result


# ---------------------------------------------------------------- judge ----

def _load_corpus(path: Path) -> list:
    """Load a corpus as the list of raw input values. Refuses (a) an empty
    corpus and (b) a corpus pre-wrapped in the child stdin framing (issue
    #29: every line is {"i": <int>, "input": ...}). The oracle adds that
    envelope itself when streaming to the child, so pre-wrapped lines
    double-wrap — both sides raise the same TypeError on every run and the
    judge reports a hollow all-EQUIVALENT verdict. Lines that merely
    contain i/input-ish keys are legitimate data, not a refusal."""
    rows = read_jsonl(path)
    values = [v for _, v in rows]
    if not values:
        raise ValueError(f"corpus {path} is empty — nothing to judge")
    if all(isinstance(v, dict) and set(v) == {"i", "input"}
           and isinstance(v["i"], int)
           and not isinstance(v["i"], bool)  # bool is an int subclass
           for v in values):
        raise ValueError(
            f"corpus {path} is pre-wrapped in the child-process stdin "
            'framing: every line is {"i": <int>, "input": ...}. Corpus '
            "lines are raw JSON values — one input per line, exactly as "
            "the entry receives it; oracle.py adds the {\"i\", \"input\"} "
            "envelope itself when streaming to the child. See the corpus "
            "file format in the module docstring / tools/README.md "
            "(issue #29)."
        )
    return values


def cmd_judge(args) -> int:
    manifest_path = Path(args.manifest).resolve()
    manifest = read_json(manifest_path)
    for key in ("reference", "candidate", "corpus"):
        if key not in manifest:
            raise ToolError(f"manifest missing required key {key!r}", kind="usage")
    corpus_path = Path(manifest["corpus"])
    if not corpus_path.is_absolute():
        corpus_path = manifest_path.parent / corpus_path
    inputs = _load_corpus(corpus_path)
    if args.runs:
        inputs = inputs[: args.runs]
    rules = merge_rules(manifest)

    ref_recs = _execute_entry_subprocess(manifest["reference"]["entry"], inputs,
                                         manifest_path.parent, args.timeout)
    cand_recs = _execute_entry_subprocess(manifest["candidate"]["entry"], inputs,
                                          manifest_path.parent, args.timeout)

    return _emit_runs_verdicts(
        unit=manifest.get("unit"), inputs=inputs, corpus_path=corpus_path,
        ref_recs=ref_recs, cand_recs=cand_recs, rules=rules, extra={
            "manifest": str(manifest_path),
            "reference_entry": manifest["reference"]["entry"],
            "candidate_entry": manifest["candidate"]["entry"],
        }, human_flag=args.human)


def _corpus_row_status(i: int, rec: dict | None) -> dict:
    """One runner record -> its dry-run row {i, status, detail}."""
    if rec is None:
        return {"i": i, "status": "missing",
                "detail": "child produced no record for this input"}
    if rec.get("unserializable"):
        return {"i": i, "status": "unserializable", "detail": rec.get("detail")}
    if rec.get("ok"):
        return {"i": i, "status": "ok", "detail": None}
    err = rec.get("error", {})
    return {"i": i, "status": "error",
            "detail": f"{err.get('type')}: {err.get('message')}"}


def cmd_corpus(args) -> int:
    """Dry-run ONE entry over a corpus: per-row status (ok / error /
    unserializable / missing) before you spend a judge run on it — a
    malformed corpus shows up as a wall of identical errors here, not as
    a hollow all-EQUIVALENT verdict from the judge."""
    corpus_path = Path(args.validate).resolve()
    inputs = _load_corpus(corpus_path)
    if args.runs:
        inputs = inputs[: args.runs]
    recs = _execute_entry_subprocess(args.entry, inputs,
                                     corpus_path.parent, args.timeout)
    corpus_digest = sha256_bytes(corpus_path.read_bytes())

    setup_error = next((r["setup_error"] for r in recs
                        if r and r.get("setup_error")), None)
    if setup_error is not None:
        human = f"corpus dry-run: entry failed to run: {setup_error}"
        emit({
            "tool": TOOL, "version": VERSION, "entry": args.entry,
            "corpus": {"path": str(corpus_path), "sha256": corpus_digest},
            "setup_error": setup_error,
            "summary": {"exit_code": EXIT_SETUP, "human": human},
        }, human_flag=args.human)
        return EXIT_SETUP

    rows = [_corpus_row_status(i, rec) for i, rec in enumerate(recs)]
    counts = Counter(row["status"] for row in rows)
    ok = counts["ok"]
    # hollow-verdict signature (issue #29): no row produced a value, so a
    # judge run over this corpus would compare only error-vs-error pairs
    hollow = bool(rows) and ok == 0
    warning = None
    if hollow:
        warning = (
            "every reference run errored/was unserializable — a judge "
            "verdict over this corpus would be hollow (identical errors "
            "compare as equivalent). Check the corpus format (one raw JSON "
            "value per line — no {\"i\",\"input\"} pre-wrapping) and the "
            "entry's expected input shape."
        )

    code = EXIT_OK if ok == len(rows) else EXIT_FINDINGS
    human = f"corpus {corpus_path.name} vs {args.entry}: {ok}/{len(rows)} rows ok"
    if ok < len(rows):
        human += f", {len(rows) - ok} error/unserializable"
    if hollow:
        human += " — HOLLOW: no row produced a value"
    emit({
        "tool": TOOL, "version": VERSION, "entry": args.entry,
        "corpus": {"path": str(corpus_path), "lines": len(inputs),
                   "sha256": corpus_digest},
        "rows": rows, "counts": dict(sorted(counts.items())),
        "ok": ok, "total": len(rows),
        "hollow_equivalent_risk": hollow, "warning": warning,
        "summary": {"exit_code": code, "human": human},
    }, human_flag=args.human)
    return code


def cmd_compare(args) -> int:
    payload = read_json(args.runs)
    for key in ("reference", "candidate"):
        if not isinstance(payload.get(key), list):
            raise ToolError(f"runs file missing list {key!r}", kind="usage")
    rules = merge_rules(payload)
    ref_recs = _records_by_index(payload["reference"])
    cand_recs = _records_by_index(payload["candidate"])
    return _emit_runs_verdicts(
        unit=payload.get("unit"), inputs=None, corpus_path=None,
        ref_recs=ref_recs, cand_recs=cand_recs, rules=rules,
        extra={"runs_file": str(Path(args.runs).resolve())},
        human_flag=args.human)


def _records_by_index(recs: list) -> list:
    by_i = {r["i"]: r for r in recs if "i" in r}
    return [by_i.get(i) for i in range(max((r["i"] for r in recs if "i" in r), default=-1) + 1)]


def _emit_runs_verdicts(*, unit, inputs, corpus_path, ref_recs, cand_recs,
                        rules, extra: dict, human_flag: bool = False) -> int:
    runs, counts = [], {"equivalent": 0, "divergent": 0, "incomparable": 0}
    for i in range(max(len(ref_recs), len(cand_recs))):
        verdict = classify_pair(i, ref_recs[i] if i < len(ref_recs) else None,
                                cand_recs[i] if i < len(cand_recs) else None, rules)
        runs.append(verdict)
        counts[verdict["verdict"]] += 1
    total = len(runs)
    if counts["divergent"]:
        verdict = "divergent"
        code = EXIT_FINDINGS
    elif counts["incomparable"]:
        verdict = "incomparable"
        code = EXIT_INCOMPARABLE
    else:
        verdict = "equivalent"
        code = EXIT_OK

    diff_kinds: dict[str, int] = {}
    for run in runs:
        for d in run["diffs"]:
            diff_kinds[d["kind"]] = diff_kinds.get(d["kind"], 0) + 1

    verdicts_digest = sha256_bytes(
        "\n".join(f"{r['run']}:{r['verdict']}" for r in runs).encode()
    )[:16]

    human = (
        f"unit={unit or '-'}: {counts['equivalent']}/{total} equivalent, "
        f"{counts['divergent']} divergent, {counts['incomparable']} incomparable "
        f"-> {verdict.upper()}"
        + (f"; top diff kinds: {sorted(diff_kinds, key=diff_kinds.get, reverse=True)[:3]}"
           if diff_kinds else "")
    )
    payload = {
        "tool": TOOL, "version": VERSION, "unit": unit, "verdict": verdict,
        "counts": counts, "total": total, "runs": runs,
        "diff_kind_histogram": dict(sorted(diff_kinds.items())),
        "verdicts_digest": verdicts_digest,
        "normalization": _rules_public(rules),
        "summary": {"exit_code": code, "human": human},
    }
    if inputs is not None:
        payload["runs_requested"] = len(inputs)
    if corpus_path is not None:
        payload["corpus"] = {
            "path": str(corpus_path), "lines": len(inputs or []),
            "sha256": sha256_bytes(corpus_path.read_bytes()),
        }
    payload.update(extra)
    emit(payload, human_flag=human_flag)
    return code


def _rules_public(rules: dict) -> dict:
    pub = dict(rules)
    pub["error_message_ignore_patterns"] = [
        p.pattern for p in rules["error_message_ignore_patterns"]
    ]
    return pub


def cmd_rules(args) -> int:
    payload = {
        "tool": TOOL, "version": VERSION,
        "defaults": {k: v for k, v in DEFAULT_RULES.items()},
        "docs": RULE_DOCS,
        "verdicts": {
            "equivalent": "normalized outputs match",
            "divergent": "outputs differ beyond normalization; error/value or error-type mismatches included",
            "incomparable": "judge lacks ground truth for the run (reference errored, unserializable, or side failed)",
        },
        "summary": {"exit_code": EXIT_OK, "human": "normalization rules (documented defaults)"},
    }
    emit(payload, human_flag=args.human)
    return EXIT_OK


# ------------------------------------------------------------- self-test ---
# Stage 2 step 4, the judge-first hard gate: five canonical injected bugs
# (off-by-one, dropped branch, swapped args, wrong type coercion, swallowed
# error) must ALL be caught by the exact judging path used in production.

def _reference_word_count(value):
    """Deterministic reference implementation used by self-test.

    Input:  {"text": str, "sep": str}
    Output: {"words": int, "distinct": int, "counts": [[word, count], ...]} —
    counts sorted by (-count, word); raises ValueError when sep is longer
    than text.
    """
    text, sep = value["text"], value["sep"]
    if len(sep) > len(text):
        raise ValueError(f"separator longer than text ({len(sep)} > {len(text)})")
    tokens = [t for t in text.split(sep) if t != ""]
    counter: dict[str, int] = {}
    for t in tokens:
        counter[t] = counter.get(t, 0) + 1
    counts = sorted(((w, c) for w, c in counter.items()), key=lambda wc: (-wc[1], wc[0]))
    return {"words": len(tokens), "distinct": len(counter),
            "density": (len(counter) / len(tokens)) if tokens else 0.0,
            "counts": [[w, c] for w, c in counts]}


def _bug_off_by_one(value):
    out = _reference_word_count(value)
    return {**out, "words": out["words"] + 1}


def _bug_dropped_branch(value):
    out = _reference_word_count(value)
    counts = out["counts"]
    counts.append(["", 0])  # dropped branch: empty tokens survive
    return {**out, "counts": counts}


def _bug_wrong_coercion(value):
    out = _reference_word_count(value)
    return {**out, "density": int(out["density"])}  # float -> int truncation


def _bug_swapped_args(value):
    return _reference_word_count({"text": value["sep"], "sep": value["text"]})


def _bug_swallowed_error(value):
    try:
        return _reference_word_count(value)
    except ValueError:
        return {"words": 0, "distinct": 0, "counts": []}  # error path dropped


SELF_TEST_BUGS = [
    ("off-by-one", _bug_off_by_one),
    ("dropped-branch", _bug_dropped_branch),
    ("swapped-args", _bug_swapped_args),
    ("wrong-type-coercion", _bug_wrong_coercion),
    ("swallowed-error", _bug_swallowed_error),
]

SELF_TEST_INPUTS = [
    {"text": "a:b:c:a", "sep": ":"},
    {"text": "go,go,go,,rust", "sep": ","},
    {"text": "x", "sep": "----longer-than-text"},
    {"text": "aa bb aa", "sep": " "},
    {"text": "m:z:m:a:m", "sep": ":"},
    {"text": "", "sep": ","},
]


def cmd_self_test(args) -> int:
    mod = types.ModuleType("_oracle_selftest_reference")
    mod.run = _reference_word_count
    sys.modules["_oracle_selftest_reference"] = mod
    ref_recs = _execute_entry_local("_oracle_selftest_reference:run", SELF_TEST_INPUTS)

    results = []
    for name, fn in SELF_TEST_BUGS:
        bug_mod = types.ModuleType(f"_oracle_selftest_{name}")
        bug_mod.run = fn
        sys.modules[f"_oracle_selftest_{name}"] = bug_mod
        cand_recs = _execute_entry_local(f"_oracle_selftest_{name}:run", SELF_TEST_INPUTS)
        runs = [classify_pair(i, ref_recs[i], cand_recs[i], merge_rules({}))
                for i in range(len(SELF_TEST_INPUTS))]
        caught = any(r["verdict"] == "divergent" for r in runs)
        first = next((r for r in runs if r["verdict"] == "divergent"), None)
        results.append({
            "bug": name, "caught": caught,
            "divergent_runs": sum(1 for r in runs if r["verdict"] == "divergent"),
            "evidence": first["diffs"][:2] if first and first["diffs"] else None,
        })

    # prove the rules do not over-forgive: benign differences must be
    # classified equivalent (otherwise the judge is noise, not a judge)
    benign = [
        ("dict-key-order", {"b": 1, "a": 2}, {"a": 2, "b": 1}),
        ("float-tolerance", 0.1 + 0.2, 0.30000000000000004),
        ("nan", float("nan"), float("nan")),
        ("strip-field", {"v": 1, "elapsed_s": 0.2}, {"v": 1, "elapsed_s": 9.9}),
    ]
    rules = merge_rules({"normalization": {"strip_fields": ["elapsed_s"]}})
    benign_results = []
    for name, r, c in benign:
        r_rec, c_rec = {"ok": True, "value": r}, {"ok": True, "value": c}
        verdict = classify_pair(0, r_rec, c_rec, rules)
        benign_results.append({"case": name, "verdict": verdict["verdict"]})
    benign_ok = all(b["verdict"] == "equivalent" for b in benign_results)

    caught = sum(1 for r in results if r["caught"])
    passed = caught == len(SELF_TEST_BUGS) and benign_ok
    code = EXIT_OK if passed else EXIT_FINDINGS
    human = (
        f"judge-first validation: {caught}/{len(SELF_TEST_BUGS)} injected bugs caught"
        + ("" if benign_ok else "; WARNING: benign differences flagged as divergent")
    )
    emit({
        "tool": TOOL, "version": VERSION,
        "injected_bugs": len(SELF_TEST_BUGS), "caught": caught,
        "results": results,
        "benign_normalization_cases": benign_results,
        "llm_in_judging_path": False,
        "summary": {"exit_code": code, "human": human},
    }, human_flag=args.human)
    return code


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=TOOL, description=__doc__.splitlines()[0])
    ap.add_argument("--human", action="store_true",
                    help="echo the human-readable summary to stderr")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("judge", help="run reference + candidate over a corpus manifest")
    p.add_argument("--manifest", required=True, help="JSON manifest (see module docstring)")
    p.add_argument("--runs", type=int, default=0, help="cap the corpus (0 = all)")
    p.add_argument("--timeout", type=float, default=120.0, help="per-side timeout (s)")
    p.set_defaults(func=cmd_judge)

    p = sub.add_parser(
        "corpus",
        help="dry-run one entry over a corpus: per-row ok/error/unserializable "
             "reference status before judging (exit 0 all ok · 2 findings · 4 setup)")
    p.add_argument("--validate", required=True, metavar="CORPUS_JSONL",
                   help="corpus file to dry-run (one raw JSON value per line — "
                        "see module docstring; NOT pre-wrapped in {\"i\",\"input\"})")
    p.add_argument("--entry", required=True,
                   help="module:function to execute (default fn: run); module "
                        "resolves with the corpus file's directory as cwd")
    p.add_argument("--runs", type=int, default=0, help="cap the corpus (0 = all)")
    p.add_argument("--timeout", type=float, default=120.0, help="per-side timeout (s)")
    p.set_defaults(func=cmd_corpus)

    p = sub.add_parser("compare", help="classify pre-captured runs (shadow mode)")
    p.add_argument("--runs", required=True, help="JSON {reference: [...], candidate: [...]}")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("self-test", help="Stage 2 judge-first validation: 5/5 injected bugs")
    p.set_defaults(func=cmd_self_test)

    p = sub.add_parser("rules", help="print documented normalization rules")
    p.set_defaults(func=cmd_rules)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(run_cli(main, tool=TOOL, version=VERSION))
