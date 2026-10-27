#!/usr/bin/env python3
"""port_plan.py — profiling → plan generator (framework/tools/README.md #2).

Stages 0, 1. Turns measured hotspots + static analysis into a ranked,
dependency-ordered, portability-scored unit backlog — the input the Stage 1
checklist demands: nothing enters the plan without profiler evidence (hard
rule 1: profile before porting; I/O-bound code is a NO-GO signal).

Input format (canonical "hotspots JSON" — see from-cprofile for generating
it from a cProfile dump):

    [{"path": "src/pkg/parse.py", "self_cpu_s": 12.4, "io_wait_s": 3.1,
      "alloc_mb": 210, "calls": 54000}, ...]

Static analysis uses the stdlib ``ast`` module (deterministic, no
tree-sitter dependency). Unit granularity = module; a module with multiple
independently testable top-level classes can be split later by hand.

Scoring (deterministic, documented):
  io_share      = io_wait_s / (io_wait_s + self_cpu_s + epsilon)
  io_bound      = io_share >= 0.5  -> flagged NO-GO, ranked LAST regardless
                  of score (porting I/O-bound code buys nothing — Stage 0)
  portability   = 100 - 10*len(risk_flags) (floor 0)
  priority      = self_cpu_s * (1 - io_share) * portability / 100

Dependency order is leaf-first over the internal import graph; cycles are
DETECTED and reported (those units get order=null and a `cycle` risk flag —
never silently merged or dropped). Optional mypy JSON attaches type-quality
risk flags.

Output: plan JSON whose ``units`` conform to migration-state.schema.json's
unit shape (feed to state.py via --field units=[...]).

Exit codes: 0 plan produced (warnings possible) · 2 cycles found or no
usable hotspots · 1 usage/IO.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    EXIT_FINDINGS, EXIT_OK, emit, emit_error, module_name_of,
    read_json, run_cli,
)

TOOL = "port_plan.py"
VERSION = 1

# deterministic, documented risk-flag vocabulary (Stage 0 step 6 /
# Stage 1 step 5: reflection, dynamic dispatch, C extensions, task queues)
RISK_PATTERNS = {
    "reflection": {r"\beval\s*\(", r"\bexec\s*\(", r"\bgetattr\s*\(",
                   r"\bsetattr\s*\(", r"\b__getattr__\b", r"\bglobals\s*\(\s*\)"},
    "dynamic-dispatch": {r"\b__class__\s*=", r"\btype\s*\(\s*\w+\s*\)\s*=",
                         r"\bmonkeypatch", r"\b__init_subclass__\b"},
    "c-extension": set(),  # filled from C_EXT_MODULES below at import time
    "task-queue": {r"\bcelery\b", r"\bdramatiq\b", r"\brq\b", r"\bhuey\b",
                   r"\bkombu\b"},
    "threads": {r"\bthreading\b", r"\bconcurrent\.futures\b", r"\basyncio\b"},
    "introspection": {r"\binspect\.", r"\b__dict__\b", r"\bvars\s*\("},
}
C_EXT_MODULES = {
    "numpy", "lxml", "psycopg2", "cryptography", "PIL", "pillow", "regex",
    "ujson", "orjson", "msgpack", "pyyaml", "yaml", "markupsafe", "bcrypt",
    "gevent", "zmq", "czmq",
}
RISK_PATTERNS["c-extension"] = {rf"\b{re.escape(m)}\b" for m in sorted(C_EXT_MODULES)}


def unit_id_of(python_path: str, taken: set[str]) -> str:
    base = "u-" + re.sub(r"[^0-9A-Za-z]+", "-", module_name_of(python_path)).strip("-").lower()
    uid, n = base, 2
    while uid in taken:
        uid = f"{base}-{n}"
        n += 1
    taken.add(uid)
    return uid


# ------------------------------------------------------------ AST scan -----

def scan_module(path: Path) -> dict:
    """Deterministic single-pass scan: imports (dep edges), risk flags,
    def/class counts, docstring invariants count."""
    out = {"imports": set(), "risk_flags": [], "functions": 0, "classes": 0}
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        out["risk_flags"].append("parse-error")
        out["parse_error"] = str(e)
        # fall back to regex scan of the raw text for risk patterns
        for flag, pats in RISK_PATTERNS.items():
            if any(re.search(p, source) for p in pats):
                out["risk_flags"].append(flag)
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out["imports"].add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                out["imports"].add(node.module)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out["functions"] += 1
        elif isinstance(node, ast.ClassDef):
            out["classes"] += 1
    for flag, pats in RISK_PATTERNS.items():
        if any(re.search(p, source) for p in pats):
            out["risk_flags"].append(flag)
    return out


def internal_dep_modules(imports: set[str], scoped_modules: dict[str, str]) -> set[str]:
    """Map imported module names to scoped module files (exact match, or the
    longest scoped module the import extends)."""
    deps = set()
    for imp in imports:
        if imp in scoped_modules:
            deps.add(scoped_modules[imp])
            continue
        for mod, path in scoped_modules.items():
            if imp.startswith(mod + "."):
                deps.add(path)
    return deps


# --------------------------------------------------------------- ranking ---

def rank(hotspots: list[dict]) -> dict[str, dict]:
    ranked = {}
    for h in hotspots:
        cpu = float(h.get("self_cpu_s", 0.0) or 0.0)
        io = float(h.get("io_wait_s", 0.0) or 0.0)
        total = cpu + io
        io_share = (io / total) if total > 0 else 0.0
        ranked[h["path"]] = {
            "self_cpu_s": cpu, "io_wait_s": io, "io_share": round(io_share, 4),
            "io_bound": io_share >= 0.5,
            "alloc_mb": float(h.get("alloc_mb", 0.0) or 0.0),
            "calls": int(h.get("calls", 0) or 0),
        }
    return ranked


def topo_order(modules: list[str], deps_of: dict[str, set[str]]) -> tuple[dict[str, int], list[str]]:
    """Leaf-first topological order. Returns (module -> order, cycles).
    Modules the algorithm cannot order (cycle members and everything that
    depends on them) are left out of `order` and flagged upstream."""
    graph = {m: set(d for d in deps_of.get(m, set()) if d in modules) for m in modules}
    order: dict[str, int] = {}
    cycles: list[str] = []

    # DFS pass purely to report cycles readably
    state = {}  # 1 = on the DFS stack, 2 = done
    stack: list[str] = []

    def visit(m: str) -> None:
        state[m] = 1
        stack.append(m)
        for dep in sorted(graph[m]):
            if state.get(dep) == 1:
                cycles.append(" -> ".join(stack[stack.index(dep):] + [dep]))
            elif dep not in state:
                visit(dep)
        stack.pop()
        state[m] = 2

    for m in sorted(graph):
        if m not in state:
            visit(m)

    # Kahn's algorithm over the whole graph (stable, deterministic);
    # whatever it cannot order is stuck in or behind a cycle
    remaining = {m: set(graph[m]) for m in graph}
    order_idx = 0
    while remaining:
        ready = sorted(m for m, deps in remaining.items() if not deps)
        if not ready:
            cycles.append(" -> ".join(sorted(remaining)))
            break
        for m in ready:
            order_idx += 1
            order[m] = order_idx
            del remaining[m]
        for m, deps in remaining.items():
            remaining[m] = deps - set(order)
    return order, sorted(set(cycles))


# ------------------------------------------------------------ commands -----

def cmd_from_cprofile(args) -> int:
    """Convert a cProfile dump (python -m cProfile -o out.prof ...) into the
    canonical hotspots JSON: per-file cumulative self-time."""
    import pstats
    stats = pstats.Stats(args.profile)
    per_file: dict[str, dict] = {}
    for (filename, _lineno, _name), (cc, nc, tt, ct, _callers) in stats.stats.items():
        if not filename or filename.startswith("<"):
            continue
        entry = per_file.setdefault(filename, {"path": filename, "self_cpu_s": 0.0,
                                               "calls": 0})
        entry["self_cpu_s"] += tt
        entry["calls"] += nc if nc else cc
    hotspots = sorted(per_file.values(), key=lambda h: -h["self_cpu_s"])
    emit({"tool": TOOL, "version": VERSION, "profile": str(Path(args.profile).resolve()),
          "hotspots": [{"path": h["path"], "self_cpu_s": round(h["self_cpu_s"], 6),
                        "calls": h["calls"]} for h in hotspots],
          "summary": {"exit_code": EXIT_OK, "human":
                      f"cProfile dump -> {len(hotspots)} file hotspots "
                      f"(top: {hotspots[0]['path'] if hotspots else 'n/a'})"}},
         human_flag=args.human)
    return EXIT_OK


def cmd_plan(args) -> int:
    hotspots = read_json(args.hotspots)
    if not isinstance(hotspots, list) or not hotspots:
        return emit_error(
            "hotspots JSON must be a non-empty list of per-module measurements",
            kind="usage", tool=TOOL, version=VERSION, human_flag=args.human)
    project_root = Path(args.project_root or ".").resolve()

    # resolve each hotspot path against the project root; keep the original
    # hotspot dict around for metrics lookup
    resolved: dict[str, Path] = {}
    hotspot_of: dict[str, dict] = {}
    for h in hotspots:
        p = Path(h["path"])
        if not p.is_absolute():
            p = project_root / p
        if not p.exists():
            p = _find_by_suffix(h["path"], project_root)
        if p is None or not p.exists():
            return emit_error(f"hotspot path not found: {h['path']}",
                              tool=TOOL, version=VERSION, human_flag=args.human)
        resolved[str(p)] = p
        hotspot_of[str(p)] = h

    measured = rank(hotspots)
    scans = {path_str: scan_module(p) for path_str, p in resolved.items()}

    # module names derive from the path RELATIVE to the project root, so
    # "from pkg.leaf import x" maps to src/pkg/leaf.py regardless of where
    # the project is checked out
    scoped_modules = {module_name_of(_rel(ps, project_root)): ps for ps in resolved}
    deps_of = {}
    for ps, scan in scans.items():
        deps_of[ps] = {d for d in internal_dep_modules(scan["imports"], scoped_modules) if d != ps}

    order, cycles = topo_order(list(resolved), deps_of)

    # mypy type-quality flags (optional)
    mypy_flags: dict[str, str] = {}
    if args.mypy:
        mypy = read_json(args.mypy)
        for row in mypy if isinstance(mypy, list) else []:
            path = row.get("path", "")
            for ps in resolved:
                if ps.endswith(path) or path.endswith(ps):
                    if row.get("error_code") == "arg-type" or "Any" in str(row.get("message", "")):
                        mypy_flags.setdefault(ps, "any-heavy")

    units = []
    taken: set[str] = set()
    for ps in sorted(resolved):
        h = measured[hotspot_of[ps]["path"]]
        scan = scans[ps]
        flags = set(scan["risk_flags"])
        if ps in mypy_flags:
            flags.add(mypy_flags[ps])
        if ps not in order:  # cycle member: flagged, never silently dropped
            flags.add("cycle")
        if h["io_bound"]:
            flags.add("io-bound")
        portability = max(0, 100 - 10 * len(flags))
        priority = h["self_cpu_s"] * (1 - h["io_share"]) * portability / 100
        unit = {
            "id": unit_id_of(_rel(ps, project_root), taken),
            "python_path": _rel(ps, project_root),
            "rust_path": None,
            "deps": [],           # filled with unit ids below
            "order": order.get(ps),
            "risk_flags": sorted(set(flags)),
            "status": "pending",
            "metrics": {
                "self_cpu_s": h["self_cpu_s"], "io_wait_s": h["io_wait_s"],
                "io_share": h["io_share"], "io_bound": h["io_bound"],
                "alloc_mb": h["alloc_mb"], "calls": h["calls"],
                "functions": scan["functions"], "classes": scan["classes"],
            },
            "portability_score": portability,
            "priority": round(priority, 6),
            "no_go": h["io_bound"],
        }
        units.append(unit)

    # convert dep paths to unit ids
    id_by_path = {u["python_path"]: u["id"] for u in units}
    for u in units:
        ps = str(project_root / u["python_path"]) if not Path(u["python_path"]).is_absolute() \
            else u["python_path"]
        deps = deps_of.get(ps, set())
        u["deps"] = sorted(id_by_path[_rel(d, project_root)]
                           for d in deps if _rel(d, project_root) in id_by_path)

    # rank: priority descending; io_bound modules always LAST (by name for
    # determinism), per acceptance "I/O-bound units ranked last"
    portable = [u for u in units if not u["no_go"]]
    io_bound = [u for u in units if u["no_go"]]
    portable.sort(key=lambda u: (-u["priority"], u["id"]))
    io_bound.sort(key=lambda u: u["id"])
    ranked_units = portable + io_bound

    errors = []
    if cycles:
        for c in cycles:
            errors.append(f"dependency cycle: {c}")
    if all(u["no_go"] for u in units):
        errors.append("every scoped hotspot is I/O-bound — nothing worth porting "
                      "(Stage 0: recommend NO-GO)")

    code = EXIT_FINDINGS if errors else EXIT_OK
    n_io = len(io_bound)
    human = (f"plan: {len(units)} unit(s), priority-ranked; {len(portable)} portable, "
             f"{n_io} I/O-bound (NO-GO, ranked last)"
             + (f"; {len(errors)} ERROR(S)" if errors else ""))
    emit({
        "tool": TOOL, "version": VERSION,
        "project_root": str(project_root),
        "units": ranked_units,
        "cycles": cycles,
        "errors": errors,
        "warnings": [f"{u['id']}: io-bound (io_share={u['metrics']['io_share']}) — "
                     f"NO-GO signal, ranked last" for u in io_bound],
        "scoring": {"io_bound": "io_share >= 0.5 -> no_go, ranked last",
                    "portability": "100 - 10 * len(risk_flags) (floor 0)",
                    "priority": "self_cpu_s * (1 - io_share) * portability / 100"},
        "summary": {"exit_code": code, "human": human},
    }, human_flag=args.human)
    return code


def _rel(path_str: str, project_root: Path) -> str:
    p = Path(path_str)
    try:
        return str(p.resolve().relative_to(project_root))
    except ValueError:
        return str(p)


def _find_by_suffix(path: str, root: Path) -> Path | None:
    name = Path(path).name
    for candidate in root.rglob(name):
        if candidate.is_file() and "site-packages" not in str(candidate):
            return candidate
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=TOOL, description=__doc__.splitlines()[0])
    ap.add_argument("--human", action="store_true", help="echo human summary to stderr")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("from-cprofile", help="cProfile dump -> canonical hotspots JSON")
    p.add_argument("--profile", required=True, help="path to a cProfile dump (.prof)")
    p.set_defaults(func=cmd_from_cprofile)

    p = sub.add_parser("plan", help="hotspots JSON -> ranked dependency-ordered backlog")
    p.add_argument("--hotspots", required=True)
    p.add_argument("--project-root", help="root the hotspot paths resolve against (default: cwd)")
    p.add_argument("--mypy", help="optional mypy JSON list for type-quality risk flags")
    p.set_defaults(func=cmd_plan)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(run_cli(main, tool=TOOL, version=VERSION))
