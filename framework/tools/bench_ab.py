#!/usr/bin/env python3
"""bench_ab.py — matched-corpus A/B harness driving the state.py shim (issue #34).

Stages 0, 1, 6. Three gates want the same evidence shape — the SAME
measured workload, Python path vs ported path:

  - GO-1: the ≥50% end-to-end bar is a *claim* until a matched benchmark
    exists (pipeline.py prefers a measured `end_to_end_saving_pct` recorded
    on the gate over the Stage-0 projection),
  - RED-1: a redesign is approved only if a spike passes the oracle AND the
    matched-corpus benchmark clears the bar (`bench_improvement_pct`),
  - CUT-1 / the honest-reporting rule: "port wasn't win -> report honestly"
    needs a re-measurement with the ported units live.

The pieces existed (the per-unit fallback shim from `state.py shim`, the
target project's own benchmark scripts); what was missing is the harness
that drives them. This is that harness — it turns the framework's most
important rule ("gates decide, not you") from prose into a command:

    python framework/tools/bench_ab.py --state <state.json> \
        --out runs/<project>/bench/ -- <target bench command…>

What it does:

  1. derives the shim env var EXACTLY as `state.py shim` does (imported —
     the derivation cannot drift) plus the unit set actually live on the
     rust side (`ported` / `divergent_explained`; `retired` units have no
     callable fallback and are excluded);
  2. runs the given command once per side, interleaved python-first so
     thermal/drift bias hits both sides equally: the python side with
     <PREFIX>_FALLBACK=<ported-unit-ids> (those units fall back), the
     ported side with <PREFIX>_FALLBACK=none (rust live);
  3. captures wall/user/sys time per run (user/sys via os.times() child
     deltas), stdout/stderr as artifacts with sha256, and byte-identity of
     the payloads — timing without identity proves nothing, so a divergent
     stdout aborts with NO ratio claim (`--mask REGEX` may hide known
     volatile fields like printed durations; masked identity is recorded
     as such, never as identical);
  4. emits the matched-corpus benchmark artifact the gates want (command,
     corpus pinning, both timings, ratio + bootstrap CI, hashes, the
     ported-unit set, the exact repro script — no number without a repro);
  5. with --gate {go_no_go,redesign_approval,cutover} writes the
     measurement into the state file as gate evidence through state.py's
     journaled, validated writer (merge_evidence + save_validated), so
     `pipeline.py resolve-gates` can consume it. Refuses to write gate
     evidence when outputs diverge — there is no honest claim to record.

The command after `--` is the target's own bench entry (its script, pytest,
timeit — anything runnable); it runs verbatim in --cwd (default: the
recorded preflight.target.root). Its corpus is whatever it exercises — pin
it with --corpus <path> (hashed + git-pinned like bench.py) and describe it
with --corpus-desc.

Exit codes follow tools/README.md: 0 verdict produced · 2 findings (outputs
diverge, or a side failed/timed out — timings recorded, no ratio claim) ·
1 usage/IO · 4 never (no setup deps — stdlib only).
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state as state_tool  # noqa: E402  — shim derivation + journaled gate writes
from bench import bootstrap_ratio, corpus_commit_info, stats_of  # noqa: E402
from _common import (  # noqa: E402
    EXIT_FINDINGS, EXIT_OK, ToolError, emit, python_version_string,
    run_cli, sha256_bytes, sha256_file, write_json_atomic,
)

TOOL = "bench_ab.py"
VERSION = 1
RUN_TIMEOUT = 1800.0                  # one full bench run of the target command
# gates whose evidence bench_ab may write (--gate); the policy's evidence_in
# keys land verbatim: end_to_end_saving_pct (GO-1), bench_improvement_pct (RED-1)
GATES_WITH_MEASUREMENT = ("go_no_go", "redesign_approval", "cutover")
SIDES = ("python", "ported")          # python = fallback forced, ported = rust live


# ------------------------------------------------------------- helpers ----

def split_trailing_command(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split argv at the first '--': (tool flags, target bench command)."""
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _apply_masks(text: str, masks: list[str]) -> tuple[str, list[str]]:
    """Replace every regex match with <MASKED>. Returns (text, bad_patterns)
    — an invalid pattern is reported, never silently ignored."""
    bad = []
    for pattern in masks:
        try:
            text = re.sub(pattern, "<MASKED>", text)
        except re.error:
            bad.append(pattern)
    return text, bad


