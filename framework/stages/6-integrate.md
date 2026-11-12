# Stage 6 — Integration & adoption

Purpose: ship the wheel, prove the target runtime actually runs it, and
adopt it — reversibly. Adoption is the point of the run: `run_status=complete`
means the target is ON the wheel (issue #85).

## Steps
1. Build + install the wheel (L3 may additionally `maturin publish` to an
   index — publishing is the L3-only `publish_allowed` pre-authorization and
   is NOT the cutover decision; a local install needs no publish).
2. Install as a drop-in: the state.py shims take over the callers' import
   names, Python API unchanged; the preserved Python originals stay
   importable as the fallback.
3. **Shadow run** in staging: production-shaped traffic into both
   implementations; production diffing via the oracle (sampling classifier:
   Equivalent / Divergent / Incomparable).
4. Canary: one instance (Discord rollout: load-test → one canary → fleet),
   metrics: latency, memory, error rate, diff rate — measured, recorded.
5. **Adoption gate** — resolved at the `cutover` gate by policy `CUT-1`,
   which is **correctness-only** (issue #85): approve = ADOPT (route the
   runtime imports to the wheel) iff runtime shadow parity is clean over the
   sample floor, the rollback was actually exercised, and the measured
   end-to-end delta is not below the regression floor (adopt unless the port
   is materially slower, e.g. > −5% E2E). The measured speedup is **always
   recorded on the gate as evidence** — a below-bar speedup is a value
   finding for the operator, never an adoption abort ("the port wasn't the
   win" is reported honestly and adopted anyway, per the honest-reporting
   rule). Adoption is pre-authorized at L2+ (`adopt_allowed`); at L0/L1 the
   gate stays open and the operator decides between runs. Record the
   shadow/canary/rollback evidence on the gate either way.
6. **Prove it**: `tools/pipeline.py verify-adoption <state> --target-root
   <repo> --python <target venv>` asserts, on the target repo, that the
   runtime imports resolve to the shims, the live side is the installed
   wheel (identity test through the target interpreter), and flipping the
   fallback env var restores the Python originals — the tested rollback
   path, live. The resulting `adoption` block (verified=true) is what the
   terminal invariant reads. A CUT-1 rejection is correctness's veto: the
   affected units stay on their Python fallback, are recorded as escalated
   (ESC-1 defer, dossier) or explicitly human-descoped — never left
   unclassified — and adoption is retried after Stage-5 repair.
7. Document the rollback path (the fallback env var + the previous wheel
   pin) in the release notes.
8. **Re-measure end-to-end with the ported units live** — the
   honest-reporting rule ("port wasn't the win → report honestly") is a
   measurement, not an opinion: `tools/bench_ab.py` runs the target's own
   bench command through the shim env var both ways (Python fallback vs
   ported), gates the timing on byte-identical payloads, and records the
   result on the gate as evidence (`--gate cutover`). The number is
   evidence, not a verdict: it feeds the regression floor (harm) and the
   operator's value review (worth) — never an automatic abandonment of the
   switch the run exists to make.

## Exit criteria
- [ ] Shadow diff window clean (rate agreed with the operator/policy)
- [ ] Canary SLOs met; memory and CPU measured, not estimated
- [ ] Adoption gate resolved (`decided_by: policy:CUT-1` at L2+ with
      evidence journaled, the measured speedup included; at L0/L1 the gate
      stays open and the run ends for operator review — nothing adopts
      mid-run)
- [ ] CUT-1 approved ⇒ the `adoption` block records verified=true (runtime
      imports on the wheel + live-side wheel identity + rollback flip) —
      `run_status=complete` is refused otherwise (issue #85)
- [ ] CUT-1 rejected ⇒ every affected unit classified (escalated via ESC-1
      or explicitly human-descoped with a recorded reason)
- [ ] Rollback path tested, not just written

## Escalate
- Diff rate above threshold → back to Stage 5 with the diff corpus; do not
  tune thresholds to pass.
- Speedup below the Stage-0 projection → record it on the gate and adopt
  anyway (correctness held); the operator reviews the value case between
  runs. Do NOT descope units over it — "below-bar speedup" is not a valid
  descope reason.
