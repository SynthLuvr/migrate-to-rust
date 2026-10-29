# ADR 0001 — The single-session run model (one command, start to finish, multi-day)

Status: accepted · 2026-10-29 · supersedes the agent-driven "orchestrator session" run model

## Acceptance criteria (binding)

1. **One invocation takes a migration start to finish.** The operator runs one
   command; the migration reaches a terminal state (`complete` /
   `no_go_measured` / `budget_exhausted` / `failed_safely`) with no human
   interaction anywhere in between. If the run stops and needs to be
   "resumed", the implementation is broken — by definition.
2. **Runs are day-scale.** Real migrations take days of wall clock (think
   multi-days, not 3 hours). Any mechanism that caps a run at a few hours is
   a defect, not a safety feature.
3. **No agent-specific packaging.** The framework must not depend on goose
   (or any single agent) — the agent is a swappable CLI behind a template.
   The goose recipe is removed outright.

## Root cause of the observed failure (the "~3 hours then it stops" bug)

Two coupled defects, confirmed against run `20261029_58` (wicksmith, stage 4/7
parked with `run_status=pending`, 17 units pending):

1. **Work chunks were stage-sized but contexts are not.** The driver invoked
   ONE agent context per stage with `timeout=3600` s and
   `max_agent_retries_per_stage: 2` — three attempts, ≈3 h hard ceiling per
   stage. Stage 4 (translate) is intrinsically bigger than any single context
   (25 units for wicksmith), so it could never complete inside the budget;
   the run always ended `failed_safely` (driver mode) or parked mid-stage
   (agent-driven mode). The "3 hours" operators observed is exactly
   3 attempts × 1 h.
2. **Outside the driver, the session itself owned the run.** An
   agent-driven "orchestrator session" held the whole migration in one LLM
   context. When that context exhausted (~3 h), the agent wrapped up with a
   chat-level "resume" pointer and exited, leaving `run_status=pending`. The
   designed mitigation (`succession.py`) chains successor sessions, but only
   over its GLV HTTP transport — a second moving part that was not in the
   loop, so nothing continued the run. Session-level exhaustion was
   indistinguishable from "the run ended" to the operator.

Both defects share one wrong assumption: that an LLM context is a durable
home for a day-scale run. It is not. **The durable unit of execution is the
driver process; LLM contexts are disposable work buffers.**

## Design

### R1 — The session is the process

One invocation of `pipeline.py run` IS the session. It owns the stage
machine, gates, budgets, and `run_status` from init to a terminal state. It
does not stop because a context filled up, an agent exited, or a unit failed.

### R2 — Unit-granular chunks at Stage 4

The driver, not a worker, iterates the unit backlog in dependency order. Per
unit it invokes the agent with a **unit brief** (`{unit}`/`{brief_file}`
placeholders): translate that unit, gate it, judge it, record it via
`state.py`, exit. The brief is a few KB, so every chunk starts with a
near-empty context — N units ⇒ N fresh contexts, each doing one bounded job.

### R3 — Context recycling is a non-event

Any chunk exit that does not advance the state (non-zero, timeout, or exit 0
with the unit still pending) is retried in a **fresh context** with the
failure tail appended to the brief, up to
`budget.max_agent_retries_per_unit` (policy, default 3). This is the
mechanism that makes multi-day runs possible: no context ever has to "hold"
the run.

### R4 — Escalate, don't halt (already policy: ESC-1, now enforced by the driver)

A unit that exhausts its fresh-context retries is marked `escalated`
(ESC-1 defer — Python fallback stays live, dossier entry) and the backlog
continues. One bad unit can never stop the run. Run-level terminal states
are reachable only through the policy gates and the run budgets.

### R5 — Day-scale run budgets

Policy budget becomes the only thing that can stop a healthy run:
`max_wall_clock_minutes: 10080` (7 days) and `max_tokens: 50_000_000`
(policy v1.4.0; existing state files keep their copied ceilings by design —
copy happens at init). Raising a ceiling remains an operator decision
between runs.

### R6 — No recipe, no goose coupling

`framework/recipe.yaml` is deleted. `pipeline.py run --target <project>`
boots a fresh run (derives name/package, `state.py`-equivalent init, L2) when
no state file exists — the single, agent-agnostic entrypoint. `--agent-cmd`
remains the portability boundary (goose / Claude Code / Codex / anything that
reads a brief and edits files). `MIGRATION_PROMPT.md` stays as the portable
system context; stage checklists and role sheets stay as the brief corpus.

## Consequences

- `runs/` wiping between runs stays fully supported: a wipe means "fresh
  run next time"; `--target` re-inits from zero. Nothing in the run model
  depends on leftover state.
- `succession.py` keeps working for GLV-chain operators but is no longer on
  the critical path of the acceptance criteria.
- CI loses the pinned-goose recipe job; the driver's own stub-agent
  acceptance tests (full L2 run → `complete`, unit recycling, stuck-unit
  escalation, `--target` boot) become the smoke test.
- Worker contexts still honor the dual-build safety model: every autonomous
  decision stays reversible per unit; escalated units keep Python live.

## Alternatives rejected

- **Bigger contexts / longer agent timeouts** — buys minutes, not days;
  the context ceiling always wins eventually.
- **Keeping the orchestrator session + auto-resume prompts** — a parked
  session is still a stopped run; "resume" is the failure mode this ADR
  forbids.
- **Making succession.py mandatory** — adds the GLV server as a single
  point of failure on the critical path.
