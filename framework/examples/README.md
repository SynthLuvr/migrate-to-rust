# examples/ — a worked migration, artifact by artifact

Everything else in `framework/` tells you the rules. This directory shows
you the **output**: what a filled `migration-state.json`, a per-unit
`contract.json`, and an oracle verdict actually look like. The project is
fictional — `duplifind`, a near-duplicate document detector (~6.4k LOC
scoped) — but every artifact is real: the state file validates against
[`../state/migration-state.schema.json`](../state/migration-state.schema.json)
(CI runs [`../state/check_examples.py`](../state/check_examples.py) on it),
passes the coherence checks of [`../tools/state.py`](../tools/state.py)
`validate`, and the oracle verdict is **unmodified output of** [`../tools/oracle.py`](../tools/oracle.py)
`judge`, reproducible with one command.

The snapshot is mid-**Stage 7** and deliberately shows **one unit in each
of the six statuses** — including the two you never see in a clean demo:
`fallback_python` and `retired`. A sibling instance,
[`../state/examples/stage4-mid-migration.json`](../state/examples/stage4-mid-migration.json),
shows the messier mid-Stage-4 moment (escalation just opened, scope change
pending).

## File map

| Path | What it is | Produced by |
|---|---|---|
| [`migration-state.json`](migration-state.json) | The dual-build state file: 6 units, one per status, full `journal[]`, 5 human gates, budget ledger | Stages 0–7, written only through `tools/state.py` |
| [`contracts/u-tokenize-1.contract.json`](contracts/u-tokenize-1.contract.json) | One Stage-2 per-unit contract (covering tests, signatures, invariants, golden corpus) | Stage 2, normally via `tools/contract_extract.py extract` |
| [`oracle/manifests/u-tokenize-1.json`](oracle/manifests/u-tokenize-1.json) | Judge manifest: reference entry, candidate entry, corpus, normalization overrides | Stage 2 step 3 / Stage 4 step 5 |
| [`oracle/manifests/reference_tokenizer.py`](oracle/manifests/reference_tokenizer.py) | The Python side, as the oracle runs it (`module:function` over stdin JSONL) | the unit's live module |
| [`oracle/manifests/candidate_wheel.py`](oracle/manifests/candidate_wheel.py) | The candidate side — stands in for the PyO3 wheel's frozen API; carries one canonical injected bug | Stage 4 candidate / Stage 2 bug injection |
| [`oracle/corpora/tokenize-probe.jsonl`](oracle/corpora/tokenize-probe.jsonl) | 3-run probe corpus engineered to hit all three verdicts | Stage 2 (Hypothesis-derived probes / golden corpora) |
| [`oracle/u-tokenize-1.verdict.json`](oracle/u-tokenize-1.verdict.json) | The oracle's actual output: 1 equivalent, 1 divergent (structured diff), 1 incomparable | `tools/oracle.py judge` |
| this file | The walkthrough | — |

## Stage-by-stage: which step produced which artifact

**Stage 0 — Feasibility** ([`../stages/0-feasibility.md`](../stages/0-feasibility.md))
→ `migration-state.json` `go_no_go`: the signed scope (5 modules), the
projection (58% end-to-end saving — above the ≥ 50% bar), and the
`go_no_go` **human gate** recorded as approved. No units exist yet.

**Stage 1 — Plan** ([`../stages/1-plan.md`](../stages/1-plan.md)) → the
`units[]` backbone: every unit enters as `status: pending` with `deps[]`,
leaf-first `order`, contract refs, risk flags, budget. In the snapshot you
can still see one unit in that pristine state (`u-featurize-6`). The
`redesign_approval` gate covers the Stage-1 data-structure proposal
(dict-of-dicts → u64-hashed shingles).

**Stage 2 — Contracts & the parity judge**
([`../stages/2-contracts.md`](../stages/2-contracts.md)) → two artifacts:

