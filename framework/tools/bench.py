#!/usr/bin/env python3
"""bench.py — cross-language differential benchmark (framework/tools/README.md #5).

Stages 0, 5. Runs the SAME matched corpus through both entry points (the
Python original and the Rust-backed wheel — same frozen API), checks parity
first, then times both sides deterministically. No LLM anywhere
(anti-requirement), and NO NUMBER WITHOUT A REPRO: every timing report
embeds the corpus commit + sha256 and ships with a self-contained
reproduction script; a dirty corpus is refused unless --allow-dirty.

Manifest (JSON):

    {"name": "parse-bench",
     "corpus": "corpora/parse-golden.jsonl",     # JSONL inputs
     "python": {"entry": "pkg.parse:run"},
     "rust":   {"entry": "pkg_core.units.u_parse_1:run"},
     "warmup": 2, "repeats": 5,                  # defaults shown
     "normalization": {}}                        # oracle rules (optional)

Entries are python callables (module:function, default fn name ``run``) fed
one JSON value per corpus line — the same protocol oracle.py uses. Parity
runs through the oracle comparator (imported, not duplicated), so "faster"
can never mean "wronger": divergent outputs abort the benchmark.

Timing model: per side, `warmup` untimed passes, then `repeats` timed
full-corpus passes (perf_counter_ns). Statistics: median/mean/min/max; the
headline speedup is median_python / median_rust with a deterministic
percentile bootstrap CI (seed fixed at 20260210, 2000 resamples).

Exit codes: 0 verdict produced · 2 parity divergence · 4 setup failure ·
1 usage/IO.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    EXIT_ERROR, EXIT_FINDINGS, EXIT_OK, emit, python_version_string,
    read_json, read_jsonl, run_child, sha256_bytes, sha256_file,
)
from oracle import (  # noqa: E402
    RUNNER_SRC, _execute_entry_subprocess, classify_pair, merge_rules,
)

TOOL = "bench.py"
VERSION = 1
BOOTSTRAP_SEED = 20260210
BOOTSTRAP_RESAMPLES = 2000


def _git(repo_dir: Path, *args: str) -> tuple[int, str]:
    code, out, err = run_child(["git", *args], cwd=str(repo_dir), timeout=30.0)
    return code, (out or err).strip()


def corpus_commit_info(corpus_path: Path, allow_dirty: bool) -> tuple[dict, list[str]]:
    """Corpus reproducibility: git commit + cleanliness. Refuses dirty
    corpora unless allowed (a benchmark over a drifting corpus is exactly
    the vendor-benchmark-rot failure mode)."""
    info: dict = {"path": str(corpus_path), "sha256": sha256_file(corpus_path)}
    warnings = []
    repo = corpus_path.resolve().parent
    while repo != repo.parent:
        if (repo / ".git").exists():
            break
        repo = repo.parent
    if not (repo / ".git").exists():
        info["commit"] = None
        warnings.append("corpus is not inside a git repository — commit it, or the "
                        "repro script cannot pin it")
        return info, warnings
    code, commit = _git(repo, "rev-parse", "HEAD")
    info["commit"] = commit if code == 0 else None
    info["repo"] = str(repo)
    code, status = _git(repo, "status", "--porcelain", "--", str(corpus_path.resolve()))
    dirty = code == 0 and status != ""
    info["dirty"] = dirty
    if dirty:
        warnings.append("corpus has uncommitted changes — commit before benchmarking")
        if not allow_dirty:
            raise ValueError("corpus is dirty (refuse: no number without repro); "
                             "pass --allow-dirty to override")
    return info, warnings


def time_entry(entry: str, inputs: list, manifest_dir: Path, passes: int) -> list[float]:
    """Time `passes` full-corpus runs of one entry, one child process per
    pass (fresh import state — honest cold-start semantics, deterministic)."""
    timings = []
    for _ in range(passes):
        stdin = "".join(json.dumps({"i": i, "input": v}) + "\n"
                        for i, v in enumerate(inputs))
        t0 = time.perf_counter_ns()
        code, stdout, _ = run_child([sys.executable, "-c", RUNNER_SRC, entry],
                                    cwd=str(manifest_dir), input_text=stdin,
                                    timeout=600.0)
        t1 = time.perf_counter_ns()
        if code != 0:
            raise RuntimeError(f"entry {entry} exited {code}")
        timings.append((t1 - t0) / 1e9)
    return timings


def stats_of(timings: list[float]) -> dict:
    return {
        "n": len(timings),
        "median_s": round(statistics.median(timings), 6),
        "mean_s": round(statistics.fmean(timings), 6),
        "min_s": round(min(timings), 6),
        "max_s": round(max(timings), 6),
        "all_s": [round(t, 6) for t in timings],
    }


def bootstrap_ratio(py: list[float], rs: list[float]) -> dict:
    """Median/median speedup with a deterministic percentile bootstrap CI."""
    rng = random.Random(BOOTSTRAP_SEED)
    ratios = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        p = statistics.median(rng.choices(py, k=len(py)))
        r = statistics.median(rng.choices(rs, k=len(rs)))
        if r > 0:
            ratios.append(p / r)
    ratios.sort()
    def pct(q: float) -> float:
        idx = min(len(ratios) - 1, max(0, int(q * len(ratios))))
        return round(ratios[idx], 3)
    return {"method": f"bootstrap median ratio, n={BOOTSTRAP_RESAMPLES}, "
                      f"seed={BOOTSTRAP_SEED}",
            "median_speedup": round(statistics.median(py) / statistics.median(rs), 3),
            "ci05": pct(0.05), "ci95": pct(0.95)}


REPRO_TEMPLATE = """#!/usr/bin/env bash
# Auto-generated by framework/tools/bench.py — no number without a repro.
# Corpus: {corpus_path} @ {commit}
set -euo pipefail

