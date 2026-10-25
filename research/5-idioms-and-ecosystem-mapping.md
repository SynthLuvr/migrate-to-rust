# Practical Rust Idioms & Python→Rust Ecosystem Mapping

**Date:** 2026-10-25 · **Method:** Live web search + page extraction (Tavily) against crates.io / lib.rs / docs.rs / pyo3.rs / maturin.rs / peps.python.org / GitHub / CPython docs.
**Verification legend:** ✅ **V** = fetched/verified live this run · ◐ **C** = canonical/stable authoritative URL, well-known but not re-fetched this run · ⚠️ **U** = UNVERIFIED (do not trust without checking).

> **Era note (verified this run):** the `pyo3` crate was last updated 2026-10-14 with maturin docs referencing `pyo3 = "0.29.0"`; free-threading guidance in pyo3.rs "main" docs describes PyO3 ≥ 0.28 behavior. All version claims below were checked against live pages on 2026-10-25.

---

## (a) Idiom mapping table — Python → Rust (with sources)

| # | Python construct | Idiomatic Rust equivalent | Notes / strategy | Source(s) |
|---|---|---|---|---|
| 1 | Duck typing ("if it quacks…") | **Traits + generics** (`fn f(x: impl Trait)`), or **enum dispatch** for closed sets | Compile-time polymorphism replaces runtime attribute probing. Static dispatch by default; `dyn Trait` for heterogeneous collections | corrode.dev Python→Rust migration guide ✅V [1]; r/rust discussion ✅V [2]; Rust Book (traits) ◐C [8] |
| 2 | `class` (data + methods) | **`struct` + `impl` block** (data/behavior split) | Rust separates data from behavior; "prefers composition over inheritance" | corrode.dev ✅V [1] |
| 3 | Inheritance | **Composition + traits**; **enums + match** for closed variants; **`dyn Trait` / trait objects** for open polymorphism | Rust has no implementation inheritance; OO patterns must be re-modeled | corrode.dev ✅V [1]; Rust Book "Using Trait Objects" ◐C [8]; joshleeb trait-objects explainer ✅V [9] |
| 4 | Exceptions (`raise`/`try`/`except`) | **`Result<T, E>` + `?`**; `thiserror` (library errors) / `anyhow` (application errors); at the PyO3 boundary → **`PyErr` / `PyResult<T>`** | `From<E> for PyErr` (or `PyErrArguments` for lazy construction) auto-converts Rust errors into raised Python exceptions from `#[pyfunction]` | PyO3 error-handling guide ✅V [3]; BurntSushi error-handling essay ✅V [7] |
| 5 | `None` | **`Option<T>`** | Compiler-forced absence handling | corrode.dev ✅V [1]; BurntSushi ✅V [7] |
| 6 | Generators / `yield` | **`Iterator` trait, `impl Iterator`, custom iterator structs** | Pull-based iteration; state carried in struct fields instead of generator frames | Rust Book ch. 13 (iterators) ◐C [8]; corrode.dev ✅V [1] |
| 7 | Context managers (`with`) | **RAII + `Drop`** guards | Scope-based cleanup replaces `__enter__`/`__exit__` | Rust Book (Drop) ◐C [8] |
| 8 | List/dict comprehensions | **Iterator adapters**: `iter().map().filter().collect()` | Lazy, zero-cost chains | corrode.dev ✅V [1]; Rust Book ◐C [8] |
| 9 | f-strings / `.format()` | **`format!` / `write!` macros** | | rustpython.com cheatsheet ✅V [4]; programming-idioms.org ✅V [5] |
| 10 | Decorators | **Function composition, wrapper fns, attribute proc-macros**; in PyO3, `#[pyfunction]`/`#[pyclass]` attributes | Compile-time metaprogramming replaces runtime wrapping | PyO3 guide ✅V [3]; TLBORM macros book ✅V [10] |
| 11 | Metaclasses / runtime class mutation | **Derive macros, declarative/proc macros, builder pattern, trait-object registries** | Code generation at compile time; no runtime class surgery | TLBORM ✅V [10]; rust-unofficial patterns (builder) ◐C [11] |
| 12 | Monkey-patching | **No equivalent** — use enums/traits, generics, dependency injection, or registry pattern (`inventory` crate) | Verified-active `inventory` crate (2026-10-07) supplies compile-time-linked runtime registries | crates.io API ✅V [12] |
| 13 | Dynamic reflection (`getattr`, `inspect`) | **Proc macros (compile-time reflection), `inventory`/`linkme` registries, `serde`-driven schemas** | Rust reflection is compile-time only; runtime registries must be built explicitly | TLBORM ✅V [10]; crates.io ✅V [12] |
| 14 | Global interpreter state / module globals | **`Arc<Mutex<T>>`, `RwLock`, `OnceLock`, channels (mpsc), thread-local storage** | "Fearless concurrency"; free-threaded CPython makes this mandatory, not optional | Rust Book ch. 16 ◐C [8]; PyO3 free-threading guide ✅V [6] |
| 15 | `asyncio` (`async def`, `await`) | **`async`/`await` + tokio runtime**; across the boundary: `pyo3-async-runtimes` (maintained fork of pyo3-asyncio) or PyO3's native `async fn` support | Event-loop bridging asyncio↔tokio is a real integration cost — see (e) | tokio crates.io ✅V [12]; pyo3-async-runtimes crates.io ✅V [13] |
| 16 | Duck-typed function args | **Generics + trait bounds**, `impl Into<T>` parameters; at boundary: PyO3 `FromPyObject` / `IntoPyObject` derive | | PyO3 migration guide (IntoPyObject derive) ✅V [14]; corrode.dev ✅V [1] |
| 17 | `isinstance` branches | **`enum` + `match`** (exhaustive), or `dyn Trait` downcasting when open-ended | Enums model closed state machines; match exhaustiveness = compiler-checked dispatch | corrode.dev ✅V [1] |

