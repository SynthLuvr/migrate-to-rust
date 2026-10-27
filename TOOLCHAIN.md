# Toolchain for an AI Python→Rust Migration Framework

*Organized by pipeline stage. Maintenance status verified 2026-10-25 via GitHub/crates.io/PyPI APIs (see [research/3](research/3-tools-and-interop-catalog.md) for per-tool verification details). ★ = the small core set to start with.*

---

## Stage 0 — Feasibility & hotspot selection (decide what to port)

| Tool | Role in the framework | Status |
|---|---|---|
| ★ **py-spy** | Sampling profiler on live processes; finds CPU hot paths without touching code | active |
| ★ **Scalene** | CPU + GPU + **memory** attribution per line — allocation density is a port-payoff signal | active |
| ★ **memray** | Allocation flamegraphs; identifies large long-lived object graphs (the GC-scan-cost predictor from the Discord/Figma evidence) | active |
| pyinstrument | Statistical profiler, low overhead | active |
| cProfile / line_profiler / pyperf | Deterministic baselines and microbenchmark hygiene | stdlib / active |
| **cargo-flamegraph** + **Criterion** | Same measurements on the Rust side; Criterion is the cross-language comparison substrate | active |

> **Gap to build:** profiling → ranked, dependency-aware, portability-scored port plan. No tool does this (verified gap).

## Stage 1 — Static analysis & decomposition (build the work queue)

| Tool | Role | Status |
|---|---|---|
| ★ **tree-sitter-python** | Fast, error-tolerant parsing → module/class/function dependency graph, unit segmentation | active |
| ★ **LibCST** | Lossless concrete-syntax tree — round-trippable rewrites, comment/annotation preservation (Meta-maintained) | active |
| ★ **mypy** / pyright | Type annotations are the closest thing Python has to contracts; drive Rust type selection | active |
| rope | Programmatic refactoring primitives | active |
| **Semgrep**, **CodeQL** | Pattern queries to find risky constructs (dyn dispatch, reflection, monkey-patching) before translation; CodeQL supports **both Python and Rust** | active |
| difftastic | Syntax-aware diffing for reviewing LLM output against source | active |

## Stage 2 — Contracts & the parity judge (the framework's heart)

| Tool | Role | Status |
|---|---|---|
| ★ **pytest** | Run the original suite against both implementations — the suite *is* the spec (ScanCode lesson) | active |
| ★ **Hypothesis** | Property-based tests; the core of differential Python↔Rust testing (state of practice per the literature) | very active |
| syrupy | Snapshot testing for golden-output corpora | active |
| **cargo-fuzz / libFuzzer** | Fuzz the Rust side at the FFI boundary | active |
| mutmut / cosmic-ray (Py), **cargo-mutants** (Rust) | Mutation testing to prove the judge actually catches wrong code | active |
| coverage.py + cargo-llvm-cov | Prove both sides are actually exercised by the parity corpus | active |

> **Gaps to build (verified — nothing exists):** (1) an automatic equivalence oracle that generates, runs, and *judges* differential properties for dynamic Python (CETBench shows LLM judges are unreliable — use hash/output comparison + property tests); (2) spec/contract extraction from tests + types.

## Stage 3 — Translation harness (LLM orchestration)

| Tool | Role | Status |
|---|---|---|
| ★ **OpenHands** or **Aider** or **goose** | The coding-agent substrate for the translate→compile→test→fix loop. SWE-agent (research-grade, 12.5% SWE-bench-Lite) and OpenHands (53% Verified) are the reference points | active |
| Claude Code / Codex | Frontier-model agents; used by Cloudflare tsrs (Claude Opus, 20+ parallel subagents) and Anthropic's Bun migration | commercial |
| Role-separated model stack | Volatility3 pattern: one model for mechanical implementation, a different one for planning/skeletons/parity review — cheaper *and* better | pattern |

> **Honesty note:** purpose-built OSS migration harnesses are **dead or productized** — IBM `watsonx-code-migration-poc` is 404, AWS Q "transform"/Kiro is proprietary, OpenRewrite's Python support is archived behind Moderne. The realistic base is a generic coder agent + custom gates. No production-grade Python→Rust transpiler exists (py2many handles a typed subset only; TransCoder/`github/semantic` are archived).
>
> **Gaps to build:** bounded repair loop with budget/stuck-detectors; K-candidate generation ranked by the oracle (not model confidence); PyO3-import fixing (>50% of "hallucinated crates" are mis-imported real modules — a cheap mechanical fixer removes most compile failures).

## Stage 4 — Rust quality gates

