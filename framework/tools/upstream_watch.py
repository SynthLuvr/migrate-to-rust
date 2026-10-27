#!/usr/bin/env python3
"""upstream_watch.py — maintenance watcher (framework/tools/README.md #8).

Stage 7. The tsrs/Vinext pattern, mechanized: diff the Python upstream, map
changed files to the state file's units, and emit a parity re-run plan plus
a PR proposal. NO SILENT SKIPS: every changed file appears in the report —
mapped, unmapped (with reason), or out-of-scope-by-path — never dropped.

Re-run plan semantics: a changed unit's parity must be re-run, and so must
every unit that (transitively) DEPENDS on it — dependents inherit upstream
semantics changes. Units with escalated/divergent_explained status are
flagged for the Stage 7 human decision (port-forward, re-diverge, or
descope) instead of being scheduled blindly.

Usage:
  upstream_watch.py watch --state FILE --repo DIR [--base REF] [--head REF]
      base defaults to HEAD~1, head to the working tree (uncommitted review)
  upstream_watch.py watch --state FILE --repo DIR --base v1.2 --head v1.3

Exit codes: 0 plan produced, nothing escalated · 2 affected units include
escalated/divergent_explained (human decision required) · 1 usage/IO.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    EXIT_ERROR, EXIT_FINDINGS, EXIT_OK, emit, read_json, run_child,
)

TOOL = "upstream_watch.py"
VERSION = 1
OUT_OF_SCOPE_HINTS = ("tests/", "test_", "docs/", "doc/", ".github/", "examples/",
                      "benchmarks/", "setup.py", "setup.cfg", "pyproject.toml",
                      "tox.ini", "conftest.py", "README", "CHANGELOG", "LICENSE",
                      "NOTICE")


def _git(repo: Path, *args: str) -> list[str]:
    code, out, err = run_child(["git", *args], cwd=str(repo), timeout=60.0)
    if code != 0:
        print(f"git {' '.join(args)} failed (exit {code}): {err.strip()[:200]}",
              file=sys.stderr)
        raise SystemExit(EXIT_ERROR)
    return [l for l in out.splitlines() if l.strip()]


def _norm(path: str) -> str:
    return path.strip().lstrip("./").replace("\\", "/")


def map_file_to_units(changed: str, units: list[dict]) -> list[str]:
    """Exact python_path match, or a file inside a package unit's directory."""
    hits = []
    c = _norm(changed)
    for u in units:
        p = _norm(u.get("python_path", ""))
        if not p:
            continue
        if c == p:
            hits.append(u["id"])
        elif p.endswith("__init__.py") and c.startswith(p.rsplit("/", 1)[0] + "/"):
            hits.append(u["id"])  # child file of a package unit
    return sorted(set(hits))


def dependents_of(unit_id: str, units: list[dict]) -> set[str]:
    """Transitive dependents (units whose deps chain includes unit_id)."""
    rev: dict[str, set[str]] = {}
    for u in units:
        for d in u.get("deps", []):
            rev.setdefault(d, set()).add(u["id"])
    seen, frontier = set(), list(rev.get(unit_id, []))
    while frontier:
        cur = frontier.pop()
        if cur in seen:
            continue
        seen.add(cur)
        frontier.extend(rev.get(cur, []))
    return seen


