# Stage 0 — Feasibility & hotspot selection

Purpose: decide **whether** and **what** to port, from measurement. Nothing
else happens until this is signed.

## Steps
1. Deterministic baseline of a representative workload (cProfile/pyperf).
2. `py-spy` flamegraphs on the live process → CPU hot paths.
3. `Scalene` per-line CPU/GPU/memory attribution → allocation-dense lines
   (allocation density is a port-payoff signal).
4. `memray` allocation flamegraphs → find **large long-lived object graphs**
   (caches, registries). These predict the biggest wins — GC scan cost drove
   Discord's ~500 ms spikes and Figma's p99.
5. Classify each hotspot **CPU-bound (candidate)** vs **I/O-bound (NO-GO)**.
   Record negative signals explicitly; porting I/O-bound code buys nothing.
6. Portability scan (Semgrep/CodeQL, both languages): dynamic dispatch,
   reflection, monkey-patching, C extensions, task queues → risk flags per module.
7. Map dependencies against `knowledge/substitutions.json`; list any with no
   maintained Rust equivalent for human decision.
8. Project the saving: bar = **≥ 50% of end-to-end time** (Google). Estimate
   tokens ≈ $0.17/LOC × scoped LOC + regression tail ≈ 2× migration effort.

## Output
`port-report.md`: hotspots ranked with profiler evidence, proposed scope
(component(s), never "the repo"), projected budget, no-equivalent deps,
GO/NO-GO recommendation.

## Exit criteria
- [ ] Hotspot list backed by profiler output, not intuition
- [ ] Scope drawn and signed by human
- [ ] No-equivalent dependencies surfaced
- [ ] Human signed GO (or NO-GO recorded)

## Escalate
- Projection < 50% → recommend NO-GO in writing. An *optimized Python* is a
  legitimate outcome: ScanCode's original matched its Rust port once given
  similar optimization effort.
- "Migrate everything" pressure → present Volatility3/pydantic/orjson
  precedent: every verified win was scoped, hot-path-only.
