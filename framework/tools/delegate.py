#!/usr/bin/env python3
"""delegate.py — the stage-4 candidate runner (issue #33).

Mechanizes the repair loop's per-unit translator delegation
(``MIGRATION_PROMPT.md`` "The repair loop", ``stages/4-translate.md``) as
ONE command — K candidates, strict file partitioning, compile-then-oracle
ranking, bounded fix rounds — so the bookkeeping is code instead of prose
the orchestrator hand-rolls (the failure mode issue #33 reports: K=1 under
budget, partitioning by instruction, manual gates, manual judge invocation,
and a delegate hitting its turn cap mid-unit, which is exactly when the
loop must be cheap to restart).

**Build isolation (issue #55):** strict *file* partitioning does not
partition *feedback* — cargo compiles every wired ``src/*.rs``, so a
stalled sibling's half-written module fails everyone else's gates and
burns their repair rounds on errors that are not theirs. Each
candidate's gates and the wheel build therefore run in a per-candidate
**sandbox copy** of the crate with every other candidate's module
stripped (file deleted, its ``mod`` line removed), under a per-unit
``CARGO_TARGET_DIR``, so compiler feedback cites only the candidate's
own file. The real crate stays the authoritative tree the delegates
write into; ``--no-isolate-builds`` restores shared-crate behavior for
custom gates that cannot tolerate a copy.

    python framework/tools/delegate.py run-candidates \
        --state runs/<project>/migration-state.json --unit u-foo \
        --agent-cmd 'goose session run {brief_file} {feedback_file}'

(K comes from autonomy/policy.json 'candidates' — issue #59; --k overrides
and is journaled as a deviation)

Per unit it:

  0. resolves K from the policy schedule (issue #59): K=3 below the size
     threshold, K=1 + bounded repair rounds (MAX_ROUNDS) above it or when
     run-level token budget remaining < the floor fraction — enforced here
     by default, journaled in the verdict pack as ``policy:CAND-1`` with
     its inputs, never left to the orchestrator's restraint (``--k`` is an
     operator override, recorded as a deviation from the policy decision);
  1. derives per-candidate target paths (``src/u_<unit>_c1.rs`` … ``_cK``,
     under the unit's ``rust_path`` directory when recorded) and writes each
     delegate an assignment brief: its own file + the contract + the Python
     source + ``roles/translator.md`` — nothing else (strict file
     partitioning, translator rule 1);
  2. runs each candidate through the bounded repair loop: a mechanical
     import gate (extern crates must be a Cargo.toml dependency, the crate
     itself, or an approved ``knowledge/substitutions.json`` entry — most
     "hallucinated crates" are mis-imported real modules), then the compile
     gates (``cargo fmt`` / ``clippy`` / ``test`` by default), with gate
     output fed back to the delegate **verbatim** — the strongest signal,
     per the loop spec — up to MAX_ROUNDS taken from the unit's budget
     (``--max-rounds`` overrides; round 0 is the initial translation,
     repairs are rounds 1..MAX_ROUNDS);
  3. builds the wheel once (``--build-cmd``, default ``maturin develop``)
     and judges ONLY the gate-green candidates — one ``oracle.py judge``
     run per candidate via a derived manifest that retargets the
     scaffolded candidate adapter (issue #32) at that candidate's wheel
     module — then prints the ranked table and the winner;
  4. writes NOTHING to the state file (the orchestrator/driver stays the
     only writer, per the role-separation rule): it emits a JSON verdict
     pack — on stdout and at
     ``<state_dir>/delegates/<unit>/verdict-pack.json`` — carrying the
     ranking, per-candidate evidence, ready-to-run ``state.py set``/``mark``
     commands, and per-unit ``sessions[]`` entries for the state file.

Runner-agnostic: ``--agent-cmd`` is a template string (goose / Claude
Code / Codex all fit — the same pattern ``pipeline.py run`` uses).
Placeholders: ``{unit} {k} {candidate} {module} {rust_file}
{rust_file_rel} {contract} {python_source} {role_file} {brief_file}
{feedback_file} {round} {max_rounds} {target_root} {framework} {state}
{python}``. Repair feedback travels through ``{feedback_file}`` (the
verbatim gate output), so the template MUST reference it whenever repair
rounds are possible — that contract is what keeps the loop
runner-agnostic. Restartable anywhere in the loop (issue #25): re-running
re-derives everything from disk, and ``--only c2,c3`` re-runs a subset of
candidates after a budget-exhausted hand-off.

Exit codes follow tools/README.md: 0 winner judged equivalent · 2 findings
(winner divergent, no gate-green candidate, unit budget exhausted, or a
refusal) · 3 every judged candidate incomparable · 4 the wheel build
failed (judging impossible) · 1 usage/IO via the shared envelope.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state as state_tool  # noqa: E402  — read-only here; never the writer
from _common import (  # noqa: E402
    EXIT_FINDINGS, EXIT_INCOMPARABLE, EXIT_OK, EXIT_SETUP, ToolError,
    emit, read_json, run_child, run_cli, write_json_atomic,
)
import pipeline as pipeline_tool  # noqa: E402  — K is policy, not judgment (issue #59)

TOOL = "delegate.py"
VERSION = 2
FRAMEWORK = Path(__file__).resolve().parent.parent
ROLE_FILE = FRAMEWORK / "roles" / "translator.md"
SUBSTITUTIONS = FRAMEWORK / "knowledge" / "substitutions.json"
ORACLE = Path(__file__).resolve().parent / "oracle.py"

MAX_ROUNDS_DEFAULT = 5              # units[].budget.max_rounds default (schema)
DEFAULT_GATES = ["cargo fmt --check",
                 "cargo clippy --all-targets -- -D warnings",
                 "cargo test"]
DEFAULT_BUILD_CMD = "maturin develop"
AGENT_TIMEOUT = 3600.0              # one delegate round: a full stage-4 worker run
GATE_TIMEOUT = 900.0
BUILD_TIMEOUT = 1800.0
JUDGE_TIMEOUT = 300.0
OUTPUT_CAP = 65536                  # feedback/record cap; 'truncated' flags honesty
TAIL = 4000

# what a sandbox crate copy never carries: heavy caches (slow to copy,
# stale artifacts would leak between candidates). "delegates" keeps a state
# dir living inside the crate from recursing into its own sandboxes.
SANDBOX_IGNORE = {"target", ".git", "__pycache__", ".venv", "venv",
                  "node_modules", "dist", "build", ".mypy_cache",
                  ".pytest_cache", ".tox", ".ruff_cache", "delegates"}
NOT_YOURS_RULE = ("compile errors in files that are not yours are not yours "
                  "to fix — report and wait (translator rule 9)")

VERDICT_RANK = {"equivalent": 3, "divergent": 2, "incomparable": 1, "unjudged": 0}
PLACEHOLDERS = ("unit", "k", "candidate", "module", "rust_file", "rust_file_rel",
                "contract", "python_source", "role_file", "brief_file",
                "feedback_file", "round", "max_rounds", "target_root",
                "framework", "state", "python")
# cargo/rustc diagnostics cite files as `--> path:line:col`; a candidate's
# OWN errors are the spans naming its file. Attribution is recorded for the
# pack, never acted on — the gates decide on exit codes alone.
DIAG_SPAN_RE = re.compile(r"^\s*-->\s*(\S+?):\d+:\d+\s*$", re.M)
USE_RE = re.compile(r"^\s*(?:pub(?:\(crate\))?\s+)?use\s+([a-z_][a-z0-9_]*)(?:::|;)",
                    re.M)
EXTERN_RE = re.compile(r"^\s*extern\s+crate\s+([a-z_][a-z0-9_]*)\s*;", re.M)
CARGO_DEP_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s*=", re.M)
CARGO_SECTION_RE = re.compile(r"^\[(.+)\]\s*$", re.M)
DEP_TABLE_RE = re.compile(r"^(?:dev-|build-)?dependencies\.(.+)$")


class _Refusal(Exception):
    """A loop-level refusal (e.g. budget exhausted): a result with evidence,
    not a crash — mirrors pipeline.py's refusal discipline (exit 2)."""


