#!/usr/bin/env python3
"""contract_extract.py — per-unit contract extractor (framework/tools/README.md #3).

Stage 2. Turns pytest collection + source AST (optionally mypy JSON) into a
``contract.json`` per unit: covering tests, type signature, invariants,
golden-corpus pointer. Deterministic: stdlib ``ast`` only; the tool EXTRACTS,
never invents — invariants come only from tagged docstrings/asserts that
already exist (an LLM may enrich them later; that is an edit, not a check).

Test → unit mapping (deterministic, in priority order):
  1. the test file imports the unit's module (``from pkg.parse import x`` /
     ``import pkg.parse`` / aliased attribute use), matched under both the
     layout-stripped name and the un-stripped ``src.``-prefixed one
     (issue #26) — authoritative;
  2. filename convention ``test_<module>.py`` / ``<module>_test.py`` when the
     test file has no absolute imports at all.

Coverage is TRANSITIVE over the state file's ``deps[]`` graph (issue #57): a
test covering unit A also exercises everything A (transitively) imports, so a
shared helper with no directly-importing test file is covered via its
consumers' suites. Direct and transitive coverage are reported distinctly —
``covering_tests`` stays direct-only; ``transitive_covering_tests`` /
``covered_via`` record the consumer provenance — and both count toward the
acceptance criterion: 100% of scoped units covered, so units with neither
fail the run unless --allow-uncovered.

Tests matching no scoped unit split by SCOPE (issue #57). Dangling-within-
scope — the collected test file is missing from the project, or the test
file is named for a scoped unit module yet imports none of them — is the
real signal and always fails the run (never dropped silently). Tests that
exercise modules outside the approved tranche (their own units are not in
the state file), or nothing project-related at all, are out-of-scope and
reported informationally (``out_of_scope_tests``) — they never drive the
exit code, because Stage 0/1 approved a tranche, not the repo.

Invariants are taken from docstring lines tagged ``Invariant:`` /
``Postcondition:`` / ``Property:`` in the unit module and its covering tests.

Type signatures come from the AST (annotated defs render fully; unannotated
parameters render as ``name: Any``) plus optional mypy JSON rows.

Output: one ``<unit-id>.contract.json`` per unit under --out, plus a summary
report on stdout. Exit codes: 0 covered · 2 dangling/uncovered findings ·
1 usage/IO.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    EXIT_FINDINGS, EXIT_OK, ToolError, emit, import_candidates_of,
    module_name_of, read_json, run_child, run_cli, write_json_atomic,
)

TOOL = "contract_extract.py"
VERSION = 2
INVARIANT_TAGS = ("Invariant:", "Postcondition:", "Property:")
NODEID_RE = re.compile(r"^(\S+\.py)::(\S+)$")
TEST_STEM_RE = re.compile(r"^(?:test_)?(\w+?)(?:_test)?$")
NO_UNIT_MATCH = "imports no scoped unit and matches no unit module name"
NAMED_FOR_SCOPED_UNIT = "named for scoped unit module '{tail}' but imports no scoped unit"


# ---------------------------------------------------------------- pytest ---

def collect_pytest(root: Path) -> list[str]:
    """Run pytest --collect-only -q ourselves. Node IDs only; the trailing
    'N tests collected' summary lines are filtered out."""
    code, stdout, stderr = run_child(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(root)],
        cwd=str(root), timeout=300.0)
    if code not in (0, 5) or not stdout:  # 5 = no tests collected
        raise ToolError(
            f"pytest collection failed (exit {code}): {stderr.strip()[:300]}",
            kind="setup" if code == 4 else "io")
    nodeids = []
    for line in stdout.splitlines():
        line = line.strip()
        if NODEID_RE.match(line):
            nodeids.append(line)
    return nodeids


def parse_collect_file(path: Path) -> list[str]:
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if NODEID_RE.match(line):
            out.append(line)
    return out


# ------------------------------------------------------------------ AST ----

def imports_of(tree: ast.Module) -> set[str]:
    """All modules the file imports (absolute imports only)."""
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                mods.add(node.module)
    return mods


def imports_any_of(imported: set[str], candidates: list[str]) -> bool:
    """True when a candidate is among ``imported``, exactly or as a parent
    package (import ``pkg.parse`` covers the unit module ``pkg``)."""
    return any(c in imported or any(m.startswith(c + ".") for m in imported)
               for c in candidates)


def docstring_invariants(node) -> list[str]:
    doc = ast.get_docstring(node)
    if not doc:
        return []
    out = []
    for line in doc.splitlines():
        s = line.strip()
        for tag in INVARIANT_TAGS:
            if s.startswith(tag):
                out.append(s)
                break
    return out


def render_sig(fn) -> str:
    args = []
    a = fn.args
    pos = (a.posonlyargs or []) + a.args
    defaults = [None] * (len(pos) - len(a.defaults or [])) + list(a.defaults or [])
    for arg, default in zip(pos, defaults):
        annotation = ast.unparse(arg.annotation) if arg.annotation else "Any"
        args.append(f"{arg.arg}: {annotation}"
                    + (f" = {ast.unparse(default)}" if default else ""))
    if a.vararg:
        annotation = ast.unparse(a.vararg.annotation) if a.vararg.annotation else "Any"
        args.append(f"*{a.vararg.arg}: {annotation}")
    for arg, default in zip(a.kwonlyargs or [], a.kw_defaults or []):
        annotation = ast.unparse(arg.annotation) if arg.annotation else "Any"
        args.append(f"{arg.arg}: {annotation}"
                    + (f" = {ast.unparse(default)}" if default else ""))
    if a.kwarg:
        annotation = ast.unparse(a.kwarg.annotation) if a.kwarg.annotation else "Any"
        args.append(f"**{a.kwarg.arg}: {annotation}")
    ret = ast.unparse(fn.returns) if fn.returns else "Any"
    prefix = "async def" if isinstance(fn, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {fn.name}({', '.join(args)}) -> {ret}"


def signatures_of(tree: ast.Module) -> list[str]:
    sigs = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sigs.append(render_sig(node))
        elif isinstance(node, ast.ClassDef):
            methods = [render_sig(n) for n in node.body
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            sigs.append(f"class {node.name}: " + "; ".join(methods) if methods
                        else f"class {node.name}")
    return sigs


# ------------------------------------------------ test ↔ unit mapping ---

def stem_tail_of(test_file: str) -> str | None:
    """Module-name tail a test filename claims by convention:
    ``tests/test_parse.py`` / ``tests/parse_test.py`` -> ``parse``."""
    m = TEST_STEM_RE.match(Path(test_file).stem)
    return m.group(1) if m else None


def units_covered_by(tmods: set[str], test_file: str,
                     candidates_of_unit: dict[str, list[str]],
                     module_of_unit: dict[str, str]) -> list[str]:
    """Unit ids a test file covers: the authoritative scoped-import match,
    then the filename convention — but only when the file has no absolute
    imports at all, so a test that imports something yet misses every scoped
    unit stays unmatched (and gets scope-classified) instead of silently
    mapping by name."""
    matched = [uid for uid, cands in candidates_of_unit.items()
               if imports_any_of(tmods, cands)]
    if not matched and not tmods:
        tail = stem_tail_of(test_file)
        if tail:
            matched = [uid for uid, umod in module_of_unit.items()
                       if umod.split(".")[-1] == tail]
    return matched


def is_project_module(mod: str, bases: list[Path]) -> bool:
    """True when an imported dotted name resolves to source under one of the
    bases — project code, scoped or not; stdlib/third-party imports
    (``os``, ``zlib``, ...) do not resolve."""
    rel = Path(*mod.split("."))
    return any((base / rel).with_suffix(".py").is_file()
               or (base / rel / "__init__.py").is_file()
               for base in bases)


def project_code_bases(root: Path, project_root: Path,
                       unit_files: dict[str, Path]) -> list[Path]:
    """Directories ``is_project_module`` may resolve under: the roots and the
    unit files' own directories, plus their src/ and lib/ layout variants."""
    bases: list[Path] = []
    for base in (root, project_root, *(f.parent for f in unit_files.values())):
        for variant in (base, base / "src", base / "lib"):
            if variant not in bases:
                bases.append(variant)
    return bases


