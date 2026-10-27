# framework/ — executable packaging of the research

This directory turns the research design (`../SUMMARY.md`) into artifacts an AI
agent can execute directly, rather than merely read.

## Run it one of three ways

1. **goose (recommended)** — `goose run --recipe framework/recipe.yaml`
   The recipe is the orchestrator; it enforces stage order, human gates, and
   the state file. Adjust `settings:` to your provider/model.
2. **Any CLI agent** (Claude Code, Codex, Aider) — paste
   [`MIGRATION_PROMPT.md`](MIGRATION_PROMPT.md) as the agent's system context
   (`CLAUDE.md` / `AGENTS.md` / `CONVENTIONS.md` in the target repo), and tell
   the agent to load the current `stages/<n>-*.md` checklist when it enters a
   stage.
3. **Manual / hybrid** — a human orchestrates stage-by-stage using the same
   checklists, with the agent doing stages 3–4 under supervision.

## File map

| Path | Role |
|---|---|
| `MIGRATION_PROMPT.md` | The distilled harness: 10 hard rules, pipeline table, repair-loop spec, escalation policy, failure-mode table. **This is "the prompt."** |
| `recipe.yaml` | goose orchestrator recipe wrapping the same rules |
| `stages/0..7-*.md` | Per-stage checklists: purpose, steps, **exit criteria**, escalation triggers |
| `roles/translator.md` | Subagent instructions for mechanical per-unit translation |
| `roles/reviewer.md` | Subagent instructions for adversarial parity + PyO3-FFI review |
| `tools/README.md` | Build specs for the 8 verified gaps — deterministic helpers to build **before** migrating |
| `knowledge/substitutions.json` | Machine-readable crate mapping + no-equivalent zones (from `research/5`) |
| `state/migration-state.schema.json` | The dual-build state manager schema — the resumable, per-unit-reversible record |

## How the research became a framework (the transformation)

1. **Architecture → state machine.** SUMMARY.md §3's 8 boxes became 8 stage
   files, each with exit criteria the agent must *record*, not just claim.
2. **Evidence → hard rules.** Each of the 10 requirements is phrased as a
   violation-is-failure rule with its named case study attached (ScanCode,
   AlphaTrans, Bun, VERT...) so the agent can't rationalize around it.
3. **Knowledge pack → context.** research/5's Tier-1 embed list + the crate
   substitution table become agent context and a JSON mapping.
4. **The verified gaps → tools.** The 8 components no tool provides
   (equivalence oracle, port-plan generator, attribution checker, ...) are
   specified as deterministic scripts in `tools/README.md`, with acceptance
   tests. Google's rule applies: deterministic tooling for location and
   validation; the LLM only for edits.

## Before first use

- [ ] Re-verify crate maintenance in `knowledge/substitutions.json` (verified
      2026-10-25; ecosystems move)
- [ ] Scaffold Stage 3 from [SynthLuvr/rust-template](https://github.com/SynthLuvr/rust-template)
      — the gates come pre-wired; add nextest/miri/maturin per `stages/3-scaffold.md`
- [ ] Build or stub the `tools/*.py` helpers to their acceptance criteria —
      the parity oracle (gap #1) is non-negotiable before any translation
- [ ] Point `recipe.yaml` at your target project and provider/model
- [ ] Confirm the target's test suite is green and substantive enough to act
      as the spec (Stage 2 will stop you if not)
