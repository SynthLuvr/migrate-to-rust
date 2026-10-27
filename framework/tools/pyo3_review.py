#!/usr/bin/env python3
"""pyo3_review.py — PyO3-FFI linter (framework/tools/README.md #6).

Stages 4, 5. Encodes the reviewer checklist (roles/reviewer.md §2) as
deterministic rules for the FFI-hazard classes miri and clippy do NOT
cover: refcount handling, swallowed ``PyErr``, panics at the boundary,
GIL ``detach`` misuse, free-threading hazards, and PyO3 API-generation
mixing (knowledge/substitutions.json: "pin one generation; never mix").

Modes:
  files/dir   full-file analysis: tracks #[pyfunction]/#[pymethods] spans
              (comment/string-aware brace counting) so exported-function
              rules apply to real function bodies — the definitive mode
  diff        unified-diff analysis: rules applied to ADDED lines with the
              hunk's line numbers; function-scoped rules are flagged with
              context=diff (reduced confidence — re-run in file mode before
              recording a verdict)

Rules (id, severity): findings cite the reviewer checklist item they encode.
  PYO3-UNWRAP     error   unwrap/expect/panic!/unreachable!/todo! in an
                          exported function (panics must become PyErr)
  PYO3-SWALLOW    error   ``let _ =`` or ``.ok()`` discarding an FFI-ish
                          fallible call (silently swallowed errors)
  PYO3-REFCOUNT   error   into_raw/from_owned_ptr/from_borrowed_ptr/
                          Py_INCREF/Py_DECREF/steal_ref without an adjacent
                          unsafe context + SAFETY justification
  PYO3-DETACH     error   GIL-bound references used inside a
                          Python::detach / allow_threads closure (UB-adjacent)
  PYO3-GEN        error   mixed API generations in one review scope
                          (allow_threads vs attach/detach; with_gil remnants)
  PYO3-THREADSAFE warning Rc/RefCell/Cell/static mut/thread_local! in
                          #[pyclass] fields (not Send/Sync — free-threading)
  PYO3-INTWIDTH   warning isize/usize casts on Py ints near the boundary
                          (Python ints are arbitrary-precision)

Exit codes: 0 no error-severity findings · 2 findings present (errors, or
warnings with --strict) · 1 usage/IO.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import EXIT_FINDINGS, EXIT_OK, ToolError, emit, run_cli  # noqa: E402

TOOL = "pyo3_review.py"
VERSION = 1

CHECKLIST = "framework/roles/reviewer.md#2-pyo3-ffi-hazards"

UNWRAP_RE = re.compile(r"\.(unwrap|expect)\(|\b(panic!|unreachable!|todo!|unimplemented!)\(")
SWALLOW_LET_RE = re.compile(r"let\s+_\s*=\s*(.+)")
SWALLOW_OK_RE = re.compile(r"(\S+)\.ok\(\)\s*;")
FFI_ISH_RE = re.compile(r"\b(Py[A-Z]\w*|py\.\w+|\.call\w*|\.extract\w*|from_py\w*|\.py\(\))")
REFCOUNT_RE = re.compile(
    r"\.into_raw\(|from_owned_ptr\w*\(|from_borrowed_ptr\w*\(|steal_ref|"
    r"\bPy_(INC|DEC|X?)REF(?:NT)?\b|Py_XDECREF")
UNSAFE_RE = re.compile(r"\bunsafe\b")
SAFETY_RE = re.compile(r"//\s*(SAFETY|Provenance|provenance)")
DETACH_RE = re.compile(
    r"(Python::\s*(?:detach|allow_threads)\s*\(|Python::attach\s*\("
    r"|\.(?:detach|allow_threads)\s*\()")
PY_BOUND_RE = re.compile(r"\bpy\b|Py<|Bound<|\.py\(\)|Python<'_>")
GEN_OLD_RE = re.compile(r"\ballow_threads\b|\bPython::with_gil\b")
GEN_NEW_RE = re.compile(r"\bPython::(?:attach|detach)\b|\.(?:attach|detach)\s*\(")
PYCLASS_FIELD_RE = re.compile(r":\s*(Rc<|RefCell<|Cell<)|\bthread_local!")
STATIC_MUT_RE = re.compile(r"\bstatic\s+mut\b")
INT_CAST_RE = re.compile(r"as\s+(i|u)(size|\d+)|\.extract::<\(i|u\)\w*>")

ATTR_RE = re.compile(r"#\[\s*(pyfunction|pymethods|pyclass)")
FN_RE = re.compile(r"^\s*(?:pub\s+)?(?:unsafe\s+)?(?:extern\s+)?fn\s+(\w+)")
PYMETHODS_ATTR_RE = re.compile(r"#\[\s*pymethods")
PYCLASS_ATTR_RE = re.compile(r"#\[\s*pyclass")


def _is_char_literal(source: str, i: int) -> bool:
    """Disambiguate 'a' (char literal) from 'a / '_ / 'static (lifetimes).
    A char literal closes with a second quote within a few characters on the
    same line (allowing escapes); a lifetime does not."""
    j = i + 1
    if j < len(source) and source[j] == "\\":
        j += 2  # escape: '\\', '\n', '\u{...}' — accept up to 8 chars
        while j < len(source) and source[j] != "'" and source[j] != "\n" and j - i < 10:
            j += 1
        return j < len(source) and source[j] == "'"
    while j < len(source) and source[j] != "'" and source[j] != "\n" and j - i <= 3:
        j += 1
    return j < len(source) and source[j] == "'"


def strip_comments_strings(source: str) -> str:
    """Blank out // and /* */ comments plus string/char literal CONTENTS so
    brace counting and rule regexes see only code (deterministic lexer)."""
    out = []
    i, n = 0, len(source)
    mode = None  # None | 'line' | 'block' | 'str' | 'raw' | 'char'
    while i < n:
        c = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if mode is None:
            if c == "/" and nxt == "/":
                mode = "line"; out.append("  "); i += 2; continue
            if c == "/" and nxt == "*":
                mode = "block"; out.append("  "); i += 2; continue
            if c == '"':
                # raw string r"..." or r#"..."#
                prefix = source[max(0, i - 1):i]
                if prefix == "r":
                    mode = "raw"
                else:
                    mode = "str"
                out.append('"'); i += 1; continue
            if c == "'":
                if _is_char_literal(source, i):
                    mode = "char"; out.append(" "); i += 1; continue
                # lifetime ('a, '_, 'static): emit verbatim — it is code,
                # not a literal, and must not swallow the rest of the file
                out.append(c); i += 1; continue
            out.append(c); i += 1; continue
        if mode == "line":
            if c == "\n":
                mode = None; out.append("\n")
            else:
                out.append(" ")
            i += 1; continue
        if mode == "block":
            if c == "*" and nxt == "/":
                mode = None; out.append("  "); i += 2; continue
            out.append("\n" if c == "\n" else " "); i += 1; continue
        if mode in ("str", "raw", "char"):
            if c == "\n":
                out.append("\n"); i += 1; continue  # keep line structure
            if c == "\\" and mode != "raw":
                out.append("  "); i += 2; continue
            if (c == '"' and mode in ("str", "raw")) or (c == "'" and mode == "char"):
                mode = None; out.append(c)
            else:
                out.append(" ")
            i += 1; continue
    return "".join(out)


def exported_spans(code: str) -> list[tuple[int, int, str]]:
    """[(start_line, end_line, fn_name)] for functions introduced by a
    PyO3 attribute (#[pyfunction], first fn of a #[pymethods] impl).
    1-based inclusive lines."""
    lines = code.splitlines()
    spans = []
    pending_attr = False
    depth = 0
    fn_start = fn_name = fn_depth = None
    for lineno, line in enumerate(lines, start=1):
        pending_attr = pending_attr or bool(ATTR_RE.search(line))
        opened = line.count("{")
        closed = line.count("}")
        if fn_start is None and pending_attr and FN_RE.match(line):
            fn_name = FN_RE.match(line).group(1)
            fn_start = lineno
            fn_depth = depth + opened
            pending_attr = False
        depth += opened - closed
        if fn_start is not None and depth < fn_depth and closed:
            spans.append((fn_start, lineno, fn_name or "<anon>"))
            fn_start = fn_name = fn_depth = None
    if fn_start is not None:
        spans.append((fn_start, len(lines), fn_name or "<anon>"))
    return spans


def _attr_block_ranges(code: str, attr_re: re.Pattern, keyword: str) -> list[tuple[int, int]]:
    """Line ranges of `impl`/`struct` blocks introduced by a matching
    attribute line (e.g. #[pymethods] impl, #[pyclass] struct)."""
    lines = code.splitlines()
    ranges = []
    pending = False
    depth = 0
    start = None
    for lineno, line in enumerate(lines, start=1):
        if attr_re.search(line):
            pending = True
        if pending and re.match(rf"^\s*(pub\s+)?{keyword}\b", line):
            start = lineno
            pending = False
        depth += line.count("{") - line.count("}")
        if start is not None and depth <= 0 and "}" in line:
            ranges.append((start, lineno))
            start = None
    return ranges


def review_file(path: Path, findings: list[dict]) -> None:
    raw = path.read_text(encoding="utf-8", errors="replace")
    code = strip_comments_strings(raw)
    raw_lines = raw.splitlines()
    code_lines = code.splitlines()
    exported = exported_spans(code)
    # functions inside a #[pymethods] impl are exported too
    for start, end in _attr_block_ranges(code, PYMETHODS_ATTR_RE, "impl"):
        depth = 0
        fn_start = fn_name = fn_depth = None
        for lineno in range(start, end + 1):
            line = code_lines[lineno - 1]
            m = FN_RE.match(line)
            if fn_start is None and m:
                fn_name, fn_start = m.group(1), lineno
                fn_depth = depth + line.count("{")
            depth += line.count("{") - line.count("}")
            if fn_start is not None and depth < fn_depth:
                exported.append((fn_start, lineno, fn_name))
                fn_start = fn_name = fn_depth = None

    def in_exported(lineno: int) -> str | None:
        for s, e, name in exported:
            if s <= lineno <= e:
                return name
        return None

    def add(rule: str, severity: str, lineno: int, snippet: str, why: str,
            context: str = "file") -> None:
        findings.append({"rule": rule, "severity": severity, "file": str(path),
                         "line": lineno, "snippet": snippet.strip()[:120],
                         "why": why, "checklist": CHECKLIST, "context": context})

    # exported-fn rules
    for lineno, line in enumerate(code_lines, start=1):
        fname = in_exported(lineno)
        if fname is not None and UNWRAP_RE.search(line):
            add("PYO3-UNWRAP", "error", lineno, raw_lines[lineno - 1],
                f"{UNWRAP_RE.search(line).group(0)} inside exported fn {fname}: a panic "
                f"crossing the FFI boundary is UB-adjacent — convert to PyErr "
                f"(translator.md rule 4)")
        if fname is not None:
            m = SWALLOW_LET_RE.search(line)
            if m and FFI_ISH_RE.search(m.group(1)):
                add("PYO3-SWALLOW", "error", lineno, raw_lines[lineno - 1],
                    "`let _ =` discards a fallible FFI call result — errors must "
                    "propagate as PyErr, never be swallowed")
            m = SWALLOW_OK_RE.search(line)
            if m and FFI_ISH_RE.search(m.group(1)):
                add("PYO3-SWALLOW", "error", lineno, raw_lines[lineno - 1],
                    ".ok() discards the Err side of a fallible FFI call — errors "
                    "must propagate as PyErr")
        if REFCOUNT_RE.search(line):
            window = "\n".join(code_lines[max(0, lineno - 3):lineno])
            justified = SAFETY_RE.search(raw_lines[lineno - 1]) or any(
                SAFETY_RE.search(l) for l in raw_lines[max(0, lineno - 3):lineno])
            in_unsafe = UNSAFE_RE.search(window)
            sev = "warning" if (in_unsafe and justified) else "error"
            add("PYO3-REFCOUNT", sev, lineno, raw_lines[lineno - 1],
                "manual refcount/provenance transfer — requires an unsafe context "
                f"plus a SAFETY justification ({'present' if justified else 'MISSING'})")
        if INT_CAST_RE.search(line) and fname is not None:
            add("PYO3-INTWIDTH", "warning", lineno, raw_lines[lineno - 1],
                "fixed-width integer cast near the boundary: Python ints are "
                "arbitrary-precision — use try_from / checked conversions "
                "(reviewer checklist §3)")

    # detach/allow_threads closures: GIL-bound refs inside are errors
    for lineno, line in enumerate(code_lines, start=1):
        if DETACH_RE.search(line):
            # scan the closure body: from this line to the line where the
            # brace opened here closes (approximate: balanced braces)
            depth = 0
            started = False
            for j in range(lineno, min(len(code_lines), lineno + 60) + 1):
                inner = code_lines[j - 1]
                depth += inner.count("{") - inner.count("}")
                if "{" in inner:
                    started = True
                if started and depth <= 0:
                    break
                if j > lineno and PY_BOUND_RE.search(inner):
                    add("PYO3-DETACH", "error", j, raw_lines[j - 1],
                        f"GIL-bound reference used inside the detach/allow_threads "
                        f"closure opened at line {lineno} — the GIL is released there")
                    break

    # generation mixing across the whole file
    if GEN_OLD_RE.search(code) and GEN_NEW_RE.search(code):
        add("PYO3-GEN", "error", 1, "<crate-wide>",
            "mixed PyO3 API generations in one file (pre-0.28 allow_threads/with_gil "
            "alongside 0.28+ attach/detach) — pin one generation per crate "
            "(knowledge/substitutions.json pyo3_api_generations)")

    # free-threading hazards in pyclass fields
    for start, end in _attr_block_ranges(code, PYCLASS_ATTR_RE, "struct"):
        for lineno in range(start, end + 1):
            line = code_lines[lineno - 1]
            if PYCLASS_FIELD_RE.search(line) or STATIC_MUT_RE.search(line):
                add("PYO3-THREADSAFE", "warning", lineno, raw_lines[lineno - 1],
                    "non-Send/Sync interior mutability (or static mut) in a #[pyclass] "
                    "field — hazard under free-threaded CPython (PEP 703)")


DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$")
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def review_diff(path: Path, findings: list[dict]) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    current_file, new_lineno = None, 0
    for line in text.splitlines():
        m = DIFF_FILE_RE.match(line)
        if m:
            current_file = m.group(1)
            continue
        m = HUNK_RE.match(line)
        if m:
            new_lineno = int(m.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            content = line[1:]
            if UNWRAP_RE.search(content):
                findings.append({"rule": "PYO3-UNWRAP", "severity": "error",
                                 "file": current_file or "<diff>", "line": new_lineno,
                                 "snippet": content[:120],
                                 "why": "panic-capable call on an ADDED line in a PyO3 "
                                        "crate diff — verify it is not in an exported "
                                        "fn (file mode gives the definitive answer)",
                                 "checklist": CHECKLIST, "context": "diff"})
            if REFCOUNT_RE.search(content):
                findings.append({"rule": "PYO3-REFCOUNT", "severity": "error",
                                 "file": current_file or "<diff>", "line": new_lineno,
                                 "snippet": content[:120],
                                 "why": "refcount/provenance transfer on an ADDED line "
                                        "without visible unsafe context/SAFETY note",
                                 "checklist": CHECKLIST, "context": "diff"})
            m2 = SWALLOW_LET_RE.search(content)
            if m2 and FFI_ISH_RE.search(m2.group(1)):
                findings.append({"rule": "PYO3-SWALLOW", "severity": "error",
                                 "file": current_file or "<diff>", "line": new_lineno,
                                 "snippet": content[:120],
                                 "why": "`let _ =` discards a fallible FFI call "
                                        "(diff-mode line rule)",
                                 "checklist": CHECKLIST, "context": "diff"})
            new_lineno += 1
        elif line.startswith("-"):
            continue
        else:
            new_lineno += 1


RULES_DOC = {
    "PYO3-UNWRAP": "panic-capable calls in exported functions (must become PyErr)",
    "PYO3-SWALLOW": "let _ = / .ok() discarding fallible FFI results",
    "PYO3-REFCOUNT": "manual refcount/provenance ops without unsafe + SAFETY",
    "PYO3-DETACH": "GIL-bound refs inside detach/allow_threads closures",
    "PYO3-GEN": "mixed PyO3 API generations in one scope",
    "PYO3-THREADSAFE": "non-Send/Sync interior mutability in pyclass fields",
    "PYO3-INTWIDTH": "unchecked fixed-width casts on Python ints",
}


def cmd_review(args) -> int:
    findings: list[dict] = []
    targets: list[Path] = []
    if args.diff:
        review_diff(Path(args.diff), findings)
    if args.files:
        targets.extend(Path(f) for f in args.files)
    if args.dir:
        from _common import iter_tree_files
        targets.extend(iter_tree_files(args.dir, extensions={".rs"}))
    if not targets and not args.diff:
        raise ToolError("nothing to review: pass --diff, --files, or --dir",
                        kind="usage")
    for t in targets:
        if t.suffix != ".rs":
            continue
        review_file(t, findings)

    errors = [f for f in findings if f["severity"] == "error"]
    warnings = [f for f in findings if f["severity"] == "warning"]
    by_rule: dict[str, int] = {}
    for f in findings:
        by_rule[f["rule"]] = by_rule.get(f["rule"], 0) + 1
    fail = bool(errors) or (args.strict and warnings)
    code = EXIT_FINDINGS if fail else EXIT_OK
    human = (f"pyo3 review: {len(errors)} error(s), {len(warnings)} warning(s)"
             + (f" over {len(targets)} file(s)" if targets else " (diff mode)")
             + ("" if not fail else " — FIX before recording a verdict"))
    emit({"tool": TOOL, "version": VERSION,
          "mode": ("diff" if args.diff else "files") + ("+diff" if args.diff and targets else ""),
          "files_reviewed": [str(t) for t in targets],
          "diff": args.diff, "findings": findings,
          "counts": {"errors": len(errors), "warnings": len(warnings)},
          "by_rule": dict(sorted(by_rule.items())),
          "rules": RULES_DOC,
          "summary": {"exit_code": code, "human": human}}, human_flag=args.human)
    return code


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=TOOL, description=__doc__.splitlines()[0])
    ap.add_argument("--human", action="store_true", help="echo human summary to stderr")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("review", help="lint Rust sources or a diff for FFI hazards")
    p.add_argument("--diff", help="unified diff to review (added lines)")
    p.add_argument("--files", nargs="*", help="Rust files to review")
    p.add_argument("--dir", help="directory to review recursively (*.rs)")
    p.add_argument("--strict", action="store_true", help="warnings also fail")
    p.add_argument("--min-severity", choices=["error", "warning"], default="warning")
    p.set_defaults(func=cmd_review)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(run_cli(main, tool=TOOL, version=VERSION))
