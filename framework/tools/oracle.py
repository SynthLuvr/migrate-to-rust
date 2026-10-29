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
(runner default function name: ``run``). An entry may also be a relative
or absolute ``*.py:function`` file path, resolved against the child's cwd —
the manifest's directory for ``judge``, the corpus's directory for
``corpus`` — which is how ``scaffold.py`` (issue #32) ships adapters that
live in their own directory next to the manifest while the manifest stays
judge-ready verbatim.

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
exactly the framing keys, ``corpus --validate`` dry-runs an entry
per-row before judging, and the judge's ground-truth guard (below)
classifies an all-error reference corpus INCOMPARABLE so no hollow
verdict can survive to the summary (issues #29, #30).

**Child-process protocol (what the oracle writes)** — one child process
per side; the oracle frames each corpus value on the child's stdin as::

    {"i": <run id>, "input": <JSON value>}

and reads per-run records from stdout:

    {"i": 0, "ok": true, "value": <JSON value>}
    {"i": 0, "ok": false, "error": {"type": "ValueError", "message": "..."}}
    {"i": 0, "unserializable": true}
    {"setup_error": "ImportError: ..."}              # once, first line
    {"peak_rss_kb": 47216}                           # once, final line: the
                                                     # child's high-water RSS
                                                     # self-report (issue #73)

**Child lifecycle (issue #73)** — the REPL children are hardened on both
sides of the pipe. Parent side: every harness child is its own session
leader and a ``run_child`` timeout SIGKILLs the whole process group — a
reaped judge takes its children with it, none left blocked on stdin
holding RSS. Child side: a ppid watchdog (0.5 s interval timer, so it
fires even while blocked in a read) exits the child if its judge dies;
``gc.collect()`` runs every ``GC_EVERY_N_ROWS`` rows so reference cycles
in judged code cannot grow peak RSS unbounded; and judge/corpus bound
each child to ``--mem-limit-mb`` (default: the ``memory`` block of
``autonomy/policy.json`` — ``policy:MEM-1``, 2048 MB cgroup MemoryMax;
``0`` disables) so a leaking side fails ITS judgment loudly (setup_error
→ INCOMPARABLE) instead of the host. Each side's peak RSS lands in the
verdict payload's ``resources.peak_rss_mb``.

**Streaming and chunking (issue #75)** — the parent judges the corpus in
**chunks of ``--chunk-rows`` rows (default 500)**: rows stream from the
corpus file lazily (never materialized), each chunk spawns one child per
side with the chunk framed on stdin and its **own ``--timeout``** — a
pathologically slow row times out its chunk only (rows judge INCOMPARABLE
with a "chunk timed out" detail) while every other chunk still produces
verdicts. Per-run verdicts fold into counts / diff histograms /
``verdicts_digest`` as chunks complete, so the parent retains only the
first ``RETAIN_VERBATIM_RUNS`` run entries verbatim (later entries are
summarized: run, verdict, detail, diff digest) — the summary numbers
always cover every run. A **pre-flight pass** stream-counts rows and
bytes (the file is parsed again only when judging) and refuses, in the
standard error envelope and before any child is spawned, a corpus whose
estimated parent footprint (``bytes × 4``) exceeds ``--max-corpus-mb``
(default: ``policy.json`` ``memory.max_corpus_mb``, 1024 MB — issue #76);
cap the judged rows with ``--runs`` or raise the budget with
``--max-corpus-mb``.

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

Ground-truth guard (issue #30)
------------------------------
The summary verdict additionally checks that the reference side produced
ground truth at all. A corpus on which the reference has zero ``ok:true``
runs is degenerate: every per-run pair is error-vs-error, which
``compare_error`` rightly calls equivalent *per run*, but a run set with no
successful reference output carries no parity evidence (the
oracle-equivalent of a test suite where every test raises before
asserting). Such a corpus judges **INCOMPARABLE**, never EQUIVALENT — and
per-run error-path mismatches on it are corpus artifacts, not candidate
defects, so they do not make the summary divergent either. The
``normalization.min_reference_ok_fraction`` rule (default 0.5) additionally
warns when fewer reference runs succeeded; 0 disables that warning — the
zero-ok INCOMPARABLE guard itself cannot be turned off.

Subcommands
-----------
  judge       run both entries over a corpus manifest and classify every run
  compare     classify already-captured runs (Stage 6 shadow-mode diffing)
  corpus      dry-run ONE entry over a corpus: per-row ok / error /
              unserializable reference status before judging — surfaces
              corpus-format mistakes (e.g. pre-wrapped lines) in seconds
  self-test   Stage 2 judge-first validation: inject the 5 canonical bug
              classes and prove 5/5 are caught, benign differences stay
              equivalent, and an all-error reference corpus classifies
              INCOMPARABLE — the hard gate
  rules       print the normalization rules (machine-readable documentation)

judge manifest shape (relative paths resolve against the manifest's
directory; entries are ``module:function`` or relative ``*.py:function``
file paths — issue #32)::

    {"unit": "u-1",
     "reference": {"entry": "impl:run"},
     "candidate": {"entry": "../adapters/u_1_cand.py:run"},
     "corpus": "corpus.jsonl",
     "normalization": {...}}          # optional rule overrides

Exit codes: 0 all equivalent · 2 any divergent · 3 incomparable (no
divergence, or a degenerate all-error reference corpus — issue #30) ·
1 usage/IO · 4 side failed to run at all. ``corpus`` reuses
them as: 0 every row ok · 2 some row error/unserializable/missing · 4 the
entry failed to run at all.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import subprocess
import sys
import types
import unicodedata
from collections.abc import Iterator
from itertools import islice
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    EXIT_FINDINGS, EXIT_INCOMPARABLE, EXIT_OK, EXIT_SETUP, ToolError, emit,
    read_json, run_child, run_cli, sha256_bytes,
)
import pipeline as pipeline_tool  # noqa: E402  — memory is policy (issue #76)

TOOL = "oracle.py"
VERSION = 1

# ------------------------------------------------------ streaming (#75) ----
# The parent never materializes the corpus, the stdin framing, or the full
# classified run set (issue #75: the old bulk-stdin path peaked at several
# multiples of the corpus file in parent RSS). Rows stream from disk in
# chunks — one child per side per chunk, each with its own timeout — and
# verdicts fold into counts/histograms/digests as chunks complete.

#: corpus rows per child; one chunk's stdin framing is the only
#: corpus-derived buffer the parent ever holds
DEFAULT_CHUNK_ROWS = 500

#: REPL children gc.collect() every this many rows: reference cycles in
#: judged code collect between rows, keeping peak RSS near the per-row
#: working set instead of growing monotonically (issue #73)
GC_EVERY_N_ROWS = 64

#: per-child cgroup memory ceiling in MB for judge/corpus REPL children:
#: the default is policy.json memory.judge_child_mb (policy:MEM-1, issue
#: #76); this constant is the fallback quoted in --help. A leak must fail
#: THAT judgment (scope breach -> SIGKILL -> setup_error -> INCOMPARABLE),
#: never the host. ``--mem-limit-mb 0`` opts out
DEFAULT_MEM_LIMIT_MB = 2048

#: pre-flight corpus budget in MB — the default is policy.json
#: memory.max_corpus_mb (issue #76); this constant is the fallback quoted
#: in --help; see _check_corpus_budget
DEFAULT_MAX_CORPUS_MB = 1024
CORPUS_FOOTPRINT_FACTOR = 4.0
#: run entries kept verbatim in the verdict payload; later entries are
#: summarized — counts/histogram/digest still cover every run
RETAIN_VERBATIM_RUNS = 500
#: per-row status entries kept verbatim by the corpus dry-run
RETAIN_VERBATIM_ROWS = 500


def _memory_limits(mem_limit_mb: int | None, max_corpus_mb: int | None,
                   policy_path: str | None = None) -> tuple[dict, int, int]:
    """Resolve this run's memory budgets from policy (issue #76): the
    judge-child ceiling and the corpus pre-flight budget default to
    policy.json 'memory' (policy:MEM-1); an explicit flag value is an
    operator override journaled in the decision's deviations, and 0
    disables that bound. Returns (decision, mem_limit, max_corpus_mb) —
    mem_limit None = children unscoped, max_corpus_mb 0 = check off."""
    decision = pipeline_tool.resolve_memory(
        pipeline_tool.load_policy(policy_path))
    limits = decision["limits"]

    def apply(flag, key, disable_note):
        if flag is None:            # absent: the policy ceiling stands
            return
        if flag == 0:
            limits[key] = 0
            decision.setdefault("deviations", []).append(disable_note)
        elif flag != limits[key]:
            decision.setdefault("deviations", []).append(
                f"operator override: {key}={flag} (policy said "
                f"{limits[key]}) — journaled, never a judgment call")
            limits[key] = flag

    apply(mem_limit_mb, "judge_child_mb",
          "operator override: judge children unscoped (--mem-limit-mb 0)")
    apply(max_corpus_mb, "max_corpus_mb",
          "operator override: corpus pre-flight check off (--max-corpus-mb 0)")
    return (decision, limits["judge_child_mb"] or None,
            int(limits["max_corpus_mb"] or 0))


def _resources_block(memory: dict, mem_limit: int | None,
                     peak_rss_mb) -> dict:
    """The verdict payload's resources block: the ceiling in force and its
    provenance (issue #76), the GC cadence, and the caller's peak-RSS
    shape (per side for judge, a single value for corpus dry-run)."""
    return {
        "mem_limit_mb": mem_limit,
        "mem_limit_decided_by": memory["decided_by"],
        "mem_limit_deviations": memory.get("deviations"),
        "max_corpus_mb": int(memory["limits"]["max_corpus_mb"] or 0) or None,
        "gc_every_rows": GC_EVERY_N_ROWS,
        "peak_rss_mb": peak_rss_mb,
    }

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
    # ground-truth guard (issue #30) — see RULE_DOCS and the module docstring
    "min_reference_ok_fraction": 0.5,
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
    "min_reference_ok_fraction": (
        "minimum fraction of corpus rows the reference must complete (ok) — "
        "below it the judge warns (thin ground truth); at zero ok reference "
        "runs the summary verdict is INCOMPARABLE, never EQUIVALENT "
        "(issue #30: an all-error reference corpus carries no parity evidence)"
    ),
}


def merge_rules(manifest: dict) -> dict:
    rules = copy.deepcopy(DEFAULT_RULES)
    rules.update(manifest.get("normalization") or {})
    if rules["error_compare"] not in ("type-and-message", "type-only"):
        raise ValueError(f"error_compare must be 'type-and-message' or 'type-only', got {rules['error_compare']!r}")
    rules["error_message_ignore_patterns"] = [
        re.compile(p) for p in rules["error_message_ignore_patterns"]
    ]
    frac = rules["min_reference_ok_fraction"]
    if (isinstance(frac, bool) or not isinstance(frac, (int, float))
            or not 0.0 <= frac <= 1.0):
        raise ValueError(
            f"min_reference_ok_fraction must be a number in [0, 1], got {frac!r}"
        )
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


def classify_runs(ref_recs: list, cand_recs: list, rules: dict) -> list[dict]:
    """classify_pair over index-aligned record lists, one verdict per run
    (a record missing on either side judges incomparable)."""
    def rec(records: list, i: int):
        return records[i] if i < len(records) else None
    return [classify_pair(i, rec(ref_recs, i), rec(cand_recs, i), rules)
            for i in range(max(len(ref_recs), len(cand_recs)))]


def count_verdicts(runs: list[dict]) -> dict:
    """Tally per-run verdicts; fixed key order keeps the JSON output stable."""
    counts = {"equivalent": 0, "divergent": 0, "incomparable": 0}
    for run in runs:
        counts[run["verdict"]] += 1
    return counts


def _diff_kind_counts(diffs: list) -> dict[str, int]:
    counts: dict[str, int] = {}
    for d in diffs:
        counts[d["kind"]] = counts.get(d["kind"], 0) + 1
    return counts


def _summarized_run(run: dict) -> dict:
    """Bounded shadow of a run entry past the verbatim retention cap:
    keeps ``runs`` indexable without retaining every diff operand."""
    return {
        "run": run["run"],
        "verdict": run["verdict"],
        "detail": run["detail"],
        "diff_digest": sha256_bytes(
            json.dumps(run["diffs"], sort_keys=True,
                       ensure_ascii=False).encode("utf-8"))[:16]
        if run["diffs"] else None,
        "diff_kinds": _diff_kind_counts(run["diffs"]),
        "diff_truncated": run.get("diff_truncated"),
        "summarized": True,
    }


class _RunFold:
    """Streaming summary of a judgment (issue #75): per-run verdicts fold
    in as chunks complete, so the parent never retains the full classified
    run set. Everything the verdict payload needs is maintained
    incrementally — counts, the diff-kind histogram, the verdicts digest
    (same bytes as the list-era ``sha256("\\n".join(...))``), and the
    ground-truth counters — and run entries are kept verbatim only up to
    ``max_verbatim`` (default RETAIN_VERBATIM_RUNS)."""

    def __init__(self, max_verbatim: int | None = None):
        self.counts = {"equivalent": 0, "divergent": 0, "incomparable": 0}
        self.diff_kinds: dict[str, int] = {}
        self.runs: list[dict] = []
        self.summarized = 0
        self.total = 0
        self.ref_ok = 0
        self.setup_error: str | None = None
        self._digest = hashlib.sha256()
        self.max_verbatim = (RETAIN_VERBATIM_RUNS if max_verbatim is None
                             else max_verbatim)

    def add(self, run: dict, ref_rec: dict | None = None) -> None:
        """Fold one classified run; ``ref_rec`` (which the judge/corpus
        streams and compare all pass when they have it) feeds the
        ground-truth counters."""
        self.total += 1
        self.counts[run["verdict"]] += 1
        for d in run["diffs"]:
            self.diff_kinds[d["kind"]] = self.diff_kinds.get(d["kind"], 0) + 1
        part = f"{run['run']}:{run['verdict']}".encode("utf-8")
        self._digest.update(part if self.total == 1 else b"\n" + part)
        if ref_rec is not None:
            if ref_rec.get("ok") is True:
                self.ref_ok += 1
            if self.setup_error is None and ref_rec.get("setup_error"):
                self.setup_error = ref_rec["setup_error"]
        if len(self.runs) < self.max_verbatim:
            self.runs.append(run)
        else:
            self.summarized += 1
            self.runs.append(_summarized_run(run))

    @property
    def verdicts_digest(self) -> str:
        return self._digest.hexdigest()[:16]


# ------------------------------------------------------- ground truth -----
# issue #30: a corpus whose reference side never succeeds must not judge
# EQUIVALENT — the repair loop would select candidates on noise.

def _first_setup_error(recs: list) -> str | None:
    return next((r["setup_error"] for r in recs if r and r.get("setup_error")), None)


def reference_ground_truth(ref_recs: list, total: int, rules: dict) -> dict:
    """How much of a corpus the reference side actually ran to completion.

    ``ref_recs`` are the reference runner records (judge/compare shapes
    both work: per-run dicts, possibly None, or a single setup_error
    record); ``total`` is the number of runs being judged. Returns the
    ``ground_truth`` block emitted with every summary verdict:
    ``degenerate`` (zero ok reference runs — no parity evidence at all),
    a human ``detail`` for the degenerate case, and a ``warning`` when the
    ok fraction falls below ``min_reference_ok_fraction`` (thin ground
    truth)."""
    ref_ok = sum(1 for r in ref_recs if r is not None and r.get("ok") is True)
    return _ground_truth_from(ref_ok, _first_setup_error(ref_recs), total, rules)


def _ground_truth_from(ref_ok: int, setup_error: str | None, total: int,
                       rules: dict) -> dict:
    """The ``ground_truth`` block from folded counters instead of a
    materialized record list — the same block, byte for byte, that
    ``reference_ground_truth`` above builds from the record list (issue
    #75: the streamed judge folds reference-ok and setup-error facts as
    chunks complete)."""
    fraction = (ref_ok / total) if total else None
    min_frac = rules["min_reference_ok_fraction"]
    gt = {
        "reference_ok": ref_ok,
        "runs": total,
        "reference_ok_fraction": round(fraction, 6) if fraction is not None else None,
        "min_reference_ok_fraction": min_frac,
        "degenerate": bool(total > 0 and ref_ok == 0),
        "detail": None,
        "warning": None,
    }
    if gt["degenerate"]:
        if setup_error:
            gt["detail"] = (f"reference side failed to run — no ground "
                            f"truth on any row ({setup_error})")
        else:
            gt["detail"] = ("reference errored on every corpus row — "
                            "corpus is degenerate for this entry")
    elif fraction is not None and fraction < min_frac:
        gt["warning"] = (
            f"reference ok on only {ref_ok}/{total} runs "
            f"({fraction:.1%} < min_reference_ok_fraction {min_frac}) — "
            "verdict rests on thin ground truth"
        )
    return gt


def summary_verdict(counts: dict, ground_truth: dict) -> str:
    """Per-run counts + the ground-truth guard -> the summary verdict.

    A degenerate corpus (reference ok on zero rows) is INCOMPARABLE —
    never EQUIVALENT, and never DIVERGENT either: error-path mismatches
    between two all-erroring sides are corpus artifacts, not candidate
    defects (the per-run verdicts and diffs stay in the payload for the
    operator to read)."""
    if ground_truth["degenerate"]:
        return "incomparable"
    if counts["divergent"]:
        return "divergent"
    if counts["incomparable"]:
        return "incomparable"
    return "equivalent"


def _degenerate_note(counts: dict) -> str:
    """What per-run verdicts on a degenerate corpus are worth: matches
    carry no parity evidence, mismatches blame the corpus, not the
    candidate."""
    if counts["equivalent"]:
        return "error==error matches are not parity evidence"
    return ("error-path mismatches on a degenerate corpus are corpus "
            "artifacts, not candidate defects")


# --------------------------------------------------------------- runner ----

RUNNER_SRC_TEMPLATE = r"""
import gc, importlib.util, json, os, resource, signal, sys

GC_EVERY_N_ROWS = __GC_EVERY_N_ROWS__  # gc.collect() cadence, between rows
_JUDGE_PID = os.getppid()              # the judge that spawned this child


def _orphan_watchdog(signum, frame):
    # Exit when the judge is gone (timeout group kill, SIGKILL, OOM) —
    # otherwise this child blocks on stdin forever holding RSS. A 0.5 s
    # interval timer, not a per-row check: it fires even while BLOCKED
    # in a read() (issue #73).
    if os.getppid() != _JUDGE_PID:
        try:
            os.write(2, ("oracle-repl: judge pid %d is gone — exiting, "
                         "not blocking on stdin\n" % _JUDGE_PID).encode())
        except OSError:
            pass
        os._exit(75)


def _peak_rss_kb():
    # ru_maxrss is the high-water RSS: KiB on Linux, bytes on macOS
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(ru / 1024.0) if sys.platform == "darwin" else int(ru)


def _load(entry):
    # entry is "module:function" (module imported with the child's cwd on
    # sys.path) or a "*.py:function" file path, resolved against the cwd —
    # the manifest/corpus directory. scaffold.py (issue #32) emits path
    # entries so adapters can live in their own directory next to the
    # manifest; rpartition keeps Windows drive prefixes intact.
    if ":" in entry:
        mod, _, attr = entry.rpartition(":")
    else:
        mod, attr = entry, "run"
    attr = attr or "run"
    if mod.endswith(".py"):
        path = os.path.abspath(mod)
        if not os.path.isfile(path):
            raise ImportError(f"entry file not found: {mod}")
        spec = importlib.util.spec_from_file_location("__oracle_entry__", path)
        m = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = m
        spec.loader.exec_module(m)
    else:
        m = __import__(mod, fromlist=[attr])
    return getattr(m, attr)

def main(entry):
    try:
        fn = _load(entry)
    except Exception as e:  # setup failure: nothing on this side is judgeable
        print(json.dumps({"setup_error": f"{type(e).__name__}: {e}"}), flush=True)
        return
    try:
        signal.signal(signal.SIGALRM, _orphan_watchdog)
        signal.setitimer(signal.ITIMER_REAL, 0.5, 0.5)
    except (AttributeError, ValueError, OSError):
        pass  # no interval timers here (non-POSIX): the parent's group
              # kill remains the reaper of record
    rows = 0
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
        rows += 1
        if rows % GC_EVERY_N_ROWS == 0:
            gc.collect()  # collect cycles so judged code can't grow peak RSS
    try:
        print(json.dumps({"peak_rss_kb": _peak_rss_kb()}), flush=True)
    except BrokenPipeError:
        pass  # the judge is gone; the watchdog/EOF handles the exit


main(sys.argv[1])
"""

#: the source handed to children: the template with the gc cadence
#: spliced in (a raw string, so no f-string brace-doubling); one splice
#: point keeps tests asserting against the constant children really run
RUNNER_SRC = RUNNER_SRC_TEMPLATE.replace("__GC_EVERY_N_ROWS__",
                                         str(GC_EVERY_N_ROWS))


def _load_entry_callable(entry: str):
    """Resolve an entry for in-process execution (the self-test path) with
    the SAME semantics as the child runner's loader: ``module:function`` or
    a ``*.py:function`` file path (resolved against the process cwd), the
    attribute defaulting to ``run``."""
    if ":" in entry:
        mod, _, attr = entry.rpartition(":")
    else:
        mod, attr = entry, "run"
    attr = attr or "run"
    if mod.endswith(".py"):
        import importlib.util
        path = Path(mod).resolve()
        if not path.is_file():
            raise ImportError(f"entry file not found: {mod}")
        spec = importlib.util.spec_from_file_location("__oracle_entry__", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    else:
        module = __import__(mod, fromlist=[attr])
    return getattr(module, attr)


def _execute_entry_local(entry: str, inputs: list) -> list[dict]:
    """In-process execution used by self-test — the SAME record protocol the
    subprocess runner produces, so self-test judges through the identical
    path (no special-cased verdicts)."""
    records = []
    try:
        fn = _load_entry_callable(entry)
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


def _frame_stdin(rows) -> str:
    """The child stdin framing: one {"i": ..., "input": ...} per row."""
    return "".join(json.dumps({"i": i, "input": v}) + "\n" for i, v in rows)


def _parse_child_records(stdout: str) -> tuple[dict[int, dict], list[str], int]:
    """Index the child's NDJSON stdout into per-run records by ``i``, any
    setup errors in order, and the child's ``{"peak_rss_kb": N}``
    self-report (max across lines; 0 when no child reported). Lines that
    are not JSON are child chatter (prints), not fatal."""
    records: dict[int, dict] = {}
    setup_errors = []
    peak_rss_kb = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "setup_error" in rec:
            setup_errors.append(rec["setup_error"])
        elif "i" in rec:
            records[rec["i"]] = rec
        elif isinstance(rec.get("peak_rss_kb"), int):
            peak_rss_kb = max(peak_rss_kb, rec["peak_rss_kb"])
    return records, setup_errors, peak_rss_kb


def _child_exit_error(code: int, stderr: str) -> str:
    return f"child exited {code}: {stderr.strip()[:400] or 'no stderr'}"


def _peak_rss_mb(kb: int) -> float | None:
    """A child's KiB high-water self-report -> MB (one decimal); None when
    nothing reported — a null is honest, 0.0 would read as measured."""
    return None if not kb else round(kb / 1024.0, 1)


def _execute_entry_subprocess(entry: str, inputs: list, manifest_dir: Path,
                              timeout: float,
                              mem_limit_mb: int | None = None) -> list[dict]:
    """One-shot bulk-stdin execution — kept for bench.py / scaffold.py and
    small corpora; the judge/corpus commands stream via _SideRunner below."""
    code, stdout, stderr = run_child(
        [sys.executable, "-c", RUNNER_SRC, entry],
        cwd=str(manifest_dir), input_text=_frame_stdin(enumerate(inputs)),
        timeout=timeout, mem_limit_mb=mem_limit_mb,
    )
    records, setup_errors, _peak = _parse_child_records(stdout)
    if setup_errors:
        return [{"setup_error": setup_errors[0]}]
    if code != 0 and not records:
        return [{"setup_error": _child_exit_error(code, stderr)}]
    return [records.get(i) for i in range(len(inputs))]


# ------------------------------------------------------- chunked runs ----
# judge/corpus stream rows in --chunk-rows chunks: one child per side per
# chunk with its own --timeout, so stdin is bounded by one chunk and a
# pathologically slow row costs its chunk only — never the whole judgment
# (the adjacent failure behind #73's secondary observation: one slow
# reference function blowing the whole-run timeout).

def _chunks(rows_iter, size: int):
    """Yield lists of at most ``size`` rows from an iterator."""
    if size <= 0:
        raise ValueError(f"chunk size must be a positive number of rows, "
                         f"got {size!r}")
    chunk: list = []
    for row in rows_iter:
        chunk.append(row)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _chunk_timeout_records(timeout: float, rows: int) -> list[dict]:
    """Per-row pseudo-records for a chunk that blew its timeout — judged
    incomparable; the remaining chunks still judge (issue #75)."""
    detail = (f"chunk timed out after {timeout}s — {rows} rows on this "
              "side were not judged (issue #75: the timeout is "
              "chunk-local; other chunks still judge)")
    return [{"setup_error": detail} for _ in range(rows)]


def _run_entry_chunk(entry: str, chunk: list, cwd, timeout: float,
                     mem_limit_mb: int | None) -> tuple[list, str | None, int]:
    """One child over one chunk of (index, value) rows. Returns
    ``(records, terminal_error, peak_rss_kb)``: ``records`` index-aligned
    with ``chunk`` (None where the child produced nothing); a non-None
    terminal_error (a setup error, or a child exit before any record)
    means the side is dead and later chunks must not spawn. A timeout is
    NOT terminal."""
    try:
        code, stdout, stderr = run_child(
            [sys.executable, "-c", RUNNER_SRC, entry],
            cwd=str(cwd), input_text=_frame_stdin(chunk),
            timeout=timeout, mem_limit_mb=mem_limit_mb,
        )
    except subprocess.TimeoutExpired:
        return _chunk_timeout_records(timeout, len(chunk)), None, 0
    records, setup_errors, peak_rss_kb = _parse_child_records(stdout)
    if setup_errors:
        return [None] * len(chunk), setup_errors[0], peak_rss_kb
    if code != 0 and not records:
        return [None] * len(chunk), _child_exit_error(code, stderr), peak_rss_kb
    return [records.get(i) for i, _ in chunk], None, peak_rss_kb


class _SideRunner:
    """Per-side chunked execution state: remembers a terminal setup error
    so later chunks return all-None WITHOUT spawning (a side whose entry
    cannot be imported fails identically on every chunk — one child tells
    the whole story). The failing chunk's first row carries the
    setup_error record, matching the one-shot path's ``[{"setup_error"}]``
    shape when the failure is on the first chunk."""

    def __init__(self, entry: str, cwd, timeout: float,
                 mem_limit_mb: int | None = None):
        self.entry = entry
        self.cwd = cwd
        self.timeout = timeout
        self.mem_limit_mb = mem_limit_mb
        self.terminal_error: str | None = None
        #: max high-water RSS (KiB) self-reported by this side's children,
        #: folded into the verdict payload
        self.peak_rss_kb = 0

    def run_chunk(self, chunk: list) -> list:
        """Records for one chunk of rows: index-aligned, None where this
        side has nothing (terminal failure on a previous chunk, or rows
        the child never answered)."""
        if self.terminal_error is not None or not chunk:
            return [None] * len(chunk)
        records, terminal, peak_rss_kb = _run_entry_chunk(
            self.entry, chunk, self.cwd, self.timeout, self.mem_limit_mb)
        self.peak_rss_kb = max(self.peak_rss_kb, peak_rss_kb)
        if terminal is not None:
            self.terminal_error = terminal
            records = [None] * len(chunk)
            records[0] = {"setup_error": terminal}
        return records


# ---------------------------------------------------------------- judge ----

def _is_framing_envelope(value) -> bool:
    """Exactly the child stdin framing shape: {"i": <int>, "input": ...}
    and nothing else (bool does not count as the int — it is a subclass)."""
    return (isinstance(value, dict) and set(value) == {"i", "input"}
            and isinstance(value["i"], int)
            and not isinstance(value["i"], bool))


class _CorpusScan(NamedTuple):
    """Pre-flight facts about a corpus (issue #75), gathered in one
    streaming pass that never holds more than a single row."""
    rows: int
    size_bytes: int
    sha256: str


def _iter_corpus_rows(path: Path):
    """Lazily yield ``(line_number, raw_line, value)`` for every non-blank
    corpus line — the streaming counterpart of read_jsonl; same
    malformed-line refusal."""
    with open(path, "rb") as fh:
        for n, raw in enumerate(fh, start=1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"{path}:{n}: invalid JSON line: {exc}") from exc
            yield n, raw, value


def _corpus_preflight(path: Path, *, max_corpus_mb: int = DEFAULT_MAX_CORPUS_MB,
                      runs_cap: int = 0) -> _CorpusScan:
    """Apply every load-time refusal — empty, pre-wrapped in the child
    stdin framing (issue #29), over budget (issue #75) — in one streaming
    pass over the corpus, so they fire BEFORE any child is spawned."""
    rows = framed = 0
    size_bytes = 0
    hasher = hashlib.sha256()
    for _n, raw, value in _iter_corpus_rows(path):
        rows += 1
        size_bytes += len(raw)
        hasher.update(raw)
        if _is_framing_envelope(value):
            framed += 1
    if rows == 0:
        raise ValueError(f"corpus {path} is empty — nothing to judge")
    if framed == rows:
        raise ValueError(
            f"corpus {path} is pre-wrapped in the child-process stdin "
            'framing: every line is {"i": <int>, "input": ...}. Corpus '
            "lines are raw JSON values — one input per line, exactly as "
            "the entry receives it; oracle.py adds the {\"i\", \"input\"} "
            "envelope itself when streaming to the child. See the corpus "
            "file format in the module docstring / tools/README.md "
            "(issue #29)."
        )
    _check_corpus_budget(path, rows, size_bytes, runs_cap, max_corpus_mb)
    return _CorpusScan(rows, size_bytes, hasher.hexdigest())


def _check_corpus_budget(path: Path, rows: int, size_bytes: int,
                         runs_cap: int, max_corpus_mb: int) -> None:
    """Refuse a corpus whose estimated parent footprint — bytes ×
    CORPUS_FOOTPRINT_FACTOR, the parsed-rows + framing + records overhead
    of the pre-streaming bulk-stdin era — exceeds the budget (issue #75).
    A ``runs_cap`` scales the estimate to the rows that would actually be
    judged, making an explicit --runs the documented repair."""
    if max_corpus_mb <= 0:
        return
    cap = runs_cap if runs_cap > 0 else rows
    judged_fraction = min(cap, rows) / rows
    est_mb = size_bytes * judged_fraction * CORPUS_FOOTPRINT_FACTOR / (1024 * 1024)
    if est_mb > max_corpus_mb:
        raise ValueError(
            f"corpus {path} is too large to judge: {rows} rows / "
            f"{size_bytes} bytes ≈ {est_mb:.0f} MB estimated parent "
            f"footprint (x{CORPUS_FOOTPRINT_FACTOR:g} byte factor) "
            f"exceeds the {max_corpus_mb} MB budget (issue #75) — "
            "cap the judged rows with --runs N or raise the budget "
            "with --max-corpus-mb (policy:MEM-1, issue #76)"
        )


def _open_corpus(path: Path, *, runs: int = 0,
                 max_corpus_mb: int = DEFAULT_MAX_CORPUS_MB
                 ) -> tuple[_CorpusScan, int, Iterator[tuple[int, object]]]:
    """Pre-flight the corpus, then return ``(scan, judged_rows, row_iter)``:
    the scan facts, how many rows will actually be judged (``runs > 0``
    caps the corpus), and a lazy iterator over exactly those rows as
    ``(index, value)`` pairs — the index is the run id the child framing
    uses."""
    scan = _corpus_preflight(path, max_corpus_mb=max_corpus_mb, runs_cap=runs)
    judged = min(runs, scan.rows) if runs > 0 else scan.rows
    rows = ((i, value) for i, (_n, _raw, value) in enumerate(_iter_corpus_rows(path)))
    return scan, judged, islice(rows, judged)


def _load_corpus(path: Path) -> list:
    """Load a corpus as the list of raw input values — the pre-#75 list
    API kept for bench.py / scaffold.py and small corpora; the judge and
    corpus commands stream via _open_corpus instead. Same refusals."""
    _corpus_preflight(path)
    return [value for _n, _raw, value in _iter_corpus_rows(path)]


def cmd_judge(args) -> int:
    manifest_path = Path(args.manifest).resolve()
    manifest = read_json(manifest_path)
    for key in ("reference", "candidate", "corpus"):
        if key not in manifest:
            raise ToolError(f"manifest missing required key {key!r}", kind="usage")
    corpus_path = Path(manifest["corpus"])
    if not corpus_path.is_absolute():
        corpus_path = manifest_path.parent / corpus_path
    rules = merge_rules(manifest)

    memory, mem_limit, max_corpus_mb = _memory_limits(
        args.mem_limit_mb, args.max_corpus_mb)
    # refusals (empty · pre-wrapped · over-budget) fire before any spawn;
    # a slow row times out its chunk only, every other chunk still judges
    scan, judged, rows = _open_corpus(corpus_path, runs=args.runs,
                                      max_corpus_mb=max_corpus_mb)
    # judged code is untrusted for memory: a leaking side must fail its
    # own judgment (setup_error -> INCOMPARABLE), never the host
    fold = _RunFold()
    ref = _SideRunner(manifest["reference"]["entry"], manifest_path.parent,
                      args.timeout, mem_limit)
    cand = _SideRunner(manifest["candidate"]["entry"], manifest_path.parent,
                       args.timeout, mem_limit)
    for chunk in _chunks(rows, args.chunk_rows):
        ref_recs = ref.run_chunk(chunk)
        cand_recs = cand.run_chunk(chunk)
        for (i, _value), ref_rec, cand_rec in zip(chunk, ref_recs, cand_recs):
            fold.add(classify_pair(i, ref_rec, cand_rec, rules), ref_rec=ref_rec)

    resources = _resources_block(
        memory, mem_limit,
        {"reference": _peak_rss_mb(ref.peak_rss_kb),
         "candidate": _peak_rss_mb(cand.peak_rss_kb)})
    # a side's terminal setup error names a memcg-killed child — the
    # breach signal delegate.py journals per unit (issue #76)
    resources["side_terminal_errors"] = {"reference": ref.terminal_error,
                                         "candidate": cand.terminal_error}

    return _emit_runs_verdicts(
        unit=manifest.get("unit"), fold=fold, rules=rules,
        runs_requested=judged, corpus_path=corpus_path,
        corpus_sha=scan.sha256,
        extra={
            "manifest": str(manifest_path),
            "reference_entry": manifest["reference"]["entry"],
            "candidate_entry": manifest["candidate"]["entry"],
            # observability: None = no child self-reported (zero rows
            # judged, or faked children in tests)
            "resources": resources,
        }, human_flag=args.human)


def _corpus_row_status(i: int, rec: dict | None) -> dict:
    """One runner record -> its dry-run row {i, status, detail}."""
    if rec is None:
        return {"i": i, "status": "missing",
                "detail": "child produced no record for this input"}
    if rec.get("setup_error"):
        # chunk-timeout / chunk-failure pseudo-records: the row was never
        # judged — surfaced as missing, with the reason
        return {"i": i, "status": "missing", "detail": rec["setup_error"]}
    if rec.get("unserializable"):
        return {"i": i, "status": "unserializable", "detail": rec.get("detail")}
    if rec.get("ok"):
        return {"i": i, "status": "ok", "detail": None}
    err = rec.get("error", {})
    return {"i": i, "status": "error",
            "detail": f"{err.get('type')}: {err.get('message')}"}


def _fold_dry_run(side: _SideRunner, rows, chunk_rows: int):
    """Dry-run every streamed row through ``side`` and fold: per-status
    counts over every judged row, the reference-ok / setup-error facts for
    the ground-truth block, and per-row status entries kept verbatim only
    up to RETAIN_VERBATIM_ROWS."""
    kept: list[dict] = []
    truncated = 0
    counts: dict[str, int] = {}
    ref_ok = 0
    setup_error: str | None = None
    for chunk in _chunks(rows, chunk_rows):
        for (i, _value), rec in zip(chunk, side.run_chunk(chunk)):
            row = _corpus_row_status(i, rec)
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            if rec is not None and rec.get("ok") is True:
                ref_ok += 1
            if setup_error is None and rec is not None and rec.get("setup_error"):
                setup_error = rec["setup_error"]
            if len(kept) < RETAIN_VERBATIM_ROWS:
                kept.append(row)
            else:
                truncated += 1
    return kept, truncated, counts, ref_ok, setup_error


def cmd_corpus(args) -> int:
    """Dry-run ONE entry over a corpus: per-row status (ok / error /
    unserializable / missing) before you spend a judge run on it — a
    malformed corpus shows up as a wall of identical errors here, not as
    a hollow all-EQUIVALENT verdict from the judge."""
    corpus_path = Path(args.validate).resolve()
    memory, mem_limit, max_corpus_mb = _memory_limits(
        args.mem_limit_mb, args.max_corpus_mb)
    scan, total, rows = _open_corpus(corpus_path, runs=args.runs,
                                     max_corpus_mb=max_corpus_mb)
    side = _SideRunner(args.entry, corpus_path.parent, args.timeout,
                       mem_limit)
    kept_rows, rows_truncated, counts, ref_ok, first_setup = _fold_dry_run(
        side, rows, args.chunk_rows)

    if side.terminal_error is not None and ref_ok == 0:
        # the entry failed before producing a single record — the whole
        # dry-run is a setup failure (the one-shot era's shape)
        human = f"corpus dry-run: entry failed to run: {side.terminal_error}"
        emit({
            "tool": TOOL, "version": VERSION, "entry": args.entry,
            "corpus": {"path": str(corpus_path), "sha256": scan.sha256},
            "setup_error": side.terminal_error,
            "summary": {"exit_code": EXIT_SETUP, "human": human},
        }, human_flag=args.human)
        return EXIT_SETUP

    ok = counts.get("ok", 0)
    # hollow-verdict signature (issue #29): no row produced a value, so a
    # judge run over this corpus would compare only error-vs-error pairs —
    # the judge's ground-truth guard (issue #30) calls that INCOMPARABLE
    # and already warns here below the ok-fraction threshold (rule
    # defaults; the dry-run takes no manifest to override them)
    gt = _ground_truth_from(ref_ok, side.terminal_error or first_setup,
                            total, merge_rules({}))
    hollow = gt["degenerate"]
    if hollow:
        warning = (
            "every reference run errored/was unserializable — a judge "
            "verdict over this corpus would be hollow (identical errors "
            "compare as equivalent). Check the corpus format (one raw JSON "
            "value per line — no {\"i\",\"input\"} pre-wrapping) and the "
            "entry's expected input shape."
        )
    else:
        warning = gt["warning"]

    code = EXIT_OK if ok == total else EXIT_FINDINGS
    human = f"corpus {corpus_path.name} vs {args.entry}: {ok}/{total} rows ok"
    if ok < total:
        human += f", {total - ok} error/unserializable"
    if hollow:
        human += " — HOLLOW: no row produced a value"
    elif gt["warning"]:
        human += f" — THIN: {gt['warning']}"
    payload = {
        "tool": TOOL, "version": VERSION, "entry": args.entry,
        "corpus": {"path": str(corpus_path), "lines": total,
                   "sha256": scan.sha256},
        "rows": kept_rows, "counts": dict(sorted(counts.items())),
        "ok": ok, "total": total,
        "hollow_equivalent_risk": hollow, "warning": warning,
        "reference_ok_fraction": gt["reference_ok_fraction"],
        "summary": {"exit_code": code, "human": human},
    }
    if side.terminal_error is not None:
        # a side that died mid-corpus with rows already judged — later
        # rows report missing; this names the cause
        payload["setup_error"] = side.terminal_error
    if rows_truncated:
        payload["rows_truncated"] = rows_truncated
    # the dry-run side's peak RSS, same self-report the judge folds into
    # its verdict payload
    payload["resources"] = _resources_block(
        memory, mem_limit, _peak_rss_mb(side.peak_rss_kb))
    emit(payload, human_flag=args.human)
    return code


def cmd_compare(args) -> int:
    payload = read_json(args.runs)
    for key in ("reference", "candidate"):
        if not isinstance(payload.get(key), list):
            raise ToolError(f"runs file missing list {key!r}", kind="usage")
    rules = merge_rules(payload)
    ref_recs = _records_by_index(payload["reference"])
    cand_recs = _records_by_index(payload["candidate"])
    # the record lists exist by construction here, but the summary folds
    # through the same _RunFold retention cap as the streamed judge
    # (issue #75), keeping payloads uniform
    fold = _RunFold()
    for i, run in enumerate(classify_runs(ref_recs, cand_recs, rules)):
        fold.add(run, ref_rec=ref_recs[i] if i < len(ref_recs) else None)
    return _emit_runs_verdicts(
        unit=payload.get("unit"), fold=fold, rules=rules,
        extra={"runs_file": str(Path(args.runs).resolve())},
        human_flag=args.human)


def _records_by_index(recs: list) -> list:
    by_i = {r["i"]: r for r in recs if "i" in r}
    return [by_i.get(i) for i in range(max((r["i"] for r in recs if "i" in r), default=-1) + 1)]


def _emit_runs_verdicts(*, unit, fold: _RunFold, rules, extra: dict,
                        human_flag: bool = False,
                        runs_requested: int | None = None,
                        corpus_path=None, corpus_sha: str | None = None) -> int:
    """Emit the summary verdict payload from a folded judgment: counts,
    histogram, digest and ground truth cover EVERY judged run; ``fold.runs``
    holds the verbatim-capped run entries. The streamed judge passes its
    corpus facts; the compare path (a pre-captured runs file) has none."""
    counts = fold.counts
    total = fold.total
    diff_kinds = fold.diff_kinds
    gt = _ground_truth_from(fold.ref_ok, fold.setup_error, total, rules)
    verdict = summary_verdict(counts, gt)
    code = {"equivalent": EXIT_OK, "divergent": EXIT_FINDINGS,
            "incomparable": EXIT_INCOMPARABLE}[verdict]

    human = (
        f"unit={unit or '-'}: {counts['equivalent']}/{total} equivalent, "
        f"{counts['divergent']} divergent, {counts['incomparable']} incomparable "
        f"-> {verdict.upper()}"
    )
    if gt["degenerate"]:
        human += f"; {gt['detail']} — no ground truth: {_degenerate_note(counts)}"
    elif gt["warning"]:
        human += f"; WARNING: {gt['warning']}"
    if diff_kinds:
        human += (f"; top diff kinds: "
                  f"{sorted(diff_kinds, key=diff_kinds.get, reverse=True)[:3]}")
    payload = {
        "tool": TOOL, "version": VERSION, "unit": unit, "verdict": verdict,
        "counts": counts, "total": total, "runs": fold.runs,
        "diff_kind_histogram": dict(sorted(diff_kinds.items())),
        "verdicts_digest": fold.verdicts_digest,
        "ground_truth": gt,
        "normalization": _rules_public(rules),
        "summary": {"exit_code": code, "human": human},
    }
    if fold.summarized:
        payload["runs_summarized"] = fold.summarized
    if runs_requested is not None:
        payload["runs_requested"] = runs_requested
    if corpus_path is not None:
        payload["corpus"] = {
            "path": str(corpus_path), "lines": runs_requested,
            "sha256": corpus_sha,
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


def _degenerate_corpus_guard(entry: str) -> dict:
    """Issue #30 probe through the production judging path: a corpus the
    reference fully rejects (every row's separator is longer than its text,
    so the reference raises) must classify INCOMPARABLE at the summary even
    though every per-run pair is error==error equivalent — the
    hollow-verdict scenario the guard exists for. ``entry`` is the
    self-test reference (already registered in sys.modules). Returns the
    ``degenerate_corpus_guard`` block of the self-test payload."""
    inputs = [
        {"text": "a", "sep": "!!longer-than-a"},
        {"text": "ab", "sep": "!!!!longer-than-ab"},
    ]
    ref = _execute_entry_local(entry, inputs)
    cand = _execute_entry_local(entry, inputs)
    rules = merge_rules({})
    runs = classify_runs(ref, cand, rules)
    counts = count_verdicts(runs)
    gt = reference_ground_truth(ref, len(inputs), rules)
    verdict = summary_verdict(counts, gt)
    return {
        "per_run_equivalent": counts["equivalent"],
        "reference_ok": gt["reference_ok"],
        "summary_verdict": verdict,
        "held": (counts["equivalent"] == len(inputs)
                 and gt["degenerate"] and gt["reference_ok"] == 0
                 and verdict == "incomparable"),
    }


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

    guard = _degenerate_corpus_guard("_oracle_selftest_reference:run")

    caught = sum(1 for r in results if r["caught"])
    passed = caught == len(SELF_TEST_BUGS) and benign_ok and guard["held"]
    code = EXIT_OK if passed else EXIT_FINDINGS
    human = (
        f"judge-first validation: {caught}/{len(SELF_TEST_BUGS)} injected bugs caught"
        + ("" if benign_ok else "; WARNING: benign differences flagged as divergent")
        + ("; all-error reference corpus -> INCOMPARABLE (guard held)"
           if guard["held"] else
           "; WARNING: all-error reference corpus did not classify INCOMPARABLE")
    )
    emit({
        "tool": TOOL, "version": VERSION,
        "injected_bugs": len(SELF_TEST_BUGS), "caught": caught,
        "results": results,
        "benign_normalization_cases": benign_results,
        "degenerate_corpus_guard": guard,
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
    p.add_argument("--timeout", type=float, default=120.0,
                   help="per-side, per-chunk timeout in s (issue #75: a slow "
                        "row times out its chunk only — those rows judge "
                        "incomparable and the remaining chunks still judge)")
    p.add_argument("--chunk-rows", type=int, default=DEFAULT_CHUNK_ROWS,
                   help=f"corpus rows per child chunk (issue #75): the corpus "
                        f"streams and is judged chunk by chunk — the parent "
                        f"never materializes the corpus, the stdin framing, "
                        f"or the full record set, and each chunk gets its own "
                        f"--timeout (default {DEFAULT_CHUNK_ROWS})")
    p.add_argument("--max-corpus-mb", type=int, default=None,
                   help=f"refuse — before any child is spawned — a corpus "
                        f"whose estimated parent footprint (bytes x "
                        f"{CORPUS_FOOTPRINT_FACTOR:g}) exceeds this many MB "
                        f"(issue #75); an explicit --runs cap scales the "
                        f"estimate and is the documented repair, and 0 "
                        f"disables the check. DEFAULT (issue #76): the "
                        f"'memory' block of autonomy/policy.json "
                        f"(policy:MEM-1, max_corpus_mb; fallback "
                        f"{DEFAULT_MAX_CORPUS_MB}); an explicit value is an "
                        f"operator override journaled in resources as a "
                        f"deviation")
    p.add_argument("--mem-limit-mb", type=int, default=None,
                   help=f"per-child cgroup memory ceiling in MB: each "
                        f"reference/candidate child runs in its own "
                        f"systemd-run user scope (cgroup-v2 MemoryMax, "
                        f"MemorySwapMax=0), so a ballooning child is killed "
                        f"alone (SIGKILL -> 137) and judged setup_error -> "
                        f"incomparable — not a host-wide OOM. DEFAULT "
                        f"(issue #76): the 'memory' block of "
                        f"autonomy/policy.json (policy:MEM-1, judge_child_mb; "
                        f"fallback {DEFAULT_MEM_LIMIT_MB}); an explicit value "
                        f"is an operator override journaled in resources as "
                        f"a deviation, 0 disables. Falls back to "
                        f"unscoped with a stderr warning when systemd-run "
                        f"is unavailable")
    p.set_defaults(func=cmd_judge)

    p = sub.add_parser(
        "corpus",
        help="dry-run one entry over a corpus: per-row ok/error/unserializable "
             "reference status before judging (exit 0 all ok · 2 findings · 4 setup)")
    p.add_argument("--validate", required=True, metavar="CORPUS_JSONL",
                   help="corpus file to dry-run (one raw JSON value per line — "
                        "see module docstring; NOT pre-wrapped in {\"i\",\"input\"})")
    p.add_argument("--entry", required=True,
                   help="module:function or path/to/file.py:function to execute "
                        "(default fn: run); resolves with the corpus file's "
                        "directory as cwd")
    p.add_argument("--runs", type=int, default=0, help="cap the corpus (0 = all)")
    p.add_argument("--timeout", type=float, default=120.0,
                   help="per-chunk timeout in s (issue #75: a slow row times "
                        "out its chunk only — those rows report missing and "
                        "the remaining chunks still dry-run)")
    p.add_argument("--chunk-rows", type=int, default=DEFAULT_CHUNK_ROWS,
                   help=f"corpus rows per child chunk (issue #75) — same "
                        f"streaming shape as judge (default {DEFAULT_CHUNK_ROWS})")
    p.add_argument("--max-corpus-mb", type=int, default=None,
                   help=f"pre-flight corpus budget in MB, same refusal as "
                        f"judge (issue #75; 0 disables the check) — DEFAULT "
                        f"(issue #76): policy.json 'memory'.max_corpus_mb "
                        f"(policy:MEM-1; fallback {DEFAULT_MAX_CORPUS_MB})")
    p.add_argument("--mem-limit-mb", type=int, default=None,
                   help=f"per-child cgroup memory ceiling in MB — same "
                        f"scoping as judge children (policy.json 'memory' "
                        f"judge_child_mb, policy:MEM-1, issue #76; fallback "
                        f"{DEFAULT_MEM_LIMIT_MB}); a breach shows as a setup "
                        f"error row, not a host OOM; 0 disables")
    p.set_defaults(func=cmd_corpus)

    p = sub.add_parser("compare", help="classify pre-captured runs (shadow mode)")
    p.add_argument("--runs", required=True, help="JSON {reference: [...], candidate: [...]}")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("self-test",
                       help="Stage 2 judge-first validation: 5/5 injected bugs "
                            "+ degenerate-corpus guard (issue #30)")
    p.set_defaults(func=cmd_self_test)

    p = sub.add_parser("rules", help="print documented normalization rules")
    p.set_defaults(func=cmd_rules)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(run_cli(main, tool=TOOL, version=VERSION))
