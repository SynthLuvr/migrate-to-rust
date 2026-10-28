# framework/ — executable packaging of the research

This directory turns the research design (`../SUMMARY.md`) into artifacts an AI
agent can execute directly, rather than merely read.

## Run it one of three ways

All relative paths in these artifacts — `stages/<n>-*.md`, `roles/*.md`,
`tools/README.md`, `knowledge/substitutions.json`,
`state/migration-state.schema.json` — live in **this framework checkout**,
not in the repo being migrated.

1. **goose (recommended)** — `goose run --recipe framework/recipe.yaml`
   The recipe is the orchestrator; it enforces stage order, gate resolution
   per the pre-authorized autonomy level, and
   the state file. Adjust `settings:` to your provider/model. Run it from
   the **root of this `migrate-to-rust` checkout** — the recipe references
   `framework/...`, `SUMMARY.md`, `TOOLCHAIN.md`, and `research/...`
   relative to that root — and point at a target project elsewhere on disk
   via the recipe's `TARGET_PYTHON_PROJECT` parameter (absolute path), not
   by `cd`-ing into the target repo.
   **Unattended runs (autonomy L2/L3, issue #22)** — put the deterministic
   driver in charge instead; it owns the loop and shells out to any CLI
   agent (goose included) as the per-stage worker, resolving every gate by
   [`autonomy/policy.json`](autonomy/policy.json) in code:

   ```console
   python framework/tools/state.py init framework/state/migration-state.json \
     --name myproject --python-path src/myproject --autonomy-level L2
   python framework/tools/pipeline.py run framework/state/migration-state.json \
     --agent-cmd 'goose run --recipe {framework}/recipe.yaml'
   ```

   The run always ends in a terminal state (`complete`, `no_go_measured`,
   `budget_exhausted`, or `failed_safely` — never "awaiting input") with a
   run report, the state file, the journal, and an escalation dossier.
2. **Any CLI agent** (Claude Code, Codex, Aider) — paste
   [`MIGRATION_PROMPT.md`](MIGRATION_PROMPT.md) as the agent's system context
   (`CLAUDE.md` / `AGENTS.md` / `CONVENTIONS.md` in the target repo), and tell
   the agent to load the current `stages/<n>-*.md` checklist when it enters a
   stage. The prompt's paths resolve only inside this framework checkout, so
   pick **one**:
   - **(a) Record the checkout's absolute path.** Append to the same file
     you pasted the prompt into, e.g.:

     ```markdown
     ## Framework checkout
     Every framework-relative path in the prompt above (`stages/…`, `roles/…`,
     `tools/…`, `knowledge/…`, `state/…`, `../SUMMARY.md`) resolves against:
     `/abs/path/to/migrate-to-rust/framework`
     ```

     The agent then reads every framework file from that path; the target
     repo holds only the code under migration.
   - **(b) Vendor the framework into the target repo** —
     `cp -r /abs/path/to/migrate-to-rust/framework ./framework/` (or a git
     subtree). The prompt's relative references then resolve as-is; keep
     the copy in sync when the framework changes.
3. **Manual / hybrid** — a human orchestrates stage-by-stage using the same
   checklists, with the agent doing stages 3–4 under supervision.

## File map

| Path | Role |
|---|---|
| `MIGRATION_PROMPT.md` | The distilled harness: 10 hard rules, pipeline table, repair-loop spec, escalation policy, failure-mode table. **This is "the prompt."** |
| `recipe.yaml` | goose orchestrator recipe wrapping the same rules — never prompts and never asks for human input: gates resolve in code (`pipeline.py resolve-gates`), a missing target ends the run `failed_safely`, and new runs default to autonomy L2 |
| `stages/0..7-*.md` | Per-stage checklists: purpose, steps, **exit criteria**, escalation triggers |
| `roles/translator.md` | Subagent instructions for mechanical per-unit translation |
| `roles/reviewer.md` | Subagent instructions for adversarial parity + PyO3-FFI review |
| `tools/*.py` + `tools/README.md` | The 8 deterministic helpers — implemented, stdlib-only, JSON-in/JSON-out, acceptance-tested in `tools/tests/` — plus `tools/pipeline.py`, the deterministic driver for unattended L2+ runs (issue #22); specs, conventions, and quick reference in `tools/README.md` |
| `autonomy/policy.json` | The autonomy policy layer (issue #22): levels L0–L3 and one machine-decidable rule per gate with conservative defaults — evaluated only in code, journaled, never LLM self-approval. Default level L2; the agent never asks a human mid-run at any level (at L0/L1 gates stay open and the run ends with the dossier for between-runs operator review) |
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