def dep_closure_of(uid: str, deps_of: dict[str, list[str]]) -> set[str]:
    """Transitive dependency closure over the deps[] unit-id graph; cycle-safe
    (a back-edge only ends the walk) and ids that are not units are skipped."""
    seen: set[str] = set()
    work = list(deps_of.get(uid, ()))
    while work:
        dep = work.pop()
        if dep in seen or dep not in deps_of:
            continue
        seen.add(dep)
        work.extend(deps_of[dep])
    return seen


def transitive_coverage(
        covering: dict[str, list[str]],
        units: list[dict]) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Per-unit (transitive tests, consumer unit ids) for coverage reached
    only through a consumer's deps[] closure (issue #57): a test of unit A
    exercises everything A transitively imports, so consumer suites cover a
    shared helper with no directly-importing test file."""
    deps_of = {u["id"]: [d for d in (u.get("deps") or []) if isinstance(d, str)]
               for u in units}
    tests_of: dict[str, set[str]] = {uid: set() for uid in covering}
    via_of: dict[str, set[str]] = {uid: set() for uid in covering}
    for uid, tests in covering.items():
        if not tests:
            continue
        for dep in dep_closure_of(uid, deps_of):
            if dep == uid or dep not in tests_of:
                continue
            tests_of[dep].update(tests)
            via_of[dep].add(uid)
    return tests_of, via_of


# ----------------------------------------------------------- the command ---

def cmd_extract(args) -> int:
    state = read_json(args.state)
    units = state.get("units", [])
    if not units:
        raise ToolError("state file has no units — run port_plan.py first",
                        kind="usage")
    root = Path(args.root or ".").resolve()
    state_path = Path(args.state).resolve()
    project_root = state_path.parent.parent  # <root>/framework/state/x.json

    # locate each unit's python file
    unit_files: dict[str, Path] = {}
    for u in units:
        cand = _find_source(u["python_path"], root, project_root)
        if cand is None:
            raise ToolError(
                f"unit {u['id']}: python file not found: {u['python_path']}")
        unit_files[u["id"]] = cand

    if args.pytest_collect:
        nodeids = parse_collect_file(Path(args.pytest_collect))
    else:
        nodeids = collect_pytest(root)

    # parse every test file once
    test_trees: dict[str, ast.Module] = {}
    for nodeid in nodeids:
        test_file = nodeid.split("::", 1)[0]
        if test_file not in test_trees:
            path = _find_source(test_file, root, project_root)
            test_trees[test_file] = ast.parse(path.read_text()) if path else None

    # module names come from the DECLARED python_path (project-relative),
    # matching how test files import them — never from the resolved location
    module_of_unit = {u["id"]: module_name_of(u["python_path"]) for u in units}
    candidates_of_unit = {u["id"]: import_candidates_of(u["python_path"])
                          for u in units}

    covering: dict[str, list[str]] = {uid: [] for uid in unit_files}
    dangling: list[dict] = []      # within scope — the real signal (exit 2)
    out_of_scope: list[dict] = []  # informational; never drives the exit code
    unit_tails = {mod.split(".")[-1] for mod in module_of_unit.values()}
    bases = project_code_bases(root, project_root, unit_files)

    for nodeid in nodeids:
        test_file = nodeid.split("::", 1)[0]
        tree = test_trees.get(test_file)
        if tree is None:
            dangling.append({"nodeid": nodeid, "reason": "test file not found in project"})
            continue
        tmods = imports_of(tree)
        matched = units_covered_by(tmods, test_file, candidates_of_unit, module_of_unit)
        if matched:
            for uid in matched:
                covering[uid].append(nodeid)
            continue
        # matched no scoped unit — split the finding by scope (issue #57)
        tail = stem_tail_of(test_file)
        if tail in unit_tails:
            # the file NAMES a scoped unit module yet imports none of them:
            # a mapping failure worth a human look, not scope noise
            dangling.append({"nodeid": nodeid,
                             "reason": NAMED_FOR_SCOPED_UNIT.format(tail=tail)})
            continue
        resident = sorted(m for m in tmods if is_project_module(m, bases))
        reason = (f"tests unscoped project module(s): {', '.join(resident)}"
                  if resident else NO_UNIT_MATCH)
        out_of_scope.append({"nodeid": nodeid, "reason": reason})

    transitive_tests, covered_via = transitive_coverage(covering, units)

    # mypy rows (optional): {"path": ..., "line": ..., "symbol": ..., "signature": ...}
    mypy_rows = read_json(args.mypy) if args.mypy else []

    out_dir = Path(args.out) if args.out else None
    contracts = []
    for u in units:
        uid = u["id"]
        path = unit_files[uid]
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        invariants = docstring_invariants(tree)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                invariants.extend(docstring_invariants(node))
        # invariants stated in covering tests count toward the contract too
        for nodeid in covering[uid]:
            ttree = test_trees.get(nodeid.split("::", 1)[0])
            if ttree is not None:
                invariants.extend(docstring_invariants(ttree))
        sigs = signatures_of(tree)
        untyped = all((": Any" in s and "-> Any" in s) for s in sigs) if sigs else None
        contract = {
            "unit": uid,
            "python_path": u["python_path"],
            "module": module_of_unit[uid],
            "covering_tests": sorted(set(covering[uid])),
            "transitive_covering_tests": sorted(transitive_tests[uid]),
            "covered_via": sorted(covered_via[uid]),
            "type_signatures": sigs,
            "invariants": sorted(set(invariants)),
            "golden_corpus": (u.get("contract") or {}).get("golden_corpus"),
            "untyped": untyped,
        }
        for row in mypy_rows:
            rp = str(row.get("path", ""))
            if rp.endswith(u["python_path"]) or u["python_path"].endswith(rp):
                if row.get("signature"):
                    contract["type_signatures"].append(f"mypy: {row['signature']}")
        contracts.append(contract)
        if out_dir:
            write_json_atomic(out_dir / f"{uid}.contract.json", contract)

    uncovered = [uid for uid in unit_files
                 if not covering[uid] and not transitive_tests[uid]]
    covered_transitively = [uid for uid in unit_files
                            if not covering[uid] and transitive_tests[uid]]
    coverage_pct = round(100.0 * (len(unit_files) - len(uncovered)) / len(unit_files), 1)

    findings = []
    for uid in uncovered:
        findings.append(f"unit {uid} ({module_of_unit[uid]}) has NO covering tests "
                        f"(direct or via consumers)")
    for d in dangling:
        findings.append(f"dangling test {d['nodeid']}: {d['reason']}")
    # only within-scope findings drive the exit code: dangling always fails,
    # uncovered units fail unless --allow-uncovered, out-of-scope never does
    hard = dangling + ([] if args.allow_uncovered else uncovered)
    code = EXIT_FINDINGS if hard else EXIT_OK
    # every unit uncovered while a within-scope test still went unmatched is
    # far more likely a layout/import-root mismatch than a useless suite —
    # hint at it instead of printing a bare wall of nodeids (issue #26)
    all_uncovered = bool(unit_files) and len(uncovered) == len(unit_files)
    layout_mismatch_suspected = all_uncovered and bool(dangling)
    mismatch_hint = (" — likely layout mismatch: no test import matched any "
                     "unit module name; check the units' python_path import "
                     "root" if layout_mismatch_suspected else "")
    human = (f"contracts: {len(contracts)} unit(s), coverage {coverage_pct}% "
             f"({len(uncovered)} uncovered, {len(covered_transitively)} via consumers), "
             f"dangling-within-scope: {len(dangling)}, "
             f"out-of-scope tests skipped: {len(out_of_scope)} (informational)"
             + ("; FAIL" if code == EXIT_FINDINGS else "") + mismatch_hint)
    payload = {
        "tool": TOOL, "version": VERSION,
        "contracts": contracts,
        "coverage_pct": coverage_pct,
        "covered_transitively": covered_transitively,
        "uncovered_units": uncovered,
        "dangling_tests": dangling,
        "out_of_scope_tests": out_of_scope,
        "layout_mismatch_suspected": layout_mismatch_suspected,
        "findings": findings,
        "written_to": str(out_dir) if out_dir else None,
        "summary": {"exit_code": code, "human": human},
    }
    emit(payload, human_flag=args.human)
    return code


def _find_source(python_path: str, root: Path, project_root: Path) -> Path | None:
    cands = []
    p = Path(python_path)
    if p.is_absolute():
        cands.append(p)
    else:
        cands.extend([root / p, project_root / p])
        # src-layout: <root>/src/<path> and direct name search
        name = Path(python_path).name
        for base in (root, project_root):
            hits = [c for c in base.rglob(name)
                    if c.is_file() and "site-packages" not in str(c)]
            cands.extend(hits[:3])
    for c in cands:
        if c.exists():
            return c
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=TOOL, description=__doc__.splitlines()[0])
    ap.add_argument("--human", action="store_true", help="echo human summary to stderr")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("extract", help="state file + pytest collection -> contracts")
    p.add_argument("--state", required=True, help="migration-state.json")
    p.add_argument("--root", help="target project root (default: cwd)")
    p.add_argument("--pytest-collect",
                   help="path to captured `pytest --collect-only -q` output "
                        "(default: run pytest ourselves)")
    p.add_argument("--mypy", help="optional JSON list of mypy rows "
                                  "(path/symbol/signature)")
    p.add_argument("--out", help="directory to write <unit>.contract.json files")
    p.add_argument("--allow-uncovered", action="store_true",
                   help="tolerate uncovered units (still reported)")
    p.set_defaults(func=cmd_extract)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(run_cli(main, tool=TOOL, version=VERSION))
