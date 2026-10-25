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
5. Cutover on clean diff window — **human approval required**.
6. Keep the dual-build state: per-unit Python fallback remains callable until
   Stage 7 retires it deliberately.
7. Document the rollback path (previous wheel pin) in the release notes.

## Exit criteria
- [ ] Shadow diff window clean (rate agreed with human)
- [ ] Canary SLOs met; memory and CPU measured, not estimated
- [ ] Cutover approved by human
- [ ] Rollback path tested, not just written

## Escalate
- Diff rate above threshold → back to Stage 5 with the diff corpus; do not
  tune thresholds to pass.