class _Template(dict):
    """format_map helper: unknown placeholders refuse with the vocabulary
    (a typo'd {rustfile} must not silently expand to nothing)."""

    def __missing__(self, key):
        raise ToolError(
            f"placeholder {{{key}}} is not defined; available: "
            f"{', '.join('{' + p + '}' for p in PLACEHOLDERS)}", kind="usage")


# --------------------------------------------------------------- inputs ----

def _load_state(path: Path) -> dict:
    try:
        state = read_json(path)
    except OSError as e:
        raise ToolError(f"state {path}: {e}", kind="io") from e
    except json.JSONDecodeError as e:
        raise ToolError(f"state {path}: invalid JSON: {e}", kind="usage") from e
    if not isinstance(state, dict):
        raise ToolError(f"state {path}: must contain a JSON object", kind="usage")
    return state


def _unit_of(state: dict, uid: str, state_path: Path) -> dict:
    # _find_unit is family-internal: composed here, not duplicated
    unit = state_tool._find_unit(state, uid)  # noqa: SLF001
    if unit is None:
        raise ToolError(f"unknown unit {uid!r} in {state_path}", kind="usage")
    return unit


def _resolve_target_root(flag_value: str | None, state: dict) -> Path:
    """--target-root wins; else the root a previous preflight recorded
    (preflight.target.root) — the same resolution pipeline.py uses, so a
    preflighted run needs no flag here either."""
    recorded = ((state.get("preflight") or {}).get("target") or {}).get("root")
    raw = flag_value or recorded
    if not raw:
        raise ToolError(
            "no target root: pass --target-root (or run pipeline.py preflight "
            "once, which journals it into the state file)", kind="usage")
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise ToolError(f"target root {raw} is not a directory", kind="io")
    return root


def _resolve_max_rounds(flag_value: int | None, budget: dict) -> int:
    """--max-rounds wins; else the unit budget's max_rounds (a null value
    counts as unset and falls back to the schema default)."""
    if flag_value is not None:
        return flag_value
    return int(budget.get("max_rounds", MAX_ROUNDS_DEFAULT) or MAX_ROUNDS_DEFAULT)