**Community cheat-sheet references:** rustpython.com "Rust Cheat Sheet for Python Developers" ✅V [4]; programming-idioms.org Python↔Rust side-by-side ✅V [5]; corrode.dev "Migrating from Python to Rust" ✅V [1]. **Official Rust docs** do not ship a dedicated Python comparison — the Rust Book's OO/trait-object chapters ◐C [8] are the closest official material.

---

## (b) Ecosystem substitution table (maintenance verified 2026-10-25 via crates.io/lib.rs API ✅V unless marked)

| Python library | Rust equivalent(s) | Maintenance evidence (last release/update) | Notes & honesty caveats |
|---|---|---|---|
| `requests` / `httpx` | **`reqwest`** | updated 2026-09-08 ✅V | De-facto HTTP client; async via tokio, blocking API available |
| `numpy` | **`ndarray`** · **`nalgebra`** | ndarray 2026-01-10 ✅V · nalgebra 2026-05-24 ✅V | ndarray = N-dim arrays (+ optional BLAS); nalgebra = linear algebra/graphics. ⚠️ **Neither matches numpy's full breadth** (ufuncs ecosystem, broadcasting ergonomics, np.fft breadth); numeric/scientific port is a design decision, not a drop-in |
| `pandas` | **`polars`** | crate 2026-08-06 ✅V; official "Coming from Pandas" guide ✅V [15]; pola.rs migration-strategies post (Aug 2026) ✅V [16] | Best-documented migration path of any pair (see [15]/[16]); Apache Arrow memory model, no indexes by design |
| `pydantic` | **`serde`** (deserialize+validate types) + **`garde`** or **`validator`** (field validation) | garde 0.23.0, 2026-05-23, ~1.14M dl/month, 253 dependents ✅V [17]; validator updated 2026-07-27 ✅V [12] | serde replaces pydantic's *type coercion* (compiler does it); garde/validator cover *value rules* via derive macros. Rust needs no runtime type validation [18] ✅V |
| `sqlalchemy` | **`sqlx`** · **`diesel`** · **`sea-orm`** | sqlx 2026-05-21 ✅V · sea-orm 2026-09-27 ✅V · diesel ◐C (not re-fetched) | sqlx = async, compile-time-checked SQL; diesel = type-safe ORM; sea-orm = dynamic async ORM. ⚠️ **No Rust ORM matches SQLAlchemy's maturity or has an Alembic-grade migrations story** — expect friction |
| `flask` / `fastapi` | **`axum`** (tokio ecosystem) · **`actix-web`** | axum 2026-04-14 ✅V · actix-web ◐C | axum+serde+tower is the current default stack. ⚠️ No FastAPI-native equivalent of auto pydantic OpenAPI docs; `utoipa` exists (◐C, unverified this run) |
| `pytest` | **built-in `cargo test`** + **`rstest`** (parametrize/fixtures) | rstest 2026-10-18 ✅V | cargo test gives parallel test harness; rstest covers fixtures/parametrization |
| `hypothesis` | **`proptest`** | 2026-10-10 ✅V | Property-based testing with shrinking |
| `celery` | **`apalis`** (active) · **`rusty-sidekiq`** (Sidekiq protocol) | apalis 2026-10-12 ✅V; rusty-sidekiq 2026-09-28 ✅V | ⚠️ **HONESTY FLAG: no maintained, Celery-wire-protocol-compatible Rust library.** The `celery` crate (rusty-celery) has been **stale since 2023-09-25** ✅V [12] — mixing Rust workers into an existing Celery deployment is therefore NOT safe today; a "CeleRS" repo exists but is new/unproven ⚠️U [19]. Choose apalis/rusty-sidekiq only for green-field queues |
| `logging` | **`tracing`** | 2025-12-18 ✅V | Structured, span-based; `tracing-subscriber` (◐C) for stdout/OTLP |
| `attrs` / `dataclasses` | **serde structs** (+ `derive_more` for trait boilerplate) | derive_more 2025-12-22 ✅V | Derives replace `@attr.s`/`@dataclass` generation |
| `json` | **`serde_json`** | ◐C (canonical; part of serde suite) | |
| `asyncio` | **`tokio`** | 2026-10-21 ✅V | Ecosystem default; smol/async-std exist ◐C |
| `boto3` | **AWS SDK for Rust** (`aws-sdk-s3`, …) | aws-sdk-s3 updated **2026-10-22** ✅V (near-daily releases) | Officially generated & maintained by AWS |
| `unittest.mock` / reflection-heavy tests | **`mockall`** ◐C (not re-fetched) | — | Compile-time mock generation |
| Monkey-patch/registry needs | **`inventory`** | 2026-10-07 ✅V | Compile-time-collected runtime registries |

