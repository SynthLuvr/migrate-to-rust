# tools/ — the deterministic helpers (the 8 verified gaps)

These are the components **no existing tool provides** (verified in
`../research/3-tools-and-interop-catalog.md`). They are scripts the agent
*calls*, not prompts: deterministic tooling for location and validation,
LLM only for edits (Google's rule). Build each to its acceptance criteria
**before** the stages that need it. Python + CLI is fine; keep them
stateless and JSON-in/JSON-out so the agent can compose them.

**All 8 are implemented here** (stdlib-only, Python ≥ 3.10), with an
acceptance-test suite in `tests/` wired into CI. Shared plumbing lives in
`_common.py`; `oracle.py`'s comparator is imported (never duplicated) by
`bench.py`.

| # | Script | Stage | Input → Output | Acceptance criteria |
|---|---|---|---|---|
| 1 | `oracle.py` — equivalence oracle | 2,4,5,6 | unit + input corpus → per-run verdicts `equivalent/divergent/incomparable` + structured diff | catches 5/5 injected bugs; normalization rules documented; no LLM in the judging path |
| 2 | `port_plan.py` — profiling→plan generator | 0,1 | profiler hotspots JSON (bare list or `from-cprofile` envelope) + stdlib-ast/mypy scan → ranked, dependency-ordered, portability-scored backlog | I/O-bound units ranked last; cycles detected; risk flags attached; profiler-noise paths skipped with a warning |
| 3 | `contract_extract.py` | 2 | pytest collection + mypy JSON → per-unit `contract.json` (tests, types, invariants) | 100% of scoped units covered; dangling tests flagged |
| 4 | `state.py` — dual-build state manager | 1–7 | validates/migrates `migration-state.json`; emits per-unit fallback switch shim | every state transition journaled (append to the state file's `journal[]`, per `../state/migration-state.schema.json`); Python fallback callable per unit |
| 5 | `bench.py` — cross-language differential benchmark | 0,5 | matched corpus + both entry points → one verdict JSON + repro script + corpus commit | same inputs/coverage both sides; no number without repro |
| 6 | `pyo3_review.py` — FFI linter | 4,5 | Rust diff → findings on refcounts, swallowed `PyErr`, `unwrap()` in exports, GIL `detach` misuse, free-threading hazards | complements miri/clippy (they don't cover these classes); reviewer checklist encoded |
| 7 | `attribution.py` — attribution checker | 4,5 | original tree + port tree → header/NOTICE diff report, fail on loss | byte-exact NOTICE check; provenance comment required per generated file |
| 8 | `upstream_watch.py` — maintenance watcher | 7 | upstream diff → changed-units list → parity re-run plan → PR proposal | maps changes to state-file units; no silent skips |

## External helper — GLV subagent transport

GLV ([SynthLuvr/glv](https://github.com/SynthLuvr/glv)) is not one of the
eight scripts — it is the transport the framework's *role separation* runs
on when a GLV server is reachable. Spec, API cheat sheet, and delegation
loop: [`glv-subagents.md`](glv-subagents.md).

## The deterministic driver — `pipeline.py` (issue #22)

Not a ninth gap-tool: a **composition layer** over the eight (it writes the
state file only through `state.py`'s journaled writer and verifies stages
with `state.py` validation + `oracle.py self-test`). The LLM is demoted to
a *stage worker* invoked via any CLI agent; the loop itself is code —
testable, deterministic, resumable by construction:

```
loop:
  stage = state.stage; run the stage checklist via the agent CLI
  verify exit criteria  → state.py validation + state-expressible checks
  resolve open gates    → evaluate ../autonomy/policy.json rules in code
  journal decisions     → state.py (decided_by + policy_version + evidence)
  advance stage or terminal-state exit
```

- **Autonomy levels** (`--autonomy L0..L3`, pre-authorized before the run):
  L2 (the default for new runs) resolves every gate by policy, publish
  disabled; L3 adds cutover/publish. L0/L1 stay with the agent-driven
  recipe, which never asks either: the gate stays open, the run ends with
  the dossier, and the operator decides between runs (the driver refuses
  these levels). Zero mid-run questions at any level.
- **Terminal-state discipline**: a run always ends in `complete` /
  `no_go_measured` / `budget_exhausted` / `failed_safely` — never "awaiting
  input" — and every terminal state emits the same deliverable set (run
  report with the gate + escalation dossiers, state file, journal).
- **Budget guard**: global token/wall-clock ceilings; over either, the run
  stops safely and resumably in `budget_exhausted`.
- Agent retries per stage are bounded (`budget.max_agent_retries_per_stage`);
  exhaustion ends the run in `failed_safely` — never a silent skip.
- **Preflight** (issue #31): `pipeline.py preflight state.json
  --target-root <repo>` runs, in one command and one auditable artifact,
  everything stages 0/3 silently depended on — target suite green + wall
  time (the Stage 2 spec baseline), a `pytest --collect-only -q` capture
  written next to the state file (replayed by
  `contract_extract.py --pytest-collect`), the `oracle.py` self-test
  re-proven under the target interpreter, rust-template reachability +
  HEAD SHA (Stage 3's provenance, captured from the same run),
  cargo/rustc/maturin/uv versions + cargo-deny/llvm-cov/shear/pandoc
  presence (`just doctor` covers the crate; nothing checked the gate stack
  *before* scaffolding), the target venv interpreter + `Py_GIL_DISABLED`
  wheel flavor + the derived `PYO3_PYTHON`, substitutions.json freshness,
  and the framework-vs-target interpreter match (a 3.10 framework python
  SyntaxErrors on 3.12+ target source). Everything lands in a journaled
  `preflight` block (`state.py` stays the only writer) plus
  `<state>.preflight.md`; exit **2** on the hard prerequisites (suite not
  green, self-test not 5/5, template unreachable — or unprovable, e.g. no
  target root recorded), so a run starts only when Stage 0's exit criteria
  are actually checkable. `--skip <check>` downgrades a hard check to an
  explicit warning (the offline corner); the target root is recorded, so
  re-runs need no flag.

## Minimal viable subset
If you only build three things before starting: **#1 oracle**, **#4 state**,
**#7 attribution**. The oracle is the difference between a migration and the
ScanCode incident; the state file makes it resumable and reversible;
attribution keeps it publishable.

## Anti-requirements
- No LLM calls inside #1/#5/#7 — they must be reproducible and diffable.
- No state kept outside `migration-state.json` (except caches).
- Every tool prints machine-readable JSON + a human-readable summary flag.

## Implementation notes

Uniform conventions (so the agent can compose the tools blind):

- **stdout is always a single JSON object** (`--human` additionally echoes
  the embedded summary to stderr). Errors are JSON too — a refusal is a
  result, not a crash. `summary.exit_code` always mirrors the exit code.
  Failure paths raise the shared `ToolError` from `_common.py` and the
  CLI boundary (`run_cli`) emits the uniform envelope:
  `error.kind` ∈ `io` (filesystem/environment)
  · `usage` (bad or missing input content) · `setup` (missing dependency,
  exit 4), plus a one-line `error.detail`. `run_cli` also converts
  missing/unreadable/malformed input files (OSError/ValueError)
  into this envelope instead of a traceback, so stdout parses as JSON on
  every exit path; genuine programming errors still crash loudly.
- **Exit codes**: `0` clean · `1` usage/IO · `2` findings (divergence,
  attribution loss, lint errors, cycles, human decision needed) · `3`
  incomparable (no divergence, or a degenerate all-error reference corpus)
  · `4` setup failure (e.g. no pytest).
- **Determinism**: fixed seeds (`PYTHONHASHSEED=0` for children, seeded
  bootstrap in #5), sorted iteration, no wall-clock in verdict fields.
  `oracle.py` emits a `verdicts_digest` — identical inputs+rules must give
  identical digests, which is what makes oracle runs diffable in review.

Quick reference (see each script's docstring for the full input formats):

```console
# #1 judge a unit (manifest: reference entry, candidate entry, corpus, rules)
python framework/tools/oracle.py judge --manifest manifests/u-parse-1.json
python framework/tools/oracle.py compare --runs shadow-runs.json   # Stage 6
python framework/tools/oracle.py self-test   # Stage 2 gate: must catch 5/5
# per-row corpus dry-run before a judge run (issue #29)
python framework/tools/oracle.py corpus --validate corpus.jsonl --entry impl:run

# #4 the only writer of the state file
python framework/tools/state.py init state/migration-state.json --name X --python-path src/x --autonomy-level L2
python framework/tools/state.py set  state.json --field 'units[u1].status=ported'
python framework/tools/state.py mark state.json --unit u1 --status escalated --note "..."
python framework/tools/state.py resolve-gate state.json --gate go_no_go --decision approved --by policy:GO-1
python framework/tools/state.py shim state.json --unit u1 --write shims/u1.py
python framework/tools/state.py validate|journal|show|migrate state.json

# the deterministic driver (issue #22): L2+ unattended runs
# preflight first (issue #31): suite green + wall time, pytest capture,
# oracle self-test 5/5, rust-template HEAD SHA, toolchain, target
# venv/PYO3_PYTHON, substitutions freshness -> one journaled block + report
python framework/tools/pipeline.py preflight state.json --target-root /abs/target
python framework/tools/pipeline.py run state.json \
  --agent-cmd 'goose run --recipe {framework}/recipe.yaml' --autonomy L2
python framework/tools/pipeline.py resolve-gates state.json   # policy only, no agent
python framework/tools/pipeline.py status|report state.json

# #2 / #3 feed Stage 1 → Stage 2  (plan takes from-cprofile's envelope as-is)
python framework/tools/port_plan.py from-cprofile --profile run.prof > hotspots.json
python framework/tools/port_plan.py plan --hotspots hotspots.json --project-root .
python framework/tools/contract_extract.py extract --state state.json --out contracts/

# #5 / #6 / #7 / #8
python framework/tools/bench.py run --manifest bench.json         # parity gate first
python framework/tools/pyo3_review.py review --dir crates/        # or --diff pr.diff
python framework/tools/attribution.py check --original ../orig --port . --state state.json
python framework/tools/upstream_watch.py watch --state state.json --repo ../upstream --base v1.2
```

**Corpus file format (#1 `oracle.py`, #5 `bench.py`)** — JSONL, **one raw
JSON value per line = one input**, exactly as the entry function receives
it (verbatim from the acceptance test `test_judge_subprocess_end_to_end`):

    {"xs": [1, 2], "bad": false}
    {"xs": [], "bad": true}

`{"i": n, "input": v}` is *internal stdin framing* `oracle.py` adds itself
when streaming a corpus to a child process — it is **never** the corpus
file format. Pre-wrapped lines double-wrap, both sides raise the same
TypeError on every run, and the judge reports a hollow all-EQUIVALENT
verdict, so `_load_corpus` refuses a corpus in which every line carries
exactly the framing keys (issue #29), and even past that guard the judge's
ground-truth rule classifies an all-error reference corpus INCOMPARABLE,
never EQUIVALENT (issue #30). `oracle.py corpus --validate`
dry-runs one entry per-row (ok / error / unserializable) and flags the
hollow signature (`hollow_equivalent_risk`) plus thin ground truth before
you spend a judge run.

Design decisions worth knowing before you extend:

- **oracle.py** runs both sides as Python callables (`module:function`,
  default fn `run`) — the Rust side ships as a PyO3 wheel with the API
  frozen, so the wheel *is* the entry point. Outputs must be
  JSON-serializable or the run is honestly *Incomparable*
  (sets need a canonical adapter). The normalization rules (float
  tolerances, int/float distinctness, dict-order insensitivity, NFC, error
  type/message policy, `strip_fields`) are
  documented in `oracle.py rules` output — Stage 2 requires them written
  down, and `int_float_distinct=true` is deliberate: silent coercion is a
  divergence, not a normalization. `self-test` injects the 5 canonical bug
  classes through the *production* judging path (no special-cased
  verdicts) and also proves benign differences are forgiven.
  **Ground-truth guard (issue #30)**: a corpus on which the reference side
  has zero `ok:true` runs is degenerate — `compare_error` rightly calls
  error==error equivalent *per run*, but a run set with no successful
  reference output carries no parity evidence, so the summary verdict is
  INCOMPARABLE, never EQUIVALENT (error-path mismatches on such a corpus
  are corpus artifacts, not candidate defects). The
  `normalization.min_reference_ok_fraction` rule (default 0.5, in `rules`
  output) warns when fewer reference runs succeeded; 0 silences the
  warning, the zero-ok guard itself cannot be disabled. `bench.py`'s
  parity gate imports the same guard: a degenerate corpus aborts the
  benchmark with no timing claim.
- **state.py** journals every transition into the state file's own
  `journal[]` (append-only; `set`/`mark` refuse to touch it), writes
  atomically (tmp + rename), and *rolls back* any change that would leave
  the file schema-invalid or coherence-broken. `mark` encodes the
  MIGRATION_PROMPT.md discipline as hard guards: `ported` requires an
  equivalent verdict, `escalated` opens the `unit_escalation` gate — and at
  autonomy L2+ immediately resolves it as deferred via policy rule ESC-1
  (defer, not halt; the Python fallback stays live) — `divergent_explained`
  requires the written explanation. `resolve-gate` records gate decisions
  (`--by human` or `--by policy:<rule-id>`, rule ids checked against
  `autonomy/policy.json`) with `policy_version` + `evidence[]`; validation
  enforces the resolution discipline (a resolved gate always records who
  decided; a policy-approved cutover requires L3 + `publish_allowed`) and
  terminal-`run_status` coherence. The
  fallback shim is reversible per unit via one env var
  (`<PROJECT>_FALLBACK=all|<unit-ids>|none`) — no state anywhere else.
  Validation includes schema checking (an internal stdlib validator for
  the draft-2020-12 subset the schema uses; cross-checked against the
  `jsonschema` package by the tests) plus coherence invariants the schema
  can't express: dangling deps, cycles, order inversions, unexplained
  divergences, and out-of-band edits detected via journal coherence.
- **attribution.py** scopes the port to the state file's units: each unit
  file must carry `// Ported from <python_path> (<license>)` naming exactly
  the unit's python_path, the original's leading comment block must appear
  in the port file's header region, and NOTICE files must match
  byte-for-byte (sha256). Files claimed by no unit fail unless allowlisted
  with a reason — "independent rewrite" claims are surfaced, not trusted.
- **pyo3_review.py** file mode tracks `#[pyfunction]`/`#[pymethods]` spans
  with a comment/string/lifetime-aware lexer, so exported-function rules
  apply to real bodies; diff mode applies line rules to added lines with
  `context: diff` (re-run file mode before recording a verdict). Findings
  cite the reviewer-checklist item they encode.
- **bench.py** runs the parity gate *first* and aborts without any timing
  claim on divergence — or on a degenerate corpus whose reference side
  errors on every row (issue #30: no ground truth, no number); refuses a
  dirty corpus; ships a self-contained repro script pinning the corpus
  commit; timing stats use a seeded bootstrap CI.
- **upstream_watch.py** never silently skips: changed files map to units
  (exact path or package-unit prefix), unmapped files are listed with
  reasons, dependents join the re-run plan transitively, and
  escalated/divergent_explained units surface the Stage 7 human decision.
