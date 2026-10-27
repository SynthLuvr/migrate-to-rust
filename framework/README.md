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
| `tools/*.py` + `tools/README.md` | The 8 deterministic helpers — implemented, stdlib-only, JSON-in/JSON-out, acceptance-tested in `tools/tests/` (specs, conventions, and quick reference in `tools/README.md`) |
| `knowledge/substitutions.json` | Machine-readable crate mapping + no-equivalent zones (from `research/5`) |
| `state/migration-state.schema.json` | The dual-build state manager schema — the resumable, per-unit-reversible record |
| `examples/` | **The worked example** — a filled `migration-state.json` (one unit in each of the six statuses), a Stage-2 `contract.json`, a real oracle verdict showing the 3-way classification, and a stage-by-stage walkthrough ([`examples/README.md`](examples/README.md)) |

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

- [ ] Skim the worked example — [`examples/README.md`](examples/README.md)
      walks one fictional project through every artifact the stages
      produce (a filled state file, a per-unit contract, an oracle verdict
      showing all three classifications)
- [ ] Re-verify crate maintenance in `knowledge/substitutions.json` (verified
      2026-10-25; ecosystems move)
- [ ] Confirm rust-template is reachable — `git clone
      https://github.com/SynthLuvr/rust-template` must work (Stage 3
      instantiates the crate from it; see `stages/3-scaffold.md`)
- [x] The `tools/*.py` helpers are implemented and acceptance-tested
      (`python -m pytest framework/tools/tests/`); verify your environment
      runs them (Python ≥ 3.10, stdlib only) and re-run
      `python framework/tools/oracle.py self-test` — the parity oracle
      (gap #1) is non-negotiable before any translation
- [ ] Point `recipe.yaml` at your target project and provider/model
- [ ] Confirm the target's test suite is green and substantive enough to act
      as the spec (Stage 2 will stop you if not)
