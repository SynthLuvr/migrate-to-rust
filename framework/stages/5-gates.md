# Stage 5 — Gates

Purpose: prove the whole port before integration. Passing a subset is the
documented way to ship a broken port.

## Steps
1. **Full** pytest suite against the Python-wrapped Rust wheel. Not a subset.
2. Property tests: Hypothesis ↔ `proptest` differential properties on the
   corpus; `cargo-fuzz` at the FFI boundary (untrusted input shapes).
3. `miri` over the test surface (tsrs caught a bug present **only** in
   optimized builds); `clippy` pedantic; `rustfmt` clean.
4. Mutation spot-check with `cargo-mutants` on sampled units — proves the
   gates can still detect wrongness after translation.
5. Benchmarks: matched corpora through the same entry points, Criterion vs
   Python baseline, **plus a reproduction script and committed corpus**.
   No number ships without its repro (ScanCode's 10–100× claim was wrong).
6. Attribution gate (`attribution.py`): every generated file carries its
   source header + provenance; NOTICE files byte-identical; no stripped
   comments masquerading as "independent rewrite".
7. Coverage proof: `coverage.py` + `cargo-llvm-cov` confirm both sides are
   actually exercised by the parity corpus.

## Exit criteria
- [ ] Full suite parity; flaky quarantines unchanged from baseline
- [ ] Property/fuzz campaigns clean or findings fixed
- [ ] Miri + clippy green
- [ ] Benchmark verdict with reproduction corpus committed
- [ ] Attribution check green
- [ ] Every remaining divergence classified + explained in the state file

## Escalate
- Any divergence without a mechanistic explanation → human review before
  Stage 6. "It's probably fine" is not a classification.
