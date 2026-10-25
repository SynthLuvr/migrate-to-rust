# Stage 3 — Scaffold

Purpose: build plumbing and CI green **before** porting, so Stage 4 feedback
is instant and trustworthy.

## Steps
1. Cargo workspace; one crate for the port scope (or per component).
2. PyO3 module skeleton matching the installed API generation: Bound API
   (0.21+), gil-refs removed (0.23), `attach`/`detach` + thread-safe default
   (0.28+). Do not mix generations.
3. `maturin`: wheel build + `pip install` smoke test. Choose abi3 vs
   version-specific wheels; free-threaded Python needs `cp314t` wheels
   (`abi3t` arrives with 3.15).
4. CI wired before any code: `rustfmt`, `clippy` (deny warnings),
   `cargo-nextest`, `miri` (nightly job), `llvm-cov`. These gates exist so
   Stage 4 failure feedback is mechanical.
5. Dev loop: `rustimport` for per-unit iteration. Error policy: Rust errors →
   `PyErr` at the boundary; no `unwrap()`/`expect()` in exported functions;
   panics caught and converted, never raised across the FFI.
6. End-to-end dry run: empty-crate parity harness executes (stub → stub).

## Exit criteria
- [ ] `maturin develop && pytest -k smoke` green
- [ ] CI red-blocking on fmt/clippy; miri scheduled
- [ ] Wheels build for target platforms incl. free-threaded variant if needed
- [ ] Parity harness runs end-to-end against the stub
