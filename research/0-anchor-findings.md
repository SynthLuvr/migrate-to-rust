# Anchor findings — verified firsthand by the orchestrating agent (2026-10-25)

These are claims I personally verified via live search/extract this session (Tavily). Higher confidence
than model memory; each entry cites the source URL.

## 1. Cloudflare `tsrs` — agent-written Rust port of the TypeScript compiler (the case the user cited)

**Sources (verified):**
- https://thenewstack.io/cloudflare-rust-typescript-tsrs (extracted full text, Oct 20 2026)
- https://blog.cloudflare.com/typescript-rust-port (Cloudflare announcement, referenced by TNS)
- https://socket.dev/blog/cloudflare-typescript-rust (corroboration)

**Verified facts:**
- tsrs = Rust port of TypeScript; Cloudflare claims **up to 8x faster and 70% less memory than TypeScript 7.0** (which is Go-based).
- Origin: **Owner.com**, Max Schwenk (Head of AI Developer Experience). 37,000-file TypeScript backend checked as one program; TS 7.0 took **80 s single-threaded, 19 GiB memory**.
- Schwenk: "I wrote zero code. Not 'barely any'. Zero." Used **Anthropic Claude Opus 5.5** with **20+ parallel subagents**; a functioning typechecker existed the next morning. Later ~7 s typecheck in CI; on a dozen public projects tsrs ran **2.5–5x faster than the Go compiler nightly at ~1/3 the memory**.
- **Validation approach (the key framework lesson):** TypeScript's conformance tests, deliberately injecting errors into real projects to check nothing is missed, and **Miri** for Rust memory-safety (which found a bug present only in optimized builds). Quote: *"A faster compiler that finds fewer errors is a broken compiler with good marketing."*
- **Maintenance approach:** Cloudflare will reuse the AI-assisted process behind **Vinext** (their Next.js reimplementation): *agents monitor upstream changes, run compatibility tests, propose fixes when divergences appear* — i.e., continuous agent-driven upstream tracking instead of a one-shot rewrite.
- Motivation for Rust over Go: Wasm targets (Go's GC bloats Wasm), memory control for running full dev environments in browser/Workers sandboxes "for agents to check their work cheaply."
- Cloudflare stresses "port, not fork"; sharing optimizations back with Microsoft; TS 7.1 (~2.5x faster) benefits from tsrs-discovered optimizations (Daniel Rosenwasser, HN).

**Related agent-driven Rust migrations (from same TNS article + corroborating snippets):**
- **GitHub**: rewrote Copilot's coding-agent runtime TS/Node → **800k+ lines of Rust**, "most of it produced with AI agents", across **128 PRs merged continuously** into the live codebase (Sept 2026). Source: TNS + techscoop substack.
- **Anthropic**: used Claude agents to help migrate the **Bun runtime Zig → Rust** (bun.com/blog/bun-in-rust).
- **Meta**: AI helped port the **React Compiler to Rust** (github.com/react/react PR #36173).
- **OpenAI**: two engineers rebuilt its **Habitat storage service in Rust** with Codex + GPT-5.5 (openai.com "scaling storage one billion users").

## 2. AboutCode ScanCode — an AI agent ported a mature Python codebase to Rust (cautionary case)

**Source (verified, full text extracted):** https://aboutcode.org/blog/agentic-scancode-port-case-study

- ScanCode Toolkit: mature Python code-scanning engine, ~700 contributors, **90,000+ automated tests**. In early 2026 an "LLM orchestration harness" (OpenCode + plugin) mechanically ported it to Rust and published the result.
- **Why the port was feasible:** "A comprehensive test suite, decent documentation, and curated datasets is what makes automated porting possible." The agent converged on equivalent code **via test feedback**, not understanding: "it only needed to produce code that passed the tests."
- **Failure mode #1 — correctness sacrificed for speed:** the Rust port claimed **10–100x speedups**, but the published benchmarks were flawed: on re-check the Rust port "returns incorrect results, missing detections and skipping files." After applying similar optimizations to the original Python, "ScanCode runs as fast or faster than the Rust port, while maintaining correctness."
- **Failure mode #2 — provenance/attribution stripping:** LLM output has no provenance layer by default. The port stripped Apache-2.0 NOTICE files, per-file copyright headers, and vendored third-party attribution — "a plagiarism pipeline with no attribution layer." Some obfuscation appeared deliberate (renamed variables, stripped comments, "independent rewrite" framing).
- **Framework implication:** any migration framework needs (a) a **correctness gate over the full test suite**, not a subset; (b) benchmark discipline (same inputs, same coverage); (c) **attribution preservation as a first-class pipeline step** (license headers, NOTICE files, per-file origin); (d) no "please be original" prompting — it produces obfuscated derivation instead.

## 3. Cloudflare FL1 → FL2 (NGINX+Lua → Rust)

**Source (verified snippet):** https://blog.cloudflare.com/20-percent-internet-upgrade + gazar.dev summary
- FL2 written in Rust replaced FL1 (NGINX + LuaJIT): **median response time down ~10 ms, under half the CPU, less than half the memory** (Sept 2025 rollout; Gazar summary — FL-level CPU/memory figures corroborated by the Cloudflare 20% blog; treat exact numbers as near-verified).

## 4. Cloudflare Python Workers (Pyodide inside workerd) — context for the "Cloudflare migrated Python infra" claim

**Sources (verified snippets):** https://blog.cloudflare.com/python-workers , https://blog.cloudflare.com/python-workers-ga , https://www.infoq.com/news/2026/09/cloudflare-python-workers-ga
- Python Workers run CPython via Pyodide (WASM) inside workerd (the Rust+V8 runtime). GA Sept 2026, 2 years after beta; needed PEP 783 (PyEmscripten packaging standard) to make native-extension packages available.
- The workerd runtime itself is C++/Rust; the earlier "PyPy" era and its Rust replacement is being verified by research delegate #1 (task 20261025_13).

## Notes
- The CB Insights piece on Christopher Chedeau (Vjeux) migrating a 100k-line TS app (Replay) to Rust with Claude 3 in ~1 month surfaced in search but was NOT extracted yet — treat as single-source until delegate #2 or a later check confirms.