| Tool | Role | Status |
|---|---|---|
| ★ **rust-template** | First-party scaffold ([SynthLuvr/rust-template](https://github.com/SynthLuvr/rust-template)): fmt/clippy/docs/coverage/deny/unused-deps gates + MSRV + CI pre-wired — Stage 3 instantiates the crate from it; add only nextest/miri/maturin on top | first-party (created 2026-10-27) |
| ★ **clippy** + **rustfmt** | Non-negotiable hygiene gates in CI | active |
| ★ **miri** | Undefined-behavior detector — caught a bug that appeared *only in optimized builds* in the tsrs project | active |
| ★ **cargo-nextest** | Fast parallel test runner for the tight agent loop | active |
| insta | Snapshot tests | active |
| loom | Concurrency/atomic scheduling verification | active |
| cargo-deny / cargo-udeps / cargo-machete | Supply-chain + unused-dependency gates for LLM-added imports | active |
| cargo-mutants | Mutation testing on the Rust port | active |

## Stage 5 — Interop, packaging, cutover

| Tool | Role | Status |
|---|---|---|
| ★ **PyO3** | Rust↔Python bindings; the drop-in-module strategy (pydantic-core, orjson, polars pattern). Know the API generations: Bound API (0.21), gil-refs removed (0.23), `attach`/`detach` rename + thread-safe default (0.28+) | very active (v0.30) |
| ★ **maturin** | Build/publish wheels (`pip install` just works); abi3 wheels for version independence — note **free-threaded 3.14 needs `cp314t` wheels; `abi3t` arrives with 3.15** | very active |
| rustimport | Import `.rs` files directly from Python — accelerates per-unit dev loops | active |
| setuptools-rust | Alternative build integration for existing setuptools projects | active |
| cargo-dist | Release engineering for the standalone-Rust variant | active |
| **pyo3-async-runtimes** | The maintained asyncio↔tokio bridge (do **not** use the abandoned `pyo3-asyncio`) | active |

## Knowledge pack (context to embed for the LLM — from [research/5](research/5-idioms-and-ecosystem-mapping.md))

**Tier 1 — embed verbatim:** PyO3 user guide (error handling, `detach`/parallelism, free-threading, migration guide) · maturin wheel/abi decision tree · corrode.dev *Migrating from Python to Rust* · BurntSushi *Error Handling in Rust* · PEP 703/779 · polars *Coming from Pandas* · The Rust Book traits/OO chapters.

**Tier 2 — retrieve on demand:** TLBORM (metaclass/reflection→macros) · rust-unofficial patterns (builders, registries) · exemplar codebases: **pydantic-core, orjson, ruff, polars** (real PyO3 mappings beat any prose guide).

**Ecosystem substitution table** (24 crates + builtin `cargo test` + the `serde structs` pattern; maintenance dates per crate in
`framework/knowledge/substitutions.json`, full table in research/5 §(b)): requests/httpx→reqwest ·
numpy→ndarray/nalgebra · pandas→polars · pydantic→serde+garde/validator ·
sqlalchemy→sqlx/diesel/sea-orm · flask/fastapi→axum/actix-web · pytest→cargo-test+rstest ·
hypothesis→proptest · celery→apalis/rusty-sidekiq (⚠️ **no maintained Celery-wire-compatible
crate exists**) · logging→tracing · dataclasses/attrs→serde structs+derive_more · json→serde_json ·
asyncio→tokio+pyo3-async-runtimes · boto3→aws-sdk-\* · unittest.mock→mockall ·
monkey-patching→inventory.

**Known no-equivalent zones (be honest with users):** SQLAlchemy/Alembic parity, numpy's full breadth, scikit-learn/matplotlib, distributed task queues with Celery wire compat.

---

## What the framework itself must build (verified gaps — this is the moat)

1. **Equivalence oracle** for dynamic Python↔Rust (generate + run + judge differential properties; classify *Equivalent / Divergent / Incomparable* like Volatility3).
2. **Profiling→port-plan generator** (ranked, dependency-ordered, portability-scored backlog with GC-scan-cost and I/O-bound signals).
3. **Contract/spec extractor** (tests + mypy + invariants → machine-usable behavioral contract per unit).
4. **Dual-build state manager** (Python original + Rust port + PyO3 shim + per-unit fallback, reversible at any granularity — maturin only builds whole crates).
5. **Cross-language differential benchmark harness** (matched corpora, one verdict: pytest-benchmark and Criterion are separate worlds today).
6. **PyO3-FFI reviewer** for LLM output (refcount leaks, `PyErr` hygiene, free-threading hazards — miri/clippy don't cover these classes).
7. **Attribution-preserving rewriter** (license headers, NOTICE files, per-file provenance — the ScanCode incident shows LLM output strips these by default).
8. **Upstream-tracking maintenance mode** (agents that watch the Python original for changes and propose port updates — the tsrs/Vinext process).