### No-good-equivalent honesty list (⚠️ verified gaps)
1. **Celery protocol compat** — stale `celery` crate ✅V (2023); no drop-in Rust worker for an existing Celery broker deployment.
2. **numpy/pandas "interactive breadth"** — polars/ndarray are excellent for pipelines but nothing replicates the notebook-interactive numpy+pandas ecosystem ergonomics 1:1.
3. **SQLAlchemy ORM + Alembic** — sea-orm/diesel are younger; migrations tooling is weaker.
4. **Python stdlib breadth** (email parsing, `re` behaviors, text encodings, batteries-included modules) — Rust stdlib is minimal; each subsystem needs a crate, and edge behaviors (e.g., email/URL parsing quirks) may differ. Verify crate-by-crate during translation.
5. **scikit-learn / matplotlib** — `linfa`/`plotters` ◐C exist but are far smaller ecosystems (unverified this run ⚠️U) — treat ML/plotting ports as high-risk.

---

## (c) Hard-to-translate constructs & community strategies

| Python construct | Why it's hard | Published strategy |
|---|---|---|
| **Duck typing** | No static types to check | Traits with method bounds; generics (`impl Trait`) for static dispatch, `Box<dyn Trait>` for dynamic; enums for closed sets (corrode.dev ✅V [1]; r/rust ✅V [2]) |
| **Exceptions** | Control flow via raise | `Result<T,E>` + `?` propagation; `thiserror` derive for library error enums, `anyhow` for app-level context (BurntSushi ✅V [7]); at the FFI boundary: implement `From<E> for PyErr` or return `PyResult`, let PyO3 raise (PyO3 guide ✅V [3]); lazy exception construction via `PyErrArguments` ✅V [3] |
| **Inheritance hierarchies** | No subclassing | Composition over inheritance; trait objects for polymorphism; enums for variant sets; template-method → default trait methods (corrode.dev ✅V [1]; Rust Book ◐C [8]) |
| **Dynamic reflection** | Nothing to reflect on at runtime | Proc macros to *generate* the code reflection would have discovered (TLBORM ✅V [10]); `inventory` registries for runtime plugin tables ✅V [12] |
| **Global interpreter state** | Free-threading kills shared-mutable globals | `Arc<Mutex>`/`RwLock`, channels; PyO3 now *requires* `#[pyclass]` data be `Sync` (migration guide ✅V [14]) |
| **Generators** | Lazy resumable frames | Iterators (`impl Iterator`), state-machine enums; adapters compose lazily (Rust Book ◐C [8]) |
| **asyncio → tokio** | Two event loops | Keep Rust-internal async in tokio; bridge with `pyo3-async-runtimes` (`into_future` / `local_future` conversions, `#[pyo3_async_runtimes::tokio::main]`) ✅V [13]; asyncio callables called via `asyncio.call_method` bridging ✅V [13] |
| **Metaclasses / monkey-patching** | Runtime class surgery | Builder pattern (rust-unofficial patterns ◐C [11]), derive macros, registry/enums; accept "no equivalent" for true monkey-patching |
| **Duck-typed args (`*args/**kwargs`)** | Arbitrary signatures | Concrete typed params + `Into<T>` conversions; PyO3 `FromPyObject`/`IntoPyObject` derives for boundary types (✅V [14]) |

