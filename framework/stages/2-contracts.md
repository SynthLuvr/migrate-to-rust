# Stage 2 — Contracts & the parity judge

Purpose: build and **validate the referee** before any Rust exists. The suite
is the spec; the judge is what makes "correct" decidable.

## Steps
1. Run the existing suite; record the green baseline. Quarantine
   failing/flaky tests explicitly with reasons — never silently.
2. Per unit, write `contract.json`: covering tests, type signature,
   invariants, golden corpora.
3. Build the equivalence oracle (implemented: `tools/oracle.py`; see
   `tools/README.md` "Implementation notes"):
   - generate inputs with Hypothesis strategies derived from contracts
   - run the Python reference; capture structured output + hash
   - classify every run **Equivalent / Divergent / Incomparable**
   - document normalization rules — `python framework/tools/oracle.py rules`
     prints them (float tolerance, dict order, unicode NFC, int/float
     distinctness, error type/message policy)
4. **Judge-first validation — hard gate:** run
   `python framework/tools/oracle.py self-test` — it injects the 5 canonical
   bug classes (off-by-one, dropped branch, swapped args, wrong type
   coercion, swallowed error) through the production judging path and must
   catch 5/5. CI re-runs this on every change.
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
- Suite too weak to serve as spec on the scoped units → STOP. Record the
  gap, keep the affected units on their Python fallback (deferred into the
  escalation dossier), and let the operator strengthen the suite *between
  runs* — never pause mid-run to co-write tests. ScanCode was only
  portable because 90k tests existed; a thin suite means no oracle.
