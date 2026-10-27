# Stage 3 — Scaffold

Purpose: build plumbing and CI green **before** porting, so Stage 4 feedback
is instant and trustworthy.

## Steps
1. Start from [`rust-template`](https://github.com/SynthLuvr/rust-template)
   — clone it as the scaffold base, do not `cargo init` from scratch. It
   pre-wires the gate stack Stage 4 depends on: `rustfmt`, `clippy`
   (`-D warnings`; `unwrap_used`/`expect_used`/`unsafe_code` denied), the
   docs gate, `cargo-deny`, `cargo-shear`, `cargo-llvm-cov` (80% line gate),
   a pandoc Markdown gate, `just` tasks mirroring CI, and CI itself
   (ubuntu+windows matrix, MSRV job, deny job, SHA-pinned actions). Adapt
   `Cargo.toml` (name, `rust-toolchain.toml` pin, MSRV, license) and add a
   workspace only if the port scope splits per component.
2. PyO3 module skeleton matching the installed API generation: Bound API
   (0.21+), gil-refs removed (0.23), `attach`/`detach` + thread-safe default
   (0.28+). Do not mix generations.
3. `maturin`: wheel build + `pip install` smoke test. Choose abi3 vs
   version-specific wheels; free-threaded Python needs `cp314t` wheels
   (`abi3t` arrives with 3.15).
4. Extend the template's CI with what a PyO3 port needs that it lacks:
   `cargo-nextest` as the test runner, a nightly `miri` job, and a wheel job
   (`maturin build` + `pip install` + `pytest -k smoke`). These gates exist
   so Stage 4 failure feedback is mechanical.
5. Dev loop: `rustimport` for per-unit iteration. Error policy: Rust errors →
   `PyErr` at the boundary; no `unwrap()`/`expect()` in exported functions
   (the template's clippy policy already denies these); panics caught and
   converted, never raised across the FFI.
6. End-to-end dry run: empty-crate parity harness executes (stub → stub).

## Exit criteria
- [ ] Scaffold based on rust-template; its full gate suite (`just check`) green
- [ ] `maturin develop && pytest -k smoke` green
- [ ] CI red-blocking on fmt/clippy; miri scheduled
- [ ] Wheels build for target platforms incl. free-threaded variant if needed
- [ ] Parity harness runs end-to-end against the stub
