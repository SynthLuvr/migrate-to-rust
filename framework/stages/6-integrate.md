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
5. Cutover on clean diff window — resolved at the `cutover` gate:
   **human approval** at L0/L1; `policy:CUT-1` at L2/L3 (approved only at
   L3 with `publish_allowed` iff shadow diff rate = 0 over ≥ N samples AND
   canary green ≥ M minutes AND rollback actually tested; at L2 the gate
   resolves *rejected* — publish disabled, the run completes on the
   dual-build). Record the shadow/canary/rollback evidence on the gate
   either way.
6. Keep the dual-build state: per-unit Python fallback remains callable until
   Stage 7 retires it deliberately.
7. Document the rollback path (previous wheel pin) in the release notes.

## Exit criteria
- [ ] Shadow diff window clean (rate agreed with the operator/policy)
- [ ] Canary SLOs met; memory and CPU measured, not estimated
- [ ] Cutover gate resolved (`decided_by: human` at L0/L1,
      `policy:CUT-1` at L2+, evidence journaled)
- [ ] Rollback path tested, not just written

## Escalate
- Diff rate above threshold → back to Stage 5 with the diff corpus; do not
  tune thresholds to pass.
