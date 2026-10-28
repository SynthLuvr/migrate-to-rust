#!/usr/bin/env python3
"""attribution.py — attribution checker (framework/tools/README.md #7).

Stages 4, 5. Attribution is mechanical, not aspirational (hard rule 9): the
ScanCode port stripped license headers and NOTICE files and became "a
plagiarism pipeline with no attribution layer". This tool makes loss a hard,
deterministic failure. No LLM anywhere (anti-requirement) — byte hashes and
string containment only.

What it checks (original Python tree vs. port tree, scope = the units in the
migration state file):
  notices    every NOTICE* file in the original tree must exist in the port
             tree with IDENTICAL bytes (byte-exact is the only honest check);
             missing or changed -> failure. LICENSE*/COPYING* drift is
             reported as a warning (a port may legitimately re-license) but
             a missing one is a failure.
  provenance every port file of a unit with a rust_path must carry a
             provenance comment — ``// Ported from <python_path>`` — naming
             EXACTLY the unit's python_path (wrong provenance fails too).
             The path is the first whitespace-delimited token (or a quoted
             string): a ``@ <sha>`` pin after it and a trailing
             parenthetical are provenance detail, not a spelling error
             (issue #54).
  headers    the original file's leading comment block (the license header)
             must appear in the port file, comment markers forgiven (# vs
             //), whitespace collapsed. An original without a header block
             trivially passes and is noted.
  strays     port-tree files claimed by no unit are listed explicitly
             (never silently skipped) — claim them via --allowlist with a
             reason (scaffold, vendored, generated-by-template...), or give
             them a ``// Scaffold provenance: <what it came from>`` header
             (scaffold-owned files not derived from a Python unit:
             rust-template's lib.rs, main.rs, build.rs, ...; reported in
             ``scaffold_files``, never silently skipped), because
             "independent rewrite" is exactly the masquerade this gate
             exists to catch.

The original tree: pass ``--original`` the target REPO root — python_path
values are target-repo-relative, so the package dir (e.g. ``<repo>/src``)
is the wrong root. ``--original`` may be omitted when the state file
records the root a previous ``pipeline.py preflight`` captured
(``preflight.target.root``); a deeper ``--original`` still resolves units
via suffix and prefix-stripped matches (issue #54).

Exit codes: 0 attribution preserved · 2 any loss · 1 usage/IO.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    EXIT_FINDINGS, EXIT_OK, ToolError, emit, iter_tree_files,
    read_json, run_cli, sha256_file,
)

TOOL = "attribution.py"
VERSION = 2

NOTICE_NAMES = ("NOTICE", "NOTICE.md", "NOTICE.txt", "NOTICE.rst")
LICENSE_NAMES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "COPYING.txt",
                 "COPYRIGHT", "AUTHORS")
SOURCE_EXTENSIONS = {".rs", ".py"}
PORTED_FROM_RE = re.compile(r"^(?://|#)[ \t]*Ported from[ \t]+(.+?)[ \t]*$", re.MULTILINE)
# scaffold-owned files (issue #54): not derived from a Python unit, but not
# unattributed either — the header must cite something (non-empty). Both
# patterns are line-bounded ([ \t], not \s — \s would cross the newline)
SCAFFOLD_PROVENANCE_RE = re.compile(
    r"^(?://|#)[ \t]*Scaffold provenance:[ \t]*(\S.*?)[ \t]*$", re.MULTILINE)
HEADER_REGION_LINES = 80

CHECK_HELP = """\
original tree + port tree + state -> report; fail on loss

