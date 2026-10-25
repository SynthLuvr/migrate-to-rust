# Stage 2 — Contracts & the parity judge

Purpose: build and **validate the referee** before any Rust exists. The suite
is the spec; the judge is what makes "correct" decidable.

## Steps
1. Run the existing suite; record the green baseline. Quarantine
   failing/flaky tests explicitly with reasons — never silently.
2. Per unit, write `contract.json`: covering tests, type signature,
   invariants, golden corpora.
3. Build the equivalence oracle (spec: `tools/README.md` #1):
   - generate inputs with Hypothesis strategies derived from contracts
   - run the Python reference; capture structured output + hash
   - classify every run **Equivalent / Divergent / Incomparable**
   - document normalization rules (float tolerance, dict order, timestamps)
4. **Judge-first validation — hard gate:** inject ≥ 5 deliberately broken
   implementations (off-by-one, dropped branch, swapped args, wrong type
   coercion, swallowed error). The oracle must catch 5/5.
5. Mutation-check the judge: `mutmut` on a sampled unit; hand-verify the
   judge kills sampled survivors.
6. Record judge-validation results in the state file.

## Exit criteria
- [ ] Baseline recorded; flaky tests quarantined with reasons
- [ ] Oracle classifies 3-ways and explains divergences
- [ ] Normalization rules written down
- [ ] Injected bugs caught 5/5
- [ ] Judge is deterministic (hash/structured/property) — **never an LLM**
      (CETBench: LLM judges unreliable)

## Escalate
- Suite too weak to serve as spec on the scoped units → STOP. Write
  characterization tests with the human before Stage 4. ScanCode was only
  portable because 90k tests existed; a thin suite means no oracle.
