# Stage 7 — Maintenance mode (permanent)

Purpose: the migration isn't done at merge. Bun shipped "100% tests green"
and still surfaced **19 post-merge regressions**; the regression tail is a
budgeted phase, not a surprise.

## Steps
1. **Upstream tracking** (tsrs/Vinext pattern): periodically diff the Python
   original; map changed regions to units in the state file; rerun parity on
   affected units; propose port updates as PRs — agents watch upstream, run
   compatibility tests, propose fixes.
2. **Regression budget**: pre-committed triage capacity per release cycle
   (plan ≈ 2× the migration effort over the tail; Microsoft's 430k-LOC port
   paid with "a few dozen regressions").
3. **Crate re-verification**: quarterly maintenance check of every crate in
   `knowledge/substitutions.json` (GitHub/crates.io activity) — research/3
   showed tools die silently; yesterday's verification expires.
4. Deprecation path: once a unit has been stable in Rust for the agreed
   window, retire its Python fallback from the dual-build state.
5. Keep the benchmark + reproduction corpus alive in CI so performance
   regressions surface like correctness ones.

## Exit criteria
- This stage never "completes" — success is: upstream changes propagate
  without human porting labor, and regressions are caught by the gates.

## Escalate
- Upstream change lands in a unit with `escalated`/`divergent_explained`
  status → the operator decides *between runs*: port-forward, re-diverge,
  or descope (recorded via `state.py resolve-gate --by human`; the run
  itself never pauses to ask).
- A load-bearing crate goes unmaintained → substitution review (Stage 1
  semantics, scoped).