provenance grammar (two accepted header forms, issue #54):
  // Ported from <python_path> [@ <sha>] [(<advisory notes>)]
      unit-owned files: the PATH is the first whitespace-delimited token
      (or a quoted string); everything after it — an `@ <git sha>` pin, a
      parenthetical license/note — is provenance detail, not a path error
  // Scaffold provenance: <what the file came from>    (must be non-empty)
      scaffold-owned files not derived from a Python unit (rust-template's
      lib.rs, main.rs, build.rs, tests/...): reported under scaffold_files,
      never silently skipped
  non-source scaffold files (Cargo.toml, justfile, deny.toml, ...) are
  outside the stray check (source extensions only: .rs, .py)

--original is the target REPO root (python_path values are repo-relative)
and may be omitted when the state file records preflight.target.root; a
deeper --original (e.g. <repo>/src) still resolves units via suffix and
prefix-stripped (unique) matches.
"""


def _header_block(lines: list[str]) -> list[str]:
    """Leading '#'-comment block of a Python file (shebang/coding skipped).
    Returns the comment contents, stripped of the marker."""
    content: list[str] = []
    started = False
    for line in lines:
        stripped = line.strip()
        if not started and (stripped.startswith("#!") or stripped.startswith("# -*-")):
            continue
        if stripped.startswith("#"):
            started = True
            text = stripped.lstrip("#").strip()
            if text:
                content.append(text)
            continue
        if started and not stripped:
            continue  # blank line inside the block; keep scanning
        break
    return content


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _port_region_normalized(path: Path) -> list[str]:
    """First HEADER_REGION_LINES lines of the port file, comment markers
    stripped and whitespace collapsed — the region a preserved header must
    live in (headers belong at the top, not buried)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    region = []
    for line in text.splitlines()[:HEADER_REGION_LINES]:
        s = line.strip()
        s = re.sub(r"^(//+|#|/\*+|\*+|\*/)\s?", "", s).strip()
        region.append(_collapse(s))
    return region


def header_preserved(original: Path, port_file: Path) -> tuple[bool, list[str]]:
    header = _header_block(original.read_text(encoding="utf-8", errors="replace").splitlines())
    if not header:
        return True, []  # nothing to preserve
    region = _port_region_normalized(port_file)
    region_blob = "\n".join(region)
    missing = [h for h in header if _collapse(h) not in region_blob]
    return (not missing), missing


def _declared_path(declared: str) -> str:
    """Path token of one declared ``Ported from`` string (issue #54): drop
    the advisory trailing parenthetical, then take a quoted string verbatim
    (paths may contain spaces) or the first whitespace-delimited token —
    what follows the path (``@ <sha>``, free-form notes) is provenance
    detail, not a path misspelling."""
    d = re.sub(r"\s*\([^()]*\)\s*$", "", declared).strip()
    if d[:1] in ('"', "'"):
        end = d.find(d[0], 1)
        return d[1:end] if end > 0 else d.strip(d[0])
    parts = d.split()
    return parts[0] if parts else ""


def cmd_check(args) -> int:
    original = Path(args.original).expanduser().resolve() if args.original else None
    if original is not None and not original.is_dir():
        raise ToolError(f"original tree {original} is not a directory")
    port = Path(args.port).resolve()
    if not port.is_dir():
        raise ToolError(f"port tree {port} is not a directory")
    state = read_json(args.state)
    if original is None:  # issue #54: repo root recorded by preflight
        original = _resolve_original(None, state)
        if not original.is_dir():
            raise ToolError(f"original tree {original} (preflight.target.root) "
                            "is not a directory — re-run pipeline.py preflight "
                            "or pass --original", kind="io")
    units = [u for u in state.get("units", []) if u.get("rust_path")]
    allowlist = _load_allowlist(args.allowlist)

    failures: list[dict] = []
    warnings: list[dict] = []
    infos: list[dict] = []

    # ---- notices / licenses: byte-exact ------------------------------------
    orig_files = {str(p.relative_to(original)): p for p in iter_tree_files(original)}
    port_files = {str(p.relative_to(port)): p for p in iter_tree_files(port)}

    notices, licenses = [], []
    for rel, opath in sorted(orig_files.items()):
        base = os.path.basename(rel)
        if base in NOTICE_NAMES:
            entry = _compare_bytes(rel, opath, port_files.get(rel), port)
            notices.append(entry)
            if entry["status"] != "identical":
                failures.append({"check": "notice", **{k: entry[k] for k in
                                                       ("path", "status", "detail")}})
        elif base in LICENSE_NAMES:
            entry = _compare_bytes(rel, opath, port_files.get(rel), port)
            licenses.append(entry)
            if entry["status"] == "missing":
                failures.append({"check": "license-file", **{k: entry[k] for k in
                                                             ("path", "status", "detail")}})
            elif entry["status"] == "changed":
                warnings.append({"check": "license-file", **{k: entry[k] for k in
                                                             ("path", "status", "detail")}})
    extra = sorted(set(port_files) - set(orig_files))
    extra_notices = [e for e in extra if os.path.basename(e) in NOTICE_NAMES + LICENSE_NAMES]
    if extra_notices:
        infos.append({"check": "extra-license-files", "paths": extra_notices})

    # ---- per-unit provenance + headers -------------------------------------
    covered_port_files: set[str] = set()
    provenance_rows = []
    for unit in units:
        rel_rust = _normalize_rel(unit["rust_path"])
        candidates = [r for r in port_files if r == rel_rust or r.endswith("/" + rel_rust)
                      or r.endswith(rel_rust)]
        if not candidates:
            failures.append({"check": "port-file", "unit": unit["id"], "path": rel_rust,
                             "status": "missing",
                             "detail": f"unit {unit['id']} declares rust_path not found in port tree"})
            continue
        rel = candidates[0]
        covered_port_files.add(rel)
        port_file = port_files[rel]
        text = port_file.read_text(encoding="utf-8", errors="replace")
        # translator.md rule 5: `// Ported from <python_path> (<license>)` —
        # compare the PATH token; the license parenthetical is advisory
        # (headers carry the binding form) and an `@ <sha>` pin after the
        # path is provenance, not an error (issue #54)
        declared = PORTED_FROM_RE.findall(text)
        declared_paths = [_declared_path(m) for m in declared]
        row = {"unit": unit["id"], "port_path": rel,
               "python_path": unit["python_path"], "declared": declared,
               "declared_paths": declared_paths,
               "declared_ok": unit["python_path"] in declared_paths}
        if not declared:
            failures.append({"check": "provenance", "unit": unit["id"], "path": rel,
                             "status": "missing-provenance",
                             "detail": "no '// Ported from <python_path>' comment — every "
                                       "generated file must carry its provenance"})
        elif not row["declared_ok"]:
            failures.append({"check": "provenance", "unit": unit["id"], "path": rel,
                             "status": "wrong-provenance",
                             "detail": f"declares {declared!r} (paths {declared_paths!r}), "
                                       f"unit's python_path is {unit['python_path']!r}"})
        orig_py = _find_original(unit["python_path"], orig_files)
        if orig_py is None:
            warnings.append({"check": "original-missing", "unit": unit["id"],
                             "path": unit["python_path"],
                             "detail": "original python file not found under the original tree "
                                       "(header check skipped)"})
            row["header_preserved"] = None
        else:
            ok, missing_lines = header_preserved(orig_py, port_file)
            row["header_preserved"] = ok
            if not ok:
                failures.append({"check": "header", "unit": unit["id"], "path": rel,
                                 "status": "header-lost",
                                 "detail": f"original header line(s) not found in the first "
                                           f"{HEADER_REGION_LINES} lines of the port file: "
                                           f"{missing_lines[:3]}"})
        provenance_rows.append(row)

    # ---- strays: port files claimed by nobody ------------------------------
    # A stray is excused only by --allowlist (with a reason) or a
    # `// Scaffold provenance: <detail>` header — the scaffold files stay
    # visible in scaffold_files either way (issue #54: never silently skip)
    strays = []
    scaffold_files = []
    for rel in sorted(port_files):
        if os.path.splitext(rel)[1].lower() not in SOURCE_EXTENSIONS:
            continue
        if rel in covered_port_files or rel in allowlist:
            continue
        text = port_files[rel].read_text(encoding="utf-8", errors="replace")
        scaffold_declared = SCAFFOLD_PROVENANCE_RE.findall(text)
        if scaffold_declared:
            scaffold_files.append({"path": rel, "provenance": scaffold_declared})
            continue
        strays.append({"path": rel, "reason": allowlist.get(rel, "claimed by no unit "
                                                                 "and not allowlisted")})
    for s in strays:
        failures.append({"check": "unattributed-file", "path": s["path"],
                         "status": "unattributed", "detail": s["reason"]})

    ok = not failures
    code = EXIT_OK if ok else EXIT_FINDINGS
    counts = {"units_checked": len(units), "notices": len(notices), "licenses": len(licenses),
              "failures": len(failures), "warnings": len(warnings), "strays": len(strays),
              "scaffold_files": len(scaffold_files)}
    human = ("attribution: PRESERVED" if ok else
             f"attribution: LOSS DETECTED ({len(failures)} failure(s))")
    if scaffold_files:
        human += f", {len(scaffold_files)} scaffold file(s)"
    if warnings:
        human += f", {len(warnings)} warning(s)"
    emit({
        "tool": TOOL, "version": VERSION,
        "original": str(original), "port": str(port), "state": str(Path(args.state).resolve()),
        "notices": notices, "licenses": licenses,
        "provenance": provenance_rows,
        "scaffold_files": scaffold_files,
        "unattributed_files": strays,
        "failures": failures, "warnings": warnings, "infos": infos,
        "counts": counts, "ok": ok,
        "summary": {"exit_code": code, "human": human},
    }, human_flag=args.human)
    return code


def _normalize_rel(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _resolve_original(flag: str | None, state: dict) -> Path:
    """The original (Python) tree when ``--original`` is absent: the target
    repo root a previous preflight recorded (``preflight.target.root``) —
    the same resolution pipeline.py/delegate.py use. python_path values are
    target-repo-relative, so the repo root (not the package dir) is the
    tree they resolve against (issue #54)."""
    raw = ((state.get("preflight") or {}).get("target") or {}).get("root")
    if not raw:
        raise ToolError(
            "no original tree: pass --original (the target REPO root — python_path "
            "values are repo-relative) or run pipeline.py preflight first so "
            "preflight.target.root is recorded in the state file", kind="usage")
    return Path(raw).expanduser().resolve()


def _find_original(python_path: str, orig_files: dict[str, Path]) -> Path | None:
    """Locate the unit's original Python file under the original tree.
    python_path values are target-repo-relative, so try in order: the exact
    spelling (original tree rooted at the repo root), an anchored suffix
    match (original tree rooted deeper), then the same two with leading
    components dropped — the ``--original <repo>/src`` case — accepted only
    on a unique hit so a dangling python_path can't false-positive onto an
    unrelated same-named file (issue #54: header checks must run, not
    silently skip)."""
    rel = _normalize_rel(python_path)
    if rel in orig_files:
        return orig_files[rel]
    hits = [p for r, p in orig_files.items() if r.endswith("/" + rel)]
    if hits:
        return hits[0]
    parts = rel.split("/")
    for i in range(1, len(parts)):
        cand = "/".join(parts[i:])
        if cand in orig_files:
            return orig_files[cand]
        hits = [p for r, p in orig_files.items() if r.endswith("/" + cand)]
        if len(hits) == 1:
            return hits[0]
    return None


def _compare_bytes(rel: str, opath: Path, ppath: Path | None, port_root: Path) -> dict:
    if ppath is None:
        # a same-relative copy is required; fall back to same basename nowhere
        return {"path": rel, "status": "missing",
                "detail": "present in original tree, missing in port tree"}
    ohash, phash = sha256_file(opath), sha256_file(ppath)
    if ohash == phash:
        return {"path": rel, "status": "identical", "sha256": ohash}
    return {"path": rel, "status": "changed",
            "detail": f"bytes differ (original {ohash[:12]}.. vs port {phash[:12]}..)",
            "original_sha256": ohash, "port_sha256": phash}


def _load_allowlist(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    data = read_json(path)
    if isinstance(data, dict):
        return {k: v if isinstance(v, str) else json.dumps(v) for k, v in data.items()}
    out = {}
    for item in data:
        if isinstance(item, str):
            out[item] = "allowlisted (no reason given)"
        else:
            out[item["path"]] = item.get("reason", "allowlisted (no reason given)")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=TOOL, description=__doc__.splitlines()[0])
    ap.add_argument("--human", action="store_true", help="echo human summary to stderr")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", help="original tree + port tree + state -> report; fail on loss",
                       description=CHECK_HELP,
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--original",
                   help="original (Python) tree — the target REPO root; defaults to the root "
                        "recorded by pipeline.py preflight (preflight.target.root)")
    p.add_argument("--port", required=True, help="port (Rust) tree")
    p.add_argument("--state", required=True,
                   help="migration-state.json; its units define the port scope")
    p.add_argument("--allowlist",
                   help='JSON: {"path": "reason"} for scaffold/template files outside unit scope')
    p.set_defaults(func=cmd_check)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(run_cli(main, tool=TOOL, version=VERSION))
