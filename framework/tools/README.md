# tools/ — the deterministic helpers (the 8 verified gaps)

These are the components **no existing tool provides** (verified in
`../research/3-tools-and-interop-catalog.md`). They are scripts the agent
*calls*, not prompts: deterministic tooling for location and validation,
LLM only for edits (Google's rule). Build each to its acceptance criteria
**before** the stages that need it. Python + CLI is fine; keep them
stateless and JSON-in/JSON-out so the agent can compose them.

**All 8 are implemented here** (stdlib-only, Python ≥ 3.10), with an
acceptance-test suite in `tests/` wired into CI. Shared plumbing lives in
`_common.py`; `oracle.py`'s comparator is imported (never duplicated) by
`bench.py`.

| # | Script | Stage | Input → Output | Acceptance criteria |
|---|---|---|---|---|
| 1 | `oracle.py` — equivalence oracle | 2,4,5,6 | unit + input corpus → per-run verdicts `equivalent/divergent/incomparable` + structured diff | catches 5/5 injected bugs; normalization rules documented; no LLM in the judging path |
| 2 | `port_plan.py` — profiling→plan generator | 0,1 | profiler hotspots JSON (bare list or `from-cprofile` envelope) + stdlib-ast/mypy scan → ranked, dependency-ordered, portability-scored backlog | I/O-bound units ranked last; cycles detected; risk flags attached; profiler-noise paths skipped with a warning |
| 3 | `contract_extract.py` | 2 | pytest collection + mypy JSON → per-unit `contract.json` (tests, types, invariants) | 100% of scoped units covered; dangling tests flagged |
| 4 | `state.py` — dual-build state manager | 1–7 | validates/migrates `migration-state.json`; emits per-unit fallback switch shim | every state transition journaled (append to the state file's `journal[]`, per `../state/migration-state.schema.json`); Python fallback callable per unit |
| 5 | `bench.py` — cross-language differential benchmark | 0,5 | matched corpus + both entry points → one verdict JSON + repro script + corpus commit | same inputs/coverage both sides; no number without repro |
| 6 | `pyo3_review.py` — FFI linter | 4,5 | Rust diff → findings on refcounts, swallowed `PyErr`, `unwrap()` in exports, GIL `detach` misuse, free-threading hazards | complements miri/clippy (they don't cover these classes); reviewer checklist encoded |
| 7 | `attribution.py` — attribution checker | 4,5 | original tree + port tree → header/NOTICE diff report, fail on loss | byte-exact NOTICE check; provenance comment required per generated file |
| 8 | `upstream_watch.py` — maintenance watcher | 7 | upstream diff → changed-units list → parity re-run plan → PR proposal | maps changes to state-file units; no silent skips |

## External helper — GLV subagent transport

GLV ([SynthLuvr/glv](https://github.com/SynthLuvr/glv)) is not one of the
eight scripts — it is the transport the framework's *role separation*
runs on, and the only one: every session bootstraps it itself (server
probe/start + its own minted token) per the **GLV bootstrap** section of
[`glv-subagents.md`](glv-subagents.md) (issue #47). Spec, API cheat
sheet, and delegation loop: [`glv-subagents.md`](glv-subagents.md).

## The Stage 2/4 scaffold — `scaffold.py` (issue #32)

Also a composition layer, not a ninth gap-tool: it turns what
`contract_extract.py` extracts (the type signature) into the four
artifacts Stages 2/4 otherwise hand-write per unit — and hand-writing them
is where the corpus-format trap (#29) and the degenerate-corpus hazard
(#30) crept in:

```console
python framework/tools/scaffold.py scaffold state/migration-state.json \
  --unit u-bars --out runs/ --seed 20261028
# reviewer fresh inputs (Stage 4 step 8): same generator, different seed
python runs/corpora/gen_u_bars.py --seed <fresh> --out corpus-fresh.jsonl
# the emitted manifest is judge-ready verbatim (entries are .py file paths)
python framework/tools/oracle.py judge --manifest runs/manifests/u-bars.json
```

**Module conformance is the default (issue #48).** A unit's contract is its
*whole public API* — hard rule 2 and the Stage-4 exit criteria are per
unit — so the scaffolded adapter calls **every public function** in the
unit per corpus row and returns a
`{fn: result | {"__error__": {"type", "message"}}}` envelope: one manifest
judges the module API, a function's own raised exception is per-function
evidence (compared type-and-message through the value rules) instead of
failing the row, and a first-public-only verdict can no longer green-light
a unit whose other functions diverge (the acceptance test proves the
hazard: the same buggy candidate verdicts EQUIVALENT under
`--mode function` and DIVERGENT under the default, with the diff naming
the diverging function as `$.<fn>`). Corpus rows are `{param: value}`
objects over the *union* of the judged functions' parameters — each
function takes the row's values for its own parameters, rebuilt through
its own spec; `--function NAME` (repeatable / comma-separated) narrows the
judged subset; a parameter annotated differently across functions is
flagged for hand-completion, never guessed. `--mode function` keeps the
legacy single-entry scaffold. The module-mode dry-run tallies per-function
envelope errors: every function erroring on every row is a
degenerate-corpus finding (the module face of #30), a single all-error
function is a warning that its parity is unjudged, and a candidate wheel
missing any judged export is a setup error naming it.

Per unit, under `--out` (default `runs/`, gitignored — scaffolded files
name the target project's private code):

- `adapters/<unit>_ref.py` / `<unit>_cand.py` — **symmetric** adapters,
  rendered from one template so they differ only inside a delimited
  BACKEND section (the payload records the shared sha256 as evidence):
  a corpus row is a `{param: value}` JSON object; the adapter rebuilds the
  typed objects the signature requires (dataclasses via their real
  constructors, sets/tuples/dicts rebuilt per annotation, int/float never
  silently coerced), calls the function, and serializes back to JSON —
  canonicalizing non-JSON shapes (set → sorted list, tuple → list,
  dataclass → dict) so the verdict measures only the port. In the default
  module mode the row's result is the per-function envelope above (issue
  #48); in `--mode function` it is the single function's result.
- `corpora/gen_<unit>.py` — seeded generator (`--seed/--rows/--out`);
  same seed → byte-identical corpus; prints a row-status summary. Edge
  rows (zero values, boundaries, defaults-exercising) come from the
  signature, so the documented error paths (e.g. "empty input raises") are
  covered by construction. Random rows give list/tuple parameters
  realistic series lengths — random-walk 60–320 element series, coherent
  OHLCV bars for open/high/low/close dataclass items (issue #49) — with a
  short-length minority as further deliberate error rows.
- `corpora/corpus-<unit>.jsonl` — the corpus itself, in the documented
  format (one raw JSON value per line), emitted by the generator so the
  #29 trap cannot bite.
- `manifests/<unit>.json` — ready for `oracle.py judge`; the candidate
  entry points at the candidate adapter, whose wheel module is a
  HAND-TUNE placeholder at Stage 2 (`--candidate-module` fills it in at
  Stage 4; until then judging is an honest setup failure, never a hollow
  verdict).

The heuristic mapping is deliberately conservative: JSON scalars,
containers, `Optional`/unions, `Literal`, and project `@dataclass`es
(resolved through the unit's imports, nested ones recursed) are generated
mechanically; **everything else is flagged for hand-completion, never
guessed** — flags land in the payload's `hand_tune[]`, in a HAND-TUNE
block in both adapters, and in the generator, and make the run exit 2.
That includes shapes the generator cannot inhabit *realistically*: a list
of a non-dataclass class, or a container/dataclass holding an ungeneratable
type, gets a `hand_tune[]` flag naming the item path — lists of JSON-any
stand-ins that error on every row are never silently emitted (issue #49).
List/tuple series parameters draw **realistic lengths** (issue #49):
random-walk series of 60–320 elements dominate — coherent OHLCV bars
(high/low bracket the bar, open chains to the previous close) for
open/high/low/close dataclass items, walked floats/ints otherwise — with
a short-length minority kept as deliberate error rows; the old 0–9
element lists left min-length series functions erroring on nearly every
row.
**Derived inputs resolve per side (issue #50).** A parameter typed by a
dataclass the unit module does not define — `list[Bar]` where `Bar` comes
from the unit's `u-bars` dependency — is recorded as a derived input
(`derived_inputs[]` in the payload and the manifest's scaffold block:
name, module, path, the declared `deps[]` unit it maps to, the params
that use it) and resolved on **each side's own dependency modules**: the
reference adapter imports the python dependency module the spec records;
the candidate adapter resolves the **wheel's own class** — a flat wheel
re-exports its dependencies in the wheel module itself, a split wheel
gets its spellings hand-tuned into the candidate-only
`_WHEEL_DEP_MODULES` table — never the python project, which pre-#50
adapters silently did (the wheel was fed reference-built objects: an
asymmetric adapter, exactly what the symmetry design exists to prevent,
and it surfaced only as an oracle divergence). Both adapters resolve
every derived input at import time, so a side missing a class is an
honest setup error naming it — never silent `[]` nor per-row noise
masquerading as divergence; a dependency module that is neither the
unit's own nor in its recorded `deps[]` is called out in the warnings
(the Stage-1 record may be stale, or the module is shared/unported).
Before reporting success the scaffold also dry-runs its own fresh corpus
against the reference adapter through `oracle.py`'s runner, so a corpus
that would judge hollow (degenerate — issue #30), **thin** (reference ok
below `min_reference_ok_fraction`, a finding since issue #49 — never a
warned-through exit 0), or malformed (issue #29) is a finding in seconds,
not after a Stage-4 judge run. Existing output is never overwritten
without `--force` (hand-tuned edits must not be lost).

## The deterministic driver — `pipeline.py` (issue #22)

Not a ninth gap-tool: a **composition layer** over the eight (it writes the
state file only through `state.py`'s journaled writer and verifies stages
with `state.py` validation + `oracle.py self-test`). The LLM is demoted to
a *stage worker* invoked via any CLI agent; the loop itself is code —
testable, deterministic, resumable by construction:

```
loop:
  stage = state.stage; run the stage checklist via the agent CLI
  verify exit criteria  → state.py validation + state-expressible checks
  resolve open gates    → evaluate ../autonomy/policy.json rules in code
  journal decisions     → state.py (decided_by + policy_version + evidence)
  advance stage or terminal-state exit
```

- **Autonomy levels** (`--autonomy L0..L3`, pre-authorized before the run):
  L2 (the default for new runs) resolves every gate by policy, publish
  disabled; L3 adds cutover/publish. L0/L1 stay with the agent-driven
  recipe, which never asks either: the gate stays open, the run ends with
  the dossier, and the operator decides between runs (the driver refuses
  these levels). Zero mid-run questions at any level.
- **Terminal-state discipline**: a run always ends in `complete` /
  `no_go_measured` / `budget_exhausted` / `failed_safely` — never "awaiting
  input" — and every terminal state emits the same deliverable set (run
  report with the gate + escalation dossiers, state file, journal).
- **Budget guard**: global token/wall-clock ceilings; over either, the run
  stops safely and resumably in `budget_exhausted`.
- Agent retries per stage are bounded (`budget.max_agent_retries_per_stage`);
  exhaustion ends the run in `failed_safely` — never a silent skip.
- **Preflight** (issue #31): `pipeline.py preflight state.json
  --target-root <repo>` runs, in one command and one auditable artifact,
  everything stages 0/3 silently depended on — target suite green + wall
  time (the Stage 2 spec baseline), a `pytest --collect-only -q` capture
  written next to the state file (replayed by
  `contract_extract.py --pytest-collect`), the `oracle.py` self-test
  re-proven under the target interpreter, rust-template reachability +
  HEAD SHA (Stage 3's provenance, captured from the same run),
  cargo/rustc/maturin/uv versions + cargo-deny/llvm-cov/shear/pandoc
  presence (`just doctor` covers the crate; nothing checked the gate stack
  *before* scaffolding), the target venv interpreter + `Py_GIL_DISABLED`
  wheel flavor + the derived `PYO3_PYTHON`, substitutions.json freshness,
  and the framework-vs-target interpreter match (a 3.10 framework python
  SyntaxErrors on 3.12+ target source). Everything lands in a journaled
  `preflight` block (`state.py` stays the only writer) plus
  `<state>.preflight.md`; exit **2** on the hard prerequisites (suite not
  green, self-test not 5/5, template unreachable — or unprovable, e.g. no
  target root recorded), so a run starts only when Stage 0's exit criteria
  are actually checkable. `--skip <check>` downgrades a hard check to an
  explicit warning (the offline corner); the target root is recorded, so
  re-runs need no flag.

## The session-chain harness — `succession.py` (issue #25)

Also a composition layer, not a ninth gap-tool: it wraps a whole run in a
**chain of GLV orchestrator sessions** so that `budget_exhausted` at the
*session* level (the orchestrator's context window — the motivating run
died at stage 4/7 with ~600k of 5M run tokens spent, and a human had to
notice and restart by hand) becomes a scheduling event in code instead of
"awaiting input":

```console
python framework/tools/succession.py chain \
  --state runs/<project>/migration-state.json \
  --prompt-file runs/<project>/first-prompt.md
```

The harness bootstraps GLV itself as its first act (issue #47): it
probes `GET /api/openapi.json`, starts `glv serve` in the background
when nothing answers, mints one token per chain session
(`glv token create --name run-<project>-s<n>` — `glv token list` is
the audit trail), and exports `GLV_BASE_URL`/`GLV_API_TOKEN` itself.
A sudden run of 401s re-mints without stopping; a mid-chain connection
loss revives the server and retries; only a transport that cannot be
revived ends the chain — `failed_safely` with a journaled stop-notice
(a genuine terminal with the dossier, never a park).

Per session it: starts the GLV session with the brief (session 1 rides the
operator's single prompt; successors get a composed resume brief — state
file path, position, escalation dossier, journal tail, run-budget
remaining, chain history, hygiene findings), parks on
`GET /api/sessions/:id/wait` until it settles, and classifies from the
state file (sessions write it only through `state.py`; GLV only carries
the sessions). On session-level exhaustion — `run_status=budget_exhausted`
under the run-level ceilings, or a session that settled without recording
a terminal state — it runs **resume hygiene** (every unit's recorded
paths verified against the target root; a mismatch with exactly one
candidate on disk is repaired through `state.py`, journaled — the
`crates/<crate>/src/u.rs` vs `src/u.rs` trap of run 20261028_4), spawns
the successor (`run-<project>-s<n>`), stops the predecessor if still
live, journals a **stop-notice** (operator informed, not involved), and
resets `run_status` to `running`. Guardrails: a **depth cap**
(`budget.max_chain_depth`, policy default 10) and a **monotonic-progress
guard** (each successor must advance the stage or decide ≥1 unit vs. the
snapshot taken at its spawn); either violated → `failed_safely` with the
dossier — never an infinite resume loop. The **run-level ceilings bind
the whole chain**: token spend sums through the single state file, wall
clock sums over sessions — successors get a fresh session budget, never a
fresh run budget. Only genuine terminals (`complete` / `no_go_measured` /
`failed_safely`) — or the run-level ceilings themselves, the one honest
`budget_exhausted` — end the chain. Restartable
anywhere: a re-invocation adopts a recorded-but-unsettled session,
re-bootstrapping the transport on the way.
`succession.py hygiene` runs the resume-path check standalone (exit 2
while unresolved); `succession.py brief` prints the next session's brief
without side effects.

## The stage-4 candidate runner — `delegate.py` (issue #33)

Also a composition layer, not a ninth gap-tool: it mechanizes the repair
loop's per-unit translator delegation (K candidates, strict file
partitioning, compile-then-oracle ranking, bounded fix rounds) in ONE
command, so the orchestrator stops hand-rolling the bookkeeping — the
failure mode issue #33 documents from a real run (K=1 under budget,
partitioning by instruction, manual gates and judge invocation, and a
delegate hitting its turn cap mid-unit):

```console
python framework/tools/delegate.py run-candidates \
  --state runs/<project>/migration-state.json --unit u-foo --k 3 \
  --agent-cmd 'goose session run {brief_file} {feedback_file}'
```

Per unit it: computes the per-candidate target paths
(`src/u_<unit>_c1.rs` … `_cK`, following the unit's recorded `rust_path`
directory) and writes each delegate an assignment **brief** — its own
file + the contract + the Python source + `roles/translator.md`, nothing
else (partitioning becomes an artifact, not an instruction); runs each
candidate through round 0 + up to `MAX_ROUNDS` repair rounds (from the
unit's `budget.max_rounds`; `--max-rounds` overrides) over a mechanical
**import gate** (extern crates must be a Cargo.toml dependency, the crate
itself, or an approved `substitutions.json` entry — most "hallucinated
crates" are mis-imported real modules) and the compile gates
(`cargo fmt --check` / `clippy` / `test` by default, `--gate` overrides),
feeding gate output back **verbatim** through `{feedback_file}` — the
strongest signal, per the loop spec, and the reason the template must
reference that placeholder whenever repair rounds are possible; then
builds the wheel once (`--build-cmd`, default `maturin develop`) and
judges **only the gate-green candidates**, one `oracle.py judge` run each
via a derived manifest that retargets the scaffolded candidate adapter
(issue #32) at that candidate's wheel module
(`--candidate-module 'mycrate.units.u_foo_{candidate}'`); and prints the
ranked table (equivalent/divergent/incomparable counts, diff-kind
histogram) with the winner.

**It never writes the state file** — the orchestrator/driver stays the
only writer (role separation). It emits a JSON verdict pack (stdout +
`<state_dir>/delegates/<unit>/verdict-pack.json`) carrying the ranking,
per-round evidence (agent exits, gate outputs, `-->`-span attribution,
the exact failing diff on stall), ready-to-run `state.py set`/`mark`
commands, and per-unit `sessions[]` entries. Runner-agnostic
(`--agent-cmd` is a template — goose / Claude Code / Codex all fit, the
same pattern `pipeline.py run` uses) and restartable anywhere in the
loop (issue #25): re-runs re-derive everything from disk, `--only c2,c3`
resumes a subset after a budget-exhausted hand-off, `--dry-run` prints
the derived plan. Exit codes: 0 winner equivalent · 2 divergent winner /
no gate-green candidate / budget-exhausted refusal · 3 all judged
incomparable · 4 wheel-build failure.

## The matched-corpus A/B harness — `bench_ab.py` (issue #34)

Also a composition layer, not a ninth gap-tool: three gates require the
same evidence shape — **the same measured workload, Python path vs ported
path** — and until now that measurement had to be planned by hand (run the
target bench with the shim env var set vs unset, diff timings, verify
payload identity): exactly the step an operator skips when a run ends
budget-exhausted. `bench_ab.py` is that plan as a command:

```console
python framework/tools/bench_ab.py --state runs/<project>/migration-state.json \
  --out runs/<project>/bench/ --gate go_no_go \
  -- python -m pytest bench/            # the target's own bench command
```

It derives the shim env var exactly as `state.py shim` does (the function
is imported, so the derivation cannot drift) and the unit set actually
live on the rust side (`ported`/`divergent_explained`; `retired` units
have no callable fallback), then runs the given command once per side,
**interleaved python-first** so drift hits both sides equally: the python
side with `<PREFIX>_FALLBACK=<ported-unit-ids>`, the ported side with
`<PREFIX>_FALLBACK=none` (both set explicitly — an ambient value cannot
leak into a side). Per run it captures wall/user/sys time (child CPU via
`os.times()` deltas), stdout/stderr artifacts with sha256, and — the point
of the whole exercise — **byte-identity of the payloads**: divergent
stdout aborts with NO ratio claim (timing without identity proves
nothing); `--mask REGEX` may hide known-volatile fields (printed
durations), recorded as `masked_identical`, never as `identical`. A side
that fails or times out is findings (exit 2), never a benchmark.

The emitted artifact (`<out>/bench-ab.json`, verbatim what stdout
carried) is vendor-rot-proof per the "speed number without a repro
script" failure mode: command, corpus pinning (`--corpus` hashed +
git-committed like `bench.py`, `--corpus-desc` for the description), both
sides' timings, the seeded bootstrap ratio (imported from `bench.py`),
output hashes, the state file's rust-live unit set + sha256, and a
self-contained `bench-ab.repro.sh` that re-runs both sides with the exact
env assignments and warns when the target repo's HEAD moved.

`--gate {go_no_go,redesign_approval,cutover}` additionally writes the
measurement into the state file as gate evidence — through `state.py`'s
journaled, validated writer (`merge_evidence` + `save_validated`; never a
hand-edit): `bench_improvement_pct=` (the exact key RED-1 reads),
`end_to_end_saving_pct=` (GO-1), plus ratio/identity/artifact provenance.
`pipeline.py resolve-gates` consumes it, and `_eval_go_no_go` **prefers
the measured `end_to_end_saving_pct` over the Stage-0 projection** — the
bar was a claim until a matched benchmark existed. Gate evidence is
refused (nothing written, exit 2) when outputs diverge: no honest claim
to record. Stdout is always one JSON object; exit 0 verdict · 2 findings
(divergence/failed run) · 1 usage/IO.

## Minimal viable subset
If you only build three things before starting: **#1 oracle**, **#4 state**,
**#7 attribution**. The oracle is the difference between a migration and the
ScanCode incident; the state file makes it resumable and reversible;
attribution keeps it publishable.

## Anti-requirements
- No LLM calls inside #1/#5/#7 — they must be reproducible and diffable.
- No state kept outside `migration-state.json` (except caches).
- Every tool prints machine-readable JSON + a human-readable summary flag.

## Implementation notes

Uniform conventions (so the agent can compose the tools blind):

- **stdout is always a single JSON object** (`--human` additionally echoes
  the embedded summary to stderr). Errors are JSON too — a refusal is a
  result, not a crash. `summary.exit_code` always mirrors the exit code.
  Failure paths raise the shared `ToolError` from `_common.py` and the
  CLI boundary (`run_cli`) emits the uniform envelope:
  `error.kind` ∈ `io` (filesystem/environment)
  · `usage` (bad or missing input content) · `setup` (missing dependency,
  exit 4), plus a one-line `error.detail`. `run_cli` also converts
  missing/unreadable/malformed input files (OSError/ValueError)
  into this envelope instead of a traceback, so stdout parses as JSON on
  every exit path; genuine programming errors still crash loudly.
- **Exit codes**: `0` clean · `1` usage/IO · `2` findings (divergence,
  attribution loss, lint errors, cycles, human decision needed) · `3`
  incomparable (no divergence, or a degenerate all-error reference corpus)
  · `4` setup failure (e.g. no pytest).
- **Determinism**: fixed seeds (`PYTHONHASHSEED=0` for children, seeded
  bootstrap in #5), sorted iteration, no wall-clock in verdict fields.
  `oracle.py` emits a `verdicts_digest` — identical inputs+rules must give
  identical digests, which is what makes oracle runs diffable in review.

Quick reference (see each script's docstring for the full input formats):

```console
# #1 judge a unit (manifest: reference entry, candidate entry, corpus, rules)
python framework/tools/oracle.py judge --manifest manifests/u-parse-1.json
python framework/tools/oracle.py compare --runs shadow-runs.json   # Stage 6
python framework/tools/oracle.py self-test   # Stage 2 gate: must catch 5/5
# per-row corpus dry-run before a judge run (issue #29)
python framework/tools/oracle.py corpus --validate corpus.jsonl --entry impl:run

# the Stage 2/4 scaffold (issues #32/#48): symmetric ref/cand adapters + seeded
# corpus generator + judge-ready manifest per unit, under runs/ (gitignored);
# default mode judges the unit's WHOLE public API via a per-function envelope
python framework/tools/scaffold.py scaffold state.json --unit u-bars --seed 20261028
python runs/corpora/gen_u_bars.py --seed <fresh> --out corpus-fresh.jsonl  # reviewer
python framework/tools/oracle.py judge --manifest runs/manifests/u-bars.json

# #4 the only writer of the state file
python framework/tools/state.py init state/migration-state.json --name X --python-path src/x --autonomy-level L2
python framework/tools/state.py set  state.json --field 'units[u1].status=ported'
python framework/tools/state.py set  state.json --field 'project.workload=plain string' \
                                       --field '@json:go_no_go={"decision":"pending"}'
python framework/tools/state.py mark state.json --unit u1 --status escalated --note "..."
python framework/tools/state.py resolve-gate state.json --gate go_no_go --decision approved --by policy:GO-1
python framework/tools/state.py shim state.json --unit u1 --write shims/u1.py
python framework/tools/state.py validate|journal|show|migrate state.json

# the deterministic driver (issue #22): L2+ unattended runs
# preflight first (issue #31): suite green + wall time, pytest capture,
# oracle self-test 5/5, rust-template HEAD SHA, toolchain, target
# venv/PYO3_PYTHON, substitutions freshness -> one journaled block + report
python framework/tools/pipeline.py preflight state.json --target-root /abs/target
python framework/tools/pipeline.py run state.json \
  --agent-cmd 'goose run --recipe {framework}/recipe.yaml' --autonomy L2
python framework/tools/pipeline.py resolve-gates state.json   # policy only, no agent
python framework/tools/pipeline.py status|report state.json

# the stage-4 candidate runner (issue #33): K translators, partitioned
# files, bounded verbatim-feedback repair rounds, oracle ranking, verdict
# pack — one command per unit; the state file stays orchestrator-only
python framework/tools/delegate.py run-candidates --state state.json --unit u-foo \
  --k 3 --agent-cmd 'goose session run {brief_file} {feedback_file}' \
  --target-root /abs/target \
  --candidate-module 'mycrate.units.u_foo_{candidate}'   # scaffolded manifest
python framework/tools/delegate.py run-candidates --state state.json --unit u-foo \
  --only c2,c3 --agent-cmd '...'   # resume mid-loop (issue #25)

# the matched-corpus A/B harness (issue #34): same measured workload,
# python fallback vs ported, identity-gated timing + repro — and the
# measurement lands on the gate as evidence resolve-gates consumes
python framework/tools/bench_ab.py --state state.json \
  --gate go_no_go --corpus bench/corpus.jsonl \
  -- python -m pytest bench/            # the target's own bench command

# #2 / #3 feed Stage 1 → Stage 2  (plan takes from-cprofile's envelope as-is)
python framework/tools/port_plan.py from-cprofile --profile run.prof > hotspots.json
python framework/tools/port_plan.py plan --hotspots hotspots.json --project-root .
python framework/tools/contract_extract.py extract --state state.json --out contracts/

# #5 / #6 / #7 / #8
python framework/tools/bench.py run --manifest bench.json         # parity gate first
python framework/tools/pyo3_review.py review --dir crates/        # or --diff pr.diff
python framework/tools/attribution.py check --original ../orig --port . --state state.json
python framework/tools/upstream_watch.py watch --state state.json --repo ../upstream --base v1.2
```

**Corpus file format (#1 `oracle.py`, #5 `bench.py`)** — JSONL, **one raw
JSON value per line = one input**, exactly as the entry function receives
it (verbatim from the acceptance test `test_judge_subprocess_end_to_end`):

    {"xs": [1, 2], "bad": false}
    {"xs": [], "bad": true}

`{"i": n, "input": v}` is *internal stdin framing* `oracle.py` adds itself
when streaming a corpus to a child process — it is **never** the corpus
file format. Pre-wrapped lines double-wrap, both sides raise the same
TypeError on every run, and the judge reports a hollow all-EQUIVALENT
verdict, so `_load_corpus` refuses a corpus in which every line carries
exactly the framing keys (issue #29), and even past that guard the judge's
ground-truth rule classifies an all-error reference corpus INCOMPARABLE,
never EQUIVALENT (issue #30). `oracle.py corpus --validate`
dry-runs one entry per-row (ok / error / unserializable) and flags the
hollow signature (`hollow_equivalent_risk`) plus thin ground truth before
you spend a judge run.

Design decisions worth knowing before you extend:

- **oracle.py** runs both sides as Python callables (`module:function`,
  default fn `run`, or a relative `*.py:function` path resolved against
  the manifest's directory — issue #32's scaffolded adapters ship as
  files) — the Rust side ships as a PyO3 wheel with the API
  frozen, so the wheel *is* the entry point. Outputs must be
  JSON-serializable or the run is honestly *Incomparable*
  (sets need a canonical adapter). The normalization rules (float
  tolerances, int/float distinctness, dict-order insensitivity, NFC, error
  type/message policy, `strip_fields`) are
  documented in `oracle.py rules` output — Stage 2 requires them written
  down, and `int_float_distinct=true` is deliberate: silent coercion is a
  divergence, not a normalization. `self-test` injects the 5 canonical bug
  classes through the *production* judging path (no special-cased
  verdicts) and also proves benign differences are forgiven.
  **Ground-truth guard (issue #30)**: a corpus on which the reference side
  has zero `ok:true` runs is degenerate — `compare_error` rightly calls
  error==error equivalent *per run*, but a run set with no successful
  reference output carries no parity evidence, so the summary verdict is
  INCOMPARABLE, never EQUIVALENT (error-path mismatches on such a corpus
  are corpus artifacts, not candidate defects). The
  `normalization.min_reference_ok_fraction` rule (default 0.5, in `rules`
  output) warns when fewer reference runs succeeded; 0 silences the
  warning, the zero-ok guard itself cannot be disabled. `bench.py`'s
  parity gate imports the same guard: a degenerate corpus aborts the
  benchmark with no timing claim.
- **state.py** journals every transition into the state file's own
  `journal[]` (append-only; `set`/`mark` refuse to touch it), writes
  atomically (tmp + rename), and *rolls back* any change that would leave
  the file schema-invalid or coherence-broken. `mark` encodes the
  MIGRATION_PROMPT.md discipline as hard guards: `ported` requires an
  equivalent verdict, `escalated` opens the `unit_escalation` gate — and at
  autonomy L2+ immediately resolves it as deferred via policy rule ESC-1
  (defer, not halt; the Python fallback stays live) — `divergent_explained`
  requires the written explanation. `resolve-gate` records gate decisions
  (`--by human` or `--by policy:<rule-id>`, rule ids checked against
  `autonomy/policy.json`) with `policy_version` + `evidence[]`; validation
  enforces the resolution discipline (a resolved gate always records who
  decided; a policy-approved cutover requires L3 + `publish_allowed`) and
  terminal-`run_status` coherence. The
  fallback shim is reversible per unit via one env var
  (`<PROJECT>_FALLBACK=all|<unit-ids>|none`) — no state anywhere else.
  Validation includes schema checking (an internal stdlib validator for
  the draft-2020-12 subset the schema uses; cross-checked against the
  `jsonschema` package by the tests) plus coherence invariants the schema
  can't express: dangling deps, cycles, order inversions, unexplained
  divergences, and out-of-band edits detected via journal coherence.
- **attribution.py** scopes the port to the state file's units: each unit
  file must carry `// Ported from <python_path> (<license>)` naming exactly
  the unit's python_path, the original's leading comment block must appear
  in the port file's header region, and NOTICE files must match
  byte-for-byte (sha256). Files claimed by no unit fail unless allowlisted
  with a reason — "independent rewrite" claims are surfaced, not trusted.
- **pyo3_review.py** file mode tracks `#[pyfunction]`/`#[pymethods]` spans
  with a comment/string/lifetime-aware lexer, so exported-function rules
  apply to real bodies; diff mode applies line rules to added lines with
  `context: diff` (re-run file mode before recording a verdict). Findings
  cite the reviewer-checklist item they encode.
- **bench.py** runs the parity gate *first* and aborts without any timing
  claim on divergence — or on a degenerate corpus whose reference side
  errors on every row (issue #30: no ground truth, no number); refuses a
  dirty corpus; ships a self-contained repro script pinning the corpus
  commit; timing stats use a seeded bootstrap CI.
- **bench_ab.py** (issue #34) A/Bs the target's own bench command through
  the shim env var both ways; identity gates the claim exactly as
  bench.py's parity gate does (stdout is the payload — stderr divergence
  warns, never aborts); its `--gate` writes go through `state.py`'s
  journaled writer and are refused outright on divergent outputs, and
  `_eval_go_no_go` prefers the measured `end_to_end_saving_pct` over the
  Stage-0 projection (a projection is a claim until measured).
- **upstream_watch.py** never silently skips: changed files map to units
  (exact path or package-unit prefix), unmapped files are listed with
  reasons, dependents join the re-run plan transitively, and
  escalated/divergent_explained units surface the Stage 7 human decision.
