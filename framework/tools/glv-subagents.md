# GLV — subagent transport for the migration pipeline

[GLV](https://github.com/SynthLuvr/glv) is a self-hosted web app that
drives goose (and claude) sessions from one server. Its JSON API is the
supported transport for this pipeline's **role separation**
(`MIGRATION_PROMPT.md`) when it runs as *separate processes* instead of
in-process subagent calls: every translator candidate and every reviewer
pass is a GLV session the orchestrator starts, waits on, and reads back.
The oracle still decides — GLV only carries the roles.

Source of truth for every path and field below: `GET /api/openapi.json`
on the running server.

## Prerequisites

1. `glv serve` is running (default port 3000; `-c/--max-concurrent`,
   default 6, is the run queue shared by all served agents).
2. The orchestrator holds an API token. Mint one on the server host:

   ```bash
   glv token create migrate-orchestrator
   # raw token on stdout; expires in 30 days; only its SHA-256
   # hash is stored
   ```

   No filesystem access to the GLV data dir (SSH port-forward, sibling
   container)? With `glv serve --allow-token-bootstrap`, a loopback-only
   `POST /api/tokens` mints a token with no credential. Never enable
   that flag behind a reverse proxy — loopback detection is the security
   boundary and proxies break it.
3. Export both values, and commit neither:

   ```bash
   export GLV_BASE_URL="http://localhost:3000"
   export GLV_API_TOKEN="glv_at_…"
   ```

   Auth is `Authorization: Bearer glv_at_…` on every `/api` path; POST
   accepts the bearer only, GET also accepts a browser session cookie.
   `/api/*` answers 401 as JSON — never an HTML login redirect.

## API cheat sheet

| Purpose | Call | Notes |
|---|---|---|
| Full schema | `GET /api/openapi.json` | machine-readable, bearer-authed |
| Model pins | `GET /api/models` | each model's `pin` is the exact spelling the `model` field accepts; resolve pins here, never free-typed |
| Start a subagent | `POST /api/sessions` | multipart form: `prompt` (required), `name?`, `working_dir?`, `model?`, `session_mode?` (`temp`), `uploads?` (≤50 MB each) → `201 {id, status: "started"\|"queued"}` + `Location: /api/sessions/:id` |
| Park until settled | `GET /api/sessions/:id/wait?timeout=<ms>` | long-poll; timeout default 30 000 ms, range 0–3 600 000, `-1` parks indefinitely. `200` = settled (terminal `status` + persisted `last_run` verdict); `202` = still live, call again. Wait, don't transcript-poll. |
| Read transcript | `GET /api/sessions/:id` | `{session, messages}`; `after=<index>` returns only messages past your cursor; `fields=meta` omits the transcript entirely (pass one or the other, not both → 422) |
| Queue a repair round | `POST /api/sessions/:id/prompt` | form: `prompt`; chains behind the session's queue |
| Stop a run | `POST /api/sessions/:id/stop` | budget exhaustion, runaway delegate |
| Branch a session | `POST /api/sessions/:id/fork` | retry a unit from a transcript prefix |
| Find sessions | `GET /api/sessions?active=true&working_dir=<prefix>&agent=<id>&limit=&offset=` | `working_dir` is a prefix filter |
| Live stream (optional) | SSE `/sessions/:id/events`, `/events/status` | same bearer header |

Run-status vocabulary: `queued | running | completed | stopped | error |
idle`. For the pipeline: `completed` → read the report; `stopped`/`error`
→ handle, then requeue once or escalate; `idle` → no active run.

Quick human lookups without HTTP: `glv sessions`, `glv view <id>`,
`glv logs`.

## One GLV session = one role invocation

**Including the orchestrator itself.** When the chain harness
(`tools/succession.py`, issue #25) drives a run, the orchestrator is a
*series* of GLV sessions — `run-<project>-s<n>` — so a whole migration
finishes from one human prompt: session 1 rides the operator's prompt;
when a session's context budget exhausts before the run reaches a
terminal state, the harness composes the successor's resume brief from
the state file (position, escalation dossier, journal tail, run-budget
remaining, hygiene findings), starts the next session over this API,
stops the predecessor if still live, journals a **stop-notice** (operator
informed, not involved), and resets `run_status` to `running` — all
through `state.py`. Guardrails: depth cap (`budget.max_chain_depth`,
policy default 10) and a monotonic-progress guard (a successor that
advances neither stage nor ≥1 decided unit ends the chain
`failed_safely`); the run-level ceilings bind the *whole* chain (tokens
sum through the single state file, wall clock sums over sessions).
`budget_exhausted` survives as a final status only when the run-level
ceilings hit — or in degraded mode, GLV unreachable. GLV unreachable at
chain start is a refusal (exit 2) naming the pre-#25 fallback.

- **Name deterministically.** `unit-<unit-id>-translator-c<k>` and
  `unit-<unit-id>-reviewer-r<n>` (retries get a `-retry` suffix);
  orchestrator chain sessions `run-<project>-s<n>`. The
  names surface in `GET /api/sessions` listings and go into the state
  file's per-unit `sessions[]` (with `notes` for prose) or the top-level
  `session_chain.sessions[]` (orchestrator chain) as the audit
  trail — both defined in `../state/migration-state.schema.json`.
- **`working_dir` is the isolation unit.** Point each concurrent
  translator at its own checkout — `git worktree add`, one per in-flight
  unit — because delegates write files. Reviewers only read and may
  share the orchestrator's checkout. Strict file partitioning
  (`roles/translator.md` rule 1) becomes physical: two delegates never
  share a working copy.
- **Pin models deliberately.** Reviewer pin ≠ translator pin whenever
  the catalogue allows (Volatility3 found a different review model
  cheaper *and* better). Candidates may also spread across pins — the
  oracle ranks them; model choice never does.
- **The prompt is the role sheet + the assignment.** Compose
  `roles/translator.md` (or `roles/reviewer.md`) verbatim, then the
  unit id, the contract path, the Python source paths, the worktree
  path, and the exact reporting format the role demands. Delegates read
  what they need from disk; files outside `working_dir` ride as
  `uploads` (GLV saves them and appends their paths to the prompt).
- **Repair rounds are follow-ups on the same session.** Feed the
  oracle's failing diff back with `POST /api/sessions/:id/prompt` — the
  candidate's history is the context. Every follow-up counts against
  that unit's `MAX_ROUNDS` and token budget in the state file.

## The delegation loop (orchestrator-side)

```bash
start_delegate () {  # $1 name  $2 worktree  $3 model pin  $4 prompt file
  curl -fsS -X POST "$GLV_BASE_URL/api/sessions" \
    -H "Authorization: Bearer $GLV_API_TOKEN" \
    -F "name=$1" -F "working_dir=$2" -F "model=$3" \
    -F "prompt=<$4"
}
wait_settled () {    # $1 session id → echoes terminal status
  while :; do
    st=$(curl -fsS "$GLV_BASE_URL/api/sessions/$1/wait?timeout=60000" \
         -H "Authorization: Bearer $GLV_API_TOKEN" | jq -r .status)
    case $st in queued|running) ;; *) echo "$st"; return ;; esac
  done
}
read_new () {        # $1 session id  $2 cursor (messages already held)
  curl -fsS "$GLV_BASE_URL/api/sessions/$1?after=$2" \
    -H "Authorization: Bearer $GLV_API_TOKEN"
}
```

Per unit, in leaf-first dependency order:

1. Start K=3 translator sessions (name suffixes `-c1`…`-c3`), each in
   its own worktree.
2. `wait_settled` each — a `202` deadline just means loop again. On
   `completed`, page the transcript with `after=` and extract the best
   candidate + the reported per-run verdicts. On `error`/`stopped`,
   read the transcript tail for the failure, requeue **once** (fresh
   session, `-retry` name), then mark `escalated` per `MIGRATION_PROMPT.md`
   hard rule 6 — at L2+ (the default) policy rule ESC-1 resolves it as
   *defer* (the Python fallback stays live, the backlog continues, the
   escalation drains into the end-of-run dossier); at L0/L1 it waits in
   the dossier for the operator between runs — nothing asks mid-run.
3. Reviewer pass on a different model pin. Verdicts are
   `APPROVE` / `FIX` / `ESCALATE`: `FIX` loops via `/prompt` (counted
   against `MAX_ROUNDS`); `ESCALATE` goes to the escalation queue — the
   end-of-run dossier, deferred via ESC-1 at L2+ — with both
   candidates and the minimal repro attached.
4. The orchestrator — never the delegate — writes the verdict and the
   session ids into `migration-state.json`, then `state.save()` after
   every unit.

## Guardrails that bind over GLV

- **No LLM judging.** GLV sessions implement and review; only
  `oracle.py` produces verdicts. A delegate's self-assessment is a
  claim, not a result.
- **The state file is orchestrator-only.** Delegates receive read
  paths, never write paths, to it — the same rule as in-process
  delegation, now enforced by process boundaries.
- **Contained runs.** Every GLV run spawns in its own process group,
  signalled once when the run settles, so a delegate's background jobs
  cannot outlive it and leak into the service's cgroup. Launch freely,
  but still `stop` anything left running when you escalate: the
  six-slot queue is pipeline throughput.
- **Attribution survives the hop.** Code reaches the repo only through
  the orchestrator's commit discipline (provenance headers, checked by
  `attribution.py`).
- **Expired tokens.** Tokens live 30 days. A sudden run of 401s on a
  long migration means the token expired — mint a fresh one
  (`glv token create`) and update `GLV_API_TOKEN`. Never fall back to
  riding a browser cookie.
