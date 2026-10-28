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
   - `Option<T>` signature args conflate omitted with explicit `None`
     (both extract to `None`). When the Python default is observable
     *and* explicit `None` is legal input — e.g. `to_float(value,
     default=0.0)` called with `default=None` — demand the manual
     `#[pyo3(signature = (*args))]` + `args.get_item(i)` binding:
     omitted → literal default, `is_none()` → Python semantics.
     Corollary: pyo3 signature defaults are arbitrary Rust expressions,
     so a plain literal default cannot feed a `Py`-typed parameter
     (`pyo3_review.py` PYO3-OPTION lints this)
3. **Semantics drift:** silent coercions, dropped error branches, changed
   iteration/dict order that the normalization rules wrongly forgive,
   integer/float distinctions (`isize` vs Python's arbitrary precision),
   and CPython error wording per operator class: arithmetic raises
   `unsupported operand type(s) for *: 'NoneType' and 'float'` while
   comparisons raise `'<' not supported between instances of 'float' and
   'NoneType'` — one format string for both diverges byte-wise because
   the oracle compares errors type-and-message (operand order too).
4. **Attribution:** license header + provenance present (`// Ported from
   <python_path> [@ <sha>] [(<license>)]` per unit file — the path token
   must name the unit's python_path; `// Scaffold provenance: <detail>`
   for files not derived from a unit); NOTICE files intact; comments not
   stripped; variable names not obfuscated.
5. **Hygiene:** `clippy` pedantic + `rustfmt` clean; no new `unsafe` without
   a justification comment; no new dependencies outside
   `knowledge/substitutions.json`.

## Verdict
Return one of: `APPROVE` / `FIX` (numbered list) / `ESCALATE` (with a minimal
reproduction and both candidates attached). Every divergence gets a written
mechanistic explanation — "probably fine" is not a verdict.
