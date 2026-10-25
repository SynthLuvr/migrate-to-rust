# Catalog — Code Migration / Transformation Tools & the Python↔Rust Interop Ecosystem

**Purpose:** Inventory of existing tools an AI-agent framework could orchestrate to port Python code to Rust incrementally and verifiably.
*Tool maintenance status verified 2026-10-25 against the GitHub/crates.io/PyPI APIs. "Pushed" = last repo activity observed 2026-10-25. Anything that could not be confirmed is marked **UNVERIFIED**.

Legend for maturity: **active** = releases/pushes within ~12 months of 2026-10 · **slow** = maintained but release cadence > 12 months · **stale** = no activity ≥ 2 years · **dead/archived** = repo archived, removed, or abandoned.

---

## A. Python↔Rust Interop & Incremental Porting

| Tool | What it does | How a migration framework uses it | Maturity | License |
|---|---|---|---|---|
| **PyO3** (github.com/PyO3/pyo3) | De-facto standard Rust bindings for Python; create native extension modules. Supports CPython, PyPy, GraalPy. Key current features: `Bound<'py, T>` smart-pointer API (since 0.21), `IntoPyObject` trait (since 0.23, replaces `IntoPy`/`ToPyObject`), **abi3 / abi3t stable-ABI** support (abi3t for free-threaded 3.13t/3.14t), **free-threaded (no-GIL) CPython support** (free-threaded 3.14 builds version-specific `cp314t` wheels since abi3t isn't fully supported there). Migration guide (pyo3.rs/*/migration): **GIL Refs API deprecated in 0.23** — gated behind `gil-refs` feature with deprecation warnings, removed thereafter. | The compilation target: ported functions become PyO3 modules callable from the *un-ported* Python remainder, enabling function-level incremental migration. The framework should generate code against the **Bound API only** (avoid `gil-refs`, `IntoPy`, `ToPyObject`) to stay on supported APIs. | **active** — v0.30.1 (crates.io, 2026-10-14), pushed 2026-10-25, ~16.3k stars, 284M crate downloads | Apache-2.0 (MIT dual-option in practice) |
| **maturin** (github.com/PyO3/maturin) | Build & publish crates as Python wheels. Supports pyo3, cffi, and uniffi bindings + pure-Rust binaries. `maturin develop` installs into the current venv (fast inner dev loop); `maturin build/publish` produce manylinux wheels; auto-detects binding type from `Cargo.toml`. | The build/release engine for every ported module: framework runs `maturin develop` after each port iteration, and uses maturin's CI actions (`PyO3/maturin-action`, `messense/maturin-manylinux-wheels-action`) to produce installable artifacts for acceptance testing. | **active** — v1.15.0 (PyPI 2026-08-24, crates.io 2026-08-27), pushed 2026-10-23, ~5.8k stars | Apache-2.0 (MIT option) |
| **rustimport** (github.com/mityax/rustimport) | Import `.rs` files **directly from Python** via import hook (like `cppimport`); auto-compiles on import, caches builds keyed on source hash, CLI for pre-compilation, IPython/Jupyter support. | Ideal for the *experimentation phase*: an agent can drop a candidate `.rs` port next to the Python code, `import rustimport.import_hook; import ported_func`, and benchmark/compare it in-process without any packaging ceremony. Also useful for repo-local scratch ports during profiling-driven migration. | **active** (slightly slow) — PyPI v1.7.1 (2025-05-09), pushed 2026-06-01, ~308 stars | MIT |
| **rust-cpython** (github.com/dgrunwald/rust-cpython) | Older Rust↔Python FFI library, alternative to PyO3. | Legacy only; a new framework should standardize on PyO3. maturin still builds it, but ecosystem momentum is PyO3. | **stale-ish** — pushed 2024-08, ~1.8k stars | MIT |
| **setuptools-rust** (github.com/PyO3/setuptools-rust) | setuptools plugin to build PyO3/rust-cpython extensions inside the normal Python build (`pyproject.toml`). | Alternative packaging path when a project must keep a pure-setuptools build (e.g., mixed sdists); framework may fall back to it where maturin's workflow doesn't fit an existing build system. | **active** — v1.13.0 (PyPI 2026-06-27), pushed 2026-10-07, ~680 stars | MIT |
| **UniFFI** (Mozilla; crates.io `uniffi`) | Generates foreign-function interfaces + bindings from Rust (originally Kotlin/Swift; Python target supported via `uniffi-bindgen` ecosystem). maturin supports uniffi bindings. | Alternative declaration-based binding path (write UDL/proc-macros, generate bindings) when the framework wants less hand-written PyO3 glue for large surface areas. | **active** — v0.32.2 (2026-09-23) | MPL-2.0 |
| **cffi** (Python) | Python FFI library (ABI/API modes); the "API mode" historically used Rust/C toolchains to build version-agnostic extension modules via generated C. maturin can build `cffi`-style crates. | Lower-level fallback for stable-ABI bindings without PyO3; rarely preferable now that PyO3 supports abi3 natively. | active (upstream `cffi` project) | MIT |
| **riposte** | Python decorator library that runs Rust snippets from Python (compile-and-load via cffi). | — | **UNVERIFIED** — multiple searches (incl. GitHub topics) did not surface a live repo; presumed abandoned/obscure. Do not plan around it. | unknown |
| **pyo3-polars** (crates.io) | PyO3 bindings + plugin system for Polars: pass Arrow/Polars DataFrames between Python and Rust with zero-copy, and register Rust expression plugins. | Template for how the framework should move *data-heavy* workloads across the boundary (Arrow IPC / zero-copy rather than per-element conversion). | **active** — v0.28.0 (2026-08-04) | MIT (Polars core) |

**Key interop notes for the framework (verified):** PyO3 0.21 introduced the `Bound` API; **0.23 deprecated GIL Refs** (`gil-refs` feature, deprecation warnings) and added `IntoPyObject`; abi3 wheels cover all CPython ≥ specified min version, but **free-threaded CPython 3.14 requires per-version `cp314t` builds** (abi3t limitations) per maturin's bindings guide.

---

## B. Profiling & Benchmarking — picking what to port

| Tool | What it does | How a migration framework uses it | Maturity | License |
|---|---|---|---|---|
| **py-spy** (benfred/py-spy) | Sampling profiler written in Rust; attaches to running CPython (2.3–3.14) without code changes; flame graphs, `record`/`top`/`dump`. | Production-safe hotspot discovery: run py-spy on real workloads, parse flamegraph output, rank functions by self-time → produce the "port list" with expected speedup. | **active** — v0.4.2 (PyPI 2026-04-24), pushed 2026-10-24, ~15.6k stars | MIT |
| **Scalene** (emeryberger/scalene) | High-precision CPU **+ GPU + memory** profiler with per-line attribution and AI-suggested optimizations (optional OpenAI key). | Line-level triage that separates CPU-bound vs allocation-bound hotspots → decides *which* code benefits from Rust (allocation-heavy loops) vs other optimizations. | **active** — v2.4.0 (PyPI 2026-10-23); repo pushed 2025-04 (development now largely in emeryberger/HouseBlanket? — treat repo push date as lagging) | Apache-2.0 |
| **austin** (P403n1x87/austin) | Frame-stack sampler for CPython; austin-tui top-like view; formats feedable to speedscope/flamegraph. | Cheap, always-on sampling to build hotspot histograms across CI runs. | **active** — pushed 2026-10-23, ~2.2k stars. Note: the PyPI package named `austin` is ancient (2016); install via brew/pipx instead. | GPL-3.0 |
| **pyinstrument** (joerick/pyinstrument) | Statistical (call-stack) profiler, 1 ms sampling, low noise, HTML output. | Deterministic dev-loop profiling inside the migration harness to compare before/after call stacks. | **active** — v5.1.3 (PyPI 2026-07-29), pushed 2026-10-12, ~8k stars | BSD-3-Clause |
| **cProfile / pstats** (stdlib) | Deterministic function-level profiler built into Python. | Zero-dependency baseline: every framework run can produce cProfile stats without extra installs; good for reproducible CI measurements. | stdlib (ships with CPython) | PSF |
| **line_profiler / kernprof** (pyutils/line_profiler) | Line-by-line profiling of decorated functions. | Drill into a flagged function to decide port granularity (whole function vs inner loop). | **active** — v5.1.0 (PyPI 2026-10-18), pushed 2026-10-18, ~3.3k stars | BSD (3-clause; API shows NOASSERTION) |
| **memray** (bloomberg/memray) | Tracking memory allocator: every allocation incl. native extensions; flamegraphs, leaks, peak stats. | Memory-side candidate selection (large object churn, leaks) and post-port verification that the Rust module reduces allocation pressure. | **active** — v1.20.0 (PyPI 2026-08-07), pushed 2026-10-22, ~15.4k stars | Apache-2.0 |
| **pyperf** (psf/pyperf) | Rigorous micro-benchmark toolkit (timeit wrapper) with statistics, system-tuning checks, machine-readable output. | Gold-standard before/after timing for individual ported functions; stable numbers for the agent's "was the port actually faster?" gate. | **active** — v2.10.0 (PyPI 2026-02-07), pushed 2026-10-01, ~968 stars | MIT |
| **cargo-flamegraph** (flamegraph-rs/flamegraph) | `cargo flamegraph` — perf/dtrace-based flamegraphs for Rust, incl. binaries and tests. | Profile the *Rust* side after porting to confirm the port (not just the language) is faster, e.g. no accidental allocation/copy regressions. | **active** — pushed 2026-10-05, ~6.1k stars | Apache-2.0/MIT |
| **Criterion.rs** (crates.io `criterion`) | Statistical benchmarking harness for Rust; detects small performance changes, HTML reports, `cargo bench` integration. | Per-function Rust benchmarks as a CI gate: a port must beat (or match) the Python baseline in Criterion before it's accepted. | **active** — v0.8.2 (2026-02-04), 317M downloads | Apache-2.0/MIT |
| **pytest-benchmark** (ionelmc/pytest-benchmark) | pytest fixture for benchmarking; calibration, grouping, history comparison. | Lets the framework express Python-side perf gates *as tests*, so both Python and Rust sides report through one harness. | **active** — v5.3.0 (PyPI 2026-08-23), pushed 2026-08-23, ~1.5k stars | BSD-2-Clause |

---

## C. Fuzzing & Differential Testing (Python vs Rust)

| Tool | What it does | How a migration framework uses it | Maturity | License |
|---|---|---|---|---|
| **cargo-fuzz** (rust-fuzz/cargo-fuzz) | libFuzzer driver for Rust: `cargo fuzz new/run`, coverage-guided, sanitizer integration (ASan/MSan), `arbitrary` crate for structured inputs. | Fuzz the ported Rust module with random/structured inputs; any panic is a porting bug. Feed the same corpus to both implementations. | **active** — pushed 2026-07-20, ~1.9k stars | Apache-2.0/MIT |
| **libFuzzer** | In-process coverage-guided fuzzer (LLVM project) underlying cargo-fuzz. | Same as above (engine layer). | active (LLVM) | Apache-2.0 w/ LLVM exception |
| **AFL++** (AFLplusplus/AFLplusplus) | Fork of AFL; state-of-the-art coverage-guided file-based fuzzer; can fuzz via shared-memory or persistent modes. | Black-box fuzzing of the Python↔Rust pair via stdin/file corpora when in-process libFuzzer harnesses are impractical. | **active** — pushed 2026-10-19, ~6.8k stars | Apache-2.0 (some components AGPL-3.0) |
| **Hypothesis** (HypothesisWorks/hypothesis) | Property-based testing for Python: generates test cases from strategies, shrinks failures. | **The core differential-testing engine**: one property test runs the same generated input through the Python original and the Rust port, asserting equal results (within float tolerance). Shrinking gives minimal counterexamples for the repair loop. | **very active** — v6.170.9 (PyPI 2026-10-23), pushed 2026-10-25, ~9.1k stars | MPL-2.0 |
| **Hypothesis ghostwriter** | Built-in heuristic generator that *writes* Hypothesis tests from function signatures/docstrings automatically. | Bootstraps differential tests for thousands of functions with zero manual test authoring; can also be invoked as a CLI used by the agent. | shipped in Hypothesis (active); Hypothesis team actively combines it with LLM test-writing (2025+ experiments) | MPL-2.0 |
| **syrupy** (syrupy-project/syrupy) | Snapshot-testing pytest plugin; JSON/PNG/SVG serializers, multiple snapshot extensions. | Snapshot the outputs of the Python implementation on golden inputs; re-run same inputs against Rust port and diff snapshots to catch output drift. (Repo moved from tophat → syrupy-project.) | **active** — v6.1.1 (PyPI 2026-09-13), pushed 2026-10-24, ~894 stars | MIT |
| **pytest** (pytest-dev) | The test runner that hosts all of the above. | Execution substrate for differential tests, benchmarks, and acceptance suites. | very active | MIT |

---

## D. Semantic Analysis / Parsing / Program Analysis

| Tool | What it does | How a migration framework uses it | Maturity | License |
|---|---|---|---|---|
| **tree-sitter-python** (tree-sitter/tree-sitter-python) | Incremental error-tolerant parser/grammar for Python (MIT). Powers editors, difftastic, code indexing. | Fast syntactic pass over the whole repo: extract function inventory, sizes, dependencies, build port-plan graph; also used for structural diffs of agent edits. | **active** — pushed 2026-09-13, ~572 stars; `tree-sitter` PyPI v0.26 (2026-06-30) | MIT |
| **LibCST** (Instagram/LibCST) | Meta's lossless concrete-syntax-tree library for Python 3.0→3.15; comment/whitespace-preserving transforms ("codemods"). | Safe, format-preserving mechanical edits on the Python side: replace a function body with a call to the new Rust extension, remove old code, add py.typed stubs — without breaking diffs. | **active** — v1.9.0 (PyPI 2026-07-29), pushed 2026-08-11, ~2k stars | MIT |
| **ast** (stdlib) | Built-in Python AST. | Cheapest route for dependency/order analysis and cheap metrics; no extra deps in the agent sandbox. | stdlib | PSF |
| **mypy** | Static type checker (PEP 484). | Type information (stubs, signatures) feeds the Rust signature generator; also validates generated `.pyi` stubs for the ported module. | very active | MIT + PSF variants |
| **pyright** (microsoft/pyright) | Fast node-based type checker/inference engine for large codebases. | Same role as mypy where inference quality beats stub availability; used by many editors. | very active | MIT (docs differ on npm distribution) |
| **jedi** | Static autocomplete/refactoring library (pure Python). | In-process symbol resolution when the framework must avoid Node or external processes. | maintained | MIT |
| **rope** (python-rope/rope) | Python refactoring library (rename/extract/move) with project-wide static analysis; no Node dependency; supports most 3.10 syntax. | Programmatic, semantics-aware refactors while shim-wrapping ported functions (renames, import rewiring). | **active** — v1.15.0 (PyPI 2026-09-26), pushed 2026-10-24, ~2.2k stars | LGPL-3.0 |
| **Scalpel** (SMAT-Lab/Scalpel) | Research framework for Python static analysis (call graphs, type inference, data-flow, taint, SMT-based analysis). Note: the PyPI package named `scalpel` is an *unrelated* 2010 package — use the GitHub repo. | Optional deep analysis (call graph, data flow) to reason about side effects before porting a function. | **slow/stale** — last push 2024-03-28, ~334 stars | Apache-2.0 |
| **OpenRewrite** (openrewrite/rewrite) | Mass automated refactoring platform (LST-based recipes; mature for Java/C#/Kotlin). Python support lives in **openrewrite/rewrite-python**, which is a **WIP and now ARCHIVED** (no license, last push 2026-01); Python recipes are usable only via the **commercial Moderne CLI/Platform**. | Mostly *not* usable for Python without Moderne's commercial tooling; listed as prior art for "governed, deterministic recipe" migration style rather than a component. | core: active (Apache-2.0); **rewrite-python: archived** | Apache-2.0 (core) |
| **Semgrep** (semgrep/semgrep) | Pattern-based static analysis with simple rule syntax; Python among many languages; OSS CLI + commercial platform. | Guardrail engine: custom rules to detect constructs the port must preserve (e.g., `global` mutation, `try/finally`, monkey-patching) — negative-pattern checks on both Python and generated Rust. | **very active** — v1.182.1 (PyPI 2026-10-22), pushed 2026-10-23, ~17.3k stars | LGPL-2.1 (OSS CLI) |
| **CodeQL** (github/codeql) | Semantic database + QL query language over code (12+ languages incl. Python **and Rust**); free for open-source research. | Deep data-flow queries (taint from inputs to outputs) to identify function contracts the port must reproduce; also queries for unsafe patterns in generated Rust. | **active** — pushed 2026-10-23, ~10.2k stars; engine is proprietary GitHub product | MIT (queries/libs); engine proprietary |

---

## E. Transpilers & Research Systems (prior art — mostly NOT production paths)

| System | What it is | Status | License |
|---|---|---|---|
| **py2many** (py2many/py2many) | The most real mechanical Python transpiler: Python → **Rust** (standalone *or* as a PyO3 extension), C++, Go, Zig, Julia, Nim, Dart, Kotlin. Subset-of-Python semantics (typed subset, no full dynamism). | **active** — pushed 2026-10-22, ~1.3k stars, PyPI v0.9 (2026-08-04) | MIT |
| **c2rust** (immunant/c2rust) | C99 → unsafe, semantics-preserving Rust transpiler + refactoring tools; the canonical "transpile-then-manually-fix" model. | **active** — pushed 2026-10-23, ~4.8k stars, crate v0.22.1 (2026-02-11). GitHub org moved (c2rust/c2rust → immunant/c2rust). | BSD-3-Clause (API license field unclassified; per project docs BSD) |
| **TransCoder** (facebookresearch/TransCoder) | Unsupervised neural code translation (C++↔Java↔Python); research artifact. | **dead/archived** — repo archived, last push 2021-09. Historical prior art for LLM-based translation. | MIT-ish (NOASSERTION; research release) |
| **CodeXGLUE / CodeTrans** (microsoft/CodeXGLUE) | ML benchmark suite incl. code-to-code translation (Java↔C#), with the CodeTrans model family. | Published 2021; benchmark only, not a tool; superseded by general LLMs. | MIT (CodeXGLUE) |
| **KodCode** (KodCode-AI/kodcode) | Synthetic, test-verified coding dataset framework (447K question–solution–test triplets, 2025). Not a transpiler, but a *verified-code-generation* pipeline model. | **active-ish** — pushed 2025-09, ~322 stars | Apache-2.0 |
| **`semantic`** (github/semantic) | GitHub's Haskell library/CLI for parsing, analyzing, diffing code across languages (tree-sitter based). | **dead/archived** — repo archived (notice recommends forking); last push 2025-04. Its tree-sitter diff idea survives in difftastic. | none listed |
| **`rust-python-compiler-benchmark`** (chino) | Reported benchmark of Python-interpreter-in-Rust projects (RustPython, Cannoli…). | **UNVERIFIED** — could not locate the repo via search/API; likely removed. Related verified artifacts: DobromirM/rust-python-benchmark (benchmarks of Python libs written in Rust) and the "Cannoli" Python-subset compiler in Rust (2018, unmaintained). | — |
| **py2rs** (rochacbruno/py2rs) | Despite the name, **not a transpiler** — a field-guide/reference mapping Python concepts to Rust for developers. | active-ish (guide; mdbook) | MIT |
| **"octoweave"** | Nothing found — only mosaic-tile products. Treated as nonexistent. | **UNVERIFIED / likely does not exist** | — |
| **Grumpy, Cannoli** | Python→Go transcompiler (Google, dead); Python-subset compiler in Rust (2018). | dead — cited as historical prior art only | various |

**Takeaway:** no mechanical Python→Rust transpiler handles real-world dynamic Python; py2many is the only live OSS candidate and covers a typed subset only. Production systems (e.g., inside Pydantic, Polars, Cryptography, Ruff/Biome ecosystems) hand-port hot code with PyO3.

---

## F. Harnesses for LLM-driven Code Migration & Agentic Coders

| System | What it does | Validation approach | Maturity | License |
|---|---|---|---|---|
| **IBM watsonx-code-migration-poc** | A 2024 open-source **proof-of-concept** agentic pipeline for enterprise code migration (Java→Java refactors; was showcased for mainframe/COBOL-era migrations using LLM agents + validation steps). | LLM-generated code + test/validation stage in the pipeline; PoC-grade. | **dead — 404**: `github.com/IBM/watsonx-code-migration-poc` no longer resolves (confirmed via GitHub API and page extraction, 2026-10). Use only as design prior art. | was Apache-2.0 (unverifiable now) |
| **presidio-oss** (Presidio Inc. org) | 11 public repos of AI-dev tooling; the flagship is **presidio-oss/cline-based-code-generator** — a VS Code extension for AI-powered task execution, file management, code generation (Cline-based). Migration-specific repos named in early 2024 write-ups could not be confirmed as still published. | Interactive agent executes tasks in IDE; validation left to developer/CI. | **partial** — org active; verified repo pushed 2025-10, 66 stars / 7.3k forks (fork activity); other migration repos **UNVERIFIED** | Apache-2.0 (verified repo) |
| **AWS Kiro** | Spec-driven "agentic IDE" (Code OSS base): natural-language requirements → user stories, acceptance criteria, design docs, task lists that agents execute; requirements-as-artifacts for auditability. | Spec/EARS-format acceptance criteria + agent-executed tasks with human gates; proprietary SaaS (free/paid tiers). | **active product** (launched 2025, GA since; still preview-flavored) | proprietary (not OSS) |
| **SWE-agent** (SWE-agent/SWE-agent, Princeton NLP) | Agent-Computer Interface framework: LLM agent fixes real GitHub issues using purpose-built CLI tools (file viewer, linter, search). SOTA-competitive on SWE-bench. | Runs the repo's own test suite to validate each fix; SWE-bench harness for grading. | **very active** — pushed 2026-10-19, ~20.6k stars | MIT |
| **OpenHands** (OpenHands/OpenHands, formerly OpenDevin/All-Hands-AI) | Open platform for AI software developers: agents modify code, run commands, browse, commit/PR, in sandboxed runtimes. | Iterates against tests/linters; evaluation modes incl. SWE-bench; human-in-loop optional. Repo **moved orgs** (All-Hands-AI → OpenHands) — links under the old org redirect. | **very active** — pushed 2026-10-25, ~91.8k stars | MIT |
| **Aider** (Aider-AI/aider) | Terminal AI pair programmer that edits files in a local git repo; multi-model (GPT-4/Claude/Gemini/DeepSeek/local via Ollama); auto-commits with sane messages; repo-map context. | Validates via user-specified test commands (`--test-cmd`) run after edits, plus lint hooks; git history is the audit trail. Writes most of its own code (self-reported). | **active** — pushed 2026-05-22, ~49.7k stars | Apache-2.0 |
| **OpenAI Swarm** (openai/swarm) | Minimalist educational multi-agent framework (routines + handoffs). Explicitly experimental/educational; OpenAI's production path moved to the **Agents SDK**. | None built in — orchestration only. | **educational/frozen** — pushed 2026-04 (little movement), ~22.1k stars; superseded by openai-agents-python | MIT |
| **Claude Code** (Anthropic) | Commercial agentic CLI coder (terminal, tools, hooks, MCP). Widely used by teams for large mechanical migrations ("put Claude Code in a loop over modules"). | Test/lint hooks + CI; user-defined validation commands. | **active product** | proprietary |
| **block/goose** | OSS (Rust) extensible AI agent/CLI (MCP-based) that can drive dev workflows; relevant as an *orchestration shell* the migration framework could extend rather than build from scratch. | Tool/MCP-driven; validation via whatever tools it calls. | **active** | MIT/Apache-2.0 |
| **Hypothesis × LLM test-writing** (hypothesis.works, 2025+) | Research/experimentation combining ghostwriter + LLM agents (e.g., Claude) to auto-write and run property tests against OSS packages; found real bugs (NumPy, pandas, SDKs). | Property tests themselves are the validator. | active research line; no standalone product | n/a |

**Pattern for a migration framework:** pair an orchestrating agent shell (OpenHands / Aider / SWE-agent / goose — all OSS, scripted) with validation gates built from §B–§D tools; none of these harnesses understands Python→Rust semantics natively — that must be supplied as custom tools/gates.

---

## G. Verification & Equivalence Tooling

| Tool | What it does | How a migration framework uses it | Maturity | License |
|---|---|---|---|---|
| **Differential property testing pattern** (Hypothesis + PyO3 module) | Run both implementations on generated inputs in one process; compare results/errors/perf. | The primary *semantic equivalence* gate — property tests double as spec extraction. No ready-made "port equivalence" product exists; it's composed from Hypothesis + pytest. | composed; components active | — |
| **CETBench** (arXiv 2506.04019, 2025) | Benchmark for LLM **code-equivalence checking** built via program transformations; finds LLMs still weak at semantic equivalence. | Research reference when using an LLM as an equivalence judge — evidence that LLM-only judgment is unreliable; ground it with execution-based differential tests. | paper (2025) | n/a |
| **"ECG" equivalence checker** | **UNVERIFIED** — no tool by this name located; treat as nonexistent/unknown. | — | — | — |
| **ChatDBG** (plasma-umass/ChatDBG) | AI-assisted debugger (LLM inside pdb/gdb/lldb); root-cause analysis + suggested fixes; FSE'25 paper. | Speeds up the repair loop when a differential test or fuzz case fails on the Rust port (debug via pdb on Python side / lldb on Rust side). | **active** — pushed 2026-07-20, ~1.1k stars | Apache-2.0 |
| **difftastic** (Wilfred/difftastic) | Structural, tree-sitter-based syntax-aware diff (30+ languages incl. Python and Rust). | Review gate: show *semantic* diffs of agent edits; auto-review of "did the port change behavior-visible structure?". | **active** — v0.71.0 (crates.io 2026-09-18), pushed 2026-10-16, ~26.1k stars | MIT |
| **coverage.py** (PyPI `coverage`) | Standard Python coverage measurement (line/branch). | Gate on the Python side: the differential test corpus must cover the ported function's branches *before* porting; post-port, run the same suite through the shim. | **very active** — v7.17.0 (2026-10-22) | Apache-2.0 + BSD |
| **cargo-llvm-cov** (taiki-e/cargo-llvm-cov) | LLVM source-based coverage for Rust incl. branch coverage (MC/DC via nightly option). | Mirror coverage gate on the Rust side; compare against the Python coverage to find untested port paths. | **active** — pushed 2026-10-15, ~1.5k stars | Apache-2.0/MIT |
| **cargo-tarpaulin** (xd009642/tarpaulin) | Rust coverage tool (container-friendly, Linux). | Alternative Rust coverage backend for CI where LLVM tooling is inconvenient. | **active** — pushed 2026-10-19, ~3k stars | Apache-2.0/MIT |
| **insta** (mitsuhiko/insta) | Rust snapshot testing + `cargo-insta` review workflow. | Snapshot the Rust port's outputs on golden inputs (complements syrupy snapshots on the Python side). | **active** — v1.49.0 (2026-10-03), ~3k stars | Apache-2.0 |

---

## H. CI / Quality Gates for the Rust Side

| Tool | What it does | Use in framework | Maturity | License |
|---|---|---|---|---|
| **clippy** | Rust linting (idiomatic, correctness, perf lints). | Every generated crate must pass `cargo clippy -- -D warnings` before acceptance. | ships with Rust toolchain; very active | Apache-2.0/MIT |
| **rustfmt** | Canonical formatting. | Normalize all generated Rust; stable diffs. | toolchain; active | Apache-2.0/MIT |
| **cargo-machete** (bnjbvr) | Finds unused Cargo dependencies (fast, stable toolchain, `--fix`). | Clean up LLM-generated `Cargo.toml` bloat automatically. | **active** — v0.9.2 (2026-04-15), ~1.4k stars | MIT |
| **cargo-udeps** (est31) | Unused-dependency detection via deep compile analysis (nightly). | Second-opinion pass where machete's text-based heuristic is insufficient. | **active** — v0.1.61 (2026-04-29); license field unclassified in API (MIT per repo history) | MIT (approx.) |
| **miri** (rust-lang/miri) | MIR interpreter detecting **undefined behavior** in `unsafe` Rust. | Critical: LLM-generated PyO3 code is full of FFI `unsafe`; run miri over unit tests of the unsafe seams. | **active** — pushed 2026-10-24, ~6.7k stars (distributed via rustup) | Apache-2.0/MIT |
| **loom** (tokio-rs/loom) | Model-checking (permutation-exhaustive) concurrency test tool. | Only for ported code that introduces concurrency; expensive, used selectively. | **slow but maintained** — crate v0.7.2 (2024-04), ~2.8k stars | MIT |
| **cargo-deny** (EmbarkStudios) | Lints the dependency graph: advisories (RustSec), licenses, bans, sources. | Supply-chain gate on all generated crates before they're shipped. | **active** — v0.20.2 (2026-07-09), ~2.5k stars | MIT (Embark standard) |
| **cargo-nextest** (nextest-rs/nextest) | Next-gen Rust test runner: per-test processes, retries, partitions, JUnit output. | Faster, machine-readable Rust test execution in the CI loop. | **very active** — v0.9.149 (2026-10-20), ~3.4k stars | Apache-2.0/MIT |

---

## I. Packaging, Deployment & Reference Codebases

| Item | Notes | Maturity / License |
|---|---|---|
| **maturin** (vs setuptools-rust) | maturin is the recommended default (zero-config, `develop` workflow, manylinux/musllinux wheels, `abi3`, GitHub Actions via `PyO3/maturin-action` and `messense/maturin-manylinux-wheels-action`); setuptools-rust is the integration path for existing setuptools projects. | both active (see §A) |
| **manylinux wheels** | maturin builds manylinux/musllinux wheels by default; cross-compile via zig/cargo-zigbuild for full platform matrices — framework CI should produce wheels for at least x86_64/aarch64 × glibc/musl. | standard |
| **cargo-dist** (axodotdev/cargo-dist) | Release engineering: app packaging + installer generation for Rust binaries (not needed for wheels, relevant if the end product ships a Rust binary too). | **active** — pushed 2026-10-24, ~2.1k stars, Apache-2.0 |
| **pydantic-core** | Canonical reference for PyO3 engineering at scale: Rust validation kernel behind Pydantic v2. Patterns worth copying: fine-grained **error mapping** (Rust `ValError`/80+ `ErrorType` variants → Python `ValidationError` with location info), strict/lenient modes, benchmark suite. NOTE: the original `pydantic/pydantic-core` repo was **archived** — the code now **lives inside the main `pydantic` monorepo** (confirmed via archived-repo notice, 2026). | active (inside pydantic), MIT |
| **polars** (+ `pyo3-polars`) | Canonical reference for Rust-backed Python *data* packages: Arrow-based zero-copy Python↔Rust boundary, minimal per-element conversion, expressions plugins; huge PyO3 codebase with tight benchmarking culture. | **active** — pushed 2026-10-25, ~40.2k stars, MIT |
| **Other mature Rust-backed PyPI references** (verified existence, not individually audited) | cryptography (cffi+Rust), cryptography's Rust (PyO3) portions, ruff/lint ecosystem, tokenizers (HF), orjson, pydantic — useful as style guides for PyO3 module layout, error mapping, and benchmark harnesses. | active, various (MIT/Apache-2.0) |

---

## Gaps: what no existing tool covers

1. **No production-grade Python→Rust *transpiler*.** py2many handles a typed subset; everything else (TransCoder, CodeTrans, Cannoli, Grumpy, github/semantic) is dead or archived. Real migration = LLM translation + mechanical scaffolding, not deterministic compilation.
2. **No semantic-equivalence oracle for dynamic Python.** Equivalence is checked by composing Hypothesis differential tests + snapshots — there is no tool that, given `f.py` and `f.rs`, *automatically* generates, runs, and judges equivalence properties (CETBench shows even LLM judges are unreliable). A framework must build this gate itself.
3. **No migration-aware profiling → port-plan generator.** Profilers (§B) output human flamegraphs/stats; nothing turns them into a ranked, dependency-aware, *portability-scored* port backlog (e.g., accounting for GIL release benefits, allocation density, call boundaries).
4. **No spec/contract extraction tool.** Nothing mines the Python code + its tests into machine-usable behavioral contracts (types, invariants, error taxonomies) that the Rust port must satisfy. mypy/LibCST provide ingredients, not contracts.
5. **No harness that manages the dual-build state** (Python original + Rust port + shim + Python fallback at runtime) as a first-class, reversible artifact across many functions — maturin builds *one* crate; feature-flagged per-function enablement is manual.
6. **No cross-language differential benchmark harness.** pytest-benchmark and Criterion are separate worlds; nothing runs matched benchmark corpora across Python and Rust and emits a unified regression verdict (float tolerance, error-rate parity, allocation counts).
7. **Unsafe-FFI review is manual.** miri/clippy catch classes of bugs, but no tool reviews LLM-generated PyO3 code for FFI-specific hazards (refcount leaks, `PyErr` hygiene, free-threading hazards under 3.13t/3.14t).
8. **Migration-specific agent harnesses died or are productized.** IBM's poc is gone; Kiro is proprietary; OpenRewrite's Python support is archived behind a commercial platform. The reusable OSS base is generic coders (OpenHands/Aider/SWE-agent/goose) + custom gates.

---

## Sources

- PyO3 docs — migration guide: https://pyo3.rs/v0.27.2/migration (Bound API 0.21, gil-refs deprecation 0.23, IntoPyObject)
- maturin bindings guide (abi3/abi3t, free-threaded 3.14, cp314t): https://github.com/PyO3/maturin/blob/main/guide/src/bindings.md
- rustimport: https://github.com/mityax/rustimport
- py-spy: https://github.com/benfred/py-spy · Scalene: https://github.com/emeryberger/scalene · austin-tui: https://terminaltrove.com/austin-tui
- memray: https://www.opensourceforu.com/2022/04/bloomberg-open-sources-memray-a-python-memory-profiler · pyperf: https://superfastpython.com/python-benchmark-pyperf · line_profiler: https://github.com/pyutils/line_profiler
- cargo-fuzz guide: https://helpmetest.com/blog/cargo-fuzz-rust-fuzzing · AFL++: https://github.com/AFLplusplus/AFLplusplus
- Hypothesis ghostwriter + LLM experiments: https://hypothesis.works/articles/claude-code-plugin · syrupy: https://mcpmarket.com/tools/skills/syrupy-pytest-snapshot-testing · pytest-benchmark: https://github.com/ionelmc/pytest-benchmark
- LibCST: https://github.com/Instagram/LibCST · rope: https://github.com/python-rope/rope · Scalpel paper: https://www.semanticscholar.org/paper/a4c11b9e43c8ea9a2c3d7733dd73f44ea189bdf3 · repo: https://github.com/SMAT-Lab/Scalpel
- OpenRewrite language support / rewrite-python (archived): https://github.com/openrewrite/rewrite-python · https://www.moderne.io/solutions/language-support
- Semgrep: https://semgrep.dev (CLI OSS, LGPL-2.1) · CodeQL: https://github.com/github/codeql
- py2many: https://github.com/py2many/py2many · awesome-transpilers: https://github.com/transpiler/awesome-transpiler · c2rust: https://github.com/immunant/c2rust
- github/semantic (archived): http://github.com/github/semantic · TransCoder: https://www.theregister.com/2020/06/12/facebook_transcoder_ai · CodeXGLUE: https://arxiv.org/pdf/2102.04664 · KodCode: https://www.emergentmind.com/topics/kodcode
- SWE-agent: https://github.com/SWE-agent/SWE-agent · OpenHands: https://github.com/OpenHands/OpenHands · Aider: https://github.com/Aider-AI/aider · Swarm: https://github.com/openai/swarm + https://the-decoder.com/openai-introduces-experimental-multi-agent-framework-swarm · Kiro: https://medium.com/@info_29830/aws-kiro-the-new-spec-driven-agentic-ide-that-reins-in-vibe-coding-a16696837d0a · presidio-oss: https://github.com/orgs/presidio-oss/repositories
- IBM/watsonx-code-migration-poc — 404 confirmed: https://github.com/IBM/watsonx-code-migration-poc (extracted 2026-10-25)
- difftastic: https://www.linuxlinks.com/difftastic-structural-diff-tool · ChatDBG: https://github.com/plasma-umass/chatdbg · CETBench: https://www.emergentmind.com/papers/2506.04019
- Rust quality tools: cargo-llvm-cov vs tarpaulin: https://supercov.com/compare/rust-code-coverage · miri-in-CI: https://kflansburg.com/posts/improving-rust-codebase-quality-with-miri-in-ci · loom talk: https://guild.host/presentations/bugfree-concurrency-zew61c · cargo-deny: https://kloubot.com/blog/cargo-deny-rust-supply-chain-security-guide · unused-deps comparison (machete/udeps/shear): https://openillumi.com/en/en-rust-cargo-unused-deps-cleaner · insta: https://www.rustfaq.org/en/how-to-use-snapshot-testing-in-rust-insta-crate
- Packaging: setuptools-rust: https://pypi.org/project/setuptools-rust · maturin-manylinux-wheels-action: https://github.com/marketplace/actions/maturin-manylinux-wheels-action · cargo-dist: https://news.ycombinator.com/item?id=34614497 · pydantic-core error architecture: https://deepwiki.com/pydantic/pydantic-core/6-error-handling · pydantic-core archived-into-pydantic notice: https://github.com/pydantic/pydantic-core · pyo3-polars: https://crates.io/crates/pyo3-polars/0.24.0
- Machine-readable verification (2026-10-25): GitHub REST API (archived/pushed_at/license/stars for ~55 repos), crates.io API (pyo3, maturin, criterion, insta, loom, cargo-deny, cargo-machete, cargo-udeps, difftastic, c2rust, pyo3-polars, cargo-nextest, uniffi), PyPI JSON API (20 packages incl. scalene, hypothesis, syrupy, libcst, rope, coverage, py-spy, py2many, semgrep).
