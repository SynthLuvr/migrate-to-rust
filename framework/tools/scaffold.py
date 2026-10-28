#!/usr/bin/env python3
"""scaffold.py — contract-driven corpus + symmetric adapter scaffolding (issue #32).

Stages 2, 4. The most error-prone hand-work in those stages was, per unit:
(1) a deterministic seeded corpus covering the function surface plus edge
cases, (2) *symmetric* reference/candidate adapters (JSON in -> typed
Python objects -> call -> JSON out, identical on both sides so the verdict
measures only the port), (3) the oracle manifest binding them, and (4) a
second corpus with a different seed for the reviewer's fresh-inputs
re-judgment. All four are mechanical given the contract — which is exactly
this framework's own criterion for tooling. Hand-written corpora are also
where the corpus-format trap (issue #29) and the degenerate-corpus hazard
(issue #30) live; this tool emits the documented format by construction and
dry-runs its own corpus before handing it over.

Usage (mirrors the issue sketch; ``--state`` or a positional state path):

    python framework/tools/scaffold.py scaffold <state.json> --unit u-bars \\
        --out runs/ --seed 20261028

Per unit it writes under --out (default ``runs/``, which is gitignored —
scaffolded files name the target project's private code and must not reach
the repo):

    adapters/<stem>_ref.py    reference adapter: imports the ORIGINAL python
                              unit (both layout spellings, issue #26)
    adapters/<stem>_cand.py   candidate adapter: imports the PyO3 wheel
                              module (a HAND-TUNE placeholder at Stage 2 —
                              the wheel's name is not knowable yet; set it
                              here or re-run with --candidate-module)
    corpora/gen_<stem>.py     seeded generator: --seed/--rows/--out; the
                              reviewer regenerates fresh inputs with a
                              DIFFERENT seed (Stage 4 step 8); prints a
                              row-status summary
    corpora/corpus-<unit>.jsonl   the corpus itself — one raw JSON value per
                              line (a {param: value} object), never
                              pre-wrapped in {"i", "input"} framing
    manifests/<unit>.json     judge-ready for oracle.py (entries are
                              relative .py file paths, a form oracle.py
                              accepts since issue #32)

``<stem>`` is the unit id with ``-``/``.`` sanitized to ``_`` so file-path
entries and importlib behave.

Heuristic mapping (signature annotation -> generator/rebuilder):
  str/int/float/bool/None/Any, Literal  -> JSON-native generators
  list/set/frozenset/dict/tuple, the    -> recursive generators; the adapter
  Sequence/Mapping/Iterable aliases       rebuilds real list/set/dict/tuple
  Optional[X] / X | None / X | Y         -> optional / union
  @dataclass in the unit module or any   -> rows carry a {field: value}
  project module it imports                dict; the adapter calls the real
  constructor (nested dataclasses recurse)
  anything else                           -> flagged for HAND-COMPLETION,
                                           never guessed: the parameter
                                           generates None, the reason is in
                                           the payload's hand_tune[] and in
                                           a HAND-TUNE block in BOTH adapters

Symmetry is by construction: both adapters are rendered from one template
and differ ONLY inside the delimited BACKEND section; the payload records
the sha256 of the shared remainder as evidence (identical on both sides).

Self-check before reporting success: the freshly generated corpus is
dry-run against the reference adapter through oracle.py's own subprocess
runner, so a corpus that would judge hollow (all-error reference — issue
#30) or a malformed format (issue #29) is a finding here, in seconds.

Exit codes follow tools/README.md: 0 scaffolded clean · 2 findings
(hand-tune flags, degenerate/thin corpus) · 1 usage/IO · 4 setup.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import re
import sys
from pathlib import Path
from pprint import pformat

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    EXIT_FINDINGS, EXIT_OK, ToolError, emit, import_candidates_of,
    module_name_of, read_json, run_cli, write_json_atomic,
)
from contract_extract import _find_source  # noqa: E402  — same-dir composition

TOOL = "scaffold.py"
VERSION = 1
DEFAULT_SEED = 20261028
DEFAULT_RANDOM_ROWS = 96
DRY_RUN_TIMEOUT = 120.0

JSON_SCALARS = {"str": "str", "int": "int", "float": "float", "bool": "bool"}
CONTAINER_BASES = {
    "list": "list", "List": "list", "Sequence": "list", "Iterable": "list",
    "Collection": "list", "MutableSequence": "list",
    "dict": "dict", "Dict": "dict", "Mapping": "dict", "MutableMapping": "dict",
    "set": "set", "Set": "set", "AbstractSet": "set",
    "frozenset": "set", "FrozenSet": "set",
    "tuple": "tuple", "Tuple": "tuple",
}
FROZEN_BASES = {"frozenset", "FrozenSet"}
SUBSCRIPT_SPECIAL = {"Optional": "optional", "Union": "union",
                     "Literal": "literal"}
MAX_ANN_DEPTH = 12


# ----------------------------------------------------- signature analysis --

def _spec(kind: str, **kw) -> dict:
    d = {"kind": kind}
    d.update(kw)
    return d


def _ann_text(node) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "?"


def _dotted(node) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _is_dataclass_decorator(dec) -> bool:
    return ((isinstance(dec, ast.Name) and dec.id == "dataclass")
            or (isinstance(dec, ast.Attribute) and dec.attr == "dataclass"))


def _is_classvar(node) -> bool:
    return (isinstance(node, ast.Subscript)
            and _dotted(node.value) in ("ClassVar", "typing.ClassVar"))


class FileCtx:
    """Parse cache entry + import table for one project source file."""

    def __init__(self, path: Path, root: Path, cache: dict):
        self.path = path
        self.root = root
        self.cache = cache  # {resolved Path: FileCtx}
        cache[path.resolve()] = self
        self.tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        self.classes: dict[str, ast.ClassDef] = {}
        self.from_imports: dict[str, tuple[str, object]] = {}  # name -> ("module", dotted) | ("file", Path)
        self.import_binds: dict[str, str] = {}                 # alias -> dotted
        for node in self.tree.body:
            if isinstance(node, ast.ClassDef):
                self.classes[node.name] = node
            elif isinstance(node, ast.ImportFrom):
                self._add_import_from(node)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    self.import_binds[a.asname or a.name.split(".")[0]] = a.name

    def _add_import_from(self, node: ast.ImportFrom) -> None:
        if node.level == 0:
            if not node.module:
                return
            for a in node.names:
                if a.name != "*":
                    self.from_imports[a.asname or a.name] = ("module", node.module, a.name)
            return
        # relative import: resolve against this file's package directory
        base = self.path.parent
        for _ in range((node.level or 1) - 1):
            base = base.parent
        if node.module:
            base = base.joinpath(*node.module.split("."))
            cands = [base.parent / (base.name + ".py"), base / "__init__.py"]
        else:  # `from . import X` — X is a sibling module or a name in __init__
            cands = [base / "__init__.py"]
        target = next((c for c in cands if c.is_file()), None)
        for a in node.names:
            if a.name != "*":
                self.from_imports[a.asname or a.name] = ("file", target, a.name)

    def ctx_for(self, path: Path) -> "FileCtx | None":
        rp = path.resolve()
        if rp in self.cache:
            return self.cache[rp]
        if not path.is_file():
            return None
        return FileCtx(path, self.root, self.cache)


def _module_file_candidates(dotted: str, root: Path) -> list[Path]:
    parts = dotted.split(".")
    out = []
    for base in (root, root / "src"):
        out.append(base.joinpath(*parts[:-1], parts[-1] + ".py") if parts else base)
        out.append(base.joinpath(*parts, "__init__.py"))
    return out


def _find_class(name_or_dotted: str, ctx: FileCtx):
    """Locate a class definition: ("ok", file_ctx, ClassDef, module_dotted) |
    ("missing", reason). Searches the current file, then from-imports, then
    dotted module paths (``import pkg.types as t`` + ``t.Span`` and plain
    ``pkg.types.Span`` annotations)."""
    if "." not in name_or_dotted:
        name = name_or_dotted
        node = ctx.classes.get(name)
        if node is not None:
            return ("ok", ctx, node, None)
        imp = ctx.from_imports.get(name)
        if imp is not None:
            kind, payload, orig = imp
            if kind == "module":
                fctx = next((ctx.ctx_for(c) for c in _module_file_candidates(payload, ctx.root)
                             if c.is_file()), None)
            else:
                fctx = ctx.ctx_for(payload) if payload else None
            if fctx is None:
                return ("missing", f"import of {name!r} does not resolve to a project file")
            node = fctx.classes.get(orig)
            if node is None:
                return ("missing", f"{name!r} not found in its importing module")
            return ("ok", fctx, node, None)
        return ("missing", f"{name!r} is not defined or imported in {ctx.path.name}")
    prefix, cls = name_or_dotted.rsplit(".", 1)
    dotted = ctx.import_binds.get(prefix, prefix)
    fctx = next((ctx.ctx_for(c) for c in _module_file_candidates(dotted, ctx.root)
                 if c.is_file()), None)
    if fctx is None:
        return ("missing", f"module {dotted!r} does not resolve to a project file")
    node = fctx.classes.get(cls)
    if node is None:
        return ("missing", f"class {cls!r} not found in module {dotted!r}")
    return ("ok", fctx, node, dotted)


def _dataclass_spec(name_or_dotted: str, ctx: FileCtx, seen: frozenset, depth: int):
    """@dataclass lookup -> spec, or (None, reason) when unresolvable/not a
    dataclass. Never guesses: anything else stays unknown + flagged."""
    key = name_or_dotted
    if depth > MAX_ANN_DEPTH or key in seen:
        return None, f"{name_or_dotted!r} recurses or nests too deep — hand-complete"
    found = _find_class(name_or_dotted, ctx)
    if found[0] != "ok":
        return None, found[1]
    _, fctx, node, module_dotted = found
    if not any(_is_dataclass_decorator(d) for d in node.decorator_list):
        return None, f"{name_or_dotted!r} is a class but not a @dataclass — hand-complete"
    rel = fctx.path.resolve().relative_to(ctx.root) if fctx.path.resolve().is_relative_to(ctx.root) else fctx.path
    fields = []
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if _is_classvar(stmt.annotation):
                continue  # ClassVar is not a constructor argument
            fields.append({
                "name": stmt.target.id,
                "spec": _parse_ann(stmt.annotation, fctx, seen | {key}, depth + 1),
                "has_default": stmt.value is not None,
            })
        elif isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    fields.append({"name": t.id, "spec": _spec("any"),
                                   "has_default": True})
    return _spec("dataclass", name=node.name,
                 module=module_dotted or module_name_of(str(rel)),
                 path=rel.as_posix(), fields=fields), None


def _bare_container(base: str) -> dict:
    if base == "list":
        return _spec("list", item=_spec("any"))
    if base == "set":
        return _spec("set", frozen=False, item=_spec("any"))
    if base == "dict":
        return _spec("dict", key=_spec("str"), value=_spec("any"))
    return _spec("tuple", variadic=True, item=_spec("any"))


def _name_spec(name: str, ctx: FileCtx, seen: frozenset, depth: int) -> dict:
    if name in ("Any",):
        return _spec("any")
    if name in ("None", "NoneType"):
        return _spec("none")
    if name in JSON_SCALARS:
        return _spec(JSON_SCALARS[name])
    if name in CONTAINER_BASES:
        return _bare_container(CONTAINER_BASES[name])
    dc, reason = _dataclass_spec(name, ctx, seen, depth)
    if dc is not None:
        return dc
    return _spec("unknown", text=name, reason=reason)


def _union_spec(nodes: list, ctx: FileCtx, seen: frozenset, depth: int) -> dict:
    flat: list = []

    def add(n) -> None:
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr):
            add(n.left)
            add(n.right)
        else:
            flat.append(n)

    for n in nodes:
        add(n)
    specs = [_parse_ann(n, ctx, seen, depth + 1) for n in flat]
    nones = [s for s in specs if s["kind"] == "none"]
    rest = [s for s in specs if s["kind"] != "none"]
    if not rest:
        return _spec("none")
    if len(rest) == 1:
        return _spec("optional", item=rest[0]) if nones else rest[0]
    union = _spec("union", items=rest)
    return _spec("optional", item=union) if nones else union


def _subscript_spec(node: ast.Subscript, ctx: FileCtx, seen: frozenset, depth: int) -> dict:
    base = _dotted(node.value) or _ann_text(node.value)
    base_tail = base.rsplit(".", 1)[-1]
    known_ns = "." not in base or base.split(".")[0] == "typing"
    sl = node.slice
    elems = list(sl.elts) if isinstance(sl, ast.Tuple) else [sl]
    if base_tail in SUBSCRIPT_SPECIAL and known_ns:
        kind = SUBSCRIPT_SPECIAL[base_tail]
        if kind == "optional":
            inner = _union_spec(elems, ctx, seen, depth + 1)
            return inner if inner["kind"] == "optional" else _spec("optional", item=inner)
        if kind == "union":
            return _union_spec(elems, ctx, seen, depth + 1)
        if kind == "literal":
            values = []
            for e in elems:
                if isinstance(e, ast.Constant) and isinstance(e.value, (str, int, float, bool, type(None))):
                    values.append(e.value)
                else:
                    return _spec("unknown", text=_ann_text(node),
                                 reason="non-constant Literal member — hand-complete")
            return _spec("literal", values=values)
    if base_tail in CONTAINER_BASES and known_ns:
        b = CONTAINER_BASES[base_tail]
        frozen = base_tail in FROZEN_BASES
        if b == "list":
            return _spec("list", item=_parse_ann(elems[0], ctx, seen, depth + 1))
        if b == "set":
            return _spec("set", frozen=frozen, item=_parse_ann(elems[0], ctx, seen, depth + 1))
        if b == "dict":
            if len(elems) >= 2:
                return _spec("dict", key=_parse_ann(elems[0], ctx, seen, depth + 1),
                             value=_parse_ann(elems[1], ctx, seen, depth + 1))
            return _spec("dict", key=_spec("str"), value=_spec("any"))
        if b == "tuple":
            if len(elems) == 2 and isinstance(elems[1], ast.Constant) and elems[1].value is Ellipsis:
                return _spec("tuple", variadic=True,
                             item=_parse_ann(elems[0], ctx, seen, depth + 1))
            return _spec("tuple", variadic=False,
                         items=[_parse_ann(e, ctx, seen, depth + 1) for e in elems])
    dc, reason = _dataclass_spec(base, ctx, seen, depth)
    if dc is not None:
        return dc
    return _spec("unknown", text=_ann_text(node),
                 reason=f"unsupported generic {base!r} — hand-complete")


def _parse_ann(node, ctx: FileCtx, seen: frozenset = frozenset(), depth: int = 0) -> dict:
    if depth > MAX_ANN_DEPTH:
        return _spec("unknown", text=_ann_text(node) if node else "?",
                     reason="annotation nesting too deep")
    if node is None:
        return _spec("any")  # unannotated parameter: Any (contract_extract renders Any too)
    if isinstance(node, ast.Constant):
        if node.value is None:
            return _spec("none")
        if isinstance(node.value, str):  # quoted annotation: parse it
            try:
                return _parse_ann(ast.parse(node.value, mode="eval").body,
                                  ctx, seen, depth + 1)
            except (SyntaxError, ValueError):
                return _spec("unknown", text=node.value,
                             reason="unparseable string annotation")
        return _spec("unknown", text=_ann_text(node), reason="constant annotation")
    if isinstance(node, ast.Name):
        return _name_spec(node.id, ctx, seen, depth)
    if isinstance(node, ast.Attribute):
        dotted = _dotted(node)
        if dotted and dotted.rsplit(".", 1)[-1] in JSON_SCALARS and dotted.split(".")[0] == "builtins":
            return _spec(JSON_SCALARS[dotted.rsplit(".", 1)[-1]])
        if dotted:
            dc, reason = _dataclass_spec(dotted, ctx, seen, depth)
            if dc is not None:
                return dc
            return _spec("unknown", text=dotted, reason=reason)
        return _spec("unknown", text=_ann_text(node), reason="unsupported annotation form")
    if isinstance(node, ast.Subscript):
        return _subscript_spec(node, ctx, seen, depth)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _union_spec([node.left, node.right], ctx, seen, depth)
    return _spec("unknown", text=_ann_text(node), reason="unsupported annotation form")


def _public_functions(tree: ast.Module) -> list:
    return [n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not n.name.startswith("_")]


def _pick_function(tree: ast.Module, module_stem: str, override: str | None):
    fns = _public_functions(tree)
    if override:
        match = next((f for f in fns if f.name == override), None)
        if match is None:
            raise ToolError(f"--function {override!r} is not a public function in the unit module",
                            kind="usage")
        return match, "flag"
    for f in fns:
        if f.name == module_stem:
            return f, "module-stem"
    if fns:
        return fns[0], "first-public"
    raise ToolError("unit module has no public module-level function to scaffold "
                    "(classes and private helpers are not oracle entries)", kind="usage")


def _params_of(fn) -> tuple[list[dict], list[str]]:
    """(positional+keyword-only params, notes for *args/**kwargs)."""
    a = fn.args
    params, notes = [], []
    pos = (a.posonlyargs or []) + a.args
    defaults = [None] * (len(pos) - len(a.defaults or [])) + list(a.defaults or [])
    for arg, default in zip(pos, defaults):
        params.append({"name": arg.arg, "has_default": default is not None})
    kw_defaults = a.kw_defaults or []
    for arg, default in zip(a.kwonlyargs or [], kw_defaults):
        params.append({"name": arg.arg, "has_default": default is not None})
    if a.vararg:
        notes.append(f"*{a.vararg.arg} cannot be expressed as JSON kwargs rows — hand-complete")
    if a.kwarg:
        notes.append(f"**{a.kwarg.arg} cannot be expressed as JSON kwargs rows — hand-complete")
    return params, notes


# --------------------------------------------------------- edge-row maker --

def _zero(spec: dict, depth: int = 0):
    k = spec["kind"]
    if k == "str":
        return ""
    if k == "int":
        return 0
    if k == "float":
        return 0.0
    if k == "bool":
        return False
    if k == "literal":
        return spec["values"][0] if spec.get("values") else None
    if k in ("list", "set", "dict"):
        return [] if k != "dict" else {}
    if k == "tuple":
        return [] if spec.get("variadic") else [_zero(s, depth + 1) for s in spec["items"]]
    if k == "union":
        return _zero(spec["items"][0], depth + 1) if spec["items"] else None
    if k == "dataclass":
        if depth > MAX_ANN_DEPTH:
            return {}
        return {f["name"]: _zero(f["spec"], depth + 1) for f in spec["fields"]}
    return None  # none / any / unknown / optional


def _specials(spec: dict, depth: int = 0) -> list:
    k = spec["kind"]
    if k == "str":
        return [" ", "é🙂中文", "\n\t ", "a" * 512, "0"]
    if k == "int":
        return [1, -1, 2**31 - 1, -2**31, 2**63 - 1, -2**63]
    if k == "float":
        return [1.0, -0.5, 1e308, -1e-308, 0.1 + 0.2]
    if k == "bool":
        return [True]
    if k in ("list", "set"):
        z = _zero(spec["item"], depth + 1)
        return [[z], [z] * 9]
    if k == "dict":
        zv = _zero(spec["value"], depth + 1)
        zk = _zero(spec["key"], depth + 1)
        key = zk if isinstance(zk, (str, int)) else str(zk)
        return [{key: zv}, {"": zv}]
    if k == "tuple" and not spec.get("variadic"):
        return [[_zero(s, depth + 1) for s in spec["items"]]]
    if k == "tuple":
        z = _zero(spec["item"], depth + 1)
        return [[z, z, z]]
    if k == "optional":
        return [None]
    if k == "union":
        return [_zero(s, depth + 1) for s in spec["items"][:3]]
    if k == "literal":
        return spec.get("values", [])[1:4]
    return []


def _edge_rows(param_specs: dict) -> list[dict]:
    """Deterministic surface+boundary rows derived from the signature: the
    all-zero row (frequently the documented error path — 'empty input
    raises'), per-parameter boundary rows, and a defaults-exercising row."""
    specs = {n: info["spec"] for n, info in param_specs.items()}
    rows = [{name: _zero(spec) for name, spec in specs.items()}]
    for name, spec in specs.items():
        for sp in _specials(spec)[:5]:
            row = {n: _zero(s) for n, s in specs.items()}
            row[name] = sp
            rows.append(row)
    defaulted = [n for n, s in specs.items() if s.get("has_default")]
    if defaulted:
        rows.append({n: _zero(s) for n, s in specs.items() if not s.get("has_default")})
    seen, out = set(), []
    for row in rows:
        key = repr(sorted(row.items()))
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


# ------------------------------------------------------------ templates ----

ADAPTER_TEMPLATE = '''#!/usr/bin/env python3
"""Scaffolded adapter for unit @@UNIT@@ (issue #32) — one of the symmetric
_ref.py/_cand.py pair; the BACKEND section below says which side this file
is.

Generated by framework/tools/scaffold.py from the unit's type signature.
Regenerate (scaffold.py --force) rather than hand-edit when the signature
changes; hand-tune only what the scaffold flagged (below) — and apply any
edit OUTSIDE the BACKEND section identically to the sibling adapter.

SYMMETRY: the sibling adapter is rendered from the same template and
differs ONLY inside the delimited BACKEND section, so an oracle.py verdict
over both measures the port, not the adapter.

Protocol: the oracle entry is "<this file>:run". A corpus row is one JSON
object mapping @@FUNCTION@@'s parameter names to JSON values (exactly what
corpora/gen_@@STEM@@.py emits); run() rebuilds the typed Python objects
the signature requires (specs below), calls the function, and serializes
the result back to JSON — canonicalizing non-JSON shapes (set -> sorted
list, tuple -> list, dataclass -> dict) so the oracle can compare them.

HAND-TUNE@@HANDTUNE_MARK@@ — the heuristic could not express these;
complete them in BOTH adapters and in the generator:
@@HANDTUNE_LIST@@
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import importlib.util
import inspect
import json
import os
import sys
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum

UNIT = @@UNIT_REPR@@
FUNCTION = @@FUNCTION_REPR@@
# parameter -> generation/rebuild spec (from the contract's type signature)
PARAM_SPECS = @@PARAM_SPECS@@
# return-annotation spec; None = unannotated (honest _jsonify fallback)
RESULT_SPEC = @@RESULT_SPEC@@

# ==== BACKEND — the ONLY section that differs between ================
# ==== _ref.py and _cand.py ================================================

@@BACKEND@@

# ==== END BACKEND =========================================================


def _resolve_module(rel_path: str, dotted: str):
    """Import a project module by dotted name (both layout spellings,
    issue #26) with the project root(s) on sys.path; fall back to loading
    the file by path. Used for the reference unit and for dataclasses."""
    for r in (PROJECT_ROOT, os.path.join(PROJECT_ROOT, "src")):
        if os.path.isdir(r) and r not in sys.path:
            sys.path.insert(0, r)
    try:
        return importlib.import_module(dotted)
    except ImportError:
        p = os.path.join(PROJECT_ROOT, rel_path)
        spec = importlib.util.spec_from_file_location(dotted.replace(".", "_"), p)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod


def _dc_class(spec):
    return getattr(_resolve_module(spec["path"], spec["module"]), spec["name"])


def _shape_ok(value, spec) -> bool:
    """Does a JSON value plausibly inhabit this spec? (union dispatch)"""
    k = spec["kind"]
    if k in ("any", "unknown", "literal"):
        return True
    if k == "none":
        return value is None
    if k == "bool":
        return isinstance(value, bool)
    if k == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if k == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if k == "str":
        return isinstance(value, str)
    if k in ("list", "set", "tuple"):
        return isinstance(value, list)
    if k == "dict":
        return isinstance(value, dict)
    if k == "dataclass":
        return isinstance(value, dict)
    if k == "optional":
        return value is None or _shape_ok(value, spec["item"])
    if k == "union":
        return any(_shape_ok(value, s) for s in spec["items"])
    return False


def _rebuild_key(key, spec, where):
    """JSON object keys are always strings; coerce back per the key spec
    (int/float keys round-trip through their lexical form)."""
    k = spec["kind"]
    if k == "str":
        return key
    if k == "int":
        return int(key)
    if k == "float":
        return float(key)
    if k in ("any", "unknown"):
        return key
    raise TypeError(f"{where}: dict keys of spec kind {k!r} cannot round-trip "
                    "through JSON — hand-complete")


def rebuild(value, spec, where="$"):
    """JSON value -> the Python object the signature describes. STRICT: a
    corpus row that does not match the signature is a corpus bug and raises
    (oracle.py records an error run — honest, never silently coerced;
    silent int<->float coercion is a divergence, not a normalization)."""
    k = spec["kind"]
    if k == "optional":
        return None if value is None else rebuild(value, spec["item"], where)
    if k in ("any", "unknown", "literal"):
        return value
    if k == "none":
        if value is not None:
            raise TypeError(f"{where}: expected null, got {type(value).__name__}")
        return None
    if k == "bool":
        if not isinstance(value, bool):
            raise TypeError(f"{where}: expected bool, got {type(value).__name__}")
        return value
    if k == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{where}: expected int, got {type(value).__name__}")
        return value
    if k == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{where}: expected float, got {type(value).__name__}")
        return float(value)
    if k == "str":
        if not isinstance(value, str):
            raise TypeError(f"{where}: expected str, got {type(value).__name__}")
        return value
    if k == "list":
        if not isinstance(value, list):
            raise TypeError(f"{where}: expected list, got {type(value).__name__}")
        return [rebuild(v, spec["item"], f"{where}[{i}]") for i, v in enumerate(value)]
    if k == "set":
        if not isinstance(value, list):
            raise TypeError(f"{where}: expected a JSON list for a set, got {type(value).__name__}")
        items = [rebuild(v, spec["item"], f"{where}{{}}") for v in value]
        return frozenset(items) if spec.get("frozen") else set(items)
    if k == "dict":
        if not isinstance(value, dict):
            raise TypeError(f"{where}: expected dict, got {type(value).__name__}")
        out = {}
        for key, v in value.items():
            rk = _rebuild_key(key, spec["key"], where)
            out[rk] = rebuild(v, spec["value"], f"{where}.{key}")
        return out
    if k == "tuple":
        if not isinstance(value, list):
            raise TypeError(f"{where}: expected a JSON list for a tuple, got {type(value).__name__}")
        if spec.get("variadic"):
            return tuple(rebuild(v, spec["item"], f"{where}()") for v in value)
        if len(value) != len(spec["items"]):
            raise TypeError(f"{where}: expected {len(spec['items'])}-tuple, got {len(value)} items")
        return tuple(rebuild(v, s, f"{where}()") for v, s in zip(value, spec["items"]))
    if k == "union":
        if value is None:
            return None
        for item in spec["items"]:
            if _shape_ok(value, item):
                return rebuild(value, item, where)
        return value  # matches no arm's JSON shape: pass through and let the call judge
    if k == "dataclass":
        if not isinstance(value, dict):
            raise TypeError(f"{where}: expected a field dict for dataclass "
                            f"{spec['name']}, got {type(value).__name__}")
        known = {f["name"] for f in spec["fields"]}
        unknown = set(value) - known
        if unknown:
            raise TypeError(f"{where}: unknown dataclass field(s) {sorted(unknown)} "
                            f"for {spec['name']}")
        kwargs = {f["name"]: rebuild(value[f["name"]], f["spec"], f"{where}.{f['name']}")
                  for f in spec["fields"] if f["name"] in value}
        return _dc_class(spec)(**kwargs)
    raise TypeError(f"{where}: unsupported spec kind {k!r} — hand-complete")


def _sort_key(j):
    return json.dumps(j, sort_keys=True, default=repr)


def _jsonify(value):
    """Best-effort JSON view for values the signature did not describe.
    Raises TypeError for truly exotic types — oracle.py records the run as
    unserializable (incomparable), the honest verdict; units with such
    outputs need a canonical adapter (e.g. sorted(list(s))) — extend here,
    identically in both adapters."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonify(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonify(v) for v in value), key=_sort_key)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonify(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return _jsonify(value.value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unserializable output type {type(value).__name__} — "
                    "hand-complete serialize() in BOTH adapters")


def serialize(value, spec):
    """Return value -> JSON per the return spec (None = unannotated).
    Canonicalizes non-JSON shapes so the oracle compares them; anything the
    spec does not describe falls back to the honest _jsonify walk."""
    if spec is None or spec["kind"] in ("any", "unknown", "literal", "union", "optional"):
        return _jsonify(value)
    k = spec["kind"]
    if k == "set" and isinstance(value, (set, frozenset)):
        return sorted((serialize(v, spec["item"]) for v in value), key=_sort_key)
    if k == "tuple" and isinstance(value, (tuple, list)):
        if spec.get("variadic"):
            return [serialize(v, spec["item"]) for v in value]
        return [serialize(v, s) for v, s in zip(value, spec["items"])]
    if (k == "dataclass" and dataclasses.is_dataclass(value)
            and not isinstance(value, type)):
        return {f["name"]: serialize(getattr(value, f["name"]), f["spec"])
                for f in spec["fields"] if hasattr(value, f["name"])}
    return _jsonify(value)


def run(row):
    """One corpus row ({param: value} JSON object) -> JSON-serializable
    output of @@FUNCTION@@. This is the oracle entry point."""
    if not isinstance(row, dict):
        raise TypeError("corpus row must be a JSON object of {param: value}, got "
                        + type(row).__name__)
    kwargs = {}
    for name in sorted(row):
        if name not in PARAM_SPECS:
            raise TypeError(f"corpus row key {name!r} is not a parameter of "
                            f"{FUNCTION}({', '.join(sorted(PARAM_SPECS))})")
        kwargs[name] = rebuild(row[name], PARAM_SPECS[name], f"$.{name}")
    result = FN(**kwargs)
    if inspect.iscoroutine(result):  # async entries, mechanically symmetric
        result = asyncio.run(result)
    return serialize(result, RESULT_SPEC)


FN = _load_function()  # a failure here is a setup error for this side —
                       # oracle.py reports it honestly (exit 4) instead of
                       # judging half an adapter
'''

REF_BACKEND = '''# Reference side: import the ORIGINAL python unit. PROJECT_ROOT is where
# scaffold.py ran; override with MIGRATE_PROJECT_ROOT to relocate the run.
PROJECT_ROOT = os.environ.get("MIGRATE_PROJECT_ROOT", @@ROOT_REPR@@)
_MODULE_CANDIDATES = @@MODULE_CANDIDATES@@  # layout-stripped + src-prefixed (issue #26)
_UNIT_PATH = @@UNIT_PATH_REPR@@             # project-relative, for the file fallback


def _load_function():
    for r in (PROJECT_ROOT, os.path.join(PROJECT_ROOT, "src")):
        if os.path.isdir(r) and r not in sys.path:
            sys.path.insert(0, r)
    for name in _MODULE_CANDIDATES:
        try:
            return getattr(importlib.import_module(name), FUNCTION)
        except ImportError:
            continue
    mod = _resolve_module(_UNIT_PATH, _MODULE_CANDIDATES[0])
    try:
        return getattr(mod, FUNCTION)
    except AttributeError as e:
        raise ImportError(f"reference module {_MODULE_CANDIDATES[0]} exposes no "
                          f"{FUNCTION!r}") from e
'''

CAND_BACKEND = '''# Candidate side: import the PyO3 wheel. The wheel's module name is NOT
# knowable at Stage 2 scaffold time — set CANDIDATE_MODULE below (or re-run
# scaffold.py with --candidate-module <module> --force at Stage 4) before
# judging. Until then importing this adapter is a setup error, which
# oracle.py reports honestly instead of judging half an adapter.
# The wheel is normally INSTALLED, so a plain import is tried first; the
# source-tree fallback (MIGRATE_PROJECT_ROOT) only serves symmetry checks
# and pre-wheel smoke tests — it can never shadow an installed wheel.
CANDIDATE_MODULE = @@CANDIDATE_MODULE@@  # e.g. "mycrate.units.u_bars"
_CANDIDATE_FUNCTION = @@FUNCTION_REPR@@  # wheel export name; rename if it differs
PROJECT_ROOT = os.environ.get("MIGRATE_PROJECT_ROOT", @@ROOT_REPR@@)


def _load_function():
    if not CANDIDATE_MODULE:
        raise NotImplementedError(
            "scaffold.py: candidate module not set — the PyO3 wheel is unknown "
            "at Stage 2. Set CANDIDATE_MODULE (or re-run scaffold.py "
            "--candidate-module <wheel-module> --force), then re-judge (issue #32).")
    try:
        return getattr(importlib.import_module(CANDIDATE_MODULE), _CANDIDATE_FUNCTION)
    except ImportError:
        for r in (PROJECT_ROOT, os.path.join(PROJECT_ROOT, "src")):
            if os.path.isdir(r) and r not in sys.path:
                sys.path.insert(0, r)
        return getattr(importlib.import_module(CANDIDATE_MODULE), _CANDIDATE_FUNCTION)
'''

GENERATOR_TEMPLATE = '''#!/usr/bin/env python3
"""gen_@@STEM@@.py — seeded corpus generator for unit @@UNIT@@ (issue #32).

Deterministic per --seed: identical seed -> byte-identical corpus (one
random.Random(seed), fixed pools, sorted keys). The reviewer's fresh-inputs
re-judgment (Stage 4 step 8) regenerates with a DIFFERENT seed:

    python gen_@@STEM@@.py --seed <fresh> --out corpus-fresh.jsonl

Rows are the documented corpus format: one raw JSON value per line = one
input, a JSON object of {param: value} for @@FUNCTION@@ — NEVER pre-wrapped
in {"i", "input"} framing; oracle.py adds that envelope itself (issue #29).
Edge rows cover zero-values and signature boundaries (often the documented
error path); random rows follow the baked-in specs.

@@INVARINTS@@Flagged parameters generate None until hand-completed:
@@FLAGGED@@
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

UNIT = @@UNIT_REPR@@
FUNCTION = @@FUNCTION_REPR@@
DEFAULT_SEED = @@SEED@@
DEFAULT_ROWS = @@ROWS@@
# {param: {"spec": <generation spec>, "has_default": bool}} — from the
# contract's type signature via scaffold.py
SPECS = @@SPECS@@
# deterministic surface/boundary rows (all-zero row, per-param boundaries,
# defaults-exercising row) derived from the signature at scaffold time
EDGE_ROWS = @@EDGE_ROWS@@

_WORDS = ["alpha", "beta", "gamma", "delta", "epsilon", "x", "y", "AB",
          "élan", "naïve", "café", "中文", "🙂", "z9z", "lead-space",
          "tab\\tsep", "new\\nline"]
_SEPS = [" ", ",", ":", ";", "-", "|", " / ", "\\t", "\\n"]
_BIG_INTS = [0, 1, -1, 2, -2, 10, 255, 256, 65535, 65536,
             2**31 - 1, -2**31, 2**53, -(2**53), 2**63 - 1, -2**63]
_SPECIAL_FLOATS = [0.0, -0.0, 1e-9, -1e-9, 1e9, -1e9, 1.5, -2.25,
                   3.141592653589793, 1e308, -1e308, 5e-324, 0.1 + 0.2,
                   float("inf"), float("-inf"), float("nan")]


def g_str(rng):
    r = rng.random()
    if r < 0.12:
        return ""
    if r < 0.24:
        return rng.choice([" ", "\\t", "\\n", "  x  ", "0", "null", "true"])
    if r < 0.5:
        return rng.choice(_WORDS)
    if r < 0.8:
        n = rng.randint(1, 4)
        return rng.choice(_SEPS).join(rng.choice(_WORDS) for _ in range(n))
    return rng.choice(_WORDS) * rng.randint(2, 97)


def g_int(rng):
    r = rng.random()
    if r < 0.3:
        return rng.choice(_BIG_INTS)
    if r < 0.8:
        return rng.randint(-1000, 1000)
    return rng.randint(-(2**63), 2**63 - 1)


def g_float(rng):
    r = rng.random()
    if r < 0.25:
        return rng.choice(_SPECIAL_FLOATS)
    if r < 0.75:
        return round(rng.uniform(-1e6, 1e6), 6)
    return round(rng.uniform(-1.0, 1.0) * 10.0 ** rng.randint(-8, 2), 9)


def gen(spec, rng, depth=0):
    k = spec["kind"]
    if depth > 8:
        return None
    if k == "str":
        return g_str(rng)
    if k == "int":
        return g_int(rng)
    if k == "float":
        return g_float(rng)
    if k == "bool":
        return rng.random() < 0.5
    if k == "none":
        return None
    if k in ("any", "unknown"):
        pick = rng.choice(["null", "bool", "int", "float", "str", "list", "dict"])
        if pick == "null":
            return None
        if pick == "bool":
            return rng.random() < 0.5
        if pick == "int":
            return g_int(rng)
        if pick == "float":
            return g_float(rng)
        if pick == "str":
            return g_str(rng)
        if pick == "list":
            return [g_str(rng) for _ in range(rng.randint(0, 3))]
        return {"k": g_str(rng)}
    if k == "literal":
        return rng.choice(spec["values"]) if spec.get("values") else None
    if k == "list":
        n = rng.choice([0, 1, 1, 2, 2, 3, 4, 6, 9])
        return [gen(spec["item"], rng, depth + 1) for _ in range(n)]
    if k == "set":
        n = rng.choice([0, 1, 1, 2, 3, 5])
        items, seen = [], set()
        for _ in range(n * 3):
            if len(items) >= n:
                break
            v = gen(spec["item"], rng, depth + 1)
            key = json.dumps(v, sort_keys=True, default=repr)
            if key not in seen:
                seen.add(key)
                items.append(v)
        return items
    if k == "dict":
        n = rng.choice([0, 1, 1, 2, 3, 5])
        out = {}
        for _ in range(n):
            key = gen(spec["key"], rng, depth + 1)
            if not isinstance(key, (str, int, float, bool)):
                key = json.dumps(key, sort_keys=True, default=repr)
            out[key] = gen(spec["value"], rng, depth + 1)
        return out
    if k == "tuple":
        if spec.get("variadic"):
            n = rng.choice([0, 1, 2, 3])
            return [gen(spec["item"], rng, depth + 1) for _ in range(n)]
        return [gen(s, rng, depth + 1) for s in spec["items"]]
    if k == "optional":
        return None if rng.random() < 0.2 else gen(spec["item"], rng, depth + 1)
    if k == "union":
        return gen(rng.choice(spec["items"]), rng, depth + 1)
    if k == "dataclass":
        out = {}
        for f in spec["fields"]:
            if f.get("has_default") and rng.random() < 0.3:
                continue  # exercise the dataclass default
            out[f["name"]] = gen(f["spec"], rng, depth + 1)
        return out
    return None


def build_rows(seed, random_rows, include_edge=True):
    """(edge rows) + seeded random rows — deterministic in (seed, counts)."""
    rng = random.Random(seed)
    rows = list(EDGE_ROWS) if include_edge else []
    for _ in range(random_rows):
        row = {}
        for name, info in SPECS.items():
            if info.get("has_default") and rng.random() < 0.25:
                continue  # exercise the function's default
            row[name] = gen(info["spec"], rng)
        rows.append(row)
    return rows


def write_corpus(path, rows):
    """One raw JSON value per line — the documented corpus format (issue
    #29); never {"i", "input"} pre-wrapping."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r, sort_keys=True, ensure_ascii=False,
                              separators=(",", ":")) + "\\n" for r in rows)
    p.write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def flagged_params():
    return sorted(n for n, info in SPECS.items() if info["spec"]["kind"] == "unknown")


def main(argv=None):
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(
        description="seeded corpus generator for unit " + UNIT + " (issue #32)")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED,
                    help="deterministic seed (reviewer fresh-inputs: pick a different one)")
    ap.add_argument("--rows", type=int, default=DEFAULT_ROWS,
                    help="random rows beyond the edge rows")
    ap.add_argument("--out", default=str(here / ("corpus-" + UNIT + ".jsonl")),
                    help="corpus output path")
    ap.add_argument("--no-edge", action="store_true", help="omit edge rows")
    args = ap.parse_args(argv)
    rows = build_rows(args.seed, args.rows, include_edge=not args.no_edge)
    digest = write_corpus(args.out, rows)
    edge = 0 if args.no_edge else len(EDGE_ROWS)
    payload = {
        "tool": "gen_" + UNIT.replace("-", "_") + ".py", "unit": UNIT,
        "seed": args.seed,
        "corpus": {"path": str(Path(args.out).resolve()), "rows": len(rows),
                   "edge_rows": edge, "random_rows": args.rows, "sha256": digest},
        "flagged_params": flagged_params(),
        "summary": {
            "exit_code": 0,
            "human": (f"corpus for {UNIT}: {len(rows)} rows ({edge} edge + "
                      f"{args.rows} random), seed {args.seed}, sha256 {digest[:12]}"
                      + (f", flagged: {', '.join(flagged_params())}" if flagged_params() else "")),
        },
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


# ------------------------------------------------------------- rendering --

def _sanitize_stem(unit_id: str) -> str:
    return "".join(c if c.isalnum() or c == "_" else "_" for c in unit_id)


def _hand_tune_block(items: list[dict]) -> str:
    if not items:
        return " none — the signature is fully covered by the heuristic."
    return "\n" + "\n".join(f"  - {it['where']}: {it['reason']}" for it in items)


def _render_adapter(*, side: str, unit: str, function: str, param_specs: dict,
                    result_spec: dict | None, hand_tune: list[dict],
                    project_root: Path, module_candidates: list[str],
                    unit_path: str, candidate_module: str | None) -> str:
    """param_specs here is the RAW {name: spec} mapping (the generator gets
    the has_default wrapper instead)."""
    stem = _sanitize_stem(unit)
    if side == "ref":
        backend = REF_BACKEND
        backend = backend.replace("@@ROOT_REPR@@", repr(str(project_root)))
        backend = backend.replace("@@MODULE_CANDIDATES@@", repr(module_candidates))
        backend = backend.replace("@@UNIT_PATH_REPR@@", repr(unit_path))
    else:
        backend = CAND_BACKEND.replace("@@CANDIDATE_MODULE@@",
                                       repr(candidate_module))
        backend = backend.replace("@@ROOT_REPR@@", repr(str(project_root)))
    backend = backend.replace("@@FUNCTION_REPR@@", repr(function))
    text = ADAPTER_TEMPLATE
    for token, value in {
        "@@UNIT@@": unit,
        "@@UNIT_REPR@@": repr(unit),
        "@@FUNCTION@@": function,
        "@@FUNCTION_REPR@@": repr(function),
        "@@STEM@@": stem,
        "@@PARAM_SPECS@@": pformat(param_specs, sort_dicts=False, width=88),
        "@@RESULT_SPEC@@": pformat(result_spec, sort_dicts=False, width=88),
        "@@HANDTUNE_MARK@@": f" ({len(hand_tune)} item(s))" if hand_tune else "",
        "@@HANDTUNE_LIST@@": _hand_tune_block(hand_tune),
        "@@BACKEND@@": backend.rstrip("\n"),
    }.items():
        text = text.replace(token, value)
    if "@@" in text:  # a template bug must never ship as a broken artifact
        raise ToolError(f"internal: unresolved template token in {side} adapter "
                        f"for {unit}", kind="setup")
    return text


def _shared_adapter_sha256(ref_text: str, cand_text: str):
    """Symmetry evidence: hash the template outside the BACKEND section.
    Both sides must agree — or the verdict would measure the adapters."""
    def shared(text: str) -> bytes:
        head, _, rest = text.partition("# ==== BACKEND")
        _, _, tail = rest.partition("# ==== END BACKEND")
        return (head + tail).encode("utf-8")
    return hashlib.sha256(shared(ref_text)).hexdigest(), \
        hashlib.sha256(shared(cand_text)).hexdigest()


def _render_generator(*, unit: str, function: str, specs: dict, edge_rows: list,
                      seed: int, rows: int, invariants: list[str]) -> str:
    stem = _sanitize_stem(unit)
    flagged = sorted(n for n, info in specs.items()
                     if info["spec"]["kind"] == "unknown")
    inv_lines = "".join(f"    {line}\n" for line in invariants[:10])
    invariants_block = (f"Contract invariants (from the unit's contract.json —\n"
                        f"the edge rows should exercise them, especially the "
                        f"error paths):\n{inv_lines}\n\n" if inv_lines else "")
    text = GENERATOR_TEMPLATE
    for token, value in {
        "@@STEM@@": stem,
        "@@UNIT@@": unit,
        "@@UNIT_REPR@@": repr(unit),
        "@@FUNCTION@@": function,
        "@@FUNCTION_REPR@@": repr(function),
        "@@SEED@@": str(seed),
        "@@ROWS@@": str(rows),
        "@@SPECS@@": pformat(specs, sort_dicts=False, width=88),
        "@@EDGE_ROWS@@": pformat(edge_rows, sort_dicts=False, width=88),
        "@@INVARINTS@@": invariants_block,
        "@@FLAGGED@@": (", ".join(flagged) if flagged else "none"),
    }.items():
        text = text.replace(token, value)
    if "@@" in text:
        raise ToolError(f"internal: unresolved template token in generator for "
                        f"{unit}", kind="setup")
    return text


# --------------------------------------------------------- the scaffold ---

def _load_contract(path: str, unit_id: str, warnings: list[str]) -> dict | None:
    """An explicit contract FILE must exist (hard error when missing); a
    contracts DIRECTORY may simply not contain this unit yet (warning) —
    contract_extract.py and scaffold.py run in either order."""
    p = Path(path)
    from_dir = p.is_dir()
    if from_dir:
        p = p / f"{unit_id}.contract.json"
    if not p.is_file():
        if from_dir:
            warnings.append(f"no contract file for {unit_id} under {path} — "
                            "scaffolding from the source signature alone")
            return None
        raise ToolError(f"contract file not found: {p}", kind="usage")
    return read_json(p)


def _scaffold_unit(u: dict, root: Path, project_root: Path, out: Path,
                   args, targets: dict, warnings: list[str]) -> dict:
    uid = u["id"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", uid):
        raise ToolError(f"unit id {uid!r} is not a safe file-name component — "
                        "corpus/manifest names derive from it", kind="usage")
    src = _find_source(u["python_path"], root, project_root)
    if src is None:
        raise ToolError(f"unit {uid}: python file not found: {u['python_path']}",
                        kind="usage")
    tree = ast.parse(src.read_text(encoding="utf-8", errors="replace"))
    ctx = FileCtx(src.resolve(), root if root.is_dir() else src.parent, {})
    module_stem = src.stem
    fn, how = _pick_function(tree, module_stem, args.function)
    params, notes = _params_of(fn)

    contract = _load_contract(args.contract, uid, warnings) if args.contract else None
    param_specs = {}
    hand_tune = []
    for p in params:
        ann = None
        for node in fn.args.posonlyargs + fn.args.args + (fn.args.kwonlyargs or []):
            if node.arg == p["name"]:
                ann = node.annotation
                break
        spec = _parse_ann(ann, ctx)
        param_specs[p["name"]] = {"spec": spec, "has_default": p["has_default"]}
        if spec["kind"] == "unknown":
            hand_tune.append({"where": f"param {p['name']}",
                              "reason": spec.get("reason", spec.get("text", "unknown type"))})
        if spec["kind"] == "dict" and spec["key"]["kind"] not in ("str", "int", "float", "any", "unknown"):
            hand_tune.append({"where": f"param {p['name']} (dict keys)",
                              "reason": f"dict keys of kind {spec['key']['kind']!r} cannot round-trip through JSON"})
    for note in notes:
        hand_tune.append({"where": "signature", "reason": note})
    result_spec = _parse_ann(fn.returns, ctx) if fn.returns else None
    if result_spec is not None and result_spec["kind"] == "unknown":
        warnings.append(f"unit {uid}: return annotation {result_spec.get('text')!r} "
                        "is not JSON-expressible — the adapters fall back to an honest "
                        "_jsonify walk (unserializable outputs judge incomparable)")

    if set(param_specs) == {"i", "input"}:
        warnings.append(f"unit {uid}: {fn.name}() takes exactly params 'i' and 'input' — "
                        "its rows are indistinguishable from pre-wrapped framing and "
                        "oracle.py will refuse the corpus (issue #29); rename or hand-tune")

    invariants = sorted(set((contract or {}).get("invariants") or []))
    if contract is not None and not any(fn.name in s for s in contract.get("type_signatures", [])):
        warnings.append(f"unit {uid}: chosen function {fn.name!r} does not appear in "
                        f"{args.contract} type_signatures — contract and source disagree")

    stem = _sanitize_stem(uid)
    module_candidates = import_candidates_of(u["python_path"])
    rel = src.resolve()
    try:
        rel = rel.relative_to(root)
    except ValueError:
        pass
    raw_specs = {n: info["spec"] for n, info in param_specs.items()}
    ref_text = _render_adapter(side="ref", unit=uid, function=fn.name,
                               param_specs=raw_specs, result_spec=result_spec,
                               hand_tune=hand_tune, project_root=root,
                               module_candidates=module_candidates,
                               unit_path=rel.as_posix(),
                               candidate_module=args.candidate_module)
    cand_text = _render_adapter(side="cand", unit=uid, function=fn.name,
                                param_specs=raw_specs, result_spec=result_spec,
                                hand_tune=hand_tune, project_root=root,
                                module_candidates=module_candidates,
                                unit_path=rel.as_posix(),
                                candidate_module=args.candidate_module)
    ref_sha, cand_sha = _shared_adapter_sha256(ref_text, cand_text)
    edge_rows = _edge_rows(param_specs)
    gen_text = _render_generator(unit=uid, function=fn.name, specs=param_specs,
                                 edge_rows=edge_rows, seed=args.seed,
                                 rows=args.rows, invariants=invariants)

    files = {
        "ref_adapter": out / "adapters" / f"{stem}_ref.py",
        "cand_adapter": out / "adapters" / f"{stem}_cand.py",
        "generator": out / "corpora" / f"gen_{stem}.py",
        "corpus": out / "corpora" / f"corpus-{uid}.jsonl",
        "manifest": out / "manifests" / f"{uid}.json",
    }
    texts = {k: (ref_text if k == "ref_adapter" else
                 cand_text if k == "cand_adapter" else
                 gen_text if k == "generator" else None)
             for k in files}
    for k, p in files.items():
        targets[p] = (uid, k)

    manifest = {
        "unit": uid,
        "reference": {"entry": f"../adapters/{stem}_ref.py:run"},
        "candidate": {"entry": f"../adapters/{stem}_cand.py:run"},
        "corpus": f"../corpora/corpus-{uid}.jsonl",
        "scaffold": {
            "tool": TOOL, "version": VERSION, "seed": args.seed,
            "function": fn.name, "function_source": how,
            "candidate_module": args.candidate_module,
            "adapters_shared_sha256": ref_sha,
        },
    }
    return {
        "unit": uid, "python_path": u["python_path"],
        "module": module_name_of(u["python_path"]),
        "function": fn.name, "function_source": how,
        "parameters": param_specs, "result": result_spec,
        "files": {k: str(p) for k, p in files.items()},
        "texts": texts, "manifest": manifest, "hand_tune": hand_tune,
        "adapters": {"symmetric": ref_sha == cand_sha,
                     "shared_sha256": ref_sha},
        "edge_rows": len(edge_rows),
        "candidate_module": args.candidate_module,
    }


def _write_unit(result: dict) -> None:
    for key, path in ((k, Path(v)) for k, v in result["files"].items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        if key in ("ref_adapter", "cand_adapter", "generator"):
            path.write_text(result["texts"][key], encoding="utf-8")
    write_json_atomic(result["files"]["manifest"], result["manifest"])


def _generate_corpus(result: dict, seed: int, rows: int) -> dict:
    """Import the just-written generator and produce the corpus in-process
    (pure functions only — nothing prints). Returns generator metadata."""
    gen_path = Path(result["files"]["generator"])
    spec = importlib.util.spec_from_file_location(
        f"_scaffold_gen_{gen_path.stem}", gen_path)
    mod = importlib.util.module_from_spec(spec)
    saved = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # keep __pycache__ out of runs/
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = saved
    all_rows = mod.build_rows(seed, rows)
    digest = mod.write_corpus(result["files"]["corpus"], all_rows)
    return {"rows": len(all_rows), "edge_rows": result["edge_rows"],
            "random_rows": rows, "sha256": digest,
            "flagged_params": mod.flagged_params()}


def _dry_run_corpus(result: dict, out_dir: Path, timeout: float) -> dict:
    """Run the fresh corpus against the reference adapter through
    oracle.py's own subprocess runner — a corpus that would judge hollow
    (issue #30) or a malformed format (issue #29) is a finding here."""
    import oracle  # same-dir composition, like bench.py importing the comparator

    from _common import read_jsonl
    values = [v for _, v in read_jsonl(result["files"]["corpus"])]
    recs = oracle._execute_entry_subprocess(
        f"{result['files']['ref_adapter']}:run", values, out_dir, timeout)
    setup_error = oracle._first_setup_error(recs)
    if setup_error is not None:
        # the reference side failed to run at all — the corpus could not be
        # validated; report it as its own finding kind, never as row noise
        return {"setup_error": setup_error, "counts": {}, "rows": [],
                "reference_ok": 0, "reference_ok_fraction": None,
                "degenerate": True, "warning": None, "first_errors": []}
    rows = [oracle._corpus_row_status(i, rec) for i, rec in enumerate(recs)]
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    gt = oracle.reference_ground_truth(recs, len(rows), oracle.merge_rules({}))
    return {
        "rows": rows, "counts": counts,
        "reference_ok": gt["reference_ok"],
        "reference_ok_fraction": gt["reference_ok_fraction"],
        "degenerate": gt["degenerate"],
        "warning": gt["warning"],
        "first_errors": [r for r in rows if r["status"] == "error"][:3],
    }


def cmd_scaffold(args) -> int:
    if not (args.state or args.state_pos):
        raise ToolError("a migration-state.json path is required (positional or --state)",
                        kind="usage")
    state_path = Path(args.state or args.state_pos)
    state = read_json(state_path)
    units = state.get("units", [])
    if not units:
        raise ToolError("state file has no units — run port_plan.py first", kind="usage")
    root = Path(args.root or ".").resolve()
    project_root = state_path.resolve().parent.parent
    out = Path(args.out).resolve() if args.out else Path("runs").resolve()

    warnings: list[str] = []
    if "runs" not in out.parts:
        warnings.append(
            f"output {out} is not under a runs/ directory: scaffolded adapters/"
            "corpora name the target project's private code and runs/ is "
            "gitignored precisely so they never reach the repo (issue #32)")

    wanted: list[str] = []
    for chunk in args.unit or []:
        wanted.extend(p.strip() for p in chunk.split(",") if p.strip())
    if wanted:
        known = {u["id"] for u in units}
        unknown = [w for w in wanted if w not in known]
        if unknown:
            raise ToolError(f"unknown unit id(s) {unknown}; state has {sorted(known)}",
                            kind="usage")
        selected = [u for u in units if u["id"] in wanted]
    else:
        selected = units

    # analyze every unit first, then refuse-before-write: hand-tuned
    # scaffolds must not be half-overwritten
    targets: dict = {}
    results = [_scaffold_unit(u, root, project_root, out, args, targets, warnings)
               for u in selected]
    existing = sorted(str(p) for p in targets if Path(p).exists())
    if existing and not args.force:
        raise ToolError(
            "refusing to overwrite existing scaffold output (hand-tuned edits "
            f"would be lost): {'; '.join(existing)} — pass --force to regenerate",
            kind="usage")

    findings: list[dict] = []
    for result in results:
        _write_unit(result)
        corpus = _generate_corpus(result, args.seed, args.rows)
        dry = _dry_run_corpus(result, out, args.timeout)
        result["corpus"] = corpus
        result["corpus_dry_run"] = {k: v for k, v in dry.items() if k != "rows"}
        result["corpus_dry_run"]["sample_rows"] = dry["rows"][:5]
        if result["hand_tune"]:
            findings.append({"unit": result["unit"], "kind": "hand_tune",
                             "items": result["hand_tune"]})
        if dry.get("setup_error"):
            findings.append({
                "unit": result["unit"], "kind": "reference_setup_error",
                "detail": f"the reference adapter failed to run, so the corpus "
                          f"could not be validated: {dry['setup_error']}"})
        elif dry["degenerate"]:
            findings.append({
                "unit": result["unit"], "kind": "degenerate_corpus",
                "detail": "the reference adapter errors on every corpus row — a judge "
                          "run would be hollow/INCOMPARABLE (issue #30); fix the corpus "
                          "or the flagged parameters"})
        elif dry["warning"]:
            warnings.append(f"unit {result['unit']}: {dry['warning']}")
        for status, n in sorted(dry["counts"].items()):
            if status != "ok" and n:
                warnings.append(f"unit {result['unit']}: {n}/{len(dry['rows'])} "
                                f"reference dry-run rows {status} (some error-path "
                                "rows are coverage; a wall of identical errors is not)")
        del result["texts"]

    code = EXIT_FINDINGS if findings else EXIT_OK
    ok_units = sum(1 for r in results if not r["hand_tune"])
    hand_tune_n = sum(len(r["hand_tune"]) for r in results)
    human = (f"scaffolded {len(results)} unit(s) under {out} (seed {args.seed}): "
             f"{ok_units} fully generated, {hand_tune_n} hand-tune flag(s)"
             + (f"; {len(findings)} finding(s)" if findings else "")
             + ("; FAIL" if findings else ""))
    emit({
        "tool": TOOL, "version": VERSION,
        "out": str(out), "seed": args.seed, "random_rows": args.rows,
        "units": results,
        "findings": findings,
        "warnings": warnings,
        "summary": {"exit_code": code, "human": human},
    }, human_flag=args.human)
    return code


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=TOOL, description=__doc__.splitlines()[0])
    ap.add_argument("--human", action="store_true", help="echo human summary to stderr")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scaffold", help="per unit: symmetric ref/cand adapters + "
                                        "seeded corpus generator + judge-ready manifest "
                                        "(issue #32)")
    p.add_argument("state_pos", nargs="?", help="migration-state.json (or use --state)")
    p.add_argument("--state", help="migration-state.json (same as the positional)")
    p.add_argument("--unit", action="append",
                   help="unit id to scaffold (repeatable / comma-separated; "
                        "default: every unit)")
    p.add_argument("--out", default="runs",
                   help="output root (default runs/, gitignored — private names "
                        "must not reach the repo)")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED,
                   help=f"corpus seed baked into the generator (default {DEFAULT_SEED}); "
                        "the reviewer regenerates fresh inputs with a different seed")
    p.add_argument("--rows", type=int, default=DEFAULT_RANDOM_ROWS,
                   help=f"random rows beyond the edge rows (default {DEFAULT_RANDOM_ROWS})")
    p.add_argument("--root", help="target project root (default: cwd)")
    p.add_argument("--contract",
                   help="optional contract.json file (or the contracts/ dir from "
                        "contract_extract.py --out) — invariants are copied into the "
                        "generator and the function choice is cross-checked")
    p.add_argument("--function", help="entry function to wrap (default: the "
                                      "module-stem-named public function, else the first)")
    p.add_argument("--candidate-module",
                   help="PyO3 wheel module for the candidate adapter (default: a "
                        "HAND-TUNE placeholder — the wheel is unknown at Stage 2)")
    p.add_argument("--force", action="store_true",
                   help="overwrite existing scaffold output (hand-tuned edits are lost)")
    p.add_argument("--timeout", type=float, default=DRY_RUN_TIMEOUT,
                   help="per-side timeout for the corpus dry-run (s)")
    p.set_defaults(func=cmd_scaffold)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(run_cli(main, tool=TOOL, version=VERSION))
