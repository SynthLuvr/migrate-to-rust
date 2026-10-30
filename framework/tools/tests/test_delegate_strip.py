#!/usr/bin/env python3
"""Regression tests for candidate-strip wiring (#83).

_strip_candidate() must remove BOTH wirings of a non-kept candidate from a
gate sandbox: the `mod <m>;` line (old behavior) and the hand-written
`crate::ffi::register_submodule(...)?;` block in the crate root (#83 —
leaving the latter dangling failed every sibling gate with E0433, which the
candidate may not fix per translator rule 9, so the repair loop burned
MAX_ROUNDS on the identical error).

Run: python3 framework/tools/tests/test_delegate_strip.py
(pytest also collects these — every test is a plain assert function.)
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import delegate  # noqa: E402

MULTI = '''\
        crate::ffi::register_submodule(
            m,
            "u_signal_result_c1",
            &crate::u_signal_result_c1::module(m.py())?,
        )?;
        crate::ffi::register_submodule(
            m,
            "u_signal_result_c2",
            &crate::u_signal_result_c2::module(m.py())?,
        )?;
'''

SINGLE = ('crate::ffi::register_submodule(m, "u_signal_result_c1", '
          '&crate::u_signal_result_c1::module(m.py())?)?;\n')


def test_multi_line_block_stripped_sibling_kept():
    out = delegate._strip_register_blocks(MULTI, {"u_signal_result_c1"})
    assert "u_signal_result_c1" not in out
    assert out.count("register_submodule") == 1  # sibling's block survives
    assert "u_signal_result_c2" in out


def test_single_line_block_stripped():
    out = delegate._strip_register_blocks(SINGLE, {"u_signal_result_c1"})
    assert "u_signal_result_c1" not in out


def test_strip_does_not_cross_statement_boundary():
    # c1 registered BEFORE c2: stripping c1 must not eat c2's block, and
    # stripping c2 must leave c1's block byte-identical.
    out1 = delegate._strip_register_blocks(MULTI, {"u_signal_result_c1"})
    assert out1.count("register_submodule") == 1
    assert out1.strip().startswith("crate::ffi::register_submodule(")
    assert out1.rstrip().endswith(")?;")
    out2 = delegate._strip_register_blocks(MULTI, {"u_signal_result_c2"})
    assert out2.count("register_submodule") == 1
    assert "u_signal_result_c1" in out2


def test_no_match_leaves_text_untouched():
    assert delegate._strip_register_blocks(MULTI, {"u_bars"}) == MULTI


def test_mod_line_strip_unchanged():
    text = "#[cfg(test)]\nmod u_signal_result_c1;\nmod other;\n"
    out = delegate._strip_mod_lines(text, {"u_signal_result_c1"})
    assert "u_signal_result_c1" not in out
    assert "mod other;" in out


def test_real_shape_from_incident():
    # The exact c2-sandbox shape from the wicksmith run (2026-10-30): kept
    # candidate c2 has mod + registration, dead candidate c1 has only the
    # registration block. Stripping c1 must leave c2's wiring intact and
    # zero c1 references — the old code passed the compile precisely
    # because it never touched this block.
    lib = ("mod u_signal_result_c2;\n\n"
           + MULTI)
    out = delegate._strip_register_blocks(lib, {"u_signal_result_c1"})
    out = delegate._strip_mod_lines(out, {"u_signal_result_c1"})
    assert "u_signal_result_c1" not in out
    assert "mod u_signal_result_c2;" in out
    assert '"u_signal_result_c2"' in out


def test_strip_candidate_register_only_counts_wired():
    # A sandbox whose lib.rs carries ONLY the register block (file already
    # absent) must still report wired=True and be cleaned — the old code
    # left it untouched because pattern.search(mod) found nothing.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        lib = root / "rust" / "src" / "lib.rs"
        lib.parent.mkdir(parents=True)
        lib.write_text("mod kept_thing;\n" + MULTI, encoding="utf-8")
        wired = delegate._strip_candidate(
            root,
            {"rust_file_rel": "rust/src/u_signal_result_c1.rs",
             "module": "u_signal_result_c1"},
            [lib])
        assert wired, "register-only wiring must count as wired (#83)"
        text = lib.read_text(encoding="utf-8")
        assert "u_signal_result_c1" not in text
        assert "mod kept_thing;" in text
        assert '"u_signal_result_c2"' in text


def test_strip_candidate_full_incident_sandbox():
    # End-to-end: mod line + file + register block all present (the state
    # every c2+ sandbox was born in before #83).
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cand = root / "rust" / "src" / "u_signal_result_c1.rs"
        cand.parent.mkdir(parents=True)
        cand.write_text("// candidate c1\n", encoding="utf-8")
        lib = root / "rust" / "src" / "lib.rs"
        lib.write_text("mod u_signal_result_c1;\nmod u_signal_result_c2;\n\n"
                       + MULTI, encoding="utf-8")
        wired = delegate._strip_candidate(
            root,
            {"rust_file_rel": "rust/src/u_signal_result_c1.rs",
             "module": "u_signal_result_c1"},
            [lib])
        assert wired
        assert not cand.exists()
        text = lib.read_text(encoding="utf-8")
        assert "u_signal_result_c1" not in text
        assert "mod u_signal_result_c2;" in text
        assert '"u_signal_result_c2"' in text


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL {name}: {exc!r}")
    sys.exit(1 if failed else 0)
