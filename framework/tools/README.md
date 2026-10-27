# tools/ — the deterministic helpers (the 8 verified gaps)

These are the components **no existing tool provides** (verified in
`../research/3-tools-and-interop-catalog.md`). They are scripts the agent
*calls*, not prompts: deterministic tooling for location and validation,
LLM only for edits (Google's rule). Build each to its acceptance criteria
**before** the stages that need it. Python + CLI is fine; keep them
stateless and JSON-in/JSON-out so the agent can compose them.

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
