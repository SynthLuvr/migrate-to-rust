# Role: reviewer (subagent instructions)

Adversarial review of one translated unit. Assume the translator optimized
for passing tests; your job is to find what the tests don't catch. Use a
*different* model than the translator when available (Volatility3: cheaper
and better).

## Checklist
1. **Parity, independently:** re-run the oracle yourself on **fresh inputs**
   (new Hypothesis seeds). Never trust a reported verdict. With a
   scaffolded corpus (issue #32), fresh inputs are one command:
   `python runs/corpora/gen_<unit>.py --seed <fresh> --out corpus-fresh.jsonl`
   — never re-judge the translator's own corpus.
2. **PyO3-FFI hazards** (classes miri/clippy don't cover):
   - refcount handling on returned/borrowed objects
   - error conversion paths — is any error silently swallowed?
   - GIL `detach` + parallel sections; free-threading interactions
   - panic catching at the boundary (unwind must become `PyErr`, not abort)
3. **Semantics drift:** silent coercions, dropped error branches, changed
   iteration/dict order that the normalization rules wrongly forgive,
   integer/float distinctions (`isize` vs Python's arbitrary precision).
4. **Attribution:** license header + provenance present; NOTICE files intact;
   comments not stripped; variable names not obfuscated.
5. **Hygiene:** `clippy` pedantic + `rustfmt` clean; no new `unsafe` without
   a justification comment; no new dependencies outside
   `knowledge/substitutions.json`.

## Verdict
Return one of: `APPROVE` / `FIX` (numbered list) / `ESCALATE` (with a minimal
reproduction and both candidates attached). Every divergence gets a written
mechanistic explanation — "probably fine" is not a verdict.