REPO={repo}
CORPUS_REL={corpus_rel}

if [ -n "$REPO" ] && [ -d "$REPO/.git" ]; then
  git -C "$REPO" fetch --quiet || true
  git -C "$REPO" checkout --quiet {commit} -- "$CORPUS_REL"
fi

# exact invocation that produced the report:
cd "{manifest_dir}"
{invocation}
"""


def cmd_run(args) -> int:
    manifest_path = Path(args.manifest).resolve()
    manifest = read_json(manifest_path)
    for key in ("name", "corpus", "python", "rust"):
        if key not in manifest:
            print(f"manifest missing required key {key!r}", file=sys.stderr)
            return EXIT_ERROR
    corpus_path = Path(manifest["corpus"])
    if not corpus_path.is_absolute():
        corpus_path = manifest_path.parent / corpus_path
    rows = read_jsonl(corpus_path)
    inputs = [v for _, v in rows]
    if not inputs:
        print(f"corpus {corpus_path} is empty", file=sys.stderr)
        return EXIT_ERROR

    try:
        corpus_info, warnings = corpus_commit_info(corpus_path, args.allow_dirty)
    except ValueError as e:
        # machine-readable refusal — a benchmark that cannot pin its corpus
        # reports that instead of a number
        emit({"tool": TOOL, "version": VERSION, "name": manifest["name"],
              "corpus": {"path": str(corpus_path), "sha256": sha256_file(corpus_path)},
              "error": str(e),
              "summary": {"exit_code": EXIT_ERROR,
                          "human": f"benchmark {manifest['name']}: REFUSED — {e}"}},
             human_flag=args.human)
        return EXIT_ERROR

    # ---- parity first: a benchmark on divergent outputs is not a benchmark
    rules = merge_rules(manifest)
    py_recs = _execute_entry_subprocess(manifest["python"]["entry"], inputs,
                                        manifest_path.parent, 600.0)
    rs_recs = _execute_entry_subprocess(manifest["rust"]["entry"], inputs,
                                        manifest_path.parent, 600.0)

    def rec(records: list, i: int):
        return records[i] if i < len(records) else None

    verdicts = [classify_pair(i, rec(py_recs, i), rec(rs_recs, i), rules)
                for i in range(len(inputs))]
    counts = {"equivalent": 0, "divergent": 0, "incomparable": 0}
    for v in verdicts:
        counts[v["verdict"]] += 1
    parity = {"verdict": ("equivalent" if counts["divergent"] == 0 and
                          counts["incomparable"] == 0 else "divergent"),
              "counts": counts, "runs": len(inputs),
              "input_digest": sha256_bytes("\n".join(
                  json.dumps(v, sort_keys=True) for v in inputs).encode())[:16]}
    if parity["verdict"] != "equivalent":
        first_div = next((v for v in verdicts if v["verdict"] != "equivalent"), None)
        code = EXIT_FINDINGS
        human = (f"benchmark {manifest['name']}: ABORTED — outputs diverge "
                 f"({counts['divergent']} divergent, {counts['incomparable']} "
                 f"incomparable of {len(inputs)} runs); no timing claim")
        emit({"tool": TOOL, "version": VERSION, "name": manifest["name"],
              "parity": parity,
              "first_divergence": (first_div or {}).get("diffs", [])[:3],
              "corpus": corpus_info, "warnings": warnings,
              "summary": {"exit_code": code, "human": human}}, human_flag=args.human)
        return code

    # ---- matched inputs both sides: verified by construction (same list),
    # recorded for audit
    warmup = int(manifest.get("warmup", 2))
    repeats = int(manifest.get("repeats", 5))
    for side in ("python", "rust"):
        time_entry(manifest[side]["entry"], inputs, manifest_path.parent, warmup)
    py_timings = time_entry(manifest["python"]["entry"], inputs, manifest_path.parent, repeats)
    rs_timings = time_entry(manifest["rust"]["entry"], inputs, manifest_path.parent, repeats)
    speedup = bootstrap_ratio(py_timings, rs_timings)

    repro_path = manifest_path.parent / f"bench-{manifest['name']}.repro.sh"
    rel = ""
    repo = corpus_info.get("repo") or ""
    if repo and corpus_path.resolve().is_relative_to(Path(repo)):
        rel = str(corpus_path.resolve().relative_to(repo))
    invocation = f"{sys.executable} {Path(__file__).resolve()} run --manifest {args.manifest}"
    repro_path.write_text(REPRO_TEMPLATE.format(
        corpus_path=str(corpus_path), commit=corpus_info.get("commit") or "HEAD",
        repo=repo, corpus_rel=rel, manifest_dir=str(manifest_path.parent),
        invocation=invocation), encoding="utf-8")
    repro_path.chmod(0o755)

    code = EXIT_OK
    human = (f"benchmark {manifest['name']}: parity equivalent on {len(inputs)} runs; "
             f"median speedup {speedup['median_speedup']}x "
             f"[{speedup['ci05']}, {speedup['ci95']}]; repro: {repro_path.name}")
    emit({
        "tool": TOOL, "version": VERSION, "name": manifest["name"],
        "corpus": corpus_info,
        "matched_inputs": {"count": len(inputs), "identical_order": True,
                           "digest": parity["input_digest"]},
        "parity": parity,
        "python": {"entry": manifest["python"]["entry"], "timings_s": stats_of(py_timings)},
        "rust": {"entry": manifest["rust"]["entry"], "timings_s": stats_of(rs_timings)},
        "speedup": speedup,
        "repro_script": str(repro_path),
        "warnings": warnings,
        "env": {"python": python_version_string()},
        "summary": {"exit_code": code, "human": human},
    }, human_flag=args.human)
    return code


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=TOOL, description=__doc__.splitlines()[0])
    ap.add_argument("--human", action="store_true", help="echo human summary to stderr")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run one benchmark manifest")
    p.add_argument("--manifest", required=True)
    p.add_argument("--allow-dirty", action="store_true",
                   help="permit an uncommitted corpus (recorded as a warning)")
    p.set_defaults(func=cmd_run)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
