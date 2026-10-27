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
| 2 | `port_plan.py` — profiling→plan generator | 0,1 | profiler JSON + tree-sitter/mypy AST → ranked, dependency-ordered, portability-scored backlog | I/O-bound units ranked last; cycles detected; risk flags attached |
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
- **Exit codes**: `0` clean · `1` usage/IO · `2` findings (divergence,
  attribution loss, lint errors, cycles, human decision needed) · `3`
  incomparable-without-divergence · `4` setup failure (e.g. no pytest).
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

# #4 the only writer of the state file
python framework/tools/state.py init state/migration-state.json --name X --python-path src/x
python framework/tools/state.py set  state.json --field 'units[u1].status=ported'
python framework/tools/state.py mark state.json --unit u1 --status escalated --note "..."
python framework/tools/state.py shim state.json --unit u1 --write shims/u1.py
python framework/tools/state.py validate|journal|show|migrate state.json

# #2 / #3 feed Stage 1 → Stage 2
python framework/tools/port_plan.py from-cprofile --profile run.prof
python framework/tools/port_plan.py plan --hotspots hotspots.json --project-root .
python framework/tools/contract_extract.py extract --state state.json --out contracts/

# #5 / #6 / #7 / #8
python framework/tools/bench.py run --manifest bench.json         # parity gate first
python framework/tools/pyo3_review.py review --dir crates/        # or --diff pr.diff
python framework/tools/attribution.py check --original ../orig --port . --state state.json
python framework/tools/upstream_watch.py watch --state state.json --repo ../upstream --base v1.2
```

Design decisions worth knowing before you extend:

- **oracle.py** runs both sides as Python callables (`module:function`,
  default fn `run`) — the Rust side ships as a PyO3 wheel with the API
  frozen, so the wheel *is* the entry point. Inputs stream over stdin as
  JSONL `{"i": n, "input": v}`; outputs must be JSON-serializable or the
  run is honestly *Incomparable* (sets need a canonical adapter). The
  normalization rules (float tolerances, int/float distinctness, dict-order
  insensitivity, NFC, error type/message policy, `strip_fields`) are
  documented in `oracle.py rules` output — Stage 2 requires them written
  down, and `int_float_distinct=true` is deliberate: silent coercion is a
  divergence, not a normalization. `self-test` injects the 5 canonical bug
  classes through the *production* judging path (no special-cased
  verdicts) and also proves benign differences are forgiven.
- **state.py** journals every transition into the state file's own
  `journal[]` (append-only; `set`/`mark` refuse to touch it), writes
  atomically (tmp + rename), and *rolls back* any change that would leave
  the file schema-invalid or coherence-broken. `mark` encodes the
  MIGRATION_PROMPT.md discipline as hard guards: `ported` requires an
  equivalent verdict, `escalated` auto-opens the `unit_escalation` human
  gate, `divergent_explained` requires the written explanation. The
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
  claim on divergence; refuses a dirty corpus; ships a self-contained
  repro script pinning the corpus commit; timing stats use a seeded
  bootstrap CI.
- **upstream_watch.py** never silently skips: changed files map to units
  (exact path or package-unit prefix), unmapped files are listed with
  reasons, dependents join the re-run plan transitively, and
  escalated/divergent_explained units surface the Stage 7 human decision.
