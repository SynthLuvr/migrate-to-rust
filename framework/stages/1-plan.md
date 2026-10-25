# Stage 1 — Static analysis & decomposition

Purpose: turn the signed scope into a dependency-ordered, portability-scored
unit backlog.

## Steps
1. Parse scoped modules with `tree-sitter-python`; build import/call graph.
2. Segment into units — module > class > function — each **independently
   verifiable** (its contract tests run in isolation).
3. Order **leaf-first** (no forward deps). AlphaTrans: naive repo-level
   ordering produced non-compilable output; dependency order reached 99.1%
   syntactic correctness.
4. Per unit: mypy/pyright types, covering tests, invariants from
   docstrings/assertions, golden corpora where output is deterministic.
5. Attach Stage 0 risk flags (reflection, dynamic dispatch, C extensions) to
   the affected units.
6. **Propose** data-structure redesigns where the Python shape fights Rust
   (dict-of-dicts → typed structs/enums; reference-heavy graphs →
   ownership-friendly designs). Figma: 20% p99 from a data swap alone.
   Proposals only — human sign-off gate; never improvised in Stage 4.
7. Write all units into `state/migration-state.json`: id, paths, deps[],
   order index, contract refs, risk flags, budget, `status: pending`.

## Exit criteria
- [ ] Every unit has deps, order, type signature, covering tests
- [ ] Cycle check clean; leaf-first order recorded
- [ ] Redesign proposals written and human-approved
- [ ] No unit contains an unflagged risky construct

## Escalate
- Units too entangled to isolate (singletons, import-time side effects) →
  propose a Python-side boundary refactor first, or descope. Isolated-file
  translation breaks on shared dependencies (Salesforce Apex lesson).