def cmd_watch(args) -> int:
    state = read_json(args.state)
    units = state.get("units", [])
    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        print(f"{repo} is not a git repository", file=sys.stderr)
        return EXIT_ERROR
    base = args.base or "HEAD~1"
    head = args.head  # None -> working tree
    if head:
        changed = _git(repo, "diff", "--name-only", base, head)
    else:
        changed = _git(repo, "diff", "--name-only", base)

    by_id = {u["id"]: u for u in units}
    mapped: dict[str, list[str]] = {}     # changed file -> unit ids
    unmapped: list[dict] = []             # changed file -> reason (never silent)
    for f in changed:
        norm = _norm(f)
        hits = map_file_to_units(norm, units)
        if hits:
            mapped[norm] = hits
        else:
            reason = ("out of scope by path" if norm.startswith(OUT_OF_SCOPE_HINTS)
                      else "no unit maps to this path")
            unmapped.append({"file": norm, "reason": reason})

    # affected = changed units + transitive dependents
    changed_units: set[str] = set()
    for hits in mapped.values():
        changed_units.update(hits)
    affected: dict[str, str] = {}
    for uid in sorted(changed_units):
        affected[uid] = "upstream-changed"
    for uid in sorted(changed_units):
        for dep in sorted(dependents_of(uid, units)):
            if dep not in affected:
                affected[dep] = f"depends-on {uid}"

    needing_human = []
    plan = []
    for uid, why in affected.items():
        u = by_id.get(uid, {})
        corpus = (u.get("contract") or {}).get("golden_corpus")
        entry = {
            "unit": uid, "why": why, "status": u.get("status", "?"),
            "parity_rerun": ("python framework/tools/oracle.py judge "
                             f"--manifest manifests/{uid}.json"
                             if corpus else
                             f"no golden_corpus recorded — characterization tests "
                             f"needed before re-judging {uid}"),
        }
        if u.get("status") in ("escalated", "divergent_explained"):
            entry["human_decision"] = ("Stage 7: port-forward, re-diverge, or descope "
                                       "(upstream change landed in an "
                                       f"{u['status']} unit)")
            needing_human.append(uid)
        plan.append(entry)

    proposal = _pr_proposal(base, head, changed, mapped, unmapped, plan, needing_human)
    if args.write:
        Path(args.write).write_text(proposal, encoding="utf-8")

    code = EXIT_FINDINGS if needing_human else EXIT_OK
    n = len(affected)
    human = (f"upstream watch: {len(changed)} changed file(s) -> {len(changed_units)} "
             f"changed unit(s), {n} unit(s) in re-run plan, {len(unmapped)} unmapped "
             f"(listed, not skipped)"
             + (f"; {len(needing_human)} unit(s) need a HUMAN decision"
                if needing_human else ""))
    emit({
        "tool": TOOL, "version": VERSION,
        "repo": str(repo), "base": base, "head": head or "(working tree)",
        "changed_files": [_norm(f) for f in changed],
        "mapping": mapped,
        "unmapped": unmapped,
        "affected_units": plan,
        "needing_human": needing_human,
        "pr_proposal": proposal,
        "pr_proposal_written_to": args.write,
        "summary": {"exit_code": code, "human": human},
    }, human_flag=args.human)
    return code


def _pr_proposal(base, head, changed, mapped, unmapped, plan, needing_human) -> str:
    lines = [
        "# Upstream parity PR proposal",
        "",
        f"- Upstream range: `{base}..{head or 'working tree'}` ({len(changed)} file(s) changed)",
        f"- Units affected: {len(plan)} · needing human decision: {len(needing_human)}",
        "",
        "## Changed files → units",
        "",
    ]
    if mapped:
        for f in sorted(mapped):
            lines.append(f"- `{f}` → {', '.join(mapped[f])}")
    else:
        lines.append("- (none)")
    lines += ["", "## Unmapped files (explicit — never silently skipped)", ""]
    if unmapped:
        for u in unmapped:
            lines.append(f"- `{u['file']}` — {u['reason']}")
    else:
        lines.append("- (none)")
    lines += ["", "## Parity re-run plan", ""]
    for p in plan:
        lines.append(f"- **{p['unit']}** ({p['status']}, {p['why']}): `{p['parity_rerun']}`")
        if "human_decision" in p:
            lines.append(f"  - :warning: {p['human_decision']}")
    lines += [
        "",
        "## Rollout",
        "",
        "1. Re-run the parity plan above (oracle manifests under `manifests/`).",
        "2. Reviewer pass (roles/reviewer.md) on any unit whose parity changed.",
        "3. `pyo3_review.py` on the port diff; `attribution.py check` if headers moved.",
        "4. Human sign-off for units flagged above before merge.",
        "",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=TOOL, description=__doc__.splitlines()[0])
    ap.add_argument("--human", action="store_true", help="echo human summary to stderr")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("watch", help="upstream diff -> changed units -> re-run plan -> PR proposal")
    p.add_argument("--state", required=True, help="migration-state.json")
    p.add_argument("--repo", required=True, help="the upstream Python repo")
    p.add_argument("--base", help="base ref (default HEAD~1)")
    p.add_argument("--head", help="head ref (default: working tree)")
    p.add_argument("--write", help="also write the PR proposal markdown to this path")
    p.set_defaults(func=cmd_watch)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
