# framework/ — executable packaging of the research

This directory turns the research design (`../SUMMARY.md`) into artifacts an AI
agent can execute directly, rather than merely read.

## Run it one of three ways

All relative paths in these artifacts — `stages/<n>-*.md`, `roles/*.md`,
`tools/README.md`, `knowledge/substitutions.json`,
`state/migration-state.schema.json` — live in **this framework checkout**,
not in the repo being migrated.

1. **The driver (recommended, design/0001)** — one command, start to
   finish, no human interaction, day-scale runs. The driver process owns
   the stage machine, gates, and budgets; it recycles an agent context per
   chunk (per unit at Stage 4), so no LLM context window bounds the run —
   only the policy budget does. Any CLI agent works behind `--agent-cmd`
   (goose, Claude Code, Codex, ...); run from the **root of this
   `migrate-to-rust` checkout** and point `--target` at the project
   elsewhere on disk (absolute path), never by `cd`-ing into it:

   ```console
   python framework/tools/pipeline.py run \
     --target /absolute/path/to/myproject \
     --agent-cmd '<any CLI agent> {brief_file}'
   ```

   `--target` boots a fresh run when no state file exists (`runs/<name>/
   migration-state.json`, `state.py`-equivalent init, autonomy L2 by
   default — a state file without an autonomy block reads as the
   supervised L0, issue #27); an existing state file is driven in place.
   Wiping `runs/` between runs is the normal reset — the next invocation
   boots fresh. Stage-by-stage, the same thing spelled out:

   ```console
   python framework/tools/state.py init framework/state/migration-state.json \
     --name myproject --python-path src/myproject --autonomy-level L2
   python framework/tools/pipeline.py run framework/state/migration-state.json \
     --agent-cmd '<any CLI agent> {stage} {state} {stage_file} {framework}'
   ```

   **Memory containment for unattended runs (issue #74)** — bound the
   whole run independently of the session server with the shipped user
   slice, so a ballooning run dies alone (attributable scope unit) instead
   of taking the machine down (the failure mode behind #73):

   ```console
   mkdir -p ~/.config/systemd/user
   cp framework/tools/migration.slice ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemd-run --user --slice=migration.slice --scope \
     -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 \
     --unit=migration-myproject \
     python framework/tools/pipeline.py run \
       --target /absolute/path/to/myproject \
       --agent-cmd '<any CLI agent> {brief_file}'
   ```

   Per-child ceilings (judge children, gates, delegate agents, stage
   workers) are policy-resolved since issue #76 — the `memory` block of
   `autonomy/policy.json` (`policy:MEM-1`) is the DEFAULT of
   `--mem-limit-mb` / `--agent-mem-limit-mb` (judge children 2048 MB,
   toolchain children 4096 MB; a breach is a journaled ESC-1 unit
   escalation, never a service crash) — details and breach semantics in
   [`tools/glv-subagents.md`](tools/glv-subagents.md) § *Memory
   containment*.

   Preflight first (issue #31) — one command that machine-checks the
   "Before first use" list below against the target and records it in the
   state file (`preflight` block + `<state>.preflight.md`); it exits 2 on
   the hard prerequisites (suite not green, oracle self-test not 5/5,
   rust-template unreachable), so the run starts only when Stage 0's exit
   criteria are actually checkable:

   ```console
   python framework/tools/pipeline.py preflight \
     framework/state/migration-state.json --target-root /abs/path/to/target
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
| `design/0001-single-session-run.md` | The run model: one command, start to finish, day-scale — unit-granular chunks, context recycling, day-scale budgets, no agent-specific packaging |
| `stages/0..7-*.md` | Per-stage checklists: purpose, steps, **exit criteria**, escalation triggers |
| `roles/translator.md` | Subagent instructions for mechanical per-unit translation |
| `roles/reviewer.md` | Subagent instructions for adversarial parity + PyO3-FFI review |
| `tools/*.py` + `tools/README.md` | The 8 deterministic helpers — implemented, stdlib-only, JSON-in/JSON-out, acceptance-tested in `tools/tests/` — plus `tools/pipeline.py`, the deterministic driver for unattended L2+ runs (issue #22), and `tools/succession.py`, the session-chain harness that auto-spawns successor GLV sessions on session-level budget exhaustion so a run finishes from one prompt (issue #25); specs, conventions, and quick reference in `tools/README.md` |
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
- [ ] Boot the driver against your target — `python framework/tools/pipeline.py
      run --target /abs/path/to/project --agent-cmd '<any CLI agent> {brief_file}'`
- [ ] Confirm the target's test suite is green and substantive enough to act
      as the spec (Stage 2 will stop you if not)
- [ ] Or machine-check the checklist items above in one command (issue #31):
      `python framework/tools/pipeline.py preflight <state.json>
      --target-root /abs/target` — runs the target suite (recording wall
      time as the Stage 2 baseline), captures `pytest --collect-only` for
      Stage 2, re-proves the oracle self-test under the target interpreter,
      records the rust-template HEAD SHA for Stage 3 provenance, probes
      cargo/rustc/maturin/uv + the gate stack, resolves the target venv /
      interpreter / `Py_GIL_DISABLED` wheel flavor / derived `PYO3_PYTHON`,
      and checks substitutions freshness + the framework-vs-target
      interpreter match — all journaled into the state file
