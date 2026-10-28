# Stage 3 — Scaffold

Purpose: build plumbing and CI green **before** porting, so Stage 4 feedback
is instant and trustworthy.

## Steps
1. Instantiate the crate from
   [`rust-template`](https://github.com/SynthLuvr/rust-template) — never
   `cargo init` from scratch. It is not a GitHub template repo, so copy the
   tree into `rust/` (for a standalone port repo, clone and re-point
   `origin` instead):

       git clone --depth 1 https://github.com/SynthLuvr/rust-template <tmp>
       cp -a <tmp>/. rust/ && rm -rf rust/.git
       git -C <tmp> rev-parse HEAD   # record the SHA for provenance

   `pipeline.py preflight` (issue #31) already captured the template's HEAD
   SHA in the state file (`preflight.checks.template.head_sha`) — reuse it
   for the provenance header (`// Scaffold provenance: rust-template @
   <sha>` — the form `attribution.py` accepts for scaffold-owned files
   not derived from a Python unit, issue #54) so the scaffold cites the
   same run that proved the template reachable.

   The template pre-wires the gate stack Stage 4 depends on: `rustfmt`,
   `clippy` (`-D warnings`; `unwrap_used`/`expect_used`/`unsafe_code`
   denied), the docs gate, `cargo-deny`, `cargo-shear`, `cargo-llvm-cov`
   (80% line gate), a pandoc Markdown gate, `just` tasks mirroring CI, and
   CI itself (ubuntu+windows matrix, MSRV job, deny job, SHA-pinned
   actions). Then:
   - **Keep as-is:** `justfile`, `deny.toml`, `rust-toolchain.toml`,
     `.gitignore`, `.gitattributes`, `scripts/pandoc-md.sh`, the `[lints]`
     tables in `Cargo.toml`, and `AGENTS.md`'s enforced agent workflow
     (extend it with the port's conventions; keep its gate rules).
   - **Adapt:** crate `name`/`description`/`license`; raise
     `rust-toolchain.toml` and the MSRV (`rust-version`, `just msrv`, the
     CI `msrv` job) only together; add a workspace only if the port scope
     splits per component; if the crate is a subdirectory of the target
     repo, point the CI workflow's checkout/working-directory at it (the
     template assumes repo root).
   - **Replace:** `src/lib.rs` → PyO3 skeleton (step 2); `src/main.rs` →
     thin wrapper, or drop for a wheel-only crate; `tests/basic.rs` →
     port tests; `README.md` → port README.
   - **Add:** `pyo3` dependency, `[lib] crate-type = ["cdylib"]` (+`rlib`
     if Rust-side tests need direct access), `pyproject.toml` with the
     maturin build backend.
2. PyO3 module skeleton matching the installed API generation: Bound API
   (0.21+), gil-refs removed (0.23), `attach`/`detach` + thread-safe default
   (0.28+). Do not mix generations.
3. `maturin`: wheel build + `pip install` smoke test. Choose abi3 vs
   version-specific wheels; free-threaded Python needs `cp314t` wheels
   (`abi3t` arrives with 3.15).
4. Extend the template's CI with what a PyO3 port needs that it lacks:
   `cargo-nextest` as the test runner, a nightly `miri` job, and a wheel job
   (`maturin build` + `pip install` + `pytest -k smoke`). Add matching
   `just` tasks — the template's AGENTS.md rule is "tooling through `just`,
   never bare gates", so a gate without a task doesn't exist for the agent
   loop. These gates exist so Stage 4 failure feedback is mechanical.
5. Dev loop: `rustimport` for per-unit iteration. Error policy: Rust errors →
   `PyErr` at the boundary; no `unwrap()`/`expect()` in exported functions;
   panics caught and converted, never raised across the FFI.
6. End-to-end dry run: empty-crate parity harness executes (stub → stub).

## Exit criteria
- [ ] Scaffold based on rust-template; `just check` green
- [ ] `maturin develop && pytest -k smoke` green
- [ ] CI red-blocking on fmt/clippy; miri scheduled
- [ ] Wheels build for target platforms incl. free-threaded variant if needed
- [ ] Parity harness runs end-to-end against the stub
