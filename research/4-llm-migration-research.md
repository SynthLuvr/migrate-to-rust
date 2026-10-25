# LLM & Agentic Code Migration / Transpilation — What Generalizes to Python→Rust

*2026-10-25. Every quantitative claim below was verified against the cited page unless explicitly marked **UNVERIFIED**.*

---

## 1. LLM Transpilation Benchmarks (and what they measure)

### 1.1 CodeXGLUE (Microsoft, 2021) — code-to-code translation task
- **Source:** arXiv:2102.04159; evaluation discussion at https://www.emergentmind.com/topics/codexglue
- **Key claim:** CodeXGLUE established the code-to-code translation task (Java↔C#) evaluated with BLEU/CodeBLEU. Later studies show these lexical metrics "can misrepresent program correctness" — e.g., a ChatGPT study reached BLEU 16.82 / CodeBLEU 48.80 on C#→Java, and code-refinement work (Toggle) argues Exact Match is fairer than BLEU/CodeBLEU because buggy and fixed methods share large non-buggy prefixes that inflate lexical scores.
- **Implication:** Do **not** use surface-similarity metrics to gate a migration framework; a correct Rust rewrite will be lexically distant from the Python source. Gates must be execution/test-based.

### 1.2 TransCoder (Meta, 2020) — unsupervised transpilation, Python↔Java↔C++
- **Source:** arXiv:2006.03511 (Lachaux et al., NeurIPS 2020), https://arxiv.org/pdf/2006.03511
- **Key claims:** Fully unsupervised neural transpiler trained on monolingual GitHub code. "Computational accuracy" = fraction of translations passing a compiled unit-test harness. Greedy decoding (Beam 1): C++→Java 60.9%, C++→Python 44.5%, Java→C++ 80.9%, Java→Python 35.0%, Python→C++ 32.2%, **Python→Java 24.7%**. Beam 25 raises these to 74.8 / 67.2 / 91.6 / 68.7 / 57.3 / 56.1% — "beam search significantly improves the computational accuracy, by up to 33.7% in Java→Python." Notably, when only the top-probability beam candidate is returned (Beam 10–Top 1), accuracy drops (e.g., Python→Java 36.6% vs 51.1%), showing **valid translations are often found but ranked wrong**. TransCoder beat rule-based baselines (j2py: 38.3% Java→Python; commercial C++→Java: 61.0%).
- **Implication:** Even in 2020, sampling multiple candidates and validating each by execution roughly **doubled** success vs. taking the model's single best guess. A framework must generate N candidates per unit and select by test outcome, not by model confidence.

### 1.3 TransCoder-ST (Meta, ICLR 2022) — automated unit tests in the loop
- **Source:** Roziere et al., "Leveraging Automated Unit Tests for Unsupervised Code Translation," ICLR 2022 — https://mlanthology.org/iclr/2022/roziere2022iclr-leveraging
- **Key claim:** Filters and self-trains on unit-test-validated pseudo-parallel data; abstract states this "significantly improves" TransCoder's computational accuracy (exact per-direction deltas **UNVERIFIED**).
- **Implication:** Unit-test validation is not just an acceptance gate — it is signal for improving the translation process itself (fine-tuning / few-shot exemplar mining).

### 1.4 MultiPL-E (Stevens/Northeastern/Microsoft Research, 2022)
- **Source:** arXiv:2208.08227, https://mgree.github.io/papers/2022_multiple.pdf
- **Key claims:** "The first parallel multi-language benchmark for natural-language-to-code generation" — compiles HumanEval into 18 more languages, preserving prompts and test harnesses for **execution-based** evaluation. Codex "matches and even exceeds its performance on Python" on several languages; error taxonomy includes language-specific idiom errors ("DoesNotKnowSyntax"). Integrated into BigCode (May 2023).
- **Implication:** Execution-based harnesses with identical test suites across languages are the standard, cheap way to compare source/target behavior — directly reusable as a Python→Rust parity harness design.

### 1.5 BabelCode / TP3 (Google, 2023)
- **Source:** arXiv:2302.01973 (Orlanski et al., "Measuring The Impact Of Programming Language Knowledge"); https://github.com/google-research/babelcode
- **Key claims:** "A framework for execution-based evaluation of any dataset in any language" with 14 languages and hypothesis-ranking (TP3); investigates whether balancing language distribution in training improves generation.
- **Implication:** Language-resource imbalance measurably affects quality — expect Rust (lower-resource than Python) to lag and budget more repair cycles for it.

### 1.6 HumanEval-X (THUDM/CodeGeeX, 2023)
- **Source:** arXiv:2306.10998; https://github.com/THUDM/CodeGeeX
- **Key claims:** "820 human-crafted coding problems in 5 programming languages (Python, C++, Java, JavaScript, and Go), each of these problems is associated with tests and solutions," supporting both generation and translation tasks with unbiased pass@k estimators.
- **Implication:** Pass@k is the community-standard metric; report pass@k alongside per-attempt cost when budgeting repair loops.

### 1.7 Python→Rust specifically
- **Pan et al., "Towards Translating Real-World Code with LLMs: A Study of Translating to Rust" (2024)** — arXiv:2405.11514, https://arxiv.org/html/2405.11514v2
  - 8,160 translation experiments over 408 real-world/LeetCode code samples × 4 feedback strategies × 5 LLMs (GPT-4(-Turbo), Claude 3, Claude 2.1, Gemini Pro, Mixtral).
  - "the LLMs achieve successful translation rates of 21% to 47%": Claude 2 47.7%, Claude 3 43.9%, GPT-4-Turbo 36.9%, Gemini Pro 33.8%, Mixtral 21.0%.
  - "feedback strategies are responsible for up to 8% absolute of this success rate"; counterexample-based feedback "actually perform worse than the simple baseline strategy"; "larger programs (in LoC) are less likely to translate successfully."
  - **Implication:** (a) Single-shot Python→Rust of real-world code fails **more than half the time even with frontier models** — iteration is mandatory; (b) naive "show failing-counterexample" prompting can hurt; simple compile/test-error feedback is the reliable baseline; (c) size predicts failure — decompose by dependency, translate smallest units first.
- **VERT (2024)** — arXiv:2404.18852, https://arxiv.org/html/2404.18852v2
  - Uses a WebAssembly compiler to mint an **oracle** target program; an LLM generates a readable candidate; candidates are verified against the oracle and regenerated "until verification succeeds."
  - "VERT increases Rust transpilations passing property-based testing from 31% to 54% and bounded model-checking from 1% to 42%" (Claude-2); with fine-tuned GPT-3.5 it "produces a verified transpilation 45% more often on average." Evaluated on 1,394 programs.
  - **Implication:** Verification-oracle + regenerate loop is the single biggest published lever for correct Rust. Practical analog: compile the Python to an executable oracle (bytecode/PyAST-runner) and differential-test the Rust candidate against it.
- **Crust-bench (2025)** — arXiv:2504.15254 — comprehensive benchmark for **C→safe-Rust** transpilation (referenced in Berkeley EECS-2025-174).
- **RustEvo² (2025)** — arXiv:2503.16922 — benchmark of **588 real Rust API changes** (380 std-library, 208 from 15 third-party crates) across Stabilizations, Signature Changes, etc.; models struggle with version-aware, idiomatic API use.
  - **Implication:** LLMs know old Rust APIs; a framework must pin the toolchain/crate versions in the prompt and validate `Cargo.toml`/API usage against the actual locked versions.
- **AlphaTrans (2024)** — arXiv:2410.24117 (see §3) — repository-level Java→Python with program-analysis-driven decomposition.

### 1.8 Known failure modes for LLM→Rust
- **Crate hallucination (2026):** "When LLMs Invent Rust Crates: An Empirical Study of Hallucination Patterns and Mitigation" — arXiv:2606.08444, https://arxiv.org/html/2606.08444v1
  - First large-scale study of crate hallucination in Rust. "hallucination behavior in Rust follows a distinct pattern: different models exhibit surprisingly consistent hallucination rates, and these rates show minimal sensitivity to model parameters."
  - "over half of the hallucinated crates are actually valid Rust modules that exist in the standard library or third-party crates, but are being misused or unqualified referenced" (e.g., calling `HashMap::new` without `use std::collections::HashMap`). Cites supply-chain attack risk (Spracklen et al. 2025).
  - **Implication:** The dominant Rust failure is not invented packages but **misqualified imports/misused APIs** — resolvable by compile diagnostics + a `cargo`-metadata-validated import rewriter. Still add an allow-list check against crates.io/locked versions before accepting any dependency.
- **Idiom/API errors:** MultiPL-E's "LanguageSpecific" and "DoesNotKnowSyntax" error themes; RustEvo² shows models lag current API versions.
- **Ecosystem asymmetry (2026):** "From Translation to Superset" (arXiv, 2026) reports for LLM agents "Python's expressiveness yields a 15.9× code reduction with negligible performance cost" when API latency dominates — i.e., rewrite-for-Rust only pays where compute dominates; scope the migration by workload type. (**Single-source, cite with care.**)

---

## 2. Agentic Repair Loops (generate → compile → test → fix)

### 2.1 Self-Debugging (Google, 2023) — the canonical inner loop
- **Source:** arXiv:2304.05128, "Teaching Large Language Models to Self-Debug" (Chen et al.); results summary https://www.alphaxiv.org/abs/2304.05128
- **Key claims:** The model re-executes, gets feedback, and rewrites its own code across iterations. "On TransCoder and MBPP where unit tests are available, Self-Debugging improves the baseline accuracy by up to 12%" (TransCoder C++→Python: GPT-3.5 80.4% → **92.7%**). On Spider (no unit tests), natural-language **code explanation** feedback gains only "2-3%" (9% on hardest problems) — runtime/test feedback >> verbal feedback.
- **Implication:** For migration, always prefer executable feedback (compiler diagnostics, test diffs) over prose critique; explanation-only loops are weak oracles and should not be the sole gate.

### 2.2 SWE-agent (2024) — agent–computer interfaces
- **Source:** arXiv:2405.15793 (NeurIPS 2024), https://neurips.cc/virtual/2024/poster/93753
- **Key claim:** Purpose-designed ACI (file viewer/editor, test runner) yields "a pass@1 rate of 12.5% [SWE-bench Lite] and 87.7% [HumanEvalFix] ... far exceeding ... non-interactive language models"; ACI design materially changes agent behavior/success.
- **Implication:** Invest in tight, low-token tool interfaces for the migration loop (one-shot `build`, `test`, `mutate-check` commands) — interface quality is a first-order performance factor.

### 2.3 OpenHands / OpenDevin (2024–25)
- **Source:** OpenHands blog, Nov 2024, https://www.openhands.dev/blog/openhands-codeact-21-an-open-state-of-the-art-software-development-agent
- **Key claim:** CodeAct 2.1 achieved **53% resolve rate on SWE-bench Verified** and 41.7% on SWE-bench Lite — sandboxed agents that run shell, browser, and tests.
- **Implication:** Long-horizon agents with a real environment now resolve ~half of realistic repo tasks — a plausible ceiling reference for fully-autonomous migration subtasks.

### 2.4 Agentless (2024) — a *fixed* 3-phase pipeline beats agents at 1/10 cost
- **Source:** Xia et al. 2024; figures quoted in SWE-Bench Lite survey https://www.emergentmind.com/topics/swe-bench-lite-287687ae-2efa-4e10-b36b-2ea90e25ec7e
- **Key claim:** "A fixed 3-phase system (localization, repair, validation) reached 27.33% resolved (82/300) with GPT-4o, outperforming all contemporary open-source agent-based systems while incurring far lower cost (**$0.34 per bug**); line-level localization accuracy achieved 34.3%."
- **Implication:** Deterministic pipeline + LLM steps beats free-form agent loops on cost and reliability for well-specified tasks — for migrations, use static analysis to pick units, then a fixed per-unit transform/validate cycle; reserve free-form agents for repair triage.

### 2.5 RepairAgent (ICSE 2025) — tools + iteration budget for repair
- **Source:** Bouzenia, Devanbu, Pradel, https://software-lab.org/publications/icse2025_RepairAgent.pdf
- **Key claims:** An autonomous agent with 14 hand-crafted tools and a fixed workflow prompt. On Defects4J (835 bugs): "plausible fixes for 186 bugs ... correct fixes for 164 bugs" (≈19.6%); on GitBug-Java "19 plausible fixes and 13 correct fixes," effective for single-line bugs ("9 out of 19") but "struggles with the 81 multi-line and multi-file bugs" (only 4 correct).
- **Implication:** Iterative repair works best on small, well-localized defects; for migration, keep each failing unit small (function/dependency-slice granularity) and expect multi-file failures to need human-scope planning, not more agent iterations.

### 2.6 Loop design across APR agents (empirical synthesis, 2025)
- **Source:** "Understanding Automated Program Repair Agents Through the Lens of Traceability" — arXiv:2506.08311, https://arxiv.org/html/2506.08311v2
- **Key claims:** All five studied agents (AutoCodeRover, Agentless, SWE-agent, OpenHands-CodeAct, MASAI) "follow a shared high-level structure of project understanding, context construction, patch generation, and testing." Differences: MASAI generates a test template **early** (staged validation); SWE-agent/OpenHands defer test generation until after patch synthesis; Agentless and MASAI **rank patch candidates**.
- **Implication:** The winning loop shape is: understand → locate → generate K candidates → validate/rank by tests → repair loop on best candidate. Test-first (MASAI-style) beats test-after for translation, where the source is itself a spec.

### 2.7 Pass@k vs long-horizon agents; stopping criteria
- **Source:** pass@k definition/survey, https://www.emergentmind.com/topics/pass-k-metrics-2508a3b6-8dc0-488f-a854-891fb35d80b0 ; TransCoder beam data (§1.2); practitioner guidance, https://www.mindstudio.ai/blog/how-to-build-agentic-loop-claude-code
- **Key claims:** pass@k = 1−(1−p)^k — with per-sample success p≈0.35 (Rust single-shot), k=10 samples gives ≈98.7% chance one candidate passes trivial tests — but only with a **cheap automatic oracle** to pick the winner. Practitioner loop guidance converges on three stopping classes: **success** (tests/build green), **failure** ("retry count ... exceeds the limit", "unrecoverable error", "agent explicitly signals it's stuck"), **budget** ("turn count ... token spend ... wall-clock time"); "Every loop should have at least one criteria from each category" and the agent must not be allowed to modify its own stopping criteria.
- **Implication:** A migration framework needs per-unit budgets (attempts, tokens, wall-clock), a "stuck" classifier (same error twice ⇒ escalate to human queue), and candidate ranking by oracle rather than relying on any single generation.

---

## 3. Functional Equivalence & Testing for Migrated Code

### 3.1 AlphaTrans (2024/25) — repository-level translation + multi-level validation
- **Source:** arXiv:2410.24117v2, https://arxiv.org/html/2410.24117v2
- **Key claims:** Neuro-symbolic repo-level Java→Python: program analysis decomposes the program into fragments translated in **reverse call order** (dependencies first); translates **source and test code**; multiple validation levels. On 10 real projects (836 classes, 8,575 methods, 2,719 tests): **6,899 fragments, 99.1% syntactically correct, 25.8% validated** for runtime behavior/functional correctness; avg 36 h to translate+validate (min 4, max 122). Two developers fixed the remainder "in 20.1 hours on average (5.5 hours for the smallest and 34 hours for the largest) and achieve all passing tests." Uses an open-weight 33B model. Notes that the only prior GPT-4 repo-level attempt (Pan et al. 2024, Apache Commons CLI Java→Python) "resulted in non-compilable code."
- **Implication:** Full-repo LLM translation with naive prompting does not compile; **dependency-ordered, analysis-guided decomposition with the original tests as oracle** is what makes repo-level migration tractable — and expect ~¼ of units to validate automatically with mid-size models, with bounded human effort for the rest.

### 3.2 Project-level I/O-equivalence validation (PLDI 2025)
- **Source:** Wang et al., "Scalable, Validated Code Translation of Entire Projects," PLDI 2025, https://mengwangoxf.github.io/Papers/PLDI25.pdf
- **Key claims:** Combines "feature mapping" (predefined rules for subtle language differences) with LLM translation, achieving "a high 73% rate of functions validated for I/O equivalence"; validation mocks dependencies during differential I/O testing. Companion user study found even translations at lower I/O-equivalence rates ("25.8% overall and 45.9% for covered functions") "substantially reduce the time developers spend translating a codebase."
- **Implication:** Predefined rule libraries for known semantic gaps (dicts↔maps, exceptions↔Result, GIL/ownership issues) plus differential I/O testing with mocked I/O is a proven recipe; even partial automation yields large human-time savings, so report **residual work**, not just pass rate.

### 3.3 VERT — property-based differential testing + bounded model checking
- See §1.7. Verification modes: property-based differential testing (31%→54% pass) and **bounded model checking** (1%→42% pass) vs an oracle program.
- **Implication:** A tiered oracle ladder (unit tests → property-based differential tests → bounded symbolic checks on critical functions) buys large correctness gains at moderate cost; reserve heavyweight verification for high-risk functions only.

### 3.4 Test generation as the oracle for translation
- **CodaMosa (Google, ICSE 2023)** — Lemieux et al., https://www.carolemieux.com/codamosa_icse23.pdf
  - LLM-generated seed tests help search-based testing escape coverage plateaus: on 486 Python modules, higher coverage on **173** benchmarks (vs SBST) and **279** (vs LLM-only) vs reductions on only 10 and 4; average coverage increase +10% / +9% vs −4% / −3% decreases.
  - **Implication:** Use LLM-generated tests to cover source code *before* migration (the tests become the differential oracle) and to close coverage gaps in the Rust port afterwards.
- **DiffSpec (2024)** — arXiv:2410.04249, https://arxiv.org/html/2410.04249v3
  - Generates **differential tests** between two implementations (e.g., migrated vs original) by having the LLM describe behavior in natural language and "reason about the differences in the two descriptions," then synthesizing tests that expose behavioral divergence.
  - **Implication:** When source tests are sparse, LLM-generated differential test suites are the current state of practice for equivalence checking of migrated code.
- **TransCoder-ST** (§1.3): unit-test-validated data fed back into the translator — tests as both oracle and training signal.

### 3.5 Mutation testing for oracle strength
- **Python:** mutmut and cosmic-ray are the standard CLI mutation tools (mature, well-documented).
- **Rust:** **cargo-mutants** (sourcefrog; v27.1.0, June 2026) is "the current practical mutation testing tool for Rust": creates source-level mutants, runs the test suite, reports survivors. Guide: https://qaskills.sh/blog/cargo-mutants-rust-mutation-testing-guide — notes Rust-specific behavior: "the compiler catches many impossible mutations before tests run," so surviving mutants (not non-compiling ones) are the signal, and the tool "must be wired to the test command that represents the behavior you care about" (nextest profiles, doc tests, etc.).
- **Implication:** After a Python→Rust migration, run cargo-mutants on the port to measure whether the parity tests actually pin behavior — surviving mutants are precisely where translation bugs can hide; expect a lower mutant-survival baseline in Rust due to compiler-killed mutants.

---

## 4. Industry Programs (methodologies + published numbers)

### 4.1 Google — "Accelerating code migrations with AI" (July 2024) + experience report (arXiv:2501.06972)
- **Sources:** https://research.google/blog/accelerating-code-migrations-with-ai ; https://www.emergentmind.com/papers/2501.06972
- **Methodology:** Three stages — (1) **targeting** locations (static tools + cross-reference expansion to tests/interfaces/dependencies; "not yet AI-driven"), (2) **edit generation and validation** (Gemini fine-tuned on internal code per the DIDACT methodology; NL instruction annotated per line; multi-file context; **multiple candidate edits generated, scored by validations, best propagated**), (3) **change review and rollout** (human review). The paper adds: success is defined as "AI saving at least 50% of the time for the end-to-end work, including change generation, finding migration points, reviews, and rollouts"; hybrid architecture — "The LLM's primary role is in edit generation, while deterministic AST techniques handle location identification and validation." Case studies: int32→int64 ID migration, JUnit3→JUnit4, Joda-Time→java.time, flag cleanup; steady increase of AI-powered migration changelists through 2024 "fostering an ecosystem that allows for economies of scale."
- **Implication:** Keep location-detection and validation deterministic (AST/analysis-based), spend the LLM only on edits, score multiple candidates, and keep humans in review/rollout. "Success" should be defined against end-to-end migration time, not model accuracy.

### 4.2 IBM watsonx Code Assistant for Enterprise Java Applications
- **Sources:** IBM Cloud Docs overview https://cloud.ibm.com/docs/watsonx-code-assistant?topic=watsonx-code-assistant-getting-started ; VS Code Marketplace https://marketplace.visualstudio.com/items?itemName=IBM.wca-eja ; partner write-up https://www.deeperthanblue.co.uk/solutions/application-integration-middleware/ibm-watsonx-code-assistant
- **Methodology:** For Java runtime modernization the tool produces "a prescriptive plan that describes the changes that are needed to modernize your application, with a detailed assessment of complexity and required development effort. Use automation to quickly implement code and configuration changes. Transform code with generative AI assistance to resolve more complex issues." Plus "enhanced code explanation, unit test generation, and fixing Java unit tests." I.e., published gate sequence: **understand/assess → plan (human-reviewed) → automated conversion for routine changes → generative transform for complex issues → generated unit tests + test repair**. (By 2026 the offering is folded into IBM's "Bob" agentic coding environment per ibm.com product page.)
- **Quantitative:** IBM-internal results reported by partner DeeperThanBlue: "90% time savings on code explanation, a 59% reduction in documentation time, and a 38% reduction in code generation and testing time." (**UNVERIFIED primary** — partner blog figures; no IBM primary source extracted.)
- **Implication:** The complexity-assessed plan as an explicit human gate before generation, and tiered handling (automation for routine vs generative for complex) are directly portable to Python→Rust program design.

### 4.3 Anthropic — "How Anthropic runs large-scale code migrations with Claude Code" (July 2026)
- **Source:** https://claude.com/resources/articles/ai-code-migration
- **Key claims/numbers:**
  - Bun **Zig→Rust**: "A million lines of code were produced in less than two weeks, with 100% of Bun's existing test suite passing in CI before merge. Nineteen regressions surfaced after merge and have all been fixed." Consumed "5.9 billion uncached input tokens and 690 million output tokens — around $165,000 at API pricing"; ~1,448 files fanned out across parallel agents.
  - Python→TypeScript (Mike Krieger): "165,000 lines of TypeScript over a weekend ... hundreds of agents, eight phase gates, three adversarial review rounds, and a final parity check that diffed every command's output against the Python original." Compile-time motivation: builds "roughly eight minutes per platform" → "about two seconds," binary "starts 6x faster." Main port 27M tokens.
  - Methodology ("you don't fix the code. You fix the process (loop) that produced the code"): **Step 0** build a "judge" — port tests into a form runnable against *both* implementations; "a judge that doesn't catch breakage isn't a judge" (validate it fails on deliberately broken code); parity harness of real-world scenarios ("Mike created a parity harness of seven real-world scenarios"). **Step 1** rulebook (translation lookup tables/policies) + dependency map + gap inventory (implicit language-difference knowledge, e.g., manual memory management; interface contracts), built **before** fan-out; skeptic reviewers. **Step 2** stress-test rules on a 3-file mini-migration ("caught two critical issues that would have created numerous issues if fanned out across all 1,448 files"). Then mechanical work queues, adversarial reviewers, fixer agents ("Individual failures are the loop's job... Your attention belongs on the patterns"), model tiering ("Smaller models handle the high-volume implementation fan-out... save your largest model for reviewers"), "Make the work queue mechanical and resumable. Done should mean 'the output file exists on disk.'"
- **Implication:** This is the most complete public blueprint for agentic Python→(typed/Rust-like) migration: test-ported judge validated against broken code, rulebook-first, pilot batch, mechanical resumable queues, adversarial review, ~$0.16/LOC order-of-magnitude token cost at frontier-model API pricing.

### 4.4 AWS — Amazon Q Developer agent for code transformation / Kiro
- **Source:** https://aws.amazon.com/blogs/devops/amazon-q-developer-just-reached-a-260-million-dollar-milestone ; product page https://aws.amazon.com/q/developer/transform ; Kiro transition https://aws.amazon.com/blogs/devops/amazon-q-developer-end-of-support-announcement
- **Key claims/numbers:** "Amazon has migrated tens of thousands of production applications from Java 8 or 11 to Java 17 ... savings of over 4,500 years of development work for over a thousand developers ... and performance improvements worth $260 million dollars in annual cost savings" (Aug 2025). Estimation basis: "it can take a day or more of a developer's time to migrate just one dependency ... many of these dependencies can be migrated in minutes." .NET: modernize "Windows .NET Framework to cross-platform ... on Linux," "up to four times faster" with "up to a 40% reduction in licensing costs" (stocktitan summary of AWS announcement). Q Developer IDE plugins reach end of support April 30, 2027, superseded by **Kiro**, an agentic IDE "built from the ground up for spec-driven development" (structured specs → plan → implement → verify).
- **Implication:** Agentic transformation is production-proven at enterprise scale when scoped to well-understood version-upgrade semantics (framework/API mappings + generated tests); spec-driven planning (Kiro) mirrors the rulebook-first pattern from Anthropic/IBM.

### 4.5 GitHub Copilot ecosystem case study (DataArt, 2024)
- **Source:** https://www.dataart.com/clients/case-studies/migrating-legacy-code-with-copilot
- **Key claims:** 500k-line .NET Framework sports-gaming app migrated to .NET 8.0 using Copilot-assisted migration: "reducing costs by up to 70%" and "delivered at approximately 1/3 the cost of traditional manual migration." (**Vendor-published case study; single source.**)
- **Implication:** Even IDE-assisted (non-agent) migration with humans in the loop yields ~2–3× cost reduction on large legacy ports — the realistic floor for a semi-automated framework.

### 4.6 Academic-adjacent industry: "alchemize" LLM transpiler (PyMC→Rust, 2026)
- **Source:** blog summary surfaced in search ("The LLM Transpiler: 3–7× Faster PyMC Models with Rust") — a "write–compile–validate loop" delivering 3–7× speedups on compute-heavy Python.
  (**UNVERIFIED details**; treat as anecdotal evidence that compile-validate loops ship in production transpiler tools.)

---

## 5. Quantitative Results Summary (all verified against cited sources unless noted)

| Metric | Value | Source |
|---|---|---|
| Single-shot LLM Python→Rust success (real-world code) | 21–47% across 5 LLMs (best Claude 2: 47.7%) | arXiv:2405.11514 |
| Feedback strategy contribution | up to +8% absolute; counterexamples **worse** than baseline | arXiv:2405.11514 |
| TransCoder Python→Java computational accuracy | 24.7% greedy → 56.1% beam-25 | arXiv:2006.03511 |
| Self-debugging on translation (C++→Python) | 80.4% → 92.7% (GPT-3.5) | arXiv:2304.05128 |
| VERT (Rust, Claude-2) | differential-test pass 31%→54%; bounded-model-check pass 1%→42% | arXiv:2404.18852 |
| AlphaTrans repo-level Java→Python | 99.1% syntactically correct; 25.8% auto-validated; 36 h avg machine time; **20.1 h avg human fix time** | arXiv:2410.24117 |
| Project-level translation w/ feature mapping | 73% of functions validated for I/O equivalence | PLDI25 |
| Agentless cost | 27.33% SWE-bench Lite at **$0.34/instance** | Agentless (Xia 2024) |
| SWE-agent | 12.5% SWE-bench Lite pass@1 | arXiv:2405.15793 |
| OpenHands CodeAct 2.1 | 53% SWE-bench Verified | openhands.dev blog |
| RepairAgent | 164/835 correct Defects4J fixes (~19.6%); strong single-line, weak multi-file | ICSE 2025 PDF |
| Bun Zig→Rust | 1M LOC < 2 weeks; ~$165k tokens; 100% tests green pre-merge; 19 post-merge regressions | claude.com article |
| AWS Q Java upgrades | "tens of thousands" of apps; 4,500 dev-years saved; $260M/yr | AWS blog |
| Google migration bar | AI must save ≥50% of end-to-end migration time | arXiv:2501.06972 |
| Google "32,000+ AI-assisted migrations" | **UNVERIFIED** — widely reported figure, not confirmed in extracted sources; confirmed facts: multiple production migrations + rising CL volume through 2024 | — |
| IBM WCA internal gains | 90%/59%/38% time savings (explanation/documentation/gen+test) — **UNVERIFIED primary** (partner blog) | DeeperThanBlue |
| Cost per line, agentic language port | ≈ $165k / 1M LOC ≈ **$0.17/LOC** (token cost only; excludes human time) | Anthropic article |

---

## 6. Formal / Heavyweight Verification for Migrations

- **Survey (MDPI Technologies 2024):** "Formal Verification of Code Conversion: A Comprehensive Survey," https://www.mdpi.com/2227-7080/12/12/244
  - Finds "Traditional validation methods, such as test cases and code coverage metrics, often fail to ensure the correctness, completeness, and equivalence of converted code," but also that "Symbolic execution was used in only one article to check code-to-code translation equivalence. However, it was used without an SMT solver. Instead, the paths' equivalence was checked manually." Concludes equivalence checking "proves translation correctness" in principle but calls for automated, scalable tools; suggests SMT-based symbolic equivalence as future work.
  - **Implication:** No practical general-purpose formal Python↔Rust equivalence checker exists today; symbolic techniques remain research-grade.
- **Berkeley EECS Tech Report EECS-2025-174:** "LLM-Based Code Translation Needs Formal Compositional..." — argues LLM translation quality requires formal, compositional verification layers (cites TransCoder-ST, Crust-bench, ownership-guided C→Rust translation). (**Title partially truncated in source; report not fully extracted.**)
- **Practical middle grounds in the literature:** VERT's bounded model checking (1%→42% on competitive-programming code) and Ownership-Guided C→Rust translation (Zhang et al., CAV 2023) show targeted formal techniques work for narrow domains; PLDI25 and AlphaTrans establish **differential I/O testing with the original tests + mocked dependencies** as the de-facto state of practice at repository scale.
- **Implication:** For a Python→Rust framework: differential testing (ported tests + LLM-generated differential tests + property-based checks) is the state of practice; use bounded/symbolic verification only for small, safety-critical functions, and treat full equivalence checking as not-yet-practical.

---

## Synthesis: What the Evidence Says a Framework Must Do

1. **Test-first, judge-first.** Before translating anything, build and *validate* a judge: port/rewrite source tests into an external parity harness runnable against both Python and Rust; prove it fails on deliberately broken code (Anthropic step 0; MultiPL-E execution-based design; AlphaTrans translating tests alongside code). Augment with LLM-generated differential and property-based tests (DiffSpec, CodaMosa, VERT) where source tests are thin.
2. **Decompose with static analysis; translate in dependency order.** Repo-level naive LLM translation "resulted in non-compilable code"; analysis-guided fragment translation in reverse-call order reached 99.1% syntactic correctness and made human fixes tractable (AlphaTrans). Google: deterministic AST/analysis for location + validation, LLM only for edits.
3. **Iterate with executable feedback, bounded.** Compile + test error feedback is the strongest signal (Self-Debugging +12% on translation; Pan et al. +8% from feedback); explanation-only feedback is weak. Every loop needs success/failure/budget stopping criteria, a stuck-detector (escalation on repeated identical errors), and hard per-unit token/iteration budgets (practitioner + RepairAgent evidence).
4. **Generate K candidates; rank by the oracle, not by confidence.** Beam 25 doubled TransCoder accuracy; "Beam N–Top 1" results show confidence ranks valid translations poorly; Agentless/MASAI rank patch candidates. Candidate fan-out with test-based selection is the highest-leverage single design decision.
5. **Treat Rust-specific failure modes as first-class.** >50% of "hallucinated crates" are real modules mis-imported/misused (arXiv:2606.08444) — add cargo-validated import fixing and version-pinned API checks (RustEvo²) before human review; expect smaller models and lower-resource language (Rust) to need more cycles (BabelCode; Pan et al.).
6. **Tier oracles by risk.** Unit-test parity for everything; property-based differential testing broadly; bounded/symbolic verification only for critical functions (VERT ladder). Full formal equivalence is not practical for Python↔Rust today (MDPI survey) — differential testing is the state of practice.
7. **Human gates on risk tiers, plan before fan-out.** IBM's prescriptive-plan gate, Google's human review/rollout, Anthropic's rulebook + gap inventory + 3-file pilot before 1,448-file fan-out, eight phase gates + three adversarial review rounds on the 165k-LOC port. Rulebook must precede the gap inventory; pilot batches catch systemic rule errors cheaply.
8. **Report residual human work, not just pass rate.** AlphaTrans: 20.1 h avg human effort per project after 25.8% auto-validation; PLDI25 user study: even 25.8–45.9% validated translations "substantially reduce" developer time. Success metric: end-to-end migration time saved ≥50% (Google's own bar), with mechanical, resumable work queues ("Done should mean the output file exists on disk").
9. **Budget expectations.** Frontier agentic language port ≈ $0.17/LOC token cost (Bun data point) with ~2× failures-to-fixes surfacing post-merge (19 regressions after "100% tests passing"); semi-automated IDE-assisted migration saves ~2–3× cost (DataArt); version-upgrade agents at ecosystem scale saved 4,500 dev-years (AWS). Plan for post-merge regression triage as a permanent phase, not an afterthought.

---

## Sources

1. arXiv:2102.04159 — CodeXGLUE (via https://www.emergentmind.com/topics/codexglue)
2. arXiv:2006.03511 — TransCoder: Unsupervised Translation of Programming Languages (Lachaux et al., NeurIPS 2020)
3. Roziere et al., ICLR 2022 — TransCoder-ST: Leveraging Automated Unit Tests for Unsupervised Code Translation — https://mlanthology.org/iclr/2022/roziere2022iclr-leveraging
4. arXiv:2208.08227 — MultiPL-E (Cassano et al.) — https://mgree.github.io/papers/2022_multiple.pdf
5. arXiv:2302.01973 — Measuring The Impact Of Programming Language Knowledge (BabelCode/TP3, Orlanski et al.) + https://github.com/google-research/babelcode
6. arXiv:2306.10998 — CodeGeeX2 / HumanEval-X (THUDM) + https://github.com/THUDM/CodeGeeX
7. arXiv:2405.11514 — Towards Translating Real-World Code with LLMs: A Study of Translating to Rust (Pan et al.)
8. arXiv:2404.18852 — VERT: Verified Equivalent Rust Transpilation (Yang et al.)
9. arXiv:2503.16922 — RustEvo²: API Evolution Benchmark for Rust
10. arXiv:2504.15254 — Crust-bench: C to safe Rust (cited in Berkeley EECS-2025-174)
11. arXiv:2606.08444 — When LLMs Invent Rust Crates: Hallucination Patterns and Mitigation (2026)
12. arXiv:2304.05128 — Teaching LLMs to Self-Debug (Chen et al.) + https://www.alphaxiv.org/abs/2304.05128
13. arXiv:2405.15793 — SWE-agent (NeurIPS 2024) + https://neurips.cc/virtual/2024/poster/93753
14. OpenHands CodeAct 2.1 blog — https://www.openhands.dev/blog/openhands-codeact-21-an-open-state-of-the-art-software-development-agent
15. Agentless (Xia et al. 2024) figures — https://www.emergentmind.com/topics/swe-bench-lite-287687ae-2efa-4e10-b36b-2ea90e25ec7e
16. RepairAgent (ICSE 2025) — https://software-lab.org/publications/icse2025_RepairAgent.pdf
17. arXiv:2506.08311 — APR Agents Through the Lens of Traceability
18. arXiv:2410.24117 — AlphaTrans: Repository-Level Compositional Code Translation and Validation
19. PLDI 2025 — Scalable, Validated Code Translation of Entire Projects — https://mengwangoxf.github.io/Papers/PLDI25.pdf
20. CodaMosa (ICSE 2023) — https://www.carolemieux.com/codamosa_icse23.pdf
21. arXiv:2410.04249 — DiffSpec: Differential testing with LLMs
22. cargo-mutants guide — https://qaskills.sh/blog/cargo-mutants-rust-mutation-testing-guide
23. Google Research blog — Accelerating code migrations with AI (July 2024) — https://research.google/blog/accelerating-code-migrations-with-ai
24. arXiv:2501.06972 — Google AI for Code Migrations (experience report)
25. IBM Cloud Docs — watsonx Code Assistant overview — https://cloud.ibm.com/docs/watsonx-code-assistant?topic=watsonx-code-assistant-getting-started
26. IBM WCA for Enterprise Java Applications — https://marketplace.visualstudio.com/items?itemName=IBM.wca-eja ; IBM product page (IBM Bob) — https://www.ibm.com/products/watsonx-code-assistant-for-enterprise-java-applications
27. DeeperThanBlue — Application modernisation with IBM watsonx Code Assistant (UNVERIFIED-primary figures)
28. Anthropic — How Anthropic runs large-scale code migrations with Claude Code — https://claude.com/resources/articles/ai-code-migration
29. AWS DevOps blog — Amazon Q Developer $260M milestone — https://aws.amazon.com/blogs/devops/amazon-q-developer-just-reached-a-260-million-dollar-milestone
30. AWS — Amazon Q Developer transform — https://aws.amazon.com/q/developer/transform ; Kiro transition — https://aws.amazon.com/blogs/devops/amazon-q-developer-end-of-support-announcement
31. DataArt — Migrating 500k Lines of Legacy Code with Copilot — https://www.dataart.com/clients/case-studies/migrating-legacy-code-with-copilot
32. MDPI Technologies 12(12):244 — Formal Verification of Code Conversion: A Comprehensive Survey
33. Berkeley EECS-2025-174 — LLM-Based Code Translation Needs Formal Compositional (title partial) — https://www2.eecs.berkeley.edu/Pubs/TechRpts/2025/EECS-2025-174.pdf
34. MindStudio — How to Build an Agentic Loop (stopping criteria practitioner guidance) — https://www.mindstudio.ai/blog/how-to-build-agentic-loop-claude-code
35. pass@k metrics survey — https://www.emergentmind.com/topics/pass-k-metrics-2508a3b6-8dc0-488f-a854-891fb35d80b0
