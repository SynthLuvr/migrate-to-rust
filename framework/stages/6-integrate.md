# Stage 6 — Integration & cutover

Purpose: ship the wheel and move traffic, reversibly.

## Steps
1. `maturin publish` (or internal index): wheels per platform, per Python
   version incl. free-threaded `cp314t` if required.
2. Install as a drop-in: Python API unchanged; callers and imports untouched.
3. **Shadow run** in staging: production-shaped traffic into both
   implementations; production diffing via the oracle (sampling classifier:
   Equivalent / Divergent / Incomparable).
4. Canary: one instance (Discord rollout: load-test → one canary → fleet),
   metrics: latency, memory, error rate, diff rate.
5. Cutover on clean diff window — resolved at the `cutover` gate by
   policy `CUT-1` (approved only at L3 with `publish_allowed` iff shadow
   diff rate = 0 over ≥ N samples AND canary green ≥ M minutes AND
   rollback actually tested; at L2 — the default — the gate resolves
   *rejected*: publish disabled, the run completes on the dual-build; at
   L0/L1 the gate stays open, nothing publishes mid-run, and the operator
   decides between runs). Record the shadow/canary/rollback evidence on
   the gate either way.
6. Keep the dual-build state: per-unit Python fallback remains callable until
   Stage 7 retires it deliberately.
7. Document the rollback path (previous wheel pin) in the release notes.
8. **Re-measure end-to-end with the ported units live** — the honest-reporting
   rule ("port wasn't the win → report honestly") is a measurement, not an
   opinion: `tools/bench_ab.py` runs the target's own bench command through
   the shim both ways (Python fallback vs ported), gates the timing on
   byte-identical payloads, and records the result on the gate as evidence
   (`--gate`) — GO-1 reads the measured `end_to_end_saving_pct` over the
   Stage-0 projection.

## Exit criteria
- [ ] Shadow diff window clean (rate agreed with the operator/policy)
- [ ] Canary SLOs met; memory and CPU measured, not estimated
- [ ] Cutover gate resolved (`decided_by: policy:CUT-1` at L2+ with
      evidence journaled; at L0/L1 the gate stays open and the run ends
      for operator review — nothing publishes mid-run)
- [ ] Rollback path tested, not just written

## Escalate
- Diff rate above threshold → back to Stage 5 with the diff corpus; do not
  tune thresholds to pass.
