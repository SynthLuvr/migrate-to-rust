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
  headers    the original file's leading comment block (the license header)
             must appear in the port file, comment markers forgiven (# vs
             //), whitespace collapsed. An original without a header block
             trivially passes and is noted.
  strays     port-tree files claimed by no unit are listed explicitly
             (never silently skipped) — claim them via --allowlist with a
             reason (scaffold, vendored, generated-by-template...), because
             "independent rewrite" is exactly the masquerade this gate exists
             to catch.

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
    EXIT_ERROR, EXIT_FINDINGS, EXIT_OK, emit, iter_tree_files, read_json, sha256_file,
)

TOOL = "attribution.py"
VERSION = 1

NOTICE_NAMES = ("NOTICE", "NOTICE.md", "NOTICE.txt", "NOTICE.rst")
LICENSE_NAMES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "COPYING.txt",
                 "COPYRIGHT", "AUTHORS")
SOURCE_EXTENSIONS = {".rs", ".py"}
PORTED_FROM_RE = re.compile(r"^(?://|#)\s*Ported from\s+(.+?)\s*$", re.MULTILINE)
HEADER_REGION_LINES = 80


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


def cmd_check(args) -> int:
    original = Path(args.original).resolve()
    port = Path(args.port).resolve()
    for label, p in (("original", original), ("port", port)):
        if not p.is_dir():
            print(f"{label} tree {p} is not a directory", file=sys.stderr)
            return EXIT_ERROR
    state = read_json(args.state)
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
        # compare the PATH; the license parenthetical is advisory (headers
        # carry the binding form)
        declared = [re.sub(r"\s*\([^()]*\)\s*$", "", m).strip().strip('"\'')
                    for m in PORTED_FROM_RE.findall(text)]
        row = {"unit": unit["id"], "port_path": rel,
               "python_path": unit["python_path"], "declared": declared,
               "declared_ok": unit["python_path"] in declared}
        if not declared:
            failures.append({"check": "provenance", "unit": unit["id"], "path": rel,
                             "status": "missing-provenance",
                             "detail": "no '// Ported from <python_path>' comment — every "
                                       "generated file must carry its provenance"})
        elif not row["declared_ok"]:
            failures.append({"check": "provenance", "unit": unit["id"], "path": rel,
                             "status": "wrong-provenance",
                             "detail": f"declares {declared!r}, unit's python_path is "
                                       f"{unit['python_path']!r}"})
        orig_py = _find_original(unit["python_path"], orig_files, original)
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
    strays = []
    for rel, p in sorted(port_files.items()):
        if os.path.splitext(rel)[1].lower() not in SOURCE_EXTENSIONS:
            continue
        if rel in covered_port_files or rel in allowlist:
            continue
        strays.append({"path": rel, "reason": allowlist.get(rel, "claimed by no unit "
                                                                 "and not allowlisted")})
    for s in strays:
        failures.append({"check": "unattributed-file", **{k: s[k] for k in ("path",)},
                         "status": "unattributed", "detail": s["reason"]})

    ok = not failures
    code = EXIT_OK if ok else EXIT_FINDINGS
    counts = {"units_checked": len(units), "notices": len(notices), "licenses": len(licenses),
              "failures": len(failures), "warnings": len(warnings), "strays": len(strays)}
    human = ("attribution: PRESERVED" if ok else
             f"attribution: LOSS DETECTED ({len(failures)} failure(s))") + \
            (f", {len(warnings)} warning(s)" if warnings else "")
    emit({
        "tool": TOOL, "version": VERSION,
        "original": str(original), "port": str(port), "state": str(Path(args.state).resolve()),
        "notices": notices, "licenses": licenses,
        "provenance": provenance_rows,
        "unattributed_files": strays,
        "failures": failures, "warnings": warnings, "infos": infos,
        "counts": counts, "ok": ok,
        "summary": {"exit_code": code, "human": human},
    }, human_flag=args.human)
    return code


def _normalize_rel(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _find_original(python_path: str, orig_files: dict, original: Path) -> Path | None:
    rel = _normalize_rel(python_path)
    if rel in orig_files:
        return orig_files[rel]
    suffix_matches = [p for r, p in orig_files.items() if r.endswith("/" + rel) or r == rel]
    return suffix_matches[0] if suffix_matches else None


def _compare_bytes(rel: str, opath: Path, ppath: Path | None, port_root: Path) -> dict:
    if ppath is None:
        # a same-relative copy is required; fall back to same basename nowhere
        return {"path": rel, "status": "missing",
                "detail": "present in original tree, missing in port tree"}
    if sha256_file(opath) == sha256_file(ppath):
        return {"path": rel, "status": "identical", "sha256": sha256_file(opath)}
    return {"path": rel, "status": "changed",
            "detail": f"bytes differ (original {sha256_file(opath)[:12]}.. vs port "
                      f"{sha256_file(ppath)[:12]}..)",
            "original_sha256": sha256_file(opath), "port_sha256": sha256_file(ppath)}


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

    p = sub.add_parser("check", help="original tree + port tree + state -> report; fail on loss")
    p.add_argument("--original", required=True, help="original (Python) tree")
    p.add_argument("--port", required=True, help="port (Rust) tree")
    p.add_argument("--state", required=True,
                   help="migration-state.json; its units define the port scope")
    p.add_argument("--allowlist",
                   help='JSON: {"path": "reason"} for scaffold/template files outside unit scope')
    p.set_defaults(func=cmd_check)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
