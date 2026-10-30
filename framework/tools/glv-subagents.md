# GLV — subagent transport for the migration pipeline

[GLV](https://github.com/SynthLuvr/glv) is a self-hosted web app that
drives goose (and claude) sessions from one server. Its JSON API is the
supported transport for this pipeline's **role separation**
(`MIGRATION_PROMPT.md`) when it runs as *separate processes* instead of
in-process subagent calls: every translator candidate and every reviewer
pass is a GLV session the orchestrator starts, waits on, and reads back.
The oracle still decides — GLV only carries the roles.

GLV is **the unconditional transport** (issue #47): there is no
alternate operating mode and no second transport — and nothing about
GLV is a human prerequisite. Every session bootstraps it itself, as its
first act, per the section below (the chain harness
`tools/succession.py` does the same in code).

Source of truth for every path and field below: `GET /api/openapi.json`
on the running server.

## GLV bootstrap — every session's first act, unconditionally

The session performs these steps itself, in this order, before any
role or chain work. No step asks a human; no failure changes the
transport.

1. **Ensure the server is up.** Probe `GET /api/openapi.json` — any
   HTTP answer (a `401` JSON body included) means the server is
   serving; authentication is the token's job, not the probe's. If
   nothing answers, start the server yourself, in the background:

   ```bash
   GLV_BASE_URL="${GLV_BASE_URL:-http://localhost:3000}"
   curl -fsS -m 5 "$GLV_BASE_URL/api/openapi.json" >/dev/null 2>&1 || \
     nohup glv serve >>/tmp/glv-serve.log 2>&1 &
   # -c/--max-concurrent (default 6) is the run queue shared by all
   # served agents; leave it at the default unless the host says no
   ```

   Then re-probe until it answers (`tools/succession.py` does exactly
   this: probe → start → wait).
2. **Mint this session's own token** — never reuse a predecessor's.
   The name is the audit trail: `glv token list` shows one
   `run-<project>-s<n>` per chain session:

   ```bash
   glv token create --name run-<project>-s<n>
   # raw token (glv_at_…) printed ONCE; expires in 30 days; only its
   # SHA-256 hash is stored server-side
   ```

   No filesystem access to the GLV data dir (SSH port-forward, sibling
   container)? When the server runs `glv serve
   --allow-token-bootstrap`, a loopback-only `POST /api/tokens` mints
   a token with no credential — the path for agents with network reach
   but no data-dir access. Never enable that flag behind a reverse
   proxy — loopback detection is the security boundary and proxies
   break it.
3. **Export both values, and commit neither:**

   ```bash
   export GLV_BASE_URL="http://localhost:3000"
   export GLV_API_TOKEN="glv_at_…"
   ```

   Auth is `Authorization: Bearer glv_at_…` on every `/api` path; POST
   accepts the bearer only, GET also accepts a browser session cookie.
   `/api/*` answers 401 as JSON — never an HTML login redirect.
4. **On a sudden run of 401s** (the 30-day expiry): re-mint
   (`glv token create --name run-<project>-s<n>`), re-export
   `GLV_API_TOKEN`, and continue — re-auth is never a stop and never a
   human ask. Never fall back to riding a browser cookie.

## Memory containment — never the host's problem (issue #74)

One runaway child must fail as a **unit-level escalation** (child exits
non-zero → oracle records `setup_error` → verdict `incomparable` → ESC-1
defers, Python fallback stays live) — never as a host or service failure.
The overnight OOM kills in #73 happened because nothing in the chain had
a memory ceiling: the kernel killed the fattest process, systemd marked
the whole `glv.service` unit failed, and the restart loop re-ran the same
ballooning workload.

**Per-child scopes (in code).** Every child the harness spawns funnels
through `_common.run_child()` — the oracle judge children, the parity
gates, cargo/miri/clippy, delegate agents (issue #74 closed the last
bypass: `delegate.py::_invoke_agent`), the wheel build, and the driver's
stage workers. `run_child(..., mem_limit_mb=N)` prefixes the command with

```bash
systemd-run --user --scope -p MemoryMax=<N>M -p MemorySwapMax=0 \
  --unit=run-child-<exe>-<pid>-<n> -- <cmd...>
```

so a breach is killed by the memcg (SIGKILL → exit 137) **inside its own
scope** — attributable by unit name in the journal — while the server,
sibling sessions, and the parent tool keep running. cgroup v2
`MemoryMax` bounds actual memory (RSS); `RLIMIT_AS` does not (it counts
address space and breaks rustc/LLVM's large reservations). When
`systemd-run` is unavailable or the scope cannot start (container CI),
`run_child` falls back to unscoped with a `[run_child]` stderr warning —
the harness stays portable and never hard-fails the tool. The ceilings
are **policy** (issue #76): the `memory` block of
`autonomy/policy.json` (`policy:MEM-1`) — `judge_child_mb` (2048 MB)
bounds each judge/corpus REPL child, `gate_child_mb` (4096 MB) bounds
the delegate agent, compile gates, wheel build, and driver stage
workers — and it is the DEFAULT of every flag (`oracle.py judge
--mem-limit-mb`, `delegate.py run-candidates --mem-limit-mb`, which
also forwards the tighter judge ceiling to the oracle's children,
`pipeline.py run --agent-mem-limit-mb`); `pipeline.py resolve-memory`
emits the decision (decided_by + evidence), an explicit flag value is
an operator override journaled as a deviation, and `0` disables
scoping. A breach is journaled the way token exhaustion is — actor,
unit, observed peak, limit — into `units[].budget.max_rss_mb` + the
journal, resolving as ESC-1 defer; per-side peak RSS rides the verdict
payload's `resources.peak_rss_mb`.

**Run-level slice (the launch path).** Per-child scopes bound each child;
a whole unattended run (driver or chain) is bounded independently
of the session server by launching it inside a dedicated user slice — the
ceiling is policy too: `memory.run_scope_mb` (`policy:MEM-1`, 8192 MB).
Install the shipped slice once:

```bash
mkdir -p ~/.config/systemd/user
cp framework/tools/migration.slice ~/.config/systemd/user/migration.slice
systemctl --user daemon-reload
```

Then prefix the launch — the driver or the chain:

```bash
systemd-run --user --slice=migration.slice --scope \
  -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 \
  --unit=migration-<project> \
  python framework/tools/pipeline.py run --target /abs/target \
    --agent-cmd '<any CLI agent> {brief_file}'
# chain harness:
systemd-run --user --slice=migration.slice --scope ... --unit=migration-<project> \
  python framework/tools/succession.py chain framework/state/migration-state.json
```

`MemoryMax=8G` mirrors policy `memory.run_scope_mb` (8192 MB, issue #76);
`MemoryHigh=6G` is the reclaim throttle below it.

A scope-killed run is attributable (`journalctl --user-unit
migration-<project>.scope`) and the session continues: the chain
composes a successor per `succession.py` — no service crash-restart.
`pipeline.py run` and `succession.py chain` report a `containment` block
(cgroup, unit, whether inside `migration.slice`) so the artifacts alone
show where the run was bounded. The GLV server itself stays OUTSIDE the
slice on purpose — it must survive a run's breach; server-side per-run
bounding is the companion glv#392.

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

**Including the orchestrator itself.** The orchestrator is a
*series* of GLV sessions — `run-<project>-s<n>` — so a whole migration
finishes from one human prompt. Succession is **session-driven**
(design/0001 amendment 2): at ~half its context window the live session
records everything through `state.py` and calls
`succession.py handoff <state> --self` — guardrails classify from the
state file, resume hygiene runs, the successor's resume brief is
composed from the state file (position, escalation dossier, journal
tail, run-budget remaining, hygiene findings), the successor starts
over this API, the stop-notice journals, `run_status` resets to
`running` — and the command exits; the caller stops itself. No monitor
process parks anywhere. The same command rescues a session that died
without handing off: it checks liveness first (`GET
/api/sessions/:id?fields=meta` — refused while genuinely live, since
two live orchestrators would clobber the state file), `--adopt` stops
the gone/stuck session and takes the run over. GLV itself never
respawns or watches context — the settle rule is the framework's,
enforced by the session that owes the handoff. The optional
`succession.py chain` watchdog enforces the same rule from outside
(bootstraps GLV itself first — serve probe/start + one minted token per
session, issue #47) for operators who want an unattended belt-and-
braces. Guardrails either way:
depth cap (`budget.max_chain_depth`, policy default 10) and a
monotonic-progress guard (a successor that advances neither stage nor
≥1 decided unit ends the chain `failed_safely`); the run-level
ceilings bind the *whole* chain (tokens sum through the single state
file, wall clock sums over sessions). `budget_exhausted` survives as a
final status only when the run-level ceilings hit; a transport loss is
self-healed (server revived, token re-minted on 401) and only an
unrevivable transport ends the chain — `failed_safely` with the
dossier.

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

1. Start K translator sessions (name suffixes `-c1`…`-cK`), each in
   its own worktree. K is policy, not judgment (issue #59): the
   `candidates` schedule in `autonomy/policy.json` (`policy:CAND-1`)
   sets K=3 below the unit size threshold and K=1 + bounded repair
   rounds above it or under the run-budget floor —
   `delegate.py run-candidates` resolves it mechanically; never pick K
   by feel.
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
  cannot outlive it and leak into the service's cgroup. Memory is
  contained the same way and stronger — a runaway child must not cost
  the *service* its unit (#73): every child the harness spawns is
  bounded by a per-child cgroup scope at the `run_child` choke point
  (ceilings are policy, `autonomy/policy.json` `memory` /
  `policy:MEM-1`, issue #76 — judge children 2048 MB, toolchain
  children 4096 MB), and a whole unattended run launches inside the
  run-level `migration.slice` (`run_scope_mb` 8192 MB — the exact
  launch command is in § *Memory containment*). A breach dies alone
  (exit 137 → ESC-1 unit escalation, journaled with observed peak +
  limit), never a host-wide OOM. Launch freely, but still `stop`
  anything left running when you escalate: the six-slot queue is
  pipeline throughput.
- **Attribution survives the hop.** Code reaches the repo only through
  the orchestrator's commit discipline (provenance headers, checked by
  `attribution.py`).
- **Expired tokens.** Tokens live 30 days. A sudden run of 401s on a
  long migration means the token expired — re-mint (`glv token create`)
  and update `GLV_API_TOKEN`. Never fall back to riding a browser
  cookie. The chain harness does this re-mint itself (issue #47); a
  session-driven loop follows bootstrap step 4 above.
