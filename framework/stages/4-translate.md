# Stage 4 — Translation (the repair loop)

Purpose: port every unit via the bounded, oracle-ranked loop. This is where
the agent spends most of its budget. Loop spec: `MIGRATION_PROMPT.md`.

## Steps
1. Pop the next unit in leaf-first order; load its contract + Python source.
2. Delegate translation to **translator** subagents (`roles/translator.md`),
   K = 3 candidates. Strict file partitioning: a delegate touches only its
   assigned files and never the state file. Different units may proceed in
   parallel; shared parents wait for all children. With a reachable GLV
   server, run each delegate as a GLV session per `tools/glv-subagents.md`
   — one worktree per in-flight unit, `-c<k>` session names.
3. Run the mechanical import fixer first — most compile failures are
   mis-imported *real* modules, not hallucinated crates (>50% per
   arXiv:2606.08444).
4. Compile each candidate; feed compiler errors back as-is (strongest signal;
   explanation-only feedback is weak).
5. Rank by the **oracle** (`oracle.py`), never by model confidence.
6. Bounded fix loop on the best candidate: executable diffs from the oracle
   only; `MAX_ROUNDS` and token budget per unit enforced in the state file.
7. On stall: record best candidate + exact failing diff → `status: escalated`.
8. Reviewer subagent (`roles/reviewer.md`) re-runs the oracle on **fresh
   inputs** and checks FFI/attribution before any verdict is recorded —
   on a different model pin, over the GLV transport when in use
   (`tools/glv-subagents.md`).
9. Update state after every unit; Python fallback stays live for unported units.

## Exit criteria
- [ ] Every unit: `ported` / `divergent_explained` / `escalated` — none left
      ambiguous
- [ ] All `divergent_explained` units carry written explanations (Volatility3
      shipped at 68.9% exact parity because divergences were explained)
- [ ] Budget ledger within projection; deviations reported

## Escalate
- Any unit hitting `MAX_ROUNDS` twice → human queue with minimal repro.
- Reviewer/translator disputes on semantics → human arbitration, both
  candidates attached.
- Temptation to weaken the oracle to make a unit pass → this is the ScanCode
  failure mode; forbidden. Escalate instead.