def _run_once(command: list[str], cwd: Path, env_var: str, env_value: str,
              label: str, out_dir: Path, timeout: float, timed: bool) -> dict:
    """One full run of the target command under one shim setting. Raw bytes
    are captured (byte-identity must not be laundered by decoding); wall via
    perf_counter_ns, child user/sys via os.times() deltas (updated when the
    child is reaped — subprocess.run waits)."""
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTHONSTARTUP", None)
    env[env_var] = env_value
    stdout_path = out_dir / f"{label}.stdout"
    stderr_path = out_dir / f"{label}.stderr"
    rec: dict = {"label": label, "timed": timed, "env": {env_var: env_value},
                 "stdout": str(stdout_path), "stderr": str(stderr_path)}
    t0 = time.perf_counter_ns()
    cpu0 = os.times()
    try:
        proc = subprocess.run(command, cwd=str(cwd), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        rec.update({"exit_code": None, "timeout": True,
                    "wall_s": None, "user_s": None, "sys_s": None,
                    "stdout_sha256": None, "stderr_sha256": None,
                    "stdout_bytes": 0, "stderr_bytes": 0})
        stdout_path.write_bytes(b"")
        stderr_path.write_bytes(b"")
        rec["detail"] = f"timed out after {timeout:g}s"
        return rec
    wall = (time.perf_counter_ns() - t0) / 1e9
    cpu1 = os.times()
    stdout_path.write_bytes(proc.stdout)
    stderr_path.write_bytes(proc.stderr)
    rec.update({
        "exit_code": proc.returncode, "timeout": False,
        "wall_s": round(wall, 6),
        "user_s": round(cpu1.children_user - cpu0.children_user, 6),
        "sys_s": round(cpu1.children_system - cpu0.children_system, 6),
        "stdout_sha256": sha256_bytes(proc.stdout),
        "stderr_sha256": sha256_bytes(proc.stderr),
        "stdout_bytes": len(proc.stdout), "stderr_bytes": len(proc.stderr),
    })
    return rec


def _identity(timed: dict, masks: list[str]) -> tuple[dict, list[str]]:
    """Byte-identity across every timed run of BOTH sides, per stream.
    stdout gates the claim (the payload); stderr is recorded and warned on
    but does not abort — diagnostics, not payload. Masked-equality is its
    own verdict, never 'identical'. A side with no captured payload
    (timeout) makes the stream not_evaluable — never a fake 'identical'."""
    warnings: list[str] = []
    bad_patterns: list[str] = []
    verdicts: dict = {}
    for stream, severity in (("stdout", "blocking"), ("stderr", "advisory")):
        raw, masked = set(), set()
        per_side: dict[str, int] = {}
        for side in SIDES:
            per_side[side] = 0
            for rec in timed[side]:
                if rec[f"{stream}_sha256"] is None:
                    continue
                per_side[side] += 1
                raw.add(rec[f"{stream}_sha256"])
                text, bad = _apply_masks(_decode(Path(rec[stream]).read_bytes()),
                                         masks)
                bad_patterns.extend(bad)
                masked.add(sha256_bytes(text.encode("utf-8")))
        if min(per_side.values()) == 0:
            v = "not_evaluable"
        elif len(raw) == 1:
            v = "identical"
        elif len(masked) == 1:
            v = "masked_identical"
            warnings.append(f"{stream}: identical only after masking "
                            f"{masks!r} — assert the masked fields are "
                            f"non-payload (timings, timestamps)")
        else:
            v = "divergent"
        verdicts[stream] = {"verdict": v, "severity": severity,
                            "raw_sha256": sorted(raw),
                            "masked_sha256": sorted(masked)}
    for pattern in sorted(set(bad_patterns)):
        warnings.append(f"invalid --mask regex (ignored): {pattern!r}")
    return verdicts, warnings


def _first_divergence(timed: dict, stream: str, limit: int = 3) -> list[dict]:
    """First differing lines, python vs ported, timed run 0 vs 0 — enough to
    act on, not a diff dump."""
    a = _decode(Path(timed["python"][0][stream]).read_bytes()).splitlines()
    b = _decode(Path(timed["ported"][0][stream]).read_bytes()).splitlines()
    out = []
    for i in range(max(len(a), len(b))):
        la = a[i] if i < len(a) else "<missing>"
        lb = b[i] if i < len(b) else "<missing>"
        if la != lb:
            out.append({"line": i + 1, "python": la[:200], "ported": lb[:200]})
            if len(out) >= limit:
                break
    return out


def _side_stats(timed: dict, side: str) -> dict:
    walls = [r["wall_s"] for r in timed[side] if r["wall_s"] is not None]
    users = [r["user_s"] for r in timed[side] if r["user_s"] is not None]
    syss = [r["sys_s"] for r in timed[side] if r["sys_s"] is not None]
    empty = {"n": 0}  # every run failed/timed out: no timings, honestly
    return {"wall_s": stats_of(walls) if walls else dict(empty),
            "user_s": stats_of(users) if users else dict(empty),
            "sys_s": stats_of(syss) if syss else dict(empty)}


REPRO_TEMPLATE = """#!/usr/bin/env bash
# Auto-generated by framework/tools/bench_ab.py — no number without a repro.
# state: {state_path} (sha256 {state_sha})
# measured at git commit {commit} in {cwd}
# masked fields (identity asserted only after masking): {masks}
set -euo pipefail

cd "{cwd}"
if [ -d .git ]; then
  HEAD="$(git rev-parse HEAD)"
  if [ "$HEAD" != "{commit}" ]; then
    echo "WARNING: HEAD is $HEAD; the recorded measurement was taken at {commit}" >&2
  fi
fi

OUT="$(mktemp -d)"
# python side — fallback forced for: {fallback_units}
{env_var}={fallback_value} {command} >"$OUT/python.stdout" 2>"$OUT/python.stderr"
# ported side — rust live for the recorded unit set
{env_var}=none {command} >"$OUT/ported.stdout" 2>"$OUT/ported.stderr"
if cmp -s "$OUT/python.stdout" "$OUT/ported.stdout"; then
  echo "identity: stdout identical"
else
  echo "identity: stdout DIFFERS (see masks above if masked fields were used)"
fi
echo "artifacts in $OUT"
"""


# ------------------------------------------------------------- gate write --

def _gate_evidence(gate_name: str, improvement_pct: float, ratio: dict,
                   identity: str, artifact_path: Path, command: list[str]) -> list[str]:
    """The key=value records the policy rules read (autonomy/policy.json
    evidence_in), extracted from the measurement — never invented."""
    ev = [f"bench_ab_artifact={artifact_path}",
          f"bench_ab_identity={identity}",
          f"bench_ab_ratio={ratio['median_speedup']:g}",
          f"bench_ab_command={shlex.join(command)}"]
    if gate_name == "go_no_go":
        ev.append(f"end_to_end_saving_pct={improvement_pct:g}")
    elif gate_name == "redesign_approval":
        ev.append(f"bench_improvement_pct={improvement_pct:g}")
    else:  # cutover — supporting evidence for the honest report; CUT-1's
        # own bar (shadow/canary/rollback) is measured elsewhere
        ev.append(f"end_to_end_saving_pct={improvement_pct:g}")
    return ev


def _write_gate_evidence(path: Path, state: dict, gate_name: str,
                         evidence: list[str]) -> tuple[dict, bool, int]:
    """Append evidence onto the newest gate of that name (opening one if
    none exists) through state.py's journaled writer — bench_ab never
    bypasses save_validated, so the write is validated, atomic, rolled
    back on failure. Raises ValueError from save_validated if invalid."""
    gate = state_tool.find_gate(state, gate_name)
    changes: list = []
    created = False
    if gate is None:
        gate = {"gate": gate_name, "state": "open",
                "note": f"{gate_name}: opened by {TOOL} to record measured "
                        f"A/B evidence"}
        state.setdefault("human_gates", []).append(gate)
        changes.append((None, f"human_gates[gate={gate_name}].state", None,
                        "open"))
        created = True
    merged = state_tool.merge_evidence(gate.get("evidence"), evidence)
    state_tool.record_gate_fields(gate, {"evidence": merged}, changes)
    state_tool.save_validated(state, path, actor=TOOL, changes=changes)
    return gate, created, len(changes)


# -------------------------------------------------------------- command ---

def cmd_run(args) -> int:
    state_path = Path(args.state).resolve()
    state = state_tool._load(state_path)  # noqa: SLF001 — same read path delegate.py uses

    # ---- what to A/B: the units actually live on the rust side
    rust_live = {u["id"]: u.get("status") for u in state.get("units", [])
                 if isinstance(u, dict) and u.get("id")
                 and u.get("status") in state_tool.RUST_LIVE_STATUSES}
    fallback_units = [uid for uid, st in rust_live.items() if st != "retired"]
    if not fallback_units:
        raise ToolError(
            "no unit is live on the rust side (status ported/"
            "divergent_explained) — the shim env var would flip nothing; "
            "there is no python-vs-ported A/B to measure", kind="usage")
    if not args.command:
        raise ToolError("no bench command given — pass it after '--' "
                        "(e.g. -- python -m pytest bench/)", kind="usage")
    if args.repeat < 1 or args.warmup < 0 or args.timeout <= 0:
        raise ToolError("--repeat >= 1, --warmup >= 0, --timeout > 0 required",
                        kind="usage")

    env_var = state_tool.fallback_env_var(state, args.env_prefix)
    python_value = ",".join(fallback_units)
    ported_value = "none"

    cwd = Path(args.cwd) if args.cwd else None
    if cwd is None:
        root = ((state.get("preflight") or {}).get("target") or {}).get("root")
        cwd = Path(root) if root else None
    if cwd is None or not cwd.is_dir():
        raise ToolError(
            "no runnable --cwd (and no recorded preflight.target.root) — the "
            "bench command must run in the target repo; pass --cwd or record "
            "it via `pipeline.py preflight --target-root`", kind="io")
    cwd = cwd.resolve()

    out_dir = Path(args.out).resolve() if args.out else state_path.parent / "bench-ab"
    out_dir.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    corpus: dict = {"provided": False,
                    "note": "the command defines its own corpus — pin it with "
                            "--corpus <path> and describe it with --corpus-desc"}
    if args.corpus_desc:
        corpus["description"] = args.corpus_desc
    if args.corpus:
        corpus_path = Path(args.corpus).resolve()
        try:
            info, corpus_warnings = corpus_commit_info(corpus_path,
                                                       args.allow_dirty)
        except ValueError as e:
            raise ToolError(f"corpus {corpus_path}: {e}", kind="usage") from e
        corpus.update({"provided": True, "note": None, **info})
        warnings.extend(corpus_warnings)

    # ---- the runs: interleaved pairs, python first, so drift hits both
    runs: dict = {side: [] for side in SIDES}
    values = {"python": python_value, "ported": ported_value}
    for w in range(args.warmup):
        for side in SIDES:
            runs[side].append(_run_once(args.command, cwd, env_var,
                                        values[side], f"{side}.w{w}",
                                        out_dir, args.timeout, timed=False))
    for r in range(args.repeat):
        for side in SIDES:
            runs[side].append(_run_once(args.command, cwd, env_var,
                                        values[side], f"{side}.{r}",
                                        out_dir, args.timeout, timed=True))
    timed = {side: [rec for rec in runs[side] if rec["timed"]]
             for side in SIDES}

    identity, id_warnings = _identity(timed, args.mask)
    warnings.extend(id_warnings)
    failures = [rec for side in SIDES for rec in timed[side]
                if rec["exit_code"] != 0]
    for rec in failures:
        detail = rec.get("detail") or f"exited {rec['exit_code']}"
        warnings.append(f"{rec['label']}: {detail}")

    py_stats, rs_stats = _side_stats(timed, "python"), _side_stats(timed, "ported")
    py_walls = [r["wall_s"] for r in timed["python"] if r["wall_s"] is not None]
    rs_walls = [r["wall_s"] for r in timed["ported"] if r["wall_s"] is not None]

    # ---- the claim: timing only with identity, only with both sides green
    ratio = improvement = None
    claimable = (not failures and py_walls and rs_walls
                 and identity["stdout"]["verdict"] != "divergent"
                 and statistics.median(py_walls) > 0)
    if claimable:
        ratio = bootstrap_ratio(py_walls, rs_walls)
        improvement = round((1 - statistics.median(rs_walls)
                             / statistics.median(py_walls)) * 100, 3)

    # ---- repro script (written even on findings — the run is reproducible)
    commit = "unknown"
    probe = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(cwd),
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=30)
    if probe.returncode == 0:
        commit = probe.stdout.decode().strip() or "unknown"
    repro_path = out_dir / "bench-ab.repro.sh"
    repro_path.write_text(REPRO_TEMPLATE.format(
        state_path=state_path, state_sha=sha256_file(state_path)[:16],
        commit=commit, cwd=cwd,
        masks=", ".join(args.mask) or "(none)",
        fallback_units=", ".join(fallback_units),
        fallback_value=python_value, env_var=env_var,
        command=shlex.join(args.command)), encoding="utf-8")
    repro_path.chmod(0o755)
    artifact_path = out_dir / "bench-ab.json"

    # ---- optional gate write: measurement -> state file evidence
    gate_block = None
    if args.gate:
        if not claimable:
            warnings.append(f"--gate {args.gate}: evidence NOT written — no "
                            "honest claim to record (divergent outputs or a "
                            "failed run)")
        else:
            evidence = _gate_evidence(args.gate, improvement, ratio,
                                      identity["stdout"]["verdict"],
                                      artifact_path, args.command)
            try:
                gate, created, n = _write_gate_evidence(state_path, state,
                                                        args.gate, evidence)
            except ValueError as e:
                raise ToolError(f"gate write rolled back — resulting state "
                                f"would be invalid: {e}", kind="usage") from e
            gate_block = {"gate": args.gate, "created": created,
                          "evidence": gate.get("evidence", []),
                          "journal_entries": n}

    code = EXIT_OK if claimable else EXIT_FINDINGS
    human = (f"bench_ab [{env_var}]: stdout {identity['stdout']['verdict']}"
             + (f", speedup {ratio['median_speedup']}x "
                f"[{ratio['ci05']}, {ratio['ci95']}], "
                f"end-to-end saving {improvement:g}%"
                if claimable else
                "; NO ratio claim (see identity/failures)"))
    if gate_block:
        human += (f"; gate {args.gate} evidence written "
                  f"({gate_block['journal_entries']} journal entries)")
    payload = {
        "tool": TOOL, "version": VERSION,
        "state": {"path": str(state_path), "sha256": sha256_file(state_path),
                  "stage": state.get("stage"),
                  "run_status": state.get("run_status", "pending")},
        "env_var": env_var,
        "sides": {"python": {"env": {env_var: python_value},
                             "fallback_units": fallback_units},
                  "ported": {"env": {env_var: ported_value}}},
        "units": {"rust_live": rust_live,
                  "fallback_list": fallback_units,
                  "retired": [u for u, s in rust_live.items()
                              if s == "retired"],
                  "shim_hint": [f"state.py shim {state_path} --unit {u} "
                                f"--write <target>/shims/{u}.py"
                                for u in fallback_units]},
        "command": args.command, "cwd": str(cwd),
        "corpus": corpus,
        "runs": runs,
        "timings": {"python": py_stats, "ported": rs_stats},
        "identity": identity,
        "first_divergence": (_first_divergence(timed, "stdout")
                             if identity["stdout"]["verdict"] == "divergent"
                             else []),
        "ratio": ratio, "improvement_pct": improvement,
        "repro_script": str(repro_path), "artifact": str(artifact_path),
        "gate": gate_block,
        "warnings": warnings,
        "env": {"python": python_version_string()},
        "summary": {"exit_code": code, "human": human},
    }
    emit(payload, human_flag=args.human)

    # the artifact on disk is exactly what was emitted (post-emit it carries
    # generated_at) — the durable half of "no number without a repro"
    write_json_atomic(artifact_path, payload)
    return code


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    head, command = split_trailing_command(argv)
    ap = argparse.ArgumentParser(
        prog=TOOL, description=__doc__.splitlines()[0],
        epilog="The command after '--' runs verbatim in --cwd (default: the "
               "recorded preflight.target.root) — e.g. "
               "`-- python -m pytest bench/` or `-- python bench/run.py`.")
    ap.add_argument("--human", action="store_true",
                    help="echo human summary to stderr")
    ap.add_argument("--state", required=True, help="migration-state.json")
    ap.add_argument("--out", help="artifact directory "
                                  "(default: <state dir>/bench-ab)")
    ap.add_argument("--gate", choices=GATES_WITH_MEASUREMENT,
                    help="also write the measurement into the state file as "
                         "gate evidence (journaled, via state.py's writer)")
    ap.add_argument("--env-prefix", help="override the shim env-var prefix "
                                         "(same flag as `state.py shim`)")
    ap.add_argument("--cwd", help="working directory for the command "
                                  "(default: preflight.target.root)")
    ap.add_argument("--repeat", type=int, default=1,
                    help="timed runs per side, interleaved (default 1)")
    ap.add_argument("--warmup", type=int, default=0,
                    help="untimed warmup runs per side (default 0)")
    ap.add_argument("--timeout", type=float, default=RUN_TIMEOUT,
                    help="per-run timeout in seconds (default 1800)")
    ap.add_argument("--corpus", help="corpus file the command exercises — "
                                     "hashed + git-pinned like bench.py")
    ap.add_argument("--corpus-desc",
                    help="free-text corpus description for the artifact")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="permit an uncommitted --corpus (recorded as a "
                         "warning)")
    ap.add_argument("--mask", action="append", default=[], metavar="REGEX",
                    help="mask known-volatile output fields (timings, "
                         "timestamps) before the identity hash — repeatable; "
                         "masked equality is recorded as masked_identical, "
                         "never as identical")
    args = ap.parse_args(head)
    args.command = command
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(run_cli(main, tool=TOOL, version=VERSION))
