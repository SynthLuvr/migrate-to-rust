# migrate-to-rust

A research-grounded design for a framework that helps **AI agents migrate Python projects to Rust** — built from verified case studies of real AI-driven and human-driven Rust migrations (2024–2026), the academic literature on LLM code translation, and a live-verified survey of ~80 existing tools.

## Documents

| Document | What it gives you |
|---|---|
| **[SUMMARY.md](SUMMARY.md)** | The research synthesis: verified case studies with numbers, the 10 evidence-backed requirements, the framework architecture, honest caveats |
| **[TOOLCHAIN.md](TOOLCHAIN.md)** | The tool list, organized by pipeline stage — what to adopt off-the-shelf vs what the framework must build |
| **[research/](research/)** | 6 source files (~1,100 lines): case studies, manual-migration lessons, tool catalog with maintenance verification, academic literature, idiom/ecosystem mapping — every quantitative claim carries its source URL; unverifiable claims are flagged |
| **[framework/](framework/)** | The executable packaging of the research: agent prompt, goose orchestrator recipe, 8 stage checklists, translator/reviewer roles, 8 gap-tool specs, the GLV subagent-transport guide, crate-substitution knowledge, the migration state schema, and a [worked example](framework/examples/) showing the artifacts a real run leaves behind |
| **[scripts/](scripts/)** | CI checker scripts (stdlib-only): `check_json.py` parse-checks every JSON file, `check_links.py` resolves every relative markdown link (plus an optional `--external` citation URL spot-check) |

## The one-paragraph thesis

Successful AI-driven ports (Volatility3: 29k LOC in 68 h for ~$3k of tokens at 68.9% exact parity; Microsoft's Copilot runtime: 430k→800k LOC for ~$120k; Cloudflare's tsrs: written by 20+ Claude subagents with zero human code) all share one shape: **a stable API, a strong test suite used as the specification, and a harness that loops the model through compile→test→fix against an equivalence oracle until parity or an explained divergence.** The framework *is* the harness: select hot paths by profiling evidence, decompose with static analysis into a dependency-ordered backlog, judge-first, gate on the full test suite, preserve license attribution mechanically, ship as a drop-in PyO3 wheel, and keep an upstream-tracking maintenance mode afterward. Everything else in the pipeline exists as maintained tooling today (PyO3, maturin, py-spy, Scalene, tree-sitter, LibCST, Hypothesis, miri, clippy, Criterion, OpenHands/Aider/goose); the eight components no tool provides are listed at the end of [TOOLCHAIN.md](TOOLCHAIN.md) — that's the build surface.

## CI

Every push and PR ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs, with no LLM calls: `goose recipe validate` + a `--render-recipe` smoke test against a pinned goose (catches recipe-format drift like the 1.53 `author`/`context` breakage), JSON parse checks, JSON Schema validation of the state file (including rejecting deliberately incomplete units), an internal-markdown-link check, and the tools test-suite. A best-effort, non-blocking citation URL spot-check runs alongside.

## What the evidence flatly rules out

- **Whole-program, one-shot translation** — fails to compile at repo scale (AlphaTrans), succeeds only 21–47% single-shot on real code (Pan et al.).
- **Trusting benchmarks without parity checks** — the ScanCode Rust port claimed 10–100× and was *wrong*; the original Python matched it once given similar optimization effort.
- **Deterministic transpilation as a strategy** — every prior Python→Rust transpiler is dead or archived; the field converged on LLM + harness.
- **Formal equivalence checking** — not practical for dynamic languages today; differential testing is the state of practice.
- **"Cloudflare migrated PyPy to Rust"** — unverifiable retelling; the real stories are tsrs (TypeScript→Rust by agents), Pingora/FL2 (nginx→Rust), and Pyodide-in-workerd (interop, not translation).

## License

[MIT](LICENSE) — free to use, adapt, and redistribute, provided the license notice is retained. The research digests and the framework artifacts (recipe, prompts, stage checklists, schema) are all covered.