def _unit_loc(unit: dict, target_root: Path) -> int | None:
    """The unit's size in non-blank Python LOC — the input the K schedule's
    size threshold reads (issue #59). None when unmeasurable; the policy
    then applies its small-unit default and says so, never a guess."""
    rel = unit.get("python_path")
    if not rel:
        return None
    try:
        text = (target_root / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return sum(1 for line in text.splitlines() if line.strip())


def _resolve_k(args, state: dict, unit: dict, target_root: Path,
               only: list[str] | None) -> tuple[int, dict]:
    """K is policy, not judgment (issue #59). Default: the schedule in
    autonomy/policy.json 'candidates' (id CAND-1) — K=3 below the size
    threshold, K=1 + bounded repair rounds above it / under the run-budget
    floor — resolved here so the schedule is enforced by the runner, not
    trusted to each orchestrator's restraint. An explicit --k is an
    operator override journaled in the pack as a deviation from the policy
    decision. --only restarts (issue #25) may widen a policy K back to the
    prior run's candidate count; that widening is recorded, not silent."""
    policy = pipeline_tool.load_policy()
    decision = pipeline_tool.resolve_k(
        state, policy, unit_loc=_unit_loc(unit, target_root))
    k, source = decision["k"], "policy"
    if args.k is not None:
        source = "flag override"
        if args.k != k:
            decision["deviation"] = (
                f"--k {args.k} overrides {decision['citation']} "
                f"(policy said K={k}: {'; '.join(decision['basis'])}) — "
                f"operator override, journaled here")
        k = args.k
    elif only:
        need = max(int(c[1:]) for c in only
                   if c.startswith("c") and c[1:].isdigit())
        if need > k:
            decision["restart_widening"] = (
                f"--only restart covers c1..c{need} from the prior run's "
                f"schedule; policy K={k} widened to {need} — recorded, "
                f"not silent")
            k = need
    decision["k_used"] = k
    decision["source"] = source
    return k, decision


def _sanitize_stem(uid: str) -> str:
    """u-tokenize-1 -> u_tokenize_1 (candidate file src/u_<unit>_c<k>.rs)."""
    stem = re.sub(r"[^A-Za-z0-9_]", "_", uid).strip("_")
    if not stem:
        raise ToolError(f"unit id {uid!r} yields an empty file stem", kind="usage")
    return stem


def _default_src_dir(unit: dict) -> str:
    """Where candidate files live inside the crate: the unit's recorded
    rust_path directory when set (e.g. crates/x/src/units), else src/."""
    rp = unit.get("rust_path")
    if rp:
        parent = Path(rp).parent.as_posix()
        if parent:
            return parent
    return "src"


def _load_substitution_crates(path: Path | None) -> set[str]:
    """Approved crate names from knowledge/substitutions.json (SUB-1's
    table) — the import gate's substitution allowlist."""
    try:
        subs = read_json(path or SUBSTITUTIONS)
    except (OSError, json.JSONDecodeError):
        return set()
    crates: set[str] = set()
    entries = subs.get("substitutions", []) if isinstance(subs, dict) else []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        names = entry.get("rust")
        if isinstance(names, str):
            names = [names]
        if isinstance(names, list):
            crates.update(str(n).split("::")[0].strip() for n in names if n)
    return crates


def _cargo_dep_names(target_root: Path) -> tuple[set[str], str | None]:
    """Dependency names parsed out of Cargo.toml. Stdlib-only line scan:
    dependency tables are flat ``name = …`` keys, so extraction is
    mechanical (no TOML parser in the 3.10 stdlib). Returns (names, note)."""
    names: set[str] = set()
    try:
        text = (target_root / "Cargo.toml").read_text(encoding="utf-8")
    except OSError:
        return set(), ("no Cargo.toml at the target root — the import gate "
                       "runs on std + substitutions.json only")
    sections = list(CARGO_SECTION_RE.finditer(text))
    for i, sec in enumerate(sections):
        body_end = sections[i + 1].start() if i + 1 < len(sections) else len(text)
        table = sec.group(1)
        inline = DEP_TABLE_RE.match(table)      # [dependencies.foo] style
        if inline:
            names.add(inline.group(1).replace("-", "_"))
        elif table.endswith("dependencies"):    # [dependencies], [dev-…],
            body = text[sec.end():body_end]     # [target.'cfg(…)'.dependencies]
            names.update(m.group(1).replace("-", "_")
                         for m in CARGO_DEP_RE.finditer(body))
    m = re.search(r'^name\s*=\s*"([^"]+)"', text, re.M)
    if m:                                     # the crate's own name
        names.add(m.group(1).replace("-", "_"))
    return names, None


# ---------------------------------------------------------------- plan ----

def _candidate_plan(state_path: Path, unit: dict, target_root: Path, k: int,
                    only: list[str] | None) -> list[dict]:
    """The per-candidate assignment table: paths, module names, brief and
    feedback file locations — everything derived, nothing guessed."""
    uid = unit["id"]
    stem = _sanitize_stem(uid)
    src_dir = _default_src_dir(unit)
    python_source = (target_root / unit["python_path"]).resolve() \
        if unit.get("python_path") else None
    delegates_dir = state_path.parent / "delegates" / uid
    wanted = only or [f"c{i}" for i in range(1, k + 1)]
    plan = []
    for i in range(1, k + 1):
        cand = f"c{i}"
        rust_file = target_root / src_dir / f"{stem}_{cand}.rs"
        plan.append({
            "candidate": cand, "index": i, "run": cand in wanted,
            "rust_file": str(rust_file),
            "rust_file_rel": rust_file.relative_to(target_root).as_posix(),
            "module": f"{stem}_{cand}",
            "brief_file": str(delegates_dir / f"brief-{cand}.md"),
            "feedback_file": str(delegates_dir / f"feedback-{cand}.txt"),
            "python_source": str(python_source) if python_source else None,
        })
    unknown = [c for c in wanted if c not in {p["candidate"] for p in plan}]
    if unknown:
        raise ToolError(f"--only names unknown candidate(s) {unknown} for k={k}",
                        kind="usage")
    return plan


def _contract_path(args, state_path: Path) -> tuple[Path | None, str]:
    if args.contract:
        p = Path(args.contract)
        if not p.is_file():
            raise ToolError(f"contract {p}: not a file", kind="io")
        return p, "explicit --contract"
    for rel in (f"contracts/{args.unit}.contract.json",
                f"../contracts/{args.unit}.contract.json"):
        p = state_path.parent / rel
        if p.is_file():
            return p, f"default {rel}"
    return None, ("none found — the brief cites the state file's contract "
                  "block instead (contract_extract.py --out writes "
                  "contracts/<unit>.contract.json)")


def _write_brief(entry: dict, unit: dict, *, contract: Path | None,
                 contract_note: str, k: int, max_rounds: int,
                 gates: list[str], candidate_module: str | None,
                 target_root: Path, isolate: bool = True) -> None:
    """The delegate's whole world: its own file + the contract + the
    Python source + the role sheet (strict file partitioning)."""
    wheel_module = (candidate_module.replace("{candidate}", entry["candidate"])
                    if candidate_module else None)
    lines = [
        f"# Translator assignment — unit {unit['id']}, candidate "
        f"{entry['candidate']} (of {k})",
        "",
        f"Read first (verbatim, they bind you): `{ROLE_FILE}` — the "
        "translator role sheet. Rules 1-9 apply in full.",
        "",
        "## Inputs (read-only)",
        f"- role sheet: `{ROLE_FILE}`",
        f"- contract: `{contract}` ({contract_note})" if contract
        else f"- contract: {contract_note}",
        f"- Python source: `{entry['python_source']}`" if entry["python_source"]
        else "- Python source: not recorded on the unit (python_path missing)",
        f"- crate root: `{target_root}` (its AGENTS.md binds tooling)",
        "",
        "## Your partition — the ONLY files you may write",
        f"- `{entry['rust_file_rel']}` (absolute: `{entry['rust_file']}`)",
        "- your own module-registration line(s) in the crate (e.g. the single "
        "`mod` line wiring YOUR module in) — nothing else in shared files; "
        "never the state file, never another candidate's file",
        "",
        "## Deliverable",
        f"- Rust module `{entry['module']}` translating the unit inside the "
        "approved data model; the contract is the spec, not the Python style",
        f"- provenance header per translator rule 5 (`// Ported from "
        f"{unit.get('python_path')} (<license>)`)",
        (f"- expose the unit's frozen API as wheel module `{wheel_module}` so "
         "oracle.py can judge it through the scaffolded adapter"
         if wheel_module else
         "- wire the module per the crate's AGENTS.md (no wheel-module pin "
         "given; judging is deferred)"),
        "",
        "## Gates (run after you finish; failures come back verbatim)",
        "- mechanical import gate: every extern crate must be a Cargo.toml "
        "dependency, the crate itself, or an approved substitutions.json "
        "entry — most 'hallucinated crates' are mis-imported real Python "
        "modules (Stage 4 step 3)",
    ]
    lines += [f"- `{g}`" for g in gates]
    if isolate:
        lines += [
            "",
            "Gates run in an isolated sandbox copy of the crate carrying "
            "only YOUR module (every other candidate's is stripped), under "
            "the unit's own CARGO_TARGET_DIR — compile feedback reflects "
            "only your file, never a sibling's half-written one (issue #55).",
        ]
    lines += [
        "",
        "## Budget",
        f"- MAX_ROUNDS = {max_rounds} repair round(s) after this initial one; "
        "on stall return your best candidate plus the exact failing diff "
        "(translator rule 8 — never weaken a check to go green)",
        "",
        "## Reporting",
        "- report (do not record): what passed, what failed, the failing "
        "diff; the orchestrator — never you — writes migration-state.json",
    ]
    p = Path(entry["brief_file"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ------------------------------------------------ build isolation (#55) ----

def _mod_line_re(module: str) -> re.Pattern:
    """The `mod <module>;` declaration line that wires a candidate's file
    into the crate (any visibility, indented, trailing comment tolerated) —
    what a sandbox must remove for every OTHER candidate, or cargo demands
    a file we just deleted."""
    return re.compile(rf"^[ \t]*(?:pub(?:\([-\w]+\))?[ \t]+)?mod[ \t]+"
                      rf"{re.escape(module)}[ \t]*;", re.M)


def _strip_mod_lines(text: str, modules: set[str]) -> str:
    """Drop each `mod <m>;` line plus the attribute lines (`#[...]`)
    directly above it — a dangling attribute is itself a compile error."""
    patterns = [_mod_line_re(m) for m in modules]
    kept: list[str] = []
    attrs: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("#["):
            attrs.append(line)
        elif any(p.match(line) for p in patterns):
            attrs.clear()               # the attributes belonged to the mod
        else:
            kept.extend(attrs)
            attrs.clear()
            kept.append(line)
    kept.extend(attrs)
    return "".join(kept)


def _strip_candidate(sandbox: Path, entry: dict,
                     rust_files: list[Path]) -> bool:
    """Remove one non-kept candidate from a sandbox copy: delete its file
    and the `mod` line wiring it. Returns whether the sandbox carried
    either."""
    path = sandbox / entry["rust_file_rel"]
    found = path.exists()
    if found:
        path.unlink()
    pattern = _mod_line_re(entry["module"])
    wired = False
    for rs in rust_files:
        try:
            text = rs.read_text(encoding="utf-8")
        except OSError:
            continue
        if pattern.search(text):
            wired = True
            rs.write_text(_strip_mod_lines(text, {entry["module"]}),
                          encoding="utf-8")
    return found or wired


def _materialize_sandbox(target_root: Path, sandbox: Path, plan: list[dict],
                         keep: set[str], cargo_target: Path) -> dict:
    """Fresh source-only copy of the crate at `sandbox` with every candidate
    NOT in `keep` stripped. The real crate is never touched — it stays the
    authoritative tree the delegates write into; only the gate/build
    criterion runs against the sandbox, so compiler feedback cites only the
    kept candidate's own file. Returns the record for the verdict pack."""
    if sandbox.exists():
        shutil.rmtree(sandbox)
    sandbox.parent.mkdir(parents=True, exist_ok=True)

    def _ignore(directory: str, names: list[str]) -> set[str]:
        # never recurse into this run's own scratch (the sandbox tree and
        # the shared cargo target), even when the state dir lives in the crate
        avoid = (sandbox, sandbox.parent, cargo_target)
        ignored = set()
        for n in names:
            p = Path(directory) / n
            if n in SANDBOX_IGNORE or p in avoid or sandbox in p.parents:
                ignored.add(n)
        return ignored

    shutil.copytree(target_root, sandbox, ignore=_ignore,
                   symlinks=True, ignore_dangling_symlinks=True)
    rust_files = [rs for rs in sandbox.rglob("*.rs") if rs.is_file()]
    others = sorted((e for e in plan if e["candidate"] not in keep),
                    key=lambda e: e["candidate"])
    stripped = [e["candidate"] for e in others
                if _strip_candidate(sandbox, e, rust_files)]
    return {"root": str(sandbox), "kept": sorted(keep),
            "stripped": stripped}


def _cargo_env(cargo_target: Path) -> dict[str, str]:
    """One CARGO_TARGET_DIR per unit: shared across its sandboxes (deps
    build once) and never the crate's own target/ (no build-lock
    contention between candidates)."""
    return {"CARGO_TARGET_DIR": str(cargo_target)}


def _build_isolation(args, delegates_dir: Path, plan: list[dict]) -> dict | None:
    """Where the unit's sandbox copies and CARGO_TARGET_DIR live, or None
    when --no-isolate-builds."""
    if not args.isolate_builds:
        return None
    sandbox_root = (Path(args.sandbox_root) if args.sandbox_root
                    else delegates_dir / "sandboxes")
    cargo_target = (Path(args.cargo_target_dir) if args.cargo_target_dir
                    else delegates_dir / "cargo-target")
    cargo_target.mkdir(parents=True, exist_ok=True)
    return {"sandbox_root": sandbox_root, "cargo_target": cargo_target,
            "plan": plan}


def _isolation_summary(isolation: dict | None) -> dict:
    """The verdict pack's build_isolation block."""
    if isolation is None:
        return {"mode": "shared"}
    return {"mode": "sandbox",
            "sandbox_root": str(isolation["sandbox_root"]),
            "cargo_target_dir": str(isolation["cargo_target"]),
            "note": "per-candidate sandbox crates, sibling candidates "
                    "stripped, one CARGO_TARGET_DIR per unit (issue #55)"}


# --------------------------------------------------------------- gates ----

def _import_findings(rust_file: Path, allow: set[str]) -> list[str]:
    """The mechanical import gate (Stage 4 step 3): every crate the
    candidate pulls in must be reachable — a Cargo.toml dependency, the
    crate itself, or an approved substitution. Unknown crates are findings,
    not guesses: >50% of 'hallucinated crates' are real modules wrongly
    imported, and the fix is mechanical, not a rewrite."""
    try:
        text = rust_file.read_text(encoding="utf-8")
    except OSError:
        return [f"candidate file {rust_file} not written yet"]
    used = set(USE_RE.findall(text)) | set(EXTERN_RE.findall(text))
    unknown = sorted(c for c in used
                     if c not in allow and c not in ("crate", "self", "super"))
    return [f"extern crate `{c}` is not a Cargo.toml dependency, the crate "
            f"itself, or in knowledge/substitutions.json — likely a "
            f"mis-imported real module; check the Python import you meant "
            f"(Stage 4 step 3)" for c in unknown]


def _attributed_spans(output: str, rust_file_rel: str) -> dict:
    """Own-span count + every cited file, from cargo-style `-->` lines."""
    spans = DIAG_SPAN_RE.findall(output)
    return {"own": sum(1 for s in spans if s.endswith(rust_file_rel)),
            "cited": sorted(set(spans))}


def _run_gate(cmd: str, subs: dict, cwd: Path, timeout: float,
              env: dict[str, str] | None = None) -> dict:
    rendered = cmd.format_map(_Template(subs))
    argv = shlex.split(rendered)
    try:
        rc, out, err = run_child(argv, cwd=str(cwd), timeout=timeout,
                                 env_extra=env)
    except subprocess.TimeoutExpired:
        return {"cmd": rendered, "exit_code": 124,
                "output": f"timed out after {timeout:g}s", "truncated": False}
    except FileNotFoundError:
        return {"cmd": rendered, "exit_code": 127,
                "output": f"command not found: {argv[0]}", "truncated": False}
    output = (out or "") + (err or "")
    return {"cmd": rendered, "exit_code": rc,
            "output": output[:OUTPUT_CAP], "truncated": len(output) > OUTPUT_CAP}


def _invoke_agent(cmd_template: str, subs: dict, cwd: Path,
                  timeout: float, env: dict[str, str] | None = None
                  ) -> tuple[int, str]:
    cmd = cmd_template.format_map(_Template(subs))
    try:
        proc = subprocess.run(shlex.split(cmd), cwd=str(cwd),
                              capture_output=True, text=True, timeout=timeout,
                              env={**dict(os.environ), **(env or {})})
    except FileNotFoundError as e:
        return 127, f"agent command not found: {e}"
    except subprocess.TimeoutExpired:
        return 124, f"agent timed out after {timeout:g}s"
    tail = ((proc.stdout or "") + (proc.stderr or ""))[-TAIL:]
    return proc.returncode, tail


def _feedback_text(entry: dict, round_no: int, max_rounds: int,
                   agent: tuple[int, str] | None,
                   gates: list[dict] | None) -> str:
    head = [f"delegate.py repair feedback — {entry['rust_file_rel']}, "
            f"candidate {entry['candidate']}, round {round_no}/{max_rounds}",
            "Everything below the markers is gate/agent output VERBATIM "
            "(compiler errors are the strongest signal — MIGRATION_PROMPT.md "
            f"repair loop). Fix YOUR file only, then finish. "
            f"{NOT_YOURS_RULE}.",
            ""]
    if agent is not None:
        rc, tail = agent
        head += [f"=== agent invocation (exit {rc}) — output tail ===", tail, ""]
    if gates is not None:
        for g in gates:
            head += [f"=== {g['cmd']} (exit {g['exit_code']}) ===",
                     g["output"] + ("\n…[truncated]" if g.get("truncated") else ""),
                     ""]
    return "\n".join(head)


# ------------------------------------------------------ judge plumbing ----

def _split_entry(entry: str) -> tuple[str, str]:
    """`../adapters/x_cand.py:run` -> (path, fn); `module:fn` -> (module, fn)."""
    if ".py:" in entry:
        path, fn = entry.rsplit(":", 1)
        return path, fn or "run"
    if ":" in entry:
        mod, fn = entry.split(":", 1)
        return mod, fn or "run"
    return entry, "run"


def _derive_judge_files(base_manifest_path: Path, manifest: dict, entry: dict,
                        wheel_module: str) -> Path | None:
    """Per-candidate manifest + adapter: copy the scaffolded candidate
    adapter (issue #32) and retarget its CANDIDATE_MODULE at this
    candidate's wheel module — one mechanical line patch, nothing guessed.
    Both derived files land next to the manifest, so every relative path in
    the manifest (corpus, reference adapter) keeps resolving."""
    adapter_rel, fn = _split_entry(manifest.get("candidate", {}).get("entry", ""))
    if not adapter_rel.endswith(".py"):
        return None
    base_adapter = (base_manifest_path.parent / adapter_rel).resolve()
    try:
        text = base_adapter.read_text(encoding="utf-8")
    except OSError:
        return None
    new_line = (f"CANDIDATE_MODULE = {wheel_module!r}  # retargeted by "
                f"{TOOL} v{VERSION} (candidate {entry['candidate']})")
    # replacement via callable: new_line must bypass re's escape processing
    patched, n = re.subn(r"(?m)^CANDIDATE_MODULE\s*=\s*.*$",
                         lambda _m: new_line, text, count=1)
    if n != 1:
        return None
    derived_adapter = base_manifest_path.parent / \
        f"{Path(adapter_rel).stem}_{entry['candidate']}.py"
    derived_adapter.write_text(patched, encoding="utf-8")
    derived_manifest = (base_manifest_path.parent /
                        f"{base_manifest_path.stem}.{entry['candidate']}.json")
    derived = dict(manifest)
    derived["candidate"] = {"entry": f"{derived_adapter.name}:{fn}"}
    derived["$comment"] = (f"derived by {TOOL} (issue #33): candidate "
                           f"{entry['candidate']} — wheel module {wheel_module}; "
                           f"do not hand-edit, re-derive")
    write_json_atomic(derived_manifest, derived)
    return derived_manifest


def _judge_candidate(derived_manifest: Path, timeout: float) -> dict:
    """Run oracle.py judge over the derived manifest as a real subprocess —
    no verdict is ever produced in here: the oracle ranks, not the model
    and not this tool."""
    cmd = [sys.executable, str(ORACLE), "judge",
           "--manifest", str(derived_manifest), "--timeout", str(timeout)]
    try:
        rc, out, err = run_child(cmd, cwd=str(derived_manifest.parent),
                                 timeout=timeout * 2 + 60)
    except subprocess.TimeoutExpired:
        return {"judged": False, "verdict": "unjudged",
                "skip_reason": f"oracle judge timed out after {timeout:g}s"}
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        return {"judged": False, "verdict": "unjudged",
                "skip_reason": f"oracle stdout not JSON: {(err or out)[:400]}"}
    if not isinstance(payload, dict) or "verdict" not in payload:
        return {"judged": False, "verdict": "unjudged",
                "skip_reason": "oracle emitted no verdict "
                               f"(exit {rc}): {(err or out)[:400]}"}
    rec = {"judged": True, "oracle_exit": rc, "verdict": payload["verdict"],
           "counts": payload.get("counts", {}),
           "total": payload.get("total", 0),
           "diff_kind_histogram": payload.get("diff_kind_histogram", {}),
           "verdicts_digest": payload.get("verdicts_digest"),
           "ground_truth_warning": ((payload.get("ground_truth") or {})
                                    .get("warning"))}
    if rc == EXIT_SETUP:
        rec["setup_error"] = ((payload.get("summary") or {}).get("human")
                              or "oracle setup failure — check the manifest "
                                 "paths and the installed wheel")
    return rec


# ---------------------------------------------------------- the loop ----

def _gates_after_agent(rec: dict, entry: dict, gates: list[str], subs: dict,
                       import_allow: set[str], target_root: Path,
                       gate_timeout: float, isolation: dict | None = None
                       ) -> tuple[list[dict], dict | None]:
    """The import gate plus the compile gates, run only after a clean agent
    exit; import findings ride rec for the verdict pack. Under isolation
    the compile gates run in a fresh sandbox copy of the crate carrying
    only this candidate's module, so a stalled sibling's half-written file
    cannot fail this verify loop. Returns (gate records, sandbox record)."""
    findings = _import_findings(Path(entry["rust_file"]), import_allow)
    rec["import_findings"] = findings
    if findings:
        return ([{"cmd": "<mechanical import gate>",
                  "exit_code": EXIT_FINDINGS,
                  "output": "\n".join(findings),
                  "truncated": False}], None)
    if isolation is None:
        return [_run_gate(g, subs, target_root, gate_timeout)
                for g in gates], None
    sandbox = _materialize_sandbox(
        target_root, isolation["sandbox_root"] / entry["candidate"],
        isolation["plan"], {entry["candidate"]}, isolation["cargo_target"])
    root = Path(sandbox["root"])
    gate_subs = {**subs, "rust_file": str(root / entry["rust_file_rel"]),
                 "target_root": sandbox["root"]}
    return [_run_gate(g, gate_subs, root, gate_timeout,
                      env=_cargo_env(isolation["cargo_target"]))
            for g in gates], sandbox


def _run_candidate(entry: dict, args, gates: list[str], import_allow: set[str],
                   base_subs: dict, target_root: Path,
                   isolation: dict | None = None) -> dict:
    """Round 0 (initial translation) + up to MAX_ROUNDS repair rounds, each
    repairing against the previous round's verbatim gate output. Every
    invocation and gate result is recorded; nothing is retried silently.
    The agent works in the REAL crate (its writes are the deliverable);
    only the gates isolate."""
    rec: dict = {"candidate": entry["candidate"], "index": entry["index"],
                 "module": entry["module"],
                 "rust_file": entry["rust_file"],
                 "rust_file_rel": entry["rust_file_rel"], "rounds": [],
                 "gate_green": False, "rounds_used": 0,
                 "import_findings": [], "agent_invocations": 0}
    feedback_path = Path(entry["feedback_file"])
    feedback_path.parent.mkdir(parents=True, exist_ok=True)
    agent_env = _cargo_env(isolation["cargo_target"]) if isolation else None
    total = 1 + args.max_rounds
    for round_no in range(total):
        subs = {**base_subs, **entry, "round": round_no}
        if round_no == 0:
            # the file exists up front so the template contract holds even
            # for agents that read it unconditionally; round 0 has no signal
            feedback_path.write_text(
                _feedback_text(entry, round_no, args.max_rounds, None, None),
                encoding="utf-8")
        rc, tail = _invoke_agent(args.agent_cmd, subs, target_root,
                                 args.agent_timeout, env=agent_env)
        rec["agent_invocations"] += 1
        round_rec: dict = {"round": round_no, "agent_exit": rc,
                           "agent_tail": tail[-800:]}
        gate_recs: list[dict] = []
        sandbox = None
        if rc == 0:
            gate_recs, sandbox = _gates_after_agent(
                rec, entry, gates, subs, import_allow, target_root,
                args.gate_timeout, isolation)
        # evidence stays exact: a round blocked by the import gate ran in no
        # sandbox, so the pack must not cite an earlier round's
        if sandbox is not None:
            round_rec["sandbox"] = rec["sandbox"] = sandbox
        else:
            rec.pop("sandbox", None)
        round_rec["gates"] = gate_recs
        green = rc == 0 and bool(gate_recs) and \
            all(g["exit_code"] == 0 for g in gate_recs)
        round_rec["gate_green"] = green
        spans = [_attributed_spans(g["output"], entry["rust_file_rel"])
                 for g in gate_recs]
        round_rec["own_error_spans"] = sum(s["own"] for s in spans)
        round_rec["cited_files"] = sorted({f for s in spans for f in s["cited"]})
        rec["rounds"].append(round_rec)
        rec["rounds_used"] = round_no
        if green:
            rec["gate_green"] = True
            break
        if round_no < total - 1:
            # the next round's repair signal: this round's gate output,
            # verbatim and never overwritten
            feedback_path.write_text(
                _feedback_text(entry, round_no, args.max_rounds, (rc, tail),
                               gate_recs or None), encoding="utf-8")
    if not rec["gate_green"]:
        last = rec["rounds"][-1]
        gate_output = "\n".join(g["output"] for g in last["gates"])
        rec["stall"] = {  # best candidate + the exact failing diff (rule 8)
            "agent_exit": last["agent_exit"],
            "gate_output": gate_output[:OUTPUT_CAP]}
    return rec


# ------------------------------------------------------------- ranking ----

def _rank(cands: list[dict]) -> list[dict]:
    def key(c):
        j = c.get("judge") or {}
        return (c["gate_green"],
                VERDICT_RANK.get(j.get("verdict", "unjudged"), 0),
                (j.get("counts") or {}).get("equivalent", 0),
                -(j.get("counts") or {}).get("divergent", 0),
                -c.get("rounds_used", 0),
                -c["index"])
    ranked = sorted(cands, key=key, reverse=True)
    for pos, c in enumerate(ranked, start=1):
        c["rank"] = pos
    return ranked


def _table(unit_id: str, ranked: list[dict], winner: dict | None,
           k_policy: dict) -> str:
    head = (f"K={k_policy['k_used']} per {k_policy['citation']}: "
            + "; ".join(k_policy["basis"]))
    if k_policy.get("rounds_as_candidates"):
        head += ("; repair rounds count as candidates — the oracle ranks "
                 "the round outputs, not the model")
    if k_policy.get("deviation"):
        head += f" | DEVIATION: {k_policy['deviation']}"
    if k_policy.get("restart_widening"):
        head += f" | restart: {k_policy['restart_widening']}"
    lines = [head,
             f"{unit_id}: {len(ranked)} candidate(s) — compile gates, then "
             f"oracle ranking (the oracle ranks, not the model)"]
    for c in ranked:
        j = c.get("judge") or {}
        if not c["gate_green"]:
            verdict = "unjudged (gates never green)"
        elif not j.get("judged"):
            verdict = j.get("skip_reason", "not judged")
        else:
            counts = j.get("counts") or {}
            verdict = (f"{j.get('verdict', '?'):13s}"
                       f"{counts.get('equivalent', 0)}/{j.get('total', 0)} eq, "
                       f"{counts.get('divergent', 0)} div, "
                       f"{counts.get('incomparable', 0)} inc")
            if j.get("diff_kind_histogram"):
                verdict += f"  diffs: {j['diff_kind_histogram']}"
        status = "GREEN" if c["gate_green"] else "RED  "
        mark = "  <== WINNER" if winner is c else ""
        lines.append(f"  {c['candidate']}  {status}  rounds={c['rounds_used']}  "
                     f"{verdict}{mark}")
    if winner is not None:
        lines.append(f"winner: {winner['candidate']} — record it via state.py "
                     f"(delegate.py never writes the state file)")
    else:
        lines.append("no winner: no gate-green candidate — escalate the unit "
                     "(state.py mark --status escalated)")
    return "\n".join(lines)


def _state_commands(unit_id: str, winner: dict | None,
                    state_path: Path, k_policy: dict) -> list[str]:
    """The exact commands the orchestrator runs to record the pack (this
    tool stays read-only on the state file — role separation)."""

    def set_cmd(field: str) -> str:
        return (f"python framework/tools/state.py set {state_path} "
                f"--field {shlex.quote(field)}")

    def mark_cmd(status: str, note: str) -> str:
        return (f"python framework/tools/state.py mark {state_path} "
                f"--unit {unit_id} --status {status} "
                f"--note {shlex.quote(note)}")

    # issue #59: the K decision rides the journal as a policy id, never as
    # prose — a "budget-driven deviation" note is what the next run would
    # re-improvise
    k_note = (f" [K={k_policy['k_used']} per {k_policy['citation']}: "
              f"{'; '.join(k_policy['basis'])}]")
    if k_policy.get("deviation"):
        k_note += f" [{k_policy['deviation']}]"

    cmds = [set_cmd(f"units[{unit_id}].k={k_policy['k_used']}")]
    if winner is None:
        cmds.append(mark_cmd("escalated",
                             "delegate.py: no gate-green candidate after "
                             f"bounded rounds{k_note}"))
        return cmds
    j = winner.get("judge") or {}
    counts = j.get("counts") or {}
    if counts:
        parity = {"runs": j.get("total", 0),
                  "equivalent": counts.get("equivalent", 0),
                  "divergent": counts.get("divergent", 0),
                  "incomparable": counts.get("incomparable", 0),
                  "last_verdict": j.get("verdict", "unjudged")}
        cmds.append(set_cmd("units[%s].parity=%s"
                            % (unit_id, json.dumps(parity, separators=(",", ":")))))
    cmds.append(set_cmd("units[%s].rust_path=%s"
                        % (unit_id, json.dumps(winner["rust_file_rel"]))))
    note = (f"delegate.py: winner {winner['candidate']} "
            f"({j.get('verdict', 'unjudged')}, "
            f"{counts.get('equivalent', 0)}/{j.get('total', 0)} equivalent, "
            f"{winner['rounds_used']} repair round(s)){k_note}")
    if j.get("verdict") == "equivalent":
        cmds.append(mark_cmd("ported", note))
    else:
        cmds.append(mark_cmd("escalated",
                             note + " — rounds exhausted / divergence unexplained"))
    return cmds


# ------------------------------------------------------------- command ----

def _validate_usage(args) -> None:
    if not args.dry_run and not args.agent_cmd:
        raise ToolError("--agent-cmd is required (goose / Claude Code / Codex — "
                        "any CLI agent; placeholders below) unless --dry-run",
                        kind="usage")
    if args.agent_cmd and args.max_rounds > 0 \
            and "{feedback_file}" not in args.agent_cmd:
        raise ToolError(
            "--agent-cmd must reference {feedback_file}: repair rounds feed "
            "gate/compiler output back VERBATIM through that file (the "
            "strongest signal, MIGRATION_PROMPT.md repair loop) — that "
            "placeholder is the runner-agnostic feedback contract",
            kind="usage")
    if args.candidate_module and "{candidate}" not in args.candidate_module:
        raise ToolError("--candidate-module must contain {candidate} so each "
                        "candidate judges its own wheel module "
                        "(e.g. 'mycrate.units.u_foo_{candidate}')", kind="usage")


def _resolve_manifest(args, state_path: Path) -> Path | None:
    if args.manifest:
        p = Path(args.manifest)
        return p if p.is_file() else None
    p = state_path.parent / "manifests" / f"{args.unit}.json"
    return p if p.is_file() else None


def _emit_dry_run(args, plan: list[dict], gates: list[str],
                  contract: Path | None, contract_note: str,
                  target_root: Path, import_allow: set[str],
                  cargo_note: str | None, k_policy: dict) -> int:
    human = (f"dry-run: {args.unit} k={args.k} ({k_policy['citation']}) "
             f"max_rounds={args.max_rounds} "
             f"gates={len(gates) + 1} target={target_root} — no agent, "
             f"no writes")
    emit({
        "tool": TOOL, "version": VERSION, "unit": args.unit,
        "dry_run": True, "k": args.k, "k_policy": k_policy,
        "max_rounds": args.max_rounds,
        "gates": ["<mechanical import gate>", *gates],
        "build_cmd": None if args.no_build else args.build_cmd,
        "candidate_module": args.candidate_module,
        "target_root": str(target_root),
        "isolate_builds": args.isolate_builds,
        "contract": str(contract) if contract else None,
        "contract_note": contract_note,
        "candidates": plan, "import_allowlist_size": len(import_allow),
        "cargo_note": cargo_note,
        "contract_resolved": contract is not None,
        "summary": {"exit_code": EXIT_OK, "human": human},
    }, human_flag=args.human)
    return EXIT_OK


def _build_wheel(args, target_root: Path, plan: list[dict],
                 green: list[dict], isolation: dict | None = None) -> dict:
    """One wheel build over the candidate set; only gate-green candidates
    reach it (they are what the wheel carries forward to judging). Under
    isolation the build runs in a sandbox copy carrying only this run's
    gate-green candidates, so a stalled candidate's half-written module
    cannot fail the winners' wheel."""
    build: dict = {"attempted": True, "cmd": args.build_cmd}
    cwd, env = target_root, None
    if isolation is not None:
        keep = {r["candidate"] for r in green}
        sandbox = _materialize_sandbox(
            target_root, isolation["sandbox_root"] / "build", plan, keep,
            isolation["cargo_target"])
        build["sandbox"] = sandbox
        build["cargo_target_dir"] = str(isolation["cargo_target"])
        cwd, env = Path(sandbox["root"]), _cargo_env(isolation["cargo_target"])
    try:
        rc, out, err = run_child(shlex.split(args.build_cmd),
                                 cwd=str(cwd),
                                 timeout=args.build_timeout, env_extra=env)
    except subprocess.TimeoutExpired:
        rc, out, err = 124, "", f"build timed out after {args.build_timeout:g}s"
    except FileNotFoundError as e:
        rc, out, err = 127, "", f"build command not found: {e}"
    build["exit_code"] = rc
    build["output_tail"] = ((out or "") + (err or ""))[-TAIL:]
    build["failed"] = rc != 0
    return build


def _skip_judging(results: list[dict], reason: str) -> None:
    for r in results:
        r["judge"] = {"judged": False, "verdict": "unjudged",
                      "skip_reason": reason}


def _judge_green_candidates(plan: list[dict], green: list[dict], args,
                            state_path: Path) -> bool:
    """One real oracle.py judge run per gate-green candidate (via a derived
    manifest — see _derive_judge_files). Returns whether the last judged
    candidate was actually judged."""
    base_manifest = _resolve_manifest(args, state_path)
    if base_manifest is None:
        _skip_judging(green, "no judge manifest found — pass --manifest "
                             "(scaffold.py writes manifests/<unit>.json)")
        return False
    if not args.candidate_module:
        _skip_judging(green, "no --candidate-module: the wheel module per "
                             "candidate is unknown (e.g. "
                             "mycrate.units.u_foo_{candidate})")
        return False
    manifest = read_json(base_manifest)
    judged_any = False
    for r in green:
        entry = next(p for p in plan if p["candidate"] == r["candidate"])
        wheel_module = args.candidate_module.replace(
            "{candidate}", r["candidate"])
        derived = _derive_judge_files(base_manifest, manifest, entry,
                                      wheel_module)
        if derived is None:
            _skip_judging([r], "candidate entry is not a scaffolded *.py "
                               "adapter with a CANDIDATE_MODULE line — "
                               "cannot retarget per candidate")
            continue
        r["judge"] = _judge_candidate(derived, args.judge_timeout)
        r["judge"]["manifest"] = str(derived)
        r["judge"]["wheel_module"] = wheel_module
        judged_any = bool(r["judge"].get("judged"))
    return judged_any


def _session_entries(unit_id: str, results: list[dict]) -> list[dict]:
    sessions = []
    for r in results:
        outcome = f"gate_green={r['gate_green']} rounds={r['rounds_used']}"
        if (r.get("judge") or {}).get("judged"):
            outcome += f" verdict={r['judge'].get('verdict')}"
        sessions.append({
            "id": f"local-{unit_id}-{r['candidate']}",
            "name": f"unit-{unit_id}-translator-{r['candidate']}",
            "role": "translator", "outcome": outcome})
    return sessions


def _exit_code(winner: dict | None, build: dict, judged_any: bool) -> int:
    if winner is None:
        return EXIT_FINDINGS
    if build.get("failed"):
        return EXIT_SETUP
    verdict = (winner.get("judge") or {}).get("verdict")
    fallback = EXIT_INCOMPARABLE if judged_any else EXIT_FINDINGS
    return {"equivalent": EXIT_OK, "divergent": EXIT_FINDINGS,
            "incomparable": EXIT_INCOMPARABLE}.get(verdict, fallback)


def cmd_run_candidates(args) -> int:
    state_path = Path(args.state).resolve()
    state = _load_state(state_path)
    unit = _unit_of(state, args.unit, state_path)
    budget = unit.get("budget") or {}
    if budget.get("exhausted"):
        raise _Refusal(f"unit {args.unit}: budget.exhausted is true — raise "
                       f"the ceiling or escalate; the repair loop refuses to "
                       f"run over budget (MIGRATION_PROMPT.md rule 6)")
    target_root = _resolve_target_root(args.target_root, state)
    args.max_rounds = _resolve_max_rounds(args.max_rounds, budget)
    only = [c.strip() for c in args.only.split(",")] if args.only else None
    args.k, k_policy = _resolve_k(args, state, unit, target_root, only)
    plan = _candidate_plan(state_path, unit, target_root, args.k, only)
    contract, contract_note = _contract_path(args, state_path)
    gates = args.gate or DEFAULT_GATES
    _validate_usage(args)
    dep_names, cargo_note = _cargo_dep_names(target_root)
    import_allow = ({"std", "core", "alloc"} | dep_names
                    | _load_substitution_crates(args.substitutions))

    if args.dry_run:
        return _emit_dry_run(args, plan, gates, contract, contract_note,
                             target_root, import_allow, cargo_note,
                             k_policy=k_policy)

    delegates_dir = state_path.parent / "delegates" / args.unit
    isolation = _build_isolation(args, delegates_dir, plan)

    base_subs = {"unit": args.unit, "k": args.k,
                 "max_rounds": args.max_rounds,
                 "contract": str(contract) if contract else "",
                 "role_file": str(ROLE_FILE), "target_root": str(target_root),
                 "framework": str(FRAMEWORK), "state": str(state_path),
                 "python": sys.executable}
    for entry in plan:
        if entry["run"]:
            _write_brief(entry, unit, contract=contract,
                         contract_note=contract_note, k=args.k,
                         max_rounds=args.max_rounds, gates=gates,
                         candidate_module=args.candidate_module,
                         target_root=target_root,
                         isolate=args.isolate_builds)

    results = [_run_candidate(entry, args, gates, import_allow, base_subs,
                              target_root, isolation=isolation)
               for entry in plan if entry["run"]]

    green = [r for r in results if r["gate_green"]]
    build = (_build_wheel(args, target_root, plan, green, isolation=isolation)
             if green and not args.no_build else {"attempted": False})
    judged_any = False
    if green and not build.get("failed"):
        judged_any = _judge_green_candidates(plan, green, args, state_path)

    ranked = _rank(results)
    winner = ranked[0] if ranked and ranked[0]["gate_green"] else None
    table = _table(args.unit, ranked, winner, k_policy=k_policy)
    state_cmds = _state_commands(args.unit, winner, state_path,
                                 k_policy=k_policy)
    sessions = _session_entries(args.unit, results)
    code = _exit_code(winner, build, judged_any)

    pack = {
        "tool": TOOL, "version": VERSION, "unit": args.unit,
        "unit_status_before": unit.get("status"),
        "k": args.k, "k_policy": k_policy, "max_rounds": args.max_rounds,
        "gates": ["<mechanical import gate>", *gates],
        "build": build, "target_root": str(target_root),
        "build_isolation": _isolation_summary(isolation),
        "contract": str(contract) if contract else None,
        "plan": plan,
        "candidates": ranked,
        "winner": winner["candidate"] if winner else None,
        "winner_verdict": ((winner.get("judge") or {}).get("verdict")
                           if winner else None),
        "state_commands": state_cmds, "state_sessions": sessions,
        "state_written": False,
        "$comment": ("delegate.py never writes migration-state.json — the "
                     "orchestrator records this pack via state.py (role "
                     "separation; sessions[] entries are ready to append)"),
    }
    pack_out = Path(args.pack_out) if args.pack_out else \
        delegates_dir / "verdict-pack.json"
    write_json_atomic(pack_out, pack)

    human = table + f"\nverdict pack: {pack_out}"
    emit(pack | {"pack_path": str(pack_out),
                 "summary": {"exit_code": code, "human": human}},
         human=human, human_flag=args.human)
    return code


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=TOOL, description=__doc__.splitlines()[0])
    ap.add_argument("--human", action="store_true",
                    help="echo the human summary (ranked table) to stderr")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run-candidates",
                       help="K translators, partitioned files, compile+oracle "
                            "ranking in one command (issue #33)")
    p.add_argument("--state", required=True,
                   help="migration-state.json (READ-ONLY here: the "
                        "orchestrator records the verdict pack via state.py)")
    p.add_argument("--unit", required=True)
    p.add_argument("--k", type=int, default=None,
                   help="candidate count; DEFAULT: the K schedule in "
                        "autonomy/policy.json 'candidates' (issue #59, "
                        "policy:CAND-1) — K=3 below the size threshold, K=1 + "
                        "bounded repair rounds above it or when run-budget "
                        "remaining < the floor fraction. An explicit --k is "
                        "an operator override journaled in the verdict pack "
                        "as a deviation from the policy decision")
    p.add_argument("--agent-cmd",
                   help="CLI agent template invoked per candidate per round; "
                        "placeholders {unit} {k} {candidate} {rust_file} "
                        "{rust_file_rel} {module} {contract} {python_source} "
                        "{role_file} {brief_file} {feedback_file} {round} "
                        "{max_rounds} {target_root} {framework} {state} "
                        "{python}; {feedback_file} is REQUIRED when repair "
                        "rounds are possible")
    p.add_argument("--target-root",
                   help="the repo/crate being migrated; defaults to the root "
                        "a previous pipeline.py preflight recorded")
    p.add_argument("--contract",
                   help="unit contract.json (default: contracts/"
                        "<unit>.contract.json next to the state file)")
    p.add_argument("--manifest",
                   help="oracle judge manifest (default: manifests/<unit>.json "
                        "next to the state file — what scaffold.py writes)")
    p.add_argument("--candidate-module",
                   help="wheel module per candidate, with {candidate}: e.g. "
                        "'mycrate.units.u_foo_{candidate}' -> …_c1/…_c2/…_c3; "
                        "without it gates still run but nothing is judged")
    p.add_argument("--max-rounds", type=int,
                   help="repair rounds after the initial translation; default: "
                        "the unit budget's max_rounds (schema default 5)")
    p.add_argument("--gate", action="append", dest="gate",
                   help="replace the default compile gates (cargo fmt --check "
                        "/ clippy / test); repeatable, same placeholders as "
                        "--agent-cmd")
    p.add_argument("--build-cmd", default=DEFAULT_BUILD_CMD,
                   help=f"wheel build (default: {DEFAULT_BUILD_CMD}, cwd "
                        "--target-root)")
    p.add_argument("--no-build", action="store_true",
                   help="skip the wheel build (judging then needs an "
                        "already-installed wheel)")
    p.add_argument("--no-isolate-builds", dest="isolate_builds",
                   action="store_false", default=True,
                   help="run gates/build in the REAL crate instead of "
                        "per-candidate sandbox copies (issue #55: cargo "
                        "compiles every wired src/*.rs, so a sibling's "
                        "half-written module fails everyone's gates — "
                        "isolation is the default; disable only for custom "
                        "gates that cannot tolerate a crate copy)")
    p.add_argument("--sandbox-root",
                   help="where per-candidate sandbox crate copies live "
                        "(default: <state_dir>/delegates/<unit>/sandboxes)")
    p.add_argument("--cargo-target-dir",
                   help="the unit's CARGO_TARGET_DIR for sandboxed gates/"
                        "builds, shared across candidates so dependencies "
                        "compile once (default: <state_dir>/delegates/<unit>/"
                        "cargo-target)")
    p.add_argument("--only",
                   help="comma-separated candidate subset (c2,c3) — restart a "
                        "budget-exhausted unit mid-loop (issue #25)")
    p.add_argument("--substitutions",
                   help="override knowledge/substitutions.json (import gate)")
    p.add_argument("--pack-out",
                   help="verdict-pack path (default: <state_dir>/delegates/"
                        "<unit>/verdict-pack.json)")
    p.add_argument("--agent-timeout", type=float, default=AGENT_TIMEOUT)
    p.add_argument("--gate-timeout", type=float, default=GATE_TIMEOUT)
    p.add_argument("--build-timeout", type=float, default=BUILD_TIMEOUT)
    p.add_argument("--judge-timeout", type=float, default=JUDGE_TIMEOUT)
    p.add_argument("--dry-run", action="store_true",
                   help="print the derived plan (paths, briefs, gates) — no "
                        "agent, no writes")
    p.set_defaults(func=cmd_run_candidates)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except _Refusal as e:
        emit({"tool": TOOL, "version": VERSION,
              "unit": getattr(args, "unit", None), "error": str(e),
              "summary": {"exit_code": EXIT_FINDINGS, "human": str(e)}},
             human_flag=args.human)
        return EXIT_FINDINGS


if __name__ == "__main__":
    sys.exit(run_cli(main, tool=TOOL, version=VERSION))