---

## (d) Interop & practical constraints for Rust-backed Python packages

### Wheels & ABI (all ✅V from maturin docs [20] unless noted)
- **`abi3` (Py_LIMITED_API, PEP 384)**: `abi3-py310` wheel runs on **all GIL-enabled CPython ≥ 3.10** — one wheel per platform instead of per-version. Trade-off: abi3 restricts you to the **subset of the C API guaranteed forward-compatible (PEP 384; PEP 803 for free-threaded)** ✅V [21] — some richer APIs are unavailable.
- **Free-threaded stable ABI (`abi3t`) exists only for CPython ≥ 3.15.** **Free-threaded CPython 3.14 has no abi3t** → maturin emits a **version-specific `cp314-cp314t` wheel** for it ✅V [20].
- **Do not build one wheel with both `abi3` and `abi3t` features** — maturin selects at most one stable-ABI family per build and emits version-specific fallbacks; publish `abi3` and `abi3t` wheels via separate builds (`maturin build --interpreter python3.10` / `python3.15t`) ✅V [20].
- Version-specific (non-abi3) wheels remain the safe default when you need full C-API access or per-version optimizations.

### Free-threaded CPython (PEP 703 / PEP 779)
- **PEP 703** (`--disable-gil` build): verified abstract — optional GIL-less, thread-safe interpreter build ✅V [22]. Phase I = **3.13** (experimental) ✅V [23].
- **PEP 779** = criteria for "supported" status; Phase II reached in **Python 3.14**: free-threaded build **officially supported (still optional)**; specializing interpreter (PEP 659) enabled; single-threaded overhead down to **~5–10%**; on Windows, build backends must now pass `Py_GIL_DISABLED` explicitly ✅V [23][24].
- Extension modules that don't declare support still work: the free-threaded build emulates GIL semantics per-module ✅V [25] (CPython free-threading howto — canonical text verified via cpython GitHub source ✅V [25]; docs.python.org rendering extraction was blocked, same content).
- **PyO3 support (verified ✅V [6], pyo3.rs/main/free-threading):**
  - PyO3 **0.23–0.27**: modules defaulted to `gil_used = true`; free-threaded support required explicit **opt-in** `#[pymodule(gil_used = false)]` (or `PyModule::gil_used`).
  - **Since PyO3 0.28: default is thread-safe** (`gil_used = false` assumed); modules with `unsafe` GIL-era assumptions must **opt out** with `#[pymodule(gil_used = true)]` until audited.
  - Guidance applies to both builds: use `Python::detach` (formerly `Python::allow_threads`) for long non-Python work ✅V [21].

