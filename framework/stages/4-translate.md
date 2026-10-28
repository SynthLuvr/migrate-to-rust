# Stage 4 — Translation (the repair loop)

Purpose: port every unit via the bounded, oracle-ranked loop. This is where
the agent spends most of its budget. Loop spec: `MIGRATION_PROMPT.md`.

## Steps
1. Pop the next unit in leaf-first order; load its contract + Python source.
2. Delegate translation to **translator** subagents (`roles/translator.md`),
   K candidates — K resolved from **policy**, not judgment
   (`autonomy/policy.json` `candidates` block, `policy:CAND-1`, issue #59):
   K=3 below the unit size threshold (cheap to parallelize, big variance
   payoff), K=1 + bounded repair rounds (MAX_ROUNDS) above it or when
   run-level budget remaining < the floor fraction; repair rounds count as
   candidates for the oracle ranking, so rule 4 holds by construction at
   any K. Strict file partitioning: a delegate touches only its
   assigned files and never the state file. Units on **separate crates**
   may proceed in parallel; shared parents wait for all children. Units on
   **one shared crate** do not — see *Build contention* below (issue #55).
   With a reachable GLV server, run each delegate as a GLV session per
   `tools/glv-subagents.md`. Mechanize the whole per-unit loop — candidate
   files, briefs, the bounded verbatim-feedback repair rounds,
   gate-then-oracle ranking, and the verdict pack for `state.py` — with
   one command: `tools/delegate.py run-candidates` (issue #33), which
   resolves K from the policy schedule by default (pass no `--k`) and
   serializes the unit's K candidates and isolates every gate/build in a
   per-candidate sandbox; the state file stays orchestrator-only either
   way.
3. Run the mechanical import fixer first — most compile failures are
   mis-imported *real* modules, not hallucinated crates (>50% per
   arXiv:2606.08444).
4. Compile each candidate; feed compiler errors back as-is (strongest signal;
   explanation-only feedback is weak).
5. Rank by the **oracle** (`python framework/tools/oracle.py judge
   --manifest manifests/<unit>.json`), never by model confidence.
6. Bounded fix loop on the best candidate: executable diffs from the oracle
   only; `MAX_ROUNDS` and token budget per unit enforced in the state file.
7. On stall: record best candidate + exact failing diff → `status: escalated`.
8. Reviewer subagent (`roles/reviewer.md`) re-runs the oracle on **fresh
   inputs** and checks FFI/attribution before any verdict is recorded —
   over the GLV transport when in use (`tools/glv-subagents.md`). Fresh
   inputs come from regenerating the scaffolded corpus with a *different*
   seed: `python runs/corpora/gen_<unit>.py --seed <fresh> --out
   corpus-fresh.jsonl` (issue #32) — never re-judge the translator's own
   corpus.
9. Update state after every unit; Python fallback stays live for unported units.

## Build contention — one crate, one verify loop at a time (issue #55)

File partitioning does not partition *feedback*: every delegate's
`cargo build` / `clippy` / `maturin develop` compiles the whole shared
crate, so a sibling's half-written module fails everyone's gates. In a
real 5-delegate run, one delegate exhausted its entire budget triaging
other units' compile errors and never wrote its own file. Therefore:

- Delegates on one crate run **sequentially** — or one tranche at a time,
  with the crate green between tranches. Parallelism stays available
  across *separate crates* and (within a unit) inside
  `delegate.py run-candidates`, which serializes the K candidates itself.
- `tools/delegate.py run-candidates` owns build isolation (default,
  `--no-isolate-builds` to disable): each candidate's gates and the wheel
  build run in a per-candidate **sandbox copy** of the crate — every
  other candidate's module stripped (file deleted, its `mod` line
  removed) — under the unit's own `CARGO_TARGET_DIR`, so compiler
  feedback cites only the candidate's own file. The real crate stays the
  authoritative tree the delegates write into.
- Stage 3's pre-wired placeholder modules (see `3-scaffold.md` step 2)
  keep the crate green with *missing* units — they do not help with
  half-written ones, so the serialization/isolation rules above still
  apply even with placeholders in place.
- Delegate briefs and `roles/translator.md` (rule 9) carry the rule
  verbatim: compile errors in files that are not yours are not yours to
  fix — report and wait.

## Exit criteria
- [ ] Every unit: `ported` / `divergent_explained` / `escalated` — none left
      ambiguous
- [ ] All `divergent_explained` units carry written explanations (Volatility3
      shipped at 68.9% exact parity because divergences were explained)
- [ ] Budget ledger within projection; deviations reported

## Escalate
- Any unit hitting `MAX_ROUNDS` twice → escalate with minimal repro. At
  L2+ (the default) policy rule ESC-1 resolves it as **defer** — the unit
  keeps its Python fallback live, the backlog continues, and the
  escalation drains into the end-of-run dossier (`tools/state.py mark`
  resolves the gate automatically); at L0/L1 it waits in the dossier for
  the operator between runs — never a mid-run pause.
- Reviewer/translator disputes on semantics → record both readings in the
  unit's notes, keep the literal translation (the conservative default),
  and route the dispute into the escalation dossier; at L2+ it defers via
  ESC-1 like any stall, and the operator arbitrates between runs.
- Temptation to weaken the oracle to make a unit pass → this is the ScanCode
  failure mode; forbidden. Escalate instead.