- `contracts/u-tokenize-1.contract.json` — step 2 ("per unit, write
  `contract.json`"). The keys are exactly what `tools/contract_extract.py`
  emits; the state file's per-unit `contract` object is its summary view
  (`type_signature` singular there, `type_signatures[]` in the full file).
  Compare: the unit's `covering_tests` in `migration-state.json` match the
  contract's, and the invariants come from tagged docstrings — extracted,
  never invented.
- `oracle/` — steps 3–4: build the oracle, classify every run
  **Equivalent / Divergent / Incomparable**, write down normalization
  rules, then pass the judge-first hard gate (`oracle.py self-test`, 5/5 —
  recorded in the state file's `judge_validation`).

**Stage 3 — Scaffold** ([`../stages/3-scaffold.md`](../stages/3-scaffold.md)) has
no artifact in this directory by design: its output is the crate scaffold
instantiated from `rust-template` — nothing state-file-shaped to show.

**Stage 4 — Translation** ([`../stages/4-translate.md`](../stages/4-translate.md))
→ statuses accumulate through the repair loop, and the `journal[]` shows
each transition with its actor:

- `u-dedupe-2`: **ported** — `state.py mark` refuses this without an
  `equivalent` parity verdict (the guard encodes "the oracle ranks, not the
  model").
- `u-shingle-3`: **divergent_explained** — 209/210 exact parity; the one
  divergence (NaN weight: reference poisons, candidate raises) got a
  written `divergence_note` and a human ruling. This is the Volatility3
  pattern the stage's exit criteria name.
- `u-index-4`: **escalated** — 5 rounds exhausted (`budget.exhausted`),
  which auto-opened the `unit_escalation` human gate. The fix needs a
  redesign, and redesigns are Stage-1-semantics proposals — never
  improvised mid-loop.

**Stage 5 — Gates** ([`../stages/5-gates.md`](../stages/5-gates.md)) →
`u-score-5`: **fallback_python**. The port passed every correctness gate,
but the matched-corpus benchmark showed numpy-vectorized Python matching it
within overlapping CIs — with the reproduction corpus committed. Keeping
Python is the documented success outcome, not a failure; the per-unit
fallback switch (never deleted) keeps the Rust candidate one env var away.

**Stage 6 — Integration** ([`../stages/6-integrate.md`](../stages/6-integrate.md))
→ the `cutover` gate (approved) and the `stage` 5→6→7 journal entries. The
Python fallback stays callable per unit — that is what makes rollback a
config change, not a rebuild.

**Stage 7 — Maintenance** ([`../stages/7-maintain.md`](../stages/7-maintain.md))
→ two artifacts of a stage that never "completes":

- `u-tokenize-1`: **retired** — stable in Rust for the agreed two-quarter
  window, so its Python fallback was *deliberately removed* (step 4). It is
  the only unit where `rust_path` is the sole implementation. The journal
  shows the full arc: `pending → ported → retired`.
- `u-featurize-6`: **pending** again — the upstream watcher flagged a new
  feature, a `scope_change` gate was opened and approved, and the backlog
  re-opened. Maintenance mode extends the state machine; it doesn't freeze
  it. The `budget_ledger.regression_triage_hours` (31h ≈ 2× migration
  effort) is the pre-committed Stage-7 regression budget.

## The six statuses at a glance

| Unit | Status | The one-sentence story | Live side |
|---|---|---|---|
| `u-dedupe-2` | `ported` | oracle-equivalent first candidate; fallback still callable | Rust |
| `u-shingle-3` | `divergent_explained` | 209/210 parity; the one divergence is human-ruled and written down | Rust |
| `u-index-4` | `escalated` | 5 rounds exhausted; redesign proposal in the human queue | Python |
| `u-score-5` | `fallback_python` | port was correct but not faster than optimized Python — human kept Python | Python |
| `u-tokenize-1` | `retired` | two stable quarters; Python fallback deliberately removed | Rust only |
| `u-featurize-6` | `pending` | added later by an approved scope change; untouched | Python |

## The oracle demo: one unit, three verdicts

Run it yourself (exit code `2` = findings, as documented in
[`../tools/README.md`](../tools/README.md)):

```console
$ python framework/tools/oracle.py judge --manifest framework/examples/oracle/manifests/u-tokenize-1.json
... exits 2; the JSON verdict is on stdout
```

That command produced [`oracle/u-tokenize-1.verdict.json`](oracle/u-tokenize-1.verdict.json)
byte-for-byte except two path fields the tool absolutizes (`manifest`,
`corpus.path`, relativized here) and `generated_at` (the run timestamp).
Everything verdict-relevant — `runs[]`, `counts`, `diff_kind_histogram`,
`verdicts_digest` (`c17b3f2f4feb6613`), `normalization` — is deterministic
and will match on any machine.

Why each run classifies the way it does:

| Run | Input | Reference | Candidate | Verdict |
|---|---|---|---|---|
| 0 | `"The quick THE fox  the FOX"` | `["the","quick","the","fox","the","fox"]` | same | **equivalent** — normalized outputs match |
| 1 | `""` | raises `ValueError("empty input")` | returns `[]` | **divergent** — `swallowed-error`; the diff cites both sides |
| 2 | `{"text": "b a b", "legacy": true}` | returns a real `set` | returns a list | **incomparable** — reference output is not JSON-serializable; the judge has no ground truth |

Run 1 is one of the 5 canonical bug classes `oracle.py self-test` injects
(the same class that gated this project's Stage-2 hard gate, recorded as
`caught: 5/5`). Run 2 is the honest verdict: the unit's legacy API returns
a set, so it needs a canonical adapter (`sorted(list(s))`) before it can be
judged at all — "incomparable" is never a synonym for "close enough".

The manifest also shows normalization overrides in place
(`error_compare: type-and-message`); the full rule set is documentation,
not vibes:

```console
$ python framework/tools/oracle.py rules
```

In Stage 4 the same manifest judges real candidates (the PyO3 wheel's
frozen API as the candidate entry), and the best-of-K ranking comes from
these verdicts — never from model confidence.

## Keeping the example honest

- CI (`.github/workflows/ci.yml`, job `state-schema`) validates this
  `migration-state.json` against the schema on every push, via
  `python framework/state/check_examples.py` — which also asserts the
  example corpus covers **all six statuses** and the guarded fields
  (`journal[]`, `scope_change` gate, unit `notes`/`sessions`).
- `python framework/tools/state.py validate framework/examples/migration-state.json`
  (stdlib-only, also in CI) adds the coherence checks the schema can't
  express: dangling deps, cycles, leaf-first order, unexplained
  divergences, and journal-vs-current-state agreement.
- The state file's `journal[]` is internally consistent: the last journaled
  status transition of every unit equals its current status — nothing was
  edited out-of-band.

Fictional, but not lazy: the artifacts cross-reference exactly the way a
real run's would (state contract ↔ contract.json, `judge_validation` ↔ the
oracle demo, human gates ↔ the journal entries that opened/approved them).
