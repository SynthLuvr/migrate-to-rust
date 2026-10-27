# Python → Rust AI Migration Framework — Research Summary

*All quantitative claims cite sources; claims that could not be verified are marked ⚠️UNVERIFIED. Full detail in [`research/`](research/).*

---

## 1. The evidence base

### 1.1 AI-assisted Python → Rust ports (verified, quantified)

| Case | What was ported | Effort / cost | Results | Source |
|---|---|---|---|---|
| **Volatility3 → Rust** ("Rusting Volatility", DFRWS USA 2026) | 38 Linux forensics plugins, **29,028 LOC**, 8 Rust crates | **68 hours**, ~900M tokens ≈ **$2,962** (GPT-5.2-Codex $812 impl + Claude Opus 4.5 $2,150 plan/review) | **2.5× median / 210× max speedup** (834 runs); Rust completed **100%** of 417 executions vs Python's **88.7%**; **68.9% exact output equivalence**, rest explainable divergence | [DFRWS paper](https://dfrws.org/wp-content/uploads/2026/04/DFRWS_USA_2026_Camera_Ready_Paper_81.pdf) |
| **Pydantic v1 → pydantic-core** | Validation engine rebuilt in Rust/PyO3; Python API unchanged | Multi-year, incremental hot-path extraction | **4–50× (≈17× typical)** validation speedup; pydantic itself became *pure* Python again | [pydantic blog](https://docs.pydantic.dev/latest/blog/pydantic-v2/) |
| **orjson** | JSON serialization in Rust behind Python API | — | **~11.8–11.9×** serialization vs stdlib | [orjson README](https://github.com/ijl/orjson) |
| **ScanCode Toolkit (⚠️ cautionary)** | Mature Python scanner ported by an LLM agent harness (OpenCode + plugin) | Agent-driven, months | Claimed 10–100×; re-testing showed **wrong results + skipped files**; optimization of the original Python made it "as fast or faster" with correct output | [AboutCode post](https://aboutcode.org/blog/agentic-scancode-port-case-study) |
| **Cloudflare Python Workers** | *Not* a code port: CPython (Pyodide/WASM) embedded **inside** the Rust `workerd` runtime with Rust-side syscall shims | — | "Language parity" without translation; ⚠️ the popular "PyPy → Rust" retelling is **unverifiable** (zero "PyPy" mentions in the primary post) | [CF blog](https://blog.cloudflare.com/python-workers) |

### 1.2 Agentic Rust ports from other languages

- **Cloudflare `tsrs`** (TypeScript compiler → Rust, Oct 2026): started at Owner.com — **37k-file TS backend**, TS 7.0 Go compiler took **80 s / 19 GiB**; Max Schwenk used **Claude Opus 5.5 with 20+ parallel subagents and wrote zero code himself**; a working typechecker existed the next morning. Result: **up to 8× faster, 70% less memory** than TS 7.0. Validation = **upstream conformance tests + deliberate error injection + Miri** ("a faster compiler that finds fewer errors is a broken compiler with good marketing"). Maintenance = agents that **watch upstream, run compatibility tests, propose fixes** (the Vinext process). ([The New Stack](https://thenewstack.io/cloudflare-rust-typescript-tsrs), [CF blog](https://blog.cloudflare.com/typescript-rust-port))
- **Microsoft Copilot runtime** (TS → Rust): **430k → 800k LOC** via agents across **135 releases / 14.5 weeks** (~1.3 PRs/day); cost "**~$120,000 in tokens + 3 dev-weeks**"; **15.9× throughput**, **1,383 MB → 126 MB**; paid for it with "**a few dozen regressions**". ([The Register](https://www.theregister.com/devops/2026/09/18/microsoft-agentically-ports-copilot-runtime-to-rust-for-120k/5297549))
- **Anthropic Bun** (Zig → Rust): **~1M LOC in <2 weeks**, ~$165k tokens (**≈ $0.17/LOC**), "100% tests green" pre-merge — yet **19 post-merge regressions** surfaced. ([claude.com](https://claude.com/resources/articles/ai-code-migration))
- Also verified: Meta porting React Compiler to Rust with AI; OpenAI rebuilding Habitat storage with Codex; a **reverse-direction control case** (Codex CLI, 648k LOC Rust → 41k LOC Python) succeeded using **public benchmarks as the objective function** — proof the harness methodology is language-agnostic ([arXiv:2604.11518](https://arxiv.org/html/2604.11518v1)).

### 1.3 Human-driven Rust migrations (process patterns, not AI)

- **Discord** Go→Rust Read States: GC scans of a large live working set caused **~500 ms spikes**; Rust "beat Go on every single performance metric"; rollout = load-test → one canary → fleet. ([Discord blog](https://discord.com/blog/why-discord-is-switching-from-go-to-rust))
- **Cloudflare Pingora**: 1T req/day at **~1/3 the CPU and memory of nginx**. ([CF blog](https://blog.cloudflare.com/how-we-built-pingora-the-proxy-that-connects-cloudflare-to-the-internet/))
- **Figma**: scoped down from full-server rewrite to one component; **data-structure change alone gave 20% p99** — data representation can dominate language choice. ([Figma blog](https://www.figma.com/blog/rust-in-production-at-figma/))
- **Dropbox** Nucleus: **deterministic seeded simulation** ("we run millions of seeds every day… if CI finds a failure, it just prints the seed"); whole-team Rust fluency was the success factor. ([Dropbox.tech](https://dropbox.tech/infrastructure/rewriting-the-heart-of-our-sync-engine))

### 1.4 What research says about the translation step itself

- Single-shot LLM Python→Rust on real code succeeds only **21–47%** ([arXiv:2405.11514](https://arxiv.org/abs/2405.11514)); a **differential-test repair loop** lifts pass rates **31% → 54%** (VERT, [arXiv:2404.18852](https://arxiv.org/abs/2404.18852)); candidate beam-search roughly **doubles** accuracy (TransCoder 24.7% → 56.1%, [arXiv:2006.03511](https://arxiv.org/abs/2006.03511)).
- Repository-level naive translation "resulted in non-compilable code"; **static-analysis-guided, dependency-ordered translation** reached 99.1% syntactic correctness — but only 25.8% auto-validated, with **20.1 h average human fix time** (AlphaTrans, [arXiv:2410.24117](https://arxiv.org/abs/2410.24117)).
- **>50% of "hallucinated Rust crates" are actually real modules imported wrongly** — compile-fixable, not invented ([arXiv:2606.08444](https://arxiv.org/abs/2606.08444)).
- **Formal equivalence checking is not yet practical** for dynamic languages (MDPI survey); **differential testing is the state of practice**.
- Google's internal bar: deterministic AST tooling for *location and validation*, LLM only for *edits*; a migration is worth automating only if it saves **≥50% of end-to-end time** ([Google Research](https://research.google/blog/accelerating-code-migrations-with-ai), [arXiv:2501.06972](https://arxiv.org/abs/2501.06972)).

---

## 2. What a framework must do (the 10 evidence-backed requirements)

1. **Select ports by profile evidence, not by whole program.** Every successful migration was scoped to a measured bottleneck. Big, long-lived object graphs (caches, registries) predict the biggest wins — GC scan cost drove Discord's and Figma's pain. I/O-bound code is a negative signal.
2. **Treat the test suite as the spec — and validate the judge first.** ScanCode's 90k tests are what made the port *possible*; Volatility3's hash-parity harness is what made it *correct*. Before translating anything, prove the parity harness fails on deliberately broken code ("judge-first", Anthropic's step 0).
3. **Decompose with static analysis; translate in dependency order.** Naive whole-repo LLM translation fails to compile. Use tree-sitter/LibCST/mypy to build a dependency-ordered work queue of small, independently verifiable units.
4. **Run bounded repair loops with executable feedback.** Compile + test errors are the strongest signal (explanation-only feedback is weak); every loop needs success/failure/**budget** stopping criteria and a stuck-detector that escalates to humans.
5. **Generate K candidates and rank with the oracle, not model confidence.** Confidence ranks valid translations poorly; test-based selection is the single highest-leverage design decision.
6. **Keep the Python API; ship the Rust core as a drop-in PyO3 wheel.** The pydantic/orjson/Cloudsmith pattern — "Rust where hot, Python everywhere else" — is the stable equilibrium every interop case converged on.
7. **Redesign data structures as a first-class (validated) step.** A literal transliteration forfeits much of the win (Figma: 20% from a data swap alone).
8. **Gate on the *full* test suite, plus property/fuzz/mutation tests, and benchmark both sides.** ScanCode's port passed *some* tests and was wrong; Volatility3 classified every run *Equivalent / Divergent / Incomparable* and shipped at 68.9% exact parity because divergences were **explained**, not ignored.
9. **Preserve attribution and license headers mechanically.** LLM output has no provenance layer by default — ScanCode's port stripped NOTICE files and per-file copyright headers. This must be a pipeline step, not a hope.
10. **Budget realistically, including the tail.** Frontier agentic ports ≈ **$0.17/LOC** token cost plus **~2× post-merge regression triage** (Bun: 19 regressions after "100% green"); Google won't automate unless ≥50% of end-to-end time is saved. Success metric = *human hours saved end-to-end*, not pass rate.

---

## 3. Architecture that falls out of the evidence

```
 ┌────────────────────────────────────────────────────────────────┐
 │ 0. FEASIBILITY GATE     profile + rewrite-criteria checks      │
 │    py-spy/Scalene/memray → is there a structural bottleneck?   │
 └──────────────┬─────────────────────────────────────────────────┘
                ▼
 ┌────────────────────────────────────────────────────────────────┐
 │ 1. PLAN                 static analysis (tree-sitter/LibCST/   │
 │    mypy/pyright, import graph) → dependency-ordered,          │
 │    portability-scored unit backlog; data-model redesign notes  │
 └──────────────┬─────────────────────────────────────────────────┘
                ▼
 ┌────────────────────────────────────────────────────────────────┐
 │ 2. CONTRACTS & JUDGE    extract spec = tests + types + invari- │
 │    ants; build parity harness; validate judge on broken code   │
 └──────────────┬─────────────────────────────────────────────────┘
                ▼
 ┌────────────────────────────────────────────────────────────────┐
 │ 3. SCAFFOLD             rust-template base + PyO3 + maturin +  │
 │    gate extension (nextest/miri) wired before porting          │
 └──────────────┬─────────────────────────────────────────────────┘
                ▼
 ┌────────────────────────────────────────────────────────────────┐
 │ 4. TRANSLATE (per unit, dep order)                             │
 │    LLM candidate generation (K candidates, role-separated      │
 │    models) → compile → run parity judge → fix loop with        │
 │    budgets + stuck-detector → human escalation queue           │
 └──────────────┬─────────────────────────────────────────────────┘
                ▼
 ┌────────────────────────────────────────────────────────────────┐
 │ 5. GATES                full-suite parity · property/fuzz/     │
 │    mutation · Miri/clippy · cross-language benchmarks ·        │
 │    license/attribution preservation check                      │
 └──────────────┬─────────────────────────────────────────────────┘
                ▼
 ┌────────────────────────────────────────────────────────────────┐
 │ 6. INTEGRATE            publish wheel (maturin) → Python API   │
 │    unchanged → shadow/canary with production diffing → cutover │
 └──────────────┬─────────────────────────────────────────────────┘
                ▼
 ┌────────────────────────────────────────────────────────────────┐
 │ 7. MAINTAIN             upstream-tracking agents (tsrs/Vinext  │
 │    pattern) + regression-triage budget as a permanent phase    │
 └────────────────────────────────────────────────────────────────┘
```

**The gaps a framework must fill** (no existing tool covers these — verified in [research/3](research/3-tools-and-interop-catalog.md)): an equivalence oracle for dynamic Python; profiling→port-plan generation; spec/contract extraction; dual-build (Python+Rust+shim) state management; a cross-language differential benchmark harness; PyO3-FFI-specific review of LLM output. Everything else can be composed from existing tools (see [TOOLCHAIN.md](TOOLCHAIN.md)).

---

## 4. Honest caveats

- **"Cloudflare migrated Python (PyPy) to Rust" is not verifiable.** The verified Cloudflare stories are: `tsrs` (TypeScript→Rust, agent-written), Pingora (nginx→Rust), FL2 (NGINX+Lua→Rust), and Python Workers = CPython-in-WASM *inside* their Rust runtime — an interop story, not a translation story.
- The IBM `watsonx-code-migration-poc` GitHub repo now returns 404; its methodology survives only in product docs. Presidio's Apex→Rust repos don't exist as remembered; their verified products are HAI Build/Modernize with webinar-grade figures.
- Benchmark claims in vendor posts are frequently flawed (see the ScanCode case: a port can be faster *and wrong*). Any framework output should ship with a reproduction-ready benchmark, not a number.
- Several widely-repeated stories failed verification against primary sources (InfinyOn "From Python to Rust", Dropbox "Open Sourcing Our Rust Libraries", Google's "32,000 migrations", Figma's 2022 update post). The research files flag each one.

## Research files

| File | Contents |
|---|---|
| [`research/0-anchor-findings.md`](research/0-anchor-findings.md) | Firsthand-verified anchors (tsrs, ScanCode, Copilot runtime, FL2, Python Workers) |
| [`research/1-case-studies-ai-assisted.md`](research/1-case-studies-ai-assisted.md) | AI-assisted ports incl. Volatility3 flagship + cross-case patterns |
| [`research/2-case-studies-manual-rust-migrations.md`](research/2-case-studies-manual-rust-migrations.md) | Discord/Pingora/Figma/Dropbox/npm/1Password + 10 transferable lessons |
| [`research/3-tools-and-interop-catalog.md`](research/3-tools-and-interop-catalog.md) | ~80 tools with live maintenance verification + gap analysis |
| [`research/4-llm-migration-research.md`](research/4-llm-migration-research.md) | Academic benchmarks, repair loops, equivalence, industry programs, 35 sources |
| [`research/5-idioms-and-ecosystem-mapping.md`](research/5-idioms-and-ecosystem-mapping.md) | Python→Rust idiom map, 22-crate substitution table (maintenance-verified), PyO3/free-threading constraints, knowledge-pack spec |
