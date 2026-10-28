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
7. Map dependencies against `knowledge/substitutions.json`; list any with
   no maintained Rust equivalent — descoped via policy `DEP-1` (the
   module keeps its Python fallback), never improvised, never queued for
   a mid-run human decision.
8. Project the saving: bar = **≥ 50% of end-to-end time** (Google). Estimate
   tokens ≈ $0.17/LOC × scoped LOC + regression tail ≈ 2× migration effort.

## Output
`port-report.md`: hotspots ranked with profiler evidence, proposed scope
(component(s), never "the repo"), projected budget, no-equivalent deps,
GO/NO-GO recommendation.

## Exit criteria
- [ ] Hotspot list backed by profiler output, not intuition
- [ ] Scope drawn (the minimal tranche meeting the bar) and the go/no-go
      gate resolved in code — `policy:GO-1` at the default L2+ (journaled
      with the projection evidence); at L0/L1 the gate stays open and the
      run ends for operator review (never asked mid-run)
- [ ] No-equivalent dependencies surfaced and descoped (policy `DEP-1` —
      the module stays Python; descope, don't stop)
- [ ] GO recorded, or NO-GO recorded — NO-GO ends the run in the
      `no_go_measured` terminal state: a *successful* outcome, not a failure

## Escalate
- Projection < 50% → recommend NO-GO in writing. An *optimized Python* is a
  legitimate outcome: ScanCode's original matched its Rust port once given
  similar optimization effort.
- "Migrate everything" pressure → present Volatility3/pydantic/orjson
  precedent: every verified win was scoped, hot-path-only.
