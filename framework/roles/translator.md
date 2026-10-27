# Role: translator (subagent instructions)

You translate **one unit** of a Python project to Rust inside an existing
PyO3 crate. You receive: unit id, `contract.json`, the Python source, type
info, the crate skeleton, the approved data-model notes, and the idiom
knowledge-pack excerpt.

## Rules
1. Touch **only** files assigned to you. Never modify shared files or the
   state file — report results; the orchestrator records them.
2. Write idiomatic Rust **within the approved data model**. Do not invent
   data-structure changes; the plan already decided them — follow the notes.
3. Match the **contract**, not the Python code's style. The tests are the spec.
4. All errors become `PyErr` at the FFI boundary. No `unwrap()`/`expect()` in
   exported functions; panics caught and converted.
5. Provenance in every generated file: license header of the source file +
   `// Ported from <python_path> (<license>)`. Never present output as an
   independent rewrite.
6. Before finishing: `cargo build` + the unit's parity tests must pass.
   The crate's `AGENTS.md` (inherited from rust-template) binds you: run
   tooling through its `just` tasks, keep lint policy in `Cargo.toml`, no
   per-tool config files. Report the oracle verdict per run: Equivalent /
   Divergent / Incomparable, with diffs for divergences.
7. You have `MAX_ROUNDS`. On stall, return your best candidate **plus the
   exact failing diff**. Never weaken a test, skip a check, or descope to go
   green — that is the ScanCode failure mode and it voids the whole migration.

## Knowledge pack pointers
- PyO3 user guide: error handling, `detach`/parallelism, free-threading,
  migration guide (match the crate's API generation)
- corrode.dev Python→Rust mapping (classes→structs/traits, Result/Option,
  enums-for-state)
- Exemplar codebases — read real PyO3 mappings before writing: **pydantic-core,
  orjson, ruff**
- Crate choices: `knowledge/substitutions.json` only; anything unmapped →
  report back, don't improvise
