# Case Studies: AI-Assisted Python → Rust Migrations

*2026-10-25. Every number below carries its source URL. Claims that could not be verified are explicitly marked **UNVERIFIED**.*

---

## Case 1 — Cloudflare: Python Workers (CPython-in-WebAssembly embedded in the Rust `workerd` runtime)

### What
In April 2024, Cloudflare launched Python Workers in open beta. This was **not** a Python→Rust code translation: Cloudflare embedded **CPython (via Pyodide, compiled to WebAssembly)** directly into `workerd`, their open-source, Rust-based Workers runtime — "we have directly integrated a Python implementation into workerd, the open-source Workers runtime. All bindings, including bindings to Vectorize, Workers AI, R2, Durable Objects, and more are supported on day one." ([blog.cloudflare.com/python-workers](https://blog.cloudflare.com/python-workers))

> **"PyPy-based implementation migrated to Rust": UNVERIFIED.** The April 2024 post contains **zero** occurrences of "PyPy", and site-restricted searches of `blog.cloudflare.com` surfaced no "PyPy → Rust" migration post. The verifiable story is: Pyodide/CPython chosen over other WASM Python options and integrated into a Rust runtime with custom Rust-side shims (e.g., the socket bridge below).

### Why
- **Deployment/footprint economics, not raw speed:** "Pyodide struck us as the perfect fit for Workers… the core interpreter and each native Python module [built] as separate WebAssembly modules, dynamically linked at runtime. This allows the code footprint… to be shared among all Workers running on the same machine… where we often run thousands of Workers per machine." (same URL)
- **Ecosystem compatibility:** run FastAPI, Langchain, Numpy unmodified; avoid Python becoming a "second-class citizen" (FFI to JS APIs via Pyodide's `from js import …`).

### How (tools & techniques)
- **Emscripten dynamic linking** selected over WASI: "only Emscripten currently supports dynamic linking… implementations of dlopen and dlsym… WASI does not yet support the dlopen/dlsym dynamic linking abstractions used by CPython." (same URL)
- **Per-Worker virtual filesystem** mirroring a Python distribution so packages are shared across isolates; **pre-warmed isolate snapshots** so "a Python Worker [behaves] the same as a JavaScript Worker."
- **Rust/JS bridging at the syscall level:** the GA follow-up post explains TCP sockets for DB drivers — "we implemented socket system calls using the Workers connect API… It translates standard Python socket operations like opening a connection and reading bytes into the corresponding JavaScript calls used by the Workers runtime." ([blog.cloudflare.com/python-workers-ga](https://blog.cloudflare.com/python-workers-ga))
- **AI/LLM usage: not documented** in either primary post — **UNVERIFIED / no evidence** the migration was AI-assisted.

### Results (quantified)
- No LOC / engineer-month / benchmark figures were published for this effort (**none found**). Outcome claims are qualitative: day-one bindings for all platform services, unmodified PyPI packages (a subset), later GA with sockets + Hyperdrive integration, and manual WASM cross-compilation of packages with C/C++/Rust extensions before a standardized path existed (GA post).

### Lessons for a migration framework
1. When you can't port the language, port the **runtime seam**: embedding an interpreter inside a Rust host (via C-API shims at syscall level) can deliver "language parity" faster than translating code.
2. Footprint-sharing (shared WASM modules across isolates) can outweigh per-request performance — decide based on the deployment model.
3. Choose your WASM toolchain by linking model (Emscripten's dlopen vs WASI) early; it constrains everything downstream.

---

## Case 2 — IBM Research: `watsonx-code-migration-poc` (Python→Rust with Granite) — **UNVERIFIED**

### What the brief claimed vs. what was found
- The GitHub repository **https://github.com/IBM/watsonx-code-migration-poc returns HTTP 404** ("This is not the web page you are looking for" — verified by direct full-page extraction on 2026-10-25).
- Searches for `"watsonx-code-migration-poc"`, `"CO2EmissionsCalculation"` + Python/Rust/watsonx, and "IBM watsonx Granite Java/Python→Rust migration pipeline" returned **no matching repo, blog, paper, or tutorial**.
- Therefore the brief's specifics — CO2EmissionsCalculation Python code, parser choice (javalang vs ANTLR), validation method, human-in-the-loop handling, cost/speedup figures — are **UNVERIFIED**. **Do not cite them.**

### What *is* verifiable about IBM in this space
- IBM open-sourced **Granite code models (3B–34B, Apache 2.0)** targeting "application modernization and code generation in 116 programming languages"; the 20B base model "powers the IBM watsonx Code Assistant." ([cioinfluence.com, THINK 2024 coverage](https://cioinfluence.com/machine-learning/ibm-introduces-new-updates-to-watsonx-platform-at-think-2024); [ibm newsroom, Granite 3.0, 2024-10-21](https://newsroom.ibm.com/2024-10-21-ibm-introduces-granite-3-0-high-performing-ai-models-built-for-business))
- IBM's watsonx Code Assistant pairs LLM code generation with generated unit tests to mitigate hallucination ("we generate code, then we generate unit test case, then we test that code… we cannot completely avoid… possibility of LLM [hallucination]"). ([IBM Developer YouTube session, 2025-06-04](https://www.youtube.com/watch?v=v3wsm5Jcs6s))

### Lessons
- Even for a vendor famous for migration tooling, the *published, verifiable* artifact may be the **pattern** (LLM + generated tests + human review), not a public repo. Framework design should treat "repo disappeared" as the normal case — archive primary evidence at collection time.

---

## Case 3 — Presidio: "HAI" Human+AI modernization — **partially verified; Rust-repo premise UNVERIFIED**

### What the brief claimed vs. what was found
- **Premise "Apex→Rust and Perl→Go/Rust migration repos" under presidio-oss: UNVERIFIED.** The [presidio-oss GitHub org lists exactly 11 repositories](https://github.com/orgs/presidio-oss/repositories?type=all): hai-build-codegen, specif-ai, hai-guardrails, factifai-agent-suite, factifai-mcp-server, specifai-mcp-server, cline-based-code-generator, cor-matrix, factif-ai, hai-build, aider-based-code-generator. **None** is an Apex→Rust or Perl→Go/Rust migration repo, and a `q=rust` search returns "0 repositories" ([verified](https://github.com/orgs/presidio-oss/repositories?q=rust)).
- **"~0.2 cents per line" cost claim: UNVERIFIED** — multiple searches ("per line" + Presidio/modernization; "cents per line" + AI migration) found no Presidio source.

### What *is* verifiable
- **HAI Build** — Presidio's open-source human+AI SDLC tooling: code indexing with FAISS, auto-generated code context, task execution via LLMs; Code Generator exists in **Cline-based** and **Aider-based (archived)** variants; supports GPT-4o/-4o-mini and Claude 3.5 Sonnet via Bedrock. ([github.com/presidio-oss/hai-build](https://github.com/presidio-oss/hai-build))
- **HAI Modernize** (commercial): "intelligent AI agents to coordinate across multiple systems, orchestrate complex workflows, understand context, and adapt to changes," with generative + traditional AI and human domain expertise, across clouds/Kubernetes for regulated environments. ([AWS Partner GenAI Solutions profile](https://awspartnergenaisolutions.cio.com/transforming-gen-ai-ideas-into-real-business-value/presidio-and-aws-powering-rapid-reliable-modernization-with-generative-ai))
- **Quantified claims from Presidio's own webinar** ("Unlock the Future of Your Legacy Applications with Human + AI (HAI)!", [YouTube](https://www.youtube.com/watch?v=GN1fOuP1xqE)):
  - A client's manual estimate: migrating **1,000 of 6,000 modules** would take "**about 24 months**" and "**about 4 million bucks**" — leaving 5,000 modules untouched. "Using Presidio's HAI assistance, we're able to get that done in about **3 to 6 months** at a fraction of the cost."
  - "From a cost perspective we've seen that in year one teams can save about **25 to 40%** in cost savings."
  - Agents "can analyze… millions of lines of code in **minutes**" (dependency mapping, tech-debt identification, initial modern code generation).
- Note: a much-cited Salesforce migration (Apex→**Java**, 3,537 files in 4 months vs. a 2-year estimate, leaf-to-root ordering) circulates in the ecosystem but is a **third-party case, not Presidio** ([buildmvpfast.com](https://www.buildmvpfast.com/blog/ai-code-migration-legacy-modernization-salesforce-2026)).

### Lessons
- The verifiable Presidio model is **agents for discovery/analysis + humans for architecture and sign-off**, sold on timeline compression (24→3–6 months) rather than per-line pricing (the per-line figure we could not substantiate).
- "Harness" framing (agents + tests + human gates) matches what other verified cases actually built — see Cross-case patterns.

---

## Case 4 — Zed Industries: extensions in Rust (a *decision against* JS/Python extension APIs) + MCP context servers

### What
Zed's extension system compiles extensions to **WebAssembly via the Component Model (WIT + `wit_bindgen`)**. Procedural parts are written in **Rust** against the `zed_extension_api` crate; Tree-sitter grammars are also compiled to Wasm. ([zed.dev/blog/zed-decoded-extensions](https://zed.dev/blog/zed-decoded-extensions), 2024-10-21)

### Why (direct quote — the anti-JS/Python decision)
> "When we were first playing around with the extension API, we were initially going in a JavaScript direction. We were going to embed a JavaScript runtime, looking at things like Deno's V8 bindings. But while Max and I were working on it, we realized we don't really want to write JavaScript. We want to write our extensions in Rust." — Thorsten Ball quoting the team (same URL)

They note the earlier Rust→Wasm attempt "fizzled out" because the **WebAssembly Component Model only recently became usable**. This is a **deliberate design substitution** (Rust/Wasm instead of embedded JS/Python), not a line-by-line migration.

### How
- Extensions compiled to `extension.wasm` ahead-of-time by Zed; installing an extension downloads Wasm + Tree-sitter queries, "you don't compile Rust code" at install time (same URL).
- Scope discipline: "most extensions will work properly without any Rust code present… only language server, context server and debugger extensions require the presence of custom Rust." ([zed.dev/docs/extensions/developing-extensions](https://zed.dev/docs/extensions/developing-extensions))
- **MCP context servers** (e.g., the PostgreSQL context server that injects DB schema into the assistant) are shipped as these Rust extension types, built on Anthropic's Model Context Protocol. ([zed.dev/blog/mcp](https://zed.dev/blog/mcp))

### Results (quantified)
- None published for the extension-API work; ecosystem stat from third-party coverage: ~**1,000 extensions** at Zed 1.0 vs 100,000+ in the VS Code marketplace ([devclass.com, 2026-05-07](https://www.devclass.com/software/2026/05/07/zed-team-releases-version-10-of-rust-built-editor/5234770)).
- **AI-assisted port stories / 2025 "agentic migration" posts: NOT FOUND** (searched zed.dev). Zed does have an "Agentic Engineering" blog category (e.g., "The Case for Software Craftsmanship in the Era of Vibes", 2025-06-12), but no verified Python/JS→Rust AI-port write-up — **UNVERIFIED** as of this research.

### Lessons
- Choosing Rust+Wasm for a plugin API buys **sandboxing + ahead-of-time compilation + versioned API compatibility** (manifest records the API version the extension compiled against); the cost is ecosystem size.
- "Right place, right time" — component-model maturity gated the design; framework plans should include a toolchain-readiness checkpoint.

---

## Case 5 — Additional verified Python→Rust cases

### 5.1 Volatility3 → Rust ("Rusting Volatility") — **the flagship quantified AI-assisted Python→Rust case**

- **What:** LLM-assisted migration of Volatility3's Linux plugin stack (memory forensics) from Python to Rust. **38 plugins, 29,028 LOC across 8 Rust crates**, preserving Volatility3's plugin architecture. ([DFRWS USA 2026 camera-ready PDF](https://dfrws.org/wp-content/uploads/2026/04/DFRWS_USA_2026_Camera_Ready_Paper_81.pdf); [presentation page](https://dfrws.org/presentation/rusting-volatility-accelerating-memory-forensics-through-llm-assisted-cross-language-migration)) — Gharaibeh & Baggili, LSU BiT Lab.
- **Why:** "practical workflows are often constrained by the runtime costs of Python-based tooling"; hypothesis that compiled reimplementation improves robustness and throughput.
- **How:**
  - Two-model division of labor: **GPT-5.2-Codex** (~400M tokens, **$812**) for iterative plugin implementation; **Claude Opus 4.5** (~500M tokens, **$2,150**) for "planning & skeleton building", structural refinement, debugging, parity-focused review. **Total ≈ 900M tokens, ~$2,962.**
  - **TDD-style loop:** "implement a plugin, run against representative samples, compare output with Python, diagnose mismatches, and iterate until equivalence or *explainable divergence*."
  - **Hash-based parity validation** classifying each plugin-sample pair as *Equivalent / Divergent / Incomparable* against the Python reference — systematic functional-equivalence checking.
- **Results (quantified, all from the PDF above):**
  - **68 hours** active development effort (measured from file-modification timestamps) → **~427 LOC/hour** including agent time.
  - **2.50× median speedup** across **834 analyzed benchmark runs**; **up to 210×** for structure-traversal plugins.
  - **Reliability flip:** Rust completed all **417** analyzed plugin-sample executions; Python **failed 47 (11.3%)** with runtime/symbol-compatibility errors.
  - **Exact output equivalence: 68.9%** among comparable executions (rest = explainable divergence/incomparable).
- **Lessons:** (a) an explicit three-way parity taxonomy converts "LLM correctness" into measurable engineering; (b) two specialized model roles beat one; (c) even at ~69% exact parity, the migration shipped because divergences were *explained*, not ignored.

### 5.2 Pydantic v1 (Python/Cython) → pydantic-core (Rust)

- **What:** Pydantic v2's validation engine **pydantic-core** was rebuilt in **Rust with PyO3**; the `pydantic` package itself returned to **pure Python** ("Pydantic V1.X is a pure python code base but is compiled with cython… Since the 'hot' code is moved to pydantic-core, pydantic itself can go back to being a pure python package"). ([docs.pydantic.dev blog](https://docs.pydantic.dev/latest/blog/pydantic-v2/))
- **Why (official motivation):** "1. Performance… 2. Recursion and code separation – with no stack and little-to-no overhead for extra function calls, Rust allows pydantic-core to be implemented as a tree of small validators… 3. Safety and complexity… it should minimise bugs." (same URL)
- **How:** incremental, hot-path-only extraction; user-facing Python API unchanged ("Nobody had to change their imports. They ran `pip install --upgrade pydantic`…" — third-party summary at [birjob.com](https://www.birjob.com/blog/rust-eating-python-tooling-good-thing)); work began ~early 2022 (Colvin: "at the beginning of 2022, I started working full-time on Pydantic, rebuilding the core in Rust. There's an amazing library called Py03…" — [gregorykapfhammer.com SE Radio recap](https://www.gregorykapfhammer.com/blog/se-radio-samuel-colvin-pydantic-ecosystem)).
- **Results (quantified):** "pydantic V2 is between **4x and 50x faster** than pydantic V1.9.1. In general… about **17x faster** than V1" on common-field models ([docs.pydantic.dev](https://docs.pydantic.dev/latest/blog/pydantic-v2/); the 17x figure was already public pre-release, [Talk Python #376, 2022-08-04](https://talkpython.fm/episodes/show/376/pydantic-v2-the-plan)).
- **Lessons:** the "keep the Python API, rewrite the hot core in Rust/PyO3" pattern is the lowest-friction Python→Rust migration for libraries; it also *simplifies* the Python side (drops Cython).

### 5.3 orjson (JSON serialization in Rust)

- **What/How:** orjson ships a pure-Rust extension built with **maturin** ("requires at least Rust 1.95, a C compiler, and the maturin build tool") exposing a drop-in-JSON API for Python. ([github.com/ijl/orjson](https://github.com/ijl/orjson))
- **Results (quantified, README benchmark tables, Python 3.11.10):** serialization of `citm_catalog.json`: orjson **0.3 ms** vs stdlib json **3 ms** (**11.8×**); `canada.json` serialization **2.5 ms vs 29.8 ms** (**11.9×**); deserialization 3.1×–6× faster. (same URL)
- **The widely-repeated claim that orjson 1.x was Cython and was rewritten in Rust for 2.0 (2019): UNVERIFIED from a primary source** (PyPI 2.0.x pages do show Rust release files — [pypi.org/project/orjson/2.0.1](https://pypi.org/project/orjson/2.0.1); secondary echo on [Reddit](https://www.reddit.com/r/Python/comments/1ii64gp/how_rust_is_quietly_taking_over_the_python)). Treat as plausible-but-uncited.
- **Lessons:** serializers/parsers are ideal Rust targets — tight hot loop, easy golden-file equivalence testing, no GIL contention.

### Checked, not applicable
- **CodSpeed blog** ([codspeed.io/blog](https://codspeed.io/blog)): no Python→Rust migration post found; their content is benchmarking methodology (useful for the *measurement* layer of a migration framework, not a case study).
- **uutils/coreutils:** skipped per brief (C, not Python).

---

## Case 6 — Adjacent (non-Python) agentic rewrites with hard numbers

### 6.1 Microsoft: Copilot runtime ported TypeScript→Rust by AI agents

([The Register, 2026-09-18](https://www.theregister.com/devops/2026/09/18/microsoft-agentically-ports-copilot-runtime-to-rust-for-120k/5297549))
- **Scale & cadence:** agents converted **430,000 lines of TypeScript into 800,000 lines of production Rust**, module-by-module across **135 releases over 14.5 weeks** (~**1.3 port PRs/day**).
- **Cost:** "**about $120,000 in AI token usage plus about three weeks of a developer's time**."
- **Performance:** 1,000 one-turn session lifecycles, shared client, 100 concurrent pipelines: TypeScript **7.55/s** → Rust **120/s** = **15.9× speedup**; memory for a 10-client agent swarm **1,383 MB → 126 MB**.
- **Quality tax:** "managers also had to grapple with **a few dozen regressions**… pointing to AI's ongoing challenges in understanding Rust."

### 6.2 Reverse-direction control case: Codex CLI Rust→Python, LLM-assisted

([arXiv 2604.11518](https://arxiv.org/html/2604.11518v1))
- LLM translated a production **648K LOC / 65-crate Rust** codebase into a **41K LOC / 28-module Python** port, using **public agent benchmarks as the objective function** ("benchmark-as-objective-function… language-agnostic… applicable to any translation where functional equivalence can be measured through automated evaluation").
- **Parity results:** SWE-bench Verified **59/80 (73.8%)** for the Python port vs **56/80 (70.0%)** for the Rust original; Terminal-Bench **42.5% vs 47.5%**.
- **Why it matters here:** demonstrates the same LLM+harness methodology works in *either* direction — target-language choice should be workload-driven, not ideological ("Python's expressiveness advantages substantially outweigh Rust's performance benefits" for their API-latency-bound agent).

---

## Cross-case patterns

1. **Interface stability is the invariant; the engine is the variable.** Every successful case kept a stable API surface while swapping the implementation beneath it — pydantic v2 (same imports), orjson (stdlib-JSON-shaped API), Cloudflare (Workers API unchanged for Python), Microsoft (module-by-module behind the runtime interface). Full-rewrite-at-once appears nowhere.

2. **The "harness" is the migration framework.** Nobody trusted raw LLM output. The recurring harness = (a) small, independently verifiable units (plugin, module, class), (b) an automated equivalence oracle — hash-based output comparison (Volatility3), generated unit tests (IBM watsonx Code Assistant pattern), or public benchmarks as fitness function (arXiv Codex CLI), (c) a triage rule for failures ("Equivalent / Divergent / Incomparable"; "explainable divergence").

3. **Token cost is the small line item; integration/regression cost is the real one.** Volatility3: **$2,962** tokens + **68 human-hours** for 29K LOC. Microsoft: **$120K** tokens + ~3 weeks dev for 430K→800K LOC — yet still "a few dozen regressions." Budget review/QA time as first-class; expect ~10% hard failures to catch (Volatility3's Python side failed 11.3% of runs — sometimes the *rewrite* is more reliable than the original).

4. **Role-separated model stacks.** Volatility3 explicitly paired a bulk-coder model (GPT-5.2-Codex) with a planning/reviewer model (Claude Opus 4.5). Presidio's HAI separates agent discovery/analysis from human architecture/sign-off. Framework implication: make "planner / translator / reviewer / test-oracle" first-class roles.

5. **Order-of-work: leaf-first, oracle-backed.** Volatility3 did plugin-by-plugin against the Python reference; the widely-cited Salesforce Apex case used leaf-to-root ordering after discovering isolated file translation breaks on shared dependencies ([buildmvpfast](https://www.buildmvpfast.com/blog/ai-code-migration-legacy-modernization-salesforce-2026)). Dependency-graph-aware batching beats file-by-file prompting.

6. **Speedups concentrate in interpreter-overhead-bound paths — and are wildly bimodal.** 2.5× median but 210× outliers (Volatility3); 15.9× (MS runtime workload); ~17× typical (pydantic); ~12× (orjson serialization). Median claims hide that traversal/serialization hot paths dominate; profile before choosing what to port.

7. **Sometimes don't migrate — embed or reverse.** Cloudflare embedded CPython in the Rust runtime instead of porting code; Codex CLI moved Rust→Python for extensibility. A credible migration framework includes an explicit "embed / wrap / stay" decision gate with footprint, latency, and ecosystem criteria.

8. **Evidence hygiene is a first-class problem.** In this very study, two of the four brief's anchor artifacts were unverifiable (IBM repo 404; Presidio Rust repos nonexistent) — published migration stories rot quickly. A framework should archive claims (URL + quote + date) alongside the migration artifacts they justify.

---

## Sources

1. https://blog.cloudflare.com/python-workers — Cloudflare, "Bringing Python to Workers using Pyodide and WebAssembly" (Apr 2024)
2. https://blog.cloudflare.com/python-workers-ga — Cloudflare, "Python Workers are now generally available"
3. https://github.com/IBM/watsonx-code-migration-poc — GitHub 404 (verified 2026-10-25)
4. https://cioinfluence.com/machine-learning/ibm-introduces-new-updates-to-watsonx-platform-at-think-2024 — Granite code models 3B–34B, 116 languages
5. https://newsroom.ibm.com/2024-10-21-ibm-introduces-granite-3-0-high-performing-ai-models-built-for-business — IBM Granite 3.0 announcement
6. https://www.youtube.com/watch?v=v3wsm5Jcs6s — IBM Developer, "Boost Developer Productivity with IBM watsonx Code Assistant" (2025-06-04)
7. https://github.com/orgs/presidio-oss/repositories?type=all — Presidio INC org repo list (11 repos, verified 2026-10-25)
8. https://github.com/presidio-oss/hai-build — Presidio HAI Build
9. https://awspartnergenaisolutions.cio.com/transforming-gen-ai-ideas-into-real-business-value/presidio-and-aws-powering-rapid-reliable-modernization-with-generative-ai — Presidio + AWS "HAI Modernize"
10. https://www.youtube.com/watch?v=GN1fOuP1xqE — Presidio webinar (24 mo/$4M → 3–6 mo; 25–40% year-1 savings)
11. https://zed.dev/blog/zed-decoded-extensions — "Life of a Zed Extension: Rust, WIT, Wasm" (2024-10-21)
12. https://zed.dev/docs/extensions/developing-extensions — Zed extension docs (Rust/Wasm requirements)
13. https://zed.dev/blog/mcp — "The Context Outside the Code" (MCP context servers)
14. https://www.devclass.com/software/2026/05/07/zed-team-releases-version-10-of-rust-built-editor/5234770 — Zed 1.0 coverage
15. https://dfrws.org/wp-content/uploads/2026/04/DFRWS_USA_2026_Camera_Ready_Paper_81.pdf — "Rusting Volatility" camera-ready paper (DFRWS USA 2026)
16. https://dfrws.org/presentation/rusting-volatility-accelerating-memory-forensics-through-llm-assisted-cross-language-migration — DFRWS presentation page
17. https://docs.pydantic.dev/latest/blog/pydantic-v2/ — "Introducing Pydantic V2" (official motivation & benchmarks)
18. https://talkpython.fm/episodes/show/376/pydantic-v2-the-plan — Talk Python #376 (17x pre-release benchmark)
19. https://www.gregorykapfhammer.com/blog/se-radio-samuel-colvin-pydantic-ecosystem — Colvin SE Radio quotes (PyO3, 2022 start)
20. https://www.birjob.com/blog/rust-eating-python-tooling-good-thing — "Rust is eating Python's tooling" (secondary survey)
21. https://github.com/ijl/orjson — orjson README (Rust/maturin, benchmark tables)
22. https://pypi.org/project/orjson/2.0.1 — orjson 2.0.1 PyPI page (Rust release files)
23. https://www.reddit.com/r/Python/comments/1ii64gp/how_rust_is_quietly_taking_over_the_python — secondary: orjson Rust-since-v2.0 claim (uncited primary)
24. https://codspeed.io/blog — CodSpeed blog index (checked; no Python→Rust migration post)
25. https://www.theregister.com/devops/2026/09/18/microsoft-agentically-ports-copilot-runtime-to-rust-for-120k/5297549 — Microsoft Copilot runtime TS→Rust agentic port
26. https://arxiv.org/html/2604.11518v1 — "From Translation to Superset" (Codex CLI Rust→Python, benchmark-driven)
27. https://www.buildmvpfast.com/blog/ai-code-migration-legacy-modernization-salesforce-2026 — third-party Salesforce Apex→Java case (context only)

### UNVERIFIED items register
- Cloudflare "PyPy-based implementation migrated to Rust" — no PyPy mention in the primary post (full-text checked) and no supporting post found.
- Any AI/LLM usage in Cloudflare's Python Workers work — not documented in primary sources.
- IBM `watsonx-code-migration-poc` specifics (CO2EmissionsCalculation, javalang/ANTLR pipeline, equivalence method, human-in-the-loop stats, cost/speedup) — repo 404; no corroborating source.
- Presidio Apex→Rust / Perl→Go-Rust repos and "~0.2 cents per line" — no such repos in org; no cost-per-line source found.
- Zed 2025 "agentic migration" posts and AI-assisted Python/JS→Rust port stories — not found.
- orjson "Cython→Rust rewrite at 2.0" — plausible, secondary-only.
