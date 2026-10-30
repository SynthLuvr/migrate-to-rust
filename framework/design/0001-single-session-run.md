# ADR 0001 — The single-session run model (one prompt, start to finish, multi-day)

Status: accepted · 2026-10-29 · supersedes the agent-driven "orchestrator
session" run model · amended in-review: the binding unit is the operator's
**prompt** (criterion 1), the GLV successor chain is on the critical path
(criterion 2), and the driver is the single-process variant of the same
guarantee

## Acceptance criteria (binding)

1. **One prompt takes a migration start to finish — and the prompt is
   yours.** The operator hands the framework one prompt —
   `succession.py chain --target <project> --prompt-file <prompt>` — and
   the migration reaches a terminal state (`complete` / `no_go_measured` /
   `budget_exhausted` / `failed_safely`) with no human interaction
   anywhere in between. Session 1 receives that prompt **verbatim**: the
   protocol never authors the task prompt — no header, no briefing, no
   default (R7). With no state file yet, `--target` boots the run
   itself (package derived, autonomy L2) — that is mechanics, and
   mechanics is all the protocol writes. If the run stops and needs to
   be "resumed", the implementation is broken — by definition.
2. **A filling context is a scheduling event, never a stop.** When a
   session's context gives out, the session settles and the harness
   starts the successor automatically over the GLV API
   (`POST /api/sessions`) with a full resume brief composed from the
   state file. The whole mechanism is one rule — *settled session +
   non-terminal state ⇒ succeed* — guarded by the chain guardrails
   (depth cap, monotonic progress). No token thresholds, no preemptive
   stops, no second signal to maintain: the session knows its context
   is filling (the framework prompt binds it to record
   `budget_exhausted` and stop), and any session that settles without
   recording a terminal state is succeeded anyway. The operator is
   informed by a journaled stop-notice — never involved.
3. **Runs are day-scale.** Real migrations take days of wall clock
   (think multi-days, not 3 hours). Any mechanism that caps a run at a
   few hours is a defect, not a safety feature.
4. **No agent-specific packaging.** The framework must not depend on
   goose (or any single agent) — the agent is a swappable CLI behind a
   template. The goose recipe is removed outright.

## Root cause of the observed failure (the "~3 hours then it stops" bug)

Two coupled defects, confirmed against run `20261029_58` (wicksmith,
stage 4/7 parked with `run_status=pending`, 17 units pending) and run
`20261028_4` (session-level death at stage 4/7, ~600k of 5M run tokens
spent):

1. **Work chunks were stage-sized but contexts are not.** The driver
   invoked ONE agent context per stage with `timeout=3600` s and
   `max_agent_retries_per_stage: 2` — three attempts, ≈3 h hard ceiling
   per stage. Stage 4 (translate) is intrinsically bigger than any
   single context (25 units for wicksmith), so it could never complete
   inside the budget; the run always ended `failed_safely` (driver
   mode) or parked mid-stage (agent-driven mode). The "3 hours"
   operators observed is exactly 3 attempts × 1 h.
2. **Outside the driver, the session itself owned the run, and nothing
   continued it.** An agent-driven "orchestrator session" held the
   whole migration in one LLM context. When that context exhausted
   (~3 h), the agent wrapped up with a chat-level "resume" pointer and
   exited, leaving `run_status=pending`. Session-level exhaustion was
   indistinguishable from "the run ended" to the operator — succession
   was a design sketch, not code in the loop.

Both defects share one wrong assumption: that an LLM context is a
durable home for a day-scale run. It is not. **The durable unit of
execution is the state file; LLM contexts — and the processes that host
them — are disposable.**

## Design

### R1 — The run is the state file; executors are disposable

One invocation of `pipeline.py run` IS a session of the driver model;
one GLV orchestrator session IS a session of the chain model. Neither
owns the run — `migration-state.json` does (written only through
`state.py`). No executor stops the run because a context filled up, an
agent exited, or a unit failed; the stage machine, gates, budgets, and
`run_status` live above all of them. Two run models, one guarantee:

- **The one-prompt chain (the acceptance path for criteria 1–2):**
  `succession.py chain` wraps the whole run in GLV orchestrator
  sessions — session 1 rides the operator's prompt verbatim, every
  succession spawns the successor over the GLV API, and the run-level
  ceilings bind the entire chain.
- **The single-process driver (the same guarantee, one process):**
  `pipeline.py run` owns the stage machine in-process and recycles an
  agent context per chunk; no GLV on that path, and no session-level
  context to fill at all.

### R2 — Unit-granular chunks at Stage 4

The driver, not a worker, iterates the unit backlog in dependency
order. Per unit it invokes the agent with a **unit brief**
(`{unit}`/`{brief_file}` placeholders): translate that unit, gate it,
judge it, record it via `state.py`, exit. The brief is a few KB, so
every chunk starts with a near-empty context — N units ⇒ N fresh
contexts, each doing one bounded job.