### PyO3 breaking-change timeline (all ✅V unless noted)
| Version | Change |
|---|---|
| **0.21** | "Bound" smart-pointer API introduced; becomes the primary interface ([migration guide text ✅V [14]]) |
| **0.22→0.23** | **`gil-refs` feature removed** — GIL-Refs API fully replaced by Bound API; `_bound`-suffix names freed (`PyTuple::new_bound` → `PyTuple::new`, old name deprecated); `FromPyObject` gains input lifetime, `extract` takes `Borrowed<'a,'py,PyAny>` ✅V [14] |
| **0.24** | Free-threading guide with `gil_used` opt-in model (page exists ✅V [6]; v0.24 rendering verified ✅V [26]) |
| **0.26** | `PyObject` alias (`Py<PyAny>`) **deprecated**; optional `bytes::Bytes` conversion support ✅V [27] |
| **0.27** | `PyCapsule` `.name()`/`.reference()` lifetime API **changed without deprecation**; capsule-name checking encouraged ✅V [28] |
| **0.28+ ("main" docs)** | **`Python::attach` replaces `Python::with_gil`; `Python::detach` replaces `Python::allow_threads`** (attach/detach = free-threaded CPython terminology, coined in PyO3 issue #3987, Oct 2024 ✅V [29]); `IntoPyObject`/`FromPyObject` derives replace `IntoPy`/`ToPyObject`; **`#[pyclass]` types must be `Sync`**; `GILPool` removed ✅V [14][21][6] |

An AI framework generating PyO3 code must emit the **current API generation** (Bound API + attach/detach + derive macros) and pin a documented target version, because tutorials older than 0.23 (and even 0.23–0.27 free-threading defaults) produce code that is deprecated or wrong today.

---

## (e) Performance gotchas — when a Rust rewrite helps and when it doesn't

1. **GIL release is mandatory for CPU-parallel Rust work.** Verified PyO3 guidance: wrap worker-thread code in `Python::detach` (ex-`allow_threads`) — otherwise rayon/threads **deadlock or gain nothing**: "you will not see any speedups … using `rayon` to parallelize code that acquires and holds the GIL" ✅V [21]. Spawning GIL-requiring workers while holding the GIL can deadlock the interpreter ✅V [21].
2. **On free-threaded builds, detach is still correct** — long-running non-Python work should still detach so other threads can attach ✅V [26].
3. **I/O-bound code: rewriting does NOT help.** Python's stdlib already releases the GIL around blocking I/O, so "the GIL has no consequence for performance of I/O-bound servers" (S.E. discussion ✅V [30]); practitioner post-rewrite retrospective: Rust rewrites pay off only when "CPU or memory bound (not I/O bound)" ✅V [31]. Reddit case discussion of AI-gateway migration notes the same ✅V [32].
4. **Boundary crossing costs.** Every call crosses Python↔Rust with argument conversion (`FromPyObject`/`IntoPyObject`) and exception marshalling; the batch-the-loop rule: keep inner loops in Rust, cross the boundary once per batch, not per element. (Cost magnitudes **⚠️U** — no authoritative number found this run; design guidance follows from PyO3's conversion model ✅V [14][3].)
5. **`#[pyclass]` mutability is borrow-checked at runtime** (RefCell-like): concurrent/aliasing mutation raises Python exceptions instead of UB ✅V [26] — a silent performance trap if Python code holds references across calls.
6. **Async bridging cost.** asyncio↔tokio translation via `pyo3-async-runtimes`/PyO3 async functions copies futures across event loops ✅V [13]; for I/O-heavy services this overhead plus re-plumbing rarely beats staying in Python (see #3). Exact overhead numbers **⚠️U**.
7. **Profiling-first port selection.** Precedent successes are CPU-bound hot spots: **pydantic-core** ("Core validation logic for pydantic written in rust" ✅V [33]) and **orjson** (PyO3-based; 3.13.0, supports dataclasses/datetimes/numpy ✅V [34]) exist to accelerate hot paths while keeping Python APIs; **ruff** ("An extremely fast Python linter and code formatter, written in Rust" ✅V [35]) shows CLI/dev-tools gains of 10–100x (Charlie Marsh PyCon talk ✅V [36]). pola.rs's migration-strategies post (Aug 2026) is directly applicable to AI-driven translation: **make input/output fixtures the spec, scope the model to one segment at a time, give Polars-specific API guidance** ✅V [16] — i.e., profile → pick hot segment → golden tests → translate bounded scope.

---

## (f) "Knowledge pack" — references an AI framework should embed as context

**Tier 1 (embed verbatim / RAG-index — authoritative, verified):**
1. **PyO3 user guide**: error handling [3], parallelism/detach [21], free-threading [6], migration guide (0.21→current API generations) [14] — pyo3.rs.
2. **Maturin bindings page** — abi3/abi3t/version-specific wheel decision tree [20].
3. **corrode.dev Python→Rust migration guide** [1] — dense Python↔Rust concept mapping (classes→structs/traits, Result/Option, enums-for-state).
4. **Polars "Coming from Pandas" guide** [15] + pola.rs migration-strategies post [16] — the model for library-level API translation guidance.
5. **PEP 703** [22], **PEP 779** [23], CPython 3.14 free-threaded notes [24], CPython free-threading howto (GitHub source) [25].
6. **BurntSushi "Error Handling in Rust"** [7] + thiserror/anyhow docs (crates: active 2026 ✅V [12]) — the exceptions→Result idiom.
7. **The Rust Book**: traits & trait objects / OO chapters [8] (canonical ◐C).

**Tier 2 (link, sample on demand):**
8. **TLBORM — The Little Book of Rust Macros** [10] (metaclass/reflection→macro mappings); rust-unofficial patterns (builder) [11]; programming-idioms.org [5]; rustpython.com cheatsheet [4].
9. **Exemplar codebases for real PyO3 mappings**: `pydantic/pydantic-core` [33] (serde↔pydantic, PyErr, Bound API), `ijl/orjson` [34] (PyO3, dataclasses/datetime/numpy conversions), `astral-sh/ruff` [35] (parser-in-Rust, Python-facing tooling), polars repo [15]. These answer "how do mature projects map exceptions/GIL/pyclasses" better than any prose guide.
10. **crates.io/lib.rs metadata** [12][17] for the substitution table — re-verify maintenance dates at translation time; the stale-`celery` finding shows why.

**Do NOT rely on:** pre-0.23 PyO3 tutorials (GIL-Refs API), pre-3.15 free-threading ABI assumptions, `pyo3-asyncio` (use maintained `pyo3-async-runtimes` [13]), and any claim of Celery wire compatibility in Rust ⚠️U.

---

## Sources

| # | Source | Status |
|---|---|---|
| 1 | corrode.dev — *Migrating from Python to Rust* — https://corrode.dev/learn/migration-guides/python-to-rust | ✅V |
| 2 | r/rust — *Why no duck typing?* — https://www.reddit.com/r/rust/comments/1g8mdz8/why_no_duck_typing | ✅V |
| 3 | PyO3 user guide — Error handling — https://pyo3.rs/main/function/error-handling | ✅V |
| 4 | rustpython.com — *Rust Cheat Sheet for Python Developers* — https://www.rustpython.com/cheatsheet | ✅V |
| 5 | programming-idioms.org — Python/Rust cheat sheet | ✅V |
| 6 | PyO3 user guide — Supporting Free-Threaded Python — https://pyo3.rs/main/free-threading (v0.24 rendering: https://pyo3.rs/v0.24.0/free-threading.html) | ✅V |
| 7 | BurntSushi (Andrew Gallant) — *Error Handling in Rust* — https://blog.burntsushi.net/rust-error-handling/ | ✅V |
| 8 | The Rust Programming Language (doc.rust-lang.org/book/) — traits / trait objects / OOP chapters | ◐C |
| 9 | joshleeb — *Rust Traits and Trait Objects* — https://joshleeb.com/posts/rust-traits-and-trait-objects | ✅V |
| 10 | The Little Book of Rust Macros — https://github.com/madsmtm/tlborm (rendered: https://veykril.github.io/tlborm/) | ✅V |
| 11 | rust-unofficial patterns — Builder — https://rust-unofficial.github.io/patterns/ | ◐C |
| 12 | crates.io API (garde*, validator 2026-07-27, apalis 2026-10-12, celery **stale 2023-09-25**, rusty-sidekiq 2026-09-28, rstest 2026-10-18, proptest 2026-10-10, derive_more 2025-12-22, ndarray 2026-01-10, nalgebra 2026-05-24, polars 2026-08-06, sqlx 2026-05-21, sea-orm 2026-09-27, axum 2026-04-14, aws-sdk-s3 2026-10-22, tracing 2025-12-18, reqwest 2026-09-08, inventory 2026-10-07, pyo3 2026-10-14, thiserror 2026-09-23, anyhow 2026-07-18, tokio 2026-10-21) — https://crates.io/api/v1/crates/<name> | ✅V |
| 13 | pyo3-async-runtimes (maintained asyncio↔tokio bridge, 0.24) — https://crates.io/crates/pyo3-async-runtimes | ✅V |
| 14 | PyO3 user guide — Appendix A: Migration guide — https://pyo3.rs/main/migration | ✅V |
| 15 | Polars user guide — *Coming from Pandas* — https://docs.pola.rs/user-guide/migration/pandas/ | ✅V |
| 16 | pola.rs — *Migration strategies for going from pandas to Polars* (2026-08-06) — https://pola.rs/posts/pandas-to-polars-migration-strategies | ✅V |
| 17 | lib.rs — garde 0.23.0 (2026-05-23), ~1.14M dl/mo — https://lib.rs/crates/garde | ✅V |
| 18 | Henkel blog — *Data Validation & Type Safety Pt 2: Rust Perspective for Python Developers* — https://medium.com/henkel-data-and-analytics/data-validation-and-type-safety-part-2-rust-perspective-for-python-developers-01a2dc62d039 | ✅V |
| 19 | CeleRS (unproven Celery-compatible Rust lib) — https://github.com/cool-japan/celers | ⚠️U |
| 20 | Maturin User Guide — Bindings (abi3/abi3t/cp314t) — https://www.maturin.rs/bindings.html | ✅V |
| 21 | PyO3 user guide — Parallelism (`Python::detach`, `gil_used`, PEP 384/803) — https://pyo3.rs/main/parallelism | ✅V |
| 22 | PEP 703 — Making the Global Interpreter Lock Optional — https://peps.python.org/pep-0703/ | ✅V |
| 23 | PEP 779 — Criteria for supported status for free-threaded Python — https://peps.python.org/pep-0779/ | ✅V |
| 24 | Python 3.14 free-threaded mode improvements (CPython release-notes mirror) — https://gdevops.frama.io/python/versions/_sources/3.14.0/free-threaded-mode-improvements/free-threaded-mode-improvements.rst.txt | ✅V (mirror of CPython whatsnew; canonical: docs.python.org/3.14) |
| 25 | CPython — free-threading HOWTO source — https://github.com/python/cpython/blob/main/Doc/howto/free-threading-python.rst (rendered: docs.python.org/3.13/howto/free_threading.html — extraction blocked) | ✅V source / ◐C rendering |
| 26 | PyO3 v0.24 free-threading guide — https://pyo3.rs/v0.24.0/free-threading.html | ✅V |
| 27 | PyO3 0.26.0 release notes — https://github.com/PyO3/pyo3/releases/tag/v0.26.0 | ✅V |
| 28 | PyO3 0.27.0 release notes — https://github.com/PyO3/pyo3/releases/tag/v0.27.0 | ✅V |
| 29 | PyO3 issue #3987 — attach/detach terminology — https://github.com/PyO3/pyo3/issues/3987 | ✅V |
| 30 | Software Engineering SE — *Why was Python written with the GIL?* (GIL released around blocking I/O) — https://softwareengineering.stackexchange.com/questions/186889 | ✅V |
| 31 | *I Rewrote Our Python API in Rust: The Performance Gains Were Embarrassing* — https://medium.com/@sohail_saifi/i-rewrote-our-python-api-in-rust-the-performance-gains-were-embarrassing-09de52049a4c | ✅V |
| 32 | r/rust — *Why migrate an AI gateway from Python to Rust* — https://www.reddit.com/r/rust/comments/1uhzbkq | ✅V |
| 33 | pydantic/pydantic-core — https://github.com/pydantic/pydantic-core | ✅V |
| 34 | ijl/orjson (Cargo.toml: PyO3-based, v3.13.0) — https://github.com/ijl/orjson | ✅V |
| 35 | astral-sh/ruff — https://github.com/astral-sh/ruff · docs: https://docs.astral.sh/ruff/ | ✅V repo / ◐C docs |
| 36 | Charlie Marsh — *Ruff: An Extremely Fast Python Linter…* (PyCon talk) — https://www.youtube.com/watch?v=r1EZ3GXuwBA | ✅V |
| 37 | "Snek in Rust" (Carl Meyer, pypackers.org) — https://www.pypackers.org/p/snek-in-rust | ⚠️U (extraction blocked twice this run; treat as secondary) |
| 38 | astral.sh/blog/ruff announcement | ⚠️U (extraction failed; use [35]/[36] instead) |

*Legend: ✅V = verified live 2026-10-25 · ◐C = canonical stable reference, not re-fetched · ⚠️U = unverified/flagged.*