### R3 — Context recycling is a non-event

Any chunk exit that does not advance the state (non-zero, timeout, or
exit 0 with the unit still pending) is retried in a **fresh context**
with the failure tail appended to the brief, up to
`budget.max_agent_retries_per_unit` (policy, default 3). This is the
mechanism that makes multi-day runs possible: no context ever has to
"hold" the run. The chain model gets the same property one level up:
N sessions ⇒ N fresh contexts, each carrying the operator's prompt or
the composed resume brief.

### R4 — Escalate, don't halt (already policy: ESC-1, now enforced by the driver)

A unit that exhausts its fresh-context retries is marked `escalated`
(ESC-1 defer — Python fallback stays live, dossier entry) and the
backlog continues. One bad unit can never stop the run. Run-level
terminal states are reachable only through the policy gates and the run
budgets.

### R5 — Day-scale run budgets

Policy budget is the only thing that can stop a healthy run:
`max_wall_clock_minutes: 10080` (7 days) and `max_tokens: 50_000_000`
(policy v1.4.0; existing state files keep their copied ceilings by
design — copy happens at init). Raising a ceiling remains an operator
decision between runs. There is no session-level budget in the policy:
context exhaustion is handled by succession (criterion 2), not
metering.

### R6 — No recipe, no goose coupling

`framework/recipe.yaml` is deleted. `pipeline.py run --target
<project>` boots a fresh run (derives name/package, `state.py`-equivalent
init, L2) when no state file exists — the driver's agent-agnostic
entrypoint — and `succession.py chain --target <project>
--prompt-file <prompt>` is the chain's (criterion 1). `--agent-cmd`
remains the driver's portability boundary (goose / Claude Code / Codex /
anything that reads a brief and edits files). `MIGRATION_PROMPT.md`
stays as the portable system context; stage checklists and role sheets
stay as the brief corpus.

### R7 — The task prompt is the operator's

The protocol composes mechanics, never the task: session 1 receives the
operator's prompt byte-for-byte, and a fresh chain without one refuses
rather than invent a default. What the protocol DOES write: the state
boot from `--target` (deterministic location, `runs/<name>/` under the
invocation directory), and each successor's **resume brief** — composed
from the state file (state path, position, escalation dossier, journal
tail, budget remaining, chain history, hygiene findings) because there
is no human around to author it. Pointing a session at the framework
checkout is the operator's prompt's business, per
`MIGRATION_PROMPT.md` § Path resolution — the documented convention.

## Consequences

- `runs/` wiping between runs stays fully supported: a wipe means
  "fresh run next time"; `--target` re-inits from zero — for both
  entrypoints. Nothing in the run model depends on leftover state.
- `succession.py` is on the critical path of criteria 1–2 (the
  one-prompt acceptance run); `pipeline.py run` carries the same
  guarantee for operators who prefer one process and no GLV. Neither
  is mandatory for the other.
- CI loses the pinned-goose recipe job. CI (no LLM calls, no live GLV)
  pins both paths: the driver's stub-agent acceptance run (full L2 run
  → `complete`, unit recycling, stuck-unit escalation, `--target`
  boot) and the chain's FakeGlv acceptance suite (one-prompt boot →
  `complete`, verbatim-prompt pinning, mid-run succession, guardrails,
  transport self-healing) plus a no-GLV `--dry-run` boot check.
- Worker contexts still honor the dual-build safety model: every
  autonomous decision stays reversible per unit; escalated units keep
  Python live.

## Alternatives rejected

- **Bigger contexts / longer agent timeouts** — buys minutes, not days;
  the context ceiling always wins eventually.
- **Keeping the orchestrator session + auto-resume prompts** — a parked
  session is still a stopped run; "resume" is the failure mode this ADR
  forbids.
- **Driver-only, with the chain demoted off the critical path** (the
  pre-amendment draft of this ADR) — rejected: it made criterion 2
  untrue. The driver never fills a context, so it never exercises GLV
  succession — but the operator's unit of work is one prompt, and the
  framework must keep a fresh context under that prompt automatically,
  over the GLV API, for as long as the run takes.
- **Making the chain mandatory / GLV a single point of failure on the
  driver path** — rejected the other direction too: the driver's
  zero-dependency path stays first-class, and the chain's transport is
  already self-bootstrapping and self-healing (issue #47).
- **Protocol-authored prompts** (chain-context headers, fresh-boot
  briefings, a default session-1 prompt) — rejected: the task is the
  operator's to define; a framework that rewrites the prompt has
  silently taken over the run. Protocol-authored text is confined to
  successor resume briefs, where no human is available to write it.
- **Proactive context metering** (polling `total_tokens`, token
  thresholds, preemptive stops) — rejected as unnecessary complexity:
  the settle rule (criterion 2) already makes context exhaustion a
  scheduling event with zero extra machinery; metering adds a second
  signal, a policy knob, and mid-flight kill risks for no behavioral
  gain.
