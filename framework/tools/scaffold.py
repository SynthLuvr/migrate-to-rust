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

Mode — module conformance by default (issue #48)
--------------------------------------------------
A unit's contract is its *whole public API* (hard rule 2; Stage-4 exit
criteria are per unit), so the default ``--mode module`` scaffold emits ONE
adapter pair + corpus + manifest that judges every public function in the
unit: the adapter calls each judged function per corpus row and returns a
``{function: result | {"__error__": {"type", "message"}}}`` envelope — a
function's own raised exception is per-function evidence compared by the
oracle's value rules, not a failed row; a first-public-only verdict can no
longer green-light a unit whose other functions diverge. Rows are JSON
objects over the UNION of the judged functions' parameters (each function
takes the row's values for its own parameters, rebuilt through its own
spec); ``--function`` (repeatable / comma-separated) narrows the judged
subset. A parameter name annotated differently across functions is flagged
for hand-completion — never guessed. ``--mode function`` keeps the legacy
single-entry scaffold (one function, raw result, no envelope).

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
                              accepts since issue #32); in the default
                              module mode it judges the unit's WHOLE
                              public API via the envelope (issue #48)

``<stem>`` is the unit id with ``-``/``.`` sanitized to ``_`` so file-path
entries and importlib behave.

Heuristic mapping (signature annotation -> generator/rebuilder):
  str/int/float/bool/None/Any, Literal  -> JSON-native generators
  list/set/frozenset/dict/tuple, the    -> recursive generators; the adapter
  Sequence/Mapping/Iterable aliases       rebuilds real list/set/dict/tuple
  Optional[X] / X | None / X | Y         -> optional / union
  list/tuple series parameters          -> realistic lengths + content
                                          (issue #49): random-walk series
                                          of 60-320 elements dominate —
                                          coherent OHLCV bars (high/low
                                          bracket the bar, open chains to
                                          the previous close) for open/
                                          high/low/close dataclass items,
                                          walked floats/ints otherwise —
                                          with a short-length minority
                                          kept as deliberate error rows;
                                          0-9 element lists left series
                                          functions (52+ bars, 63+
                                          values) erroring on nearly
                                          every row
  @dataclass in the unit module or any   -> rows carry a {field: value}
  project module it imports                dict; the adapter calls the real
  constructor (nested dataclasses recurse)
  anything else — including inside a    -> flagged for HAND-COMPLETION,
  list or a dataclass field               never guessed: the parameter
                                          generates None/JSON-any
                                          stand-ins, the reason is in the
                                          payload's hand_tune[] and in a
                                          HAND-TUNE block in BOTH adapters

Symmetry is by construction: both adapters are rendered from one template
and differ ONLY inside the delimited BACKEND section; the payload records
the sha256 of the shared remainder as evidence (identical on both sides).

Derived inputs from dependency modules (issue #50): a parameter typed by a
dataclass the unit module does not define — ``list[Bar]`` where ``Bar``
comes from the unit's ``u-bars`` dependency — is recorded as a derived
input (``derived_inputs[]`` in the payload and the manifest's scaffold
block: name, module, path, the declared ``deps[]`` unit it maps to, the
params that use it) and resolved on EACH SIDE'S OWN dependency modules:
the reference adapter imports the python dependency module the spec
records (the unit's imports — the unit need not re-export anything), the
candidate adapter resolves the WHEEL'S OWN class — a flat wheel re-exports
its dependencies in the wheel module itself; a split wheel gets its
spellings hand-tuned into the candidate-only ``_WHEEL_DEP_MODULES`` table
— never the python project, which pre-#50 adapters silently did (the
wheel was fed reference-built objects: an asymmetric adapter, exactly
what the symmetry design exists to prevent, and it surfaced only as an
oracle divergence). Both adapters resolve every derived input at IMPORT
time, so a side missing a class is an honest setup error naming it —
never silent ``[]`` nor per-row noise masquerading as divergence; a
dependency module that is neither the unit's own nor in its recorded
``deps[]`` is called out in the warnings (the Stage-1 record may be
stale, or the module is shared/unported).

Self-check before reporting success: the freshly generated corpus is
dry-run against the reference adapter through oracle.py's own subprocess
runner, so a corpus that would judge hollow (all-error reference — issue
#30), thin (reference ok below min_reference_ok_fraction — issue #49), or
malformed (issue #29) is a finding here, in seconds. In module mode the
dry-run also tallies per-function envelope errors: every judged function
erroring on every row is a degenerate-corpus finding, and a single
all-error function is a warning (its parity is unjudged).

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
VERSION = 4
DEFAULT_SEED = 20261028
DEFAULT_RANDOM_ROWS = 96
DRY_RUN_TIMEOUT = 120.0

JSON_SCALARS = {"str": "str", "int": "int", "float": "float", "bool": "bool"}
JSON_DICT_KEYS = {"str", "int", "float", "any", "unknown"}  # round-trip through JSON object keys
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
        names = [a for a in node.names if a.name != "*"]
        if node.level == 0:
            if node.module:
                for a in names:
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
        for a in names:
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


def _select_module_functions(tree: ast.Module, overrides: list[str]):
    """The public functions a module-conformance scaffold judges (issue
    #48): all of them, or the --function subset. Unknown names are a usage
    error listing what exists, not silence. Returns (functions, how) with
    how ∈ all-public | flag."""
    fns = _public_functions(tree)
    known = sorted(f.name for f in fns)
    if overrides:
        unknown = [o for o in overrides if o not in known]
        if unknown:
            raise ToolError(f"--function {unknown} is not a public function in "
                            f"the unit module; public functions: {known}",
                            kind="usage")
        wanted = set(overrides)
        return [f for f in fns if f.name in wanted], "flag"
    if not fns:
        raise ToolError("unit module has no public module-level function to scaffold "
                        "(classes and private helpers are not oracle entries)", kind="usage")
    return fns, "all-public"


def _spec_text(spec: dict) -> str:
    return spec.get("text") or spec["kind"]


def _union_of(fn_specs: dict[str, dict], hand_tune: list[dict]) -> dict:
    """{function: {param: {"spec", "has_default"}}} -> the corpus-level
    union {param: {"spec", "required"}}. ``required`` = at least one
    declaring function requires it, so corpus rows must always carry it
    (omitting the rest exercises defaults). A name annotated differently
    across functions becomes kind unknown + a hand-tune flag — one JSON
    row cannot mechanically feed conflicting annotations; never guessed."""
    decls: dict[str, list] = {}
    for fn, specs in fn_specs.items():
        for name, info in specs.items():
            decls.setdefault(name, []).append((fn, info))
    union: dict[str, dict] = {}
    for name, entries in decls.items():
        spec = entries[0][1]["spec"]
        differing = [(fn, info) for fn, info in entries[1:]
                     if info["spec"] != spec]
        if differing:
            spellings = ", ".join(f"{fn}: {_spec_text(info['spec'])}"
                                  for fn, info in entries)
            spec = _spec("unknown", text=name,
                         reason=f"annotated differently across functions ({spellings})")
            hand_tune.append({
                "where": f"union param {name}",
                "reason": f"one JSON row cannot feed conflicting annotations "
                          f"({spellings}) — hand-complete",
            })
        union[name] = {"spec": spec,
                       "required": any(not info["has_default"] for _, info in entries)}
    return union


def _signature_params(fn) -> tuple[dict[str, dict], list[str]]:
    """{param name: {"annotation": node or None, "has_default": bool}} for
    positional and keyword-only params, plus hand-tune notes for *args /
    **kwargs (which JSON kwargs rows cannot express)."""
    a = fn.args
    params: dict[str, dict] = {}
    pos = a.posonlyargs + a.args
    defaults = [None] * (len(pos) - len(a.defaults)) + a.defaults
    for arg, default in zip(pos, defaults):
        params[arg.arg] = {"annotation": arg.annotation, "has_default": default is not None}
    for arg, default in zip(a.kwonlyargs, a.kw_defaults):
        params[arg.arg] = {"annotation": arg.annotation, "has_default": default is not None}
    notes = []
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
    if k == "dict":
        return {}
    if k in ("list", "set"):
        return []
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

@@PROTOCOL@@

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
@@ENTRY_DECLS@@

# ==== BACKEND — the ONLY section that differs between ================
# ==== _ref.py and _cand.py ================================================

@@BACKEND@@

# ==== END BACKEND =========================================================


def _ensure_roots():
    """PROJECT_ROOT and its src/ layout on sys.path (issue #26)."""
    for r in (PROJECT_ROOT, os.path.join(PROJECT_ROOT, "src")):
        if os.path.isdir(r) and r not in sys.path:
            sys.path.insert(0, r)


def _resolve_module(rel_path: str, dotted: str):
    """Import a project module by dotted name, falling back to loading the
    file by path. Used for the reference unit and for dataclasses."""
    _ensure_roots()
    try:
        return importlib.import_module(dotted)
    except ImportError:
        p = os.path.join(PROJECT_ROOT, rel_path)
        spec = importlib.util.spec_from_file_location(dotted.replace(".", "_"), p)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod


def _walk_dc_specs(spec, out, seen):
    """Collect the dataclass specs nested in a spec (dataclass fields
    recurse), deduped by defining module+name (issue #50)."""
    if not isinstance(spec, dict):
        return
    if spec.get("kind") == "dataclass":
        key = (spec.get("module"), spec.get("name"))
        if key not in seen:
            seen.add(key)
            out.append(spec)
    for k in ("item", "key", "value"):
        _walk_dc_specs(spec.get(k), out, seen)
    for it in spec.get("items") or []:
        _walk_dc_specs(it, out, seen)
    for f in spec.get("fields") or []:
        _walk_dc_specs(f.get("spec"), out, seen)


def _precheck_dc_classes(specs):
    """Resolve every dataclass the judged parameters need, on THIS side,
    at import time (issue #50): a dependency class this side cannot
    resolve becomes a setup error oracle.py reports honestly (exit 4),
    not per-row error noise masquerading as divergence. _dc_class is per
    side — see the BACKEND section."""
    found, seen = [], set()
    for spec in specs:
        _walk_dc_specs(spec, found, seen)
    for spec in found:
        _dc_class(spec)


def _require_exports(mod, names, owner, remedy):
    """Every judged export must exist — a missing one is a setup error
    naming it (issue #48), never a silent gap in the verdict."""
    missing = [n for n in names if not hasattr(mod, n)]
    if missing:
        raise ImportError(f"{owner} exposes no {missing} — {remedy}")
    return {n: getattr(mod, n) for n in names}


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


@@RUN_DEF@@

@@LOAD_CALL@@
'''

# ---- mode-specific fragments rendered into ADAPTER_TEMPLATE (issue #48) --

PROTOCOL_FUNCTION = '''Protocol: the oracle entry is "<this file>:run". A corpus row is one JSON
object mapping @@FUNCTION@@'s parameter names to JSON values (exactly what
corpora/gen_@@STEM@@.py emits); run() rebuilds the typed Python objects
the signature requires (specs below), calls the function, and serializes
the result back to JSON — canonicalizing non-JSON shapes (set -> sorted
list, tuple -> list, dataclass -> dict) so the oracle can compare them.'''

PROTOCOL_MODULE = '''Protocol: the oracle entry is "<this file>:run" — MODULE CONFORMANCE
(issue #48): a unit's contract is its whole public API, so run() judges
EVERY function below per corpus row, not just a first-public pick. A row
is one JSON object over the UNION of the judged functions' parameter
names; each function is called with the row's values for its own
parameters (rebuilt through its own spec) and the row's result is the
envelope {function: result | {"__error__": {"type", "message"}}}: a
function's own raised exception is per-function evidence compared by the
oracle's value rules, not a failed row. A row key no judged function
declares, a union parameter missing where some function requires it, or a
value outside the union spec is a corpus bug and RAISES from run() (as
does an output serialize() cannot express — the honest incomparable,
never a per-function error).'''

ENTRY_DECLS_FUNCTION = '''FUNCTION = @@FUNCTION_REPR@@
# parameter -> generation/rebuild spec (from the contract's type signature)
PARAM_SPECS = @@PARAM_SPECS@@
# return-annotation spec; None = unannotated (honest _jsonify fallback)
RESULT_SPEC = @@RESULT_SPEC@@'''

ENTRY_DECLS_MODULE = '''# judged public function -> {"params": {param: spec}, "result": spec|None};
# one manifest judges the unit's WHOLE public API (issue #48)
FUNCTIONS = @@FUNCTIONS@@
# union of parameter names across FUNCTIONS; required = at least one
# function requires it, so corpus rows must always carry it (the rest may
# be omitted to exercise a default). A name annotated differently across
# functions is flagged unknown — never guessed.
UNION_SPECS = @@UNION_SPECS@@'''

RUN_DEF_FUNCTION = '''def run(row):
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
    return serialize(result, RESULT_SPEC)'''

RUN_DEF_MODULE = '''def run(row):
    """One corpus row ({param: value} over the UNION of the judged
    functions' parameters) -> the module-conformance envelope
    {function: result | {"__error__": ...}}. Oracle entry point (issue
    #48: judge the unit's whole public API, not one function)."""
    if not isinstance(row, dict):
        raise TypeError("corpus row must be a JSON object of {param: value}, got "
                        + type(row).__name__)
    unknown = sorted(set(row) - set(UNION_SPECS))
    if unknown:
        raise TypeError(f"corpus row key(s) {unknown} are not parameters of any "
                        f"judged function ({', '.join(UNION_SPECS)})")
    missing = sorted(n for n, s in UNION_SPECS.items()
                     if s["required"] and n not in row)
    if missing:
        raise TypeError(f"corpus row is missing required parameter(s) {missing} — "
                        "a function cannot be called without them")
    for name in sorted(row):  # union-spec validation: a corpus-format bug
        rebuild(row[name], UNION_SPECS[name]["spec"], f"$.{name}")  # raises
    envelope = {}
    for name, info in FUNCTIONS.items():
        try:
            kwargs = {p: rebuild(row[p], spec, f"$.{name}.{p}")
                      for p, spec in info["params"].items() if p in row}
            result = FNS[name](**kwargs)
            if inspect.iscoroutine(result):  # async entries, symmetric
                result = asyncio.run(result)
        except Exception as e:  # the function's own error path is evidence
            envelope[name] = {"__error__": {"type": type(e).__name__,
                                            "message": str(e)}}
            continue
        # serialize outside the except: an output the adapter cannot express
        # is incomparable, never the unit's error
        envelope[name] = serialize(result, info["result"])
    return envelope'''

LOAD_CALL_FUNCTION = '''FN = _load_function()  # a failure here is a setup error for this side —
                       # oracle.py reports it honestly (exit 4) instead of
                       # judging half an adapter
_precheck_dc_classes(PARAM_SPECS.values())  # derived inputs (issue #50)'''

LOAD_CALL_MODULE = '''FNS = _load_functions()  # a failure here is a setup error for this side —
                         # oracle.py reports it honestly (exit 4) instead of
                         # judging half an adapter
_precheck_dc_classes([info["spec"] for info in UNION_SPECS.values()])  # issue #50'''

# The BACKEND sections are composed per side: each side's constants and
# import dance (_load_module) are shared between function and module mode;
# only the loader tail differs (one function vs every judged export).

REF_HEADER = '''# Reference side: import the ORIGINAL python unit. PROJECT_ROOT is where
# scaffold.py ran; override with MIGRATE_PROJECT_ROOT to relocate the run.
PROJECT_ROOT = os.environ.get("MIGRATE_PROJECT_ROOT", @@ROOT_REPR@@)
_MODULE_CANDIDATES = @@MODULE_CANDIDATES@@  # layout-stripped + src-prefixed (issue #26)
_UNIT_PATH = @@UNIT_PATH_REPR@@             # project-relative, for the file fallback
'''

CAND_HEADER = '''# Candidate side: import the PyO3 wheel. The wheel's module name is NOT
# knowable at Stage 2 scaffold time — set CANDIDATE_MODULE below (or re-run
# scaffold.py with --candidate-module <module> --force at Stage 4) before
# judging. Until then importing this adapter is a setup error, which
# oracle.py reports honestly instead of judging half an adapter.
CANDIDATE_MODULE = @@CANDIDATE_MODULE@@  # e.g. "mycrate.units.u_bars"
PROJECT_ROOT = os.environ.get("MIGRATE_PROJECT_ROOT", @@ROOT_REPR@@)
'''

REF_IMPORT = '''

def _load_module():
    """Each dotted spelling of the unit (issue #26), then the file
    fallback."""
    _ensure_roots()
    for name in _MODULE_CANDIDATES:
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    return _resolve_module(_UNIT_PATH, _MODULE_CANDIDATES[0])
'''

CAND_IMPORT = '''

def _load_module():
    """The wheel is normally INSTALLED; the source-tree fallback (via
    _ensure_roots) only serves symmetry checks and pre-wheel smoke tests —
    it can never shadow an installed wheel."""
    if not CANDIDATE_MODULE:
        raise NotImplementedError(
            "scaffold.py: candidate module not set — the PyO3 wheel is unknown "
            "at Stage 2. Set CANDIDATE_MODULE (or re-run scaffold.py "
            "--candidate-module <wheel-module> --force), then re-judge (issue #32).")
    try:
        return importlib.import_module(CANDIDATE_MODULE)
    except ImportError:
        _ensure_roots()
        return importlib.import_module(CANDIDATE_MODULE)
'''

REF_DC = '''

def _dc_class(spec):
    """Reference side: a derived-input dataclass (issue #50) resolves to
    the python dependency module the spec records (the unit's imports —
    the unit need not re-export its dependency's types), never the unit
    under test's namespace."""
    mod = _resolve_module(spec["path"], spec["module"])
    try:
        return getattr(mod, spec["name"])
    except AttributeError as e:
        raise ImportError(f"reference dependency module {spec['module']!r} "
                          f"exposes no dataclass {spec['name']!r} — the unit's "
                          "signature imports it (issue #50)") from e
'''

CAND_DC = '''

# Split-wheel spellings that may define the unit's dependency types —
# HAND-TUNE only when the wheel is NOT flat (issue #50).@@DEP_NOTE@@
_WHEEL_DEP_MODULES = @@WHEEL_DEP_MODULES@@


def _dc_class(spec):
    """Candidate side: a derived-input dataclass (issue #50) must be the
    WHEEL'S OWN class, never the python project's — pre-#50 adapters
    resolved the python module on both sides, so the wheel was fed
    reference-built objects: an asymmetric adapter, exactly what the
    symmetry design exists to prevent. A flat wheel re-exports its
    dependencies in the wheel module itself (first lookup); a split
    wheel lists the defining spellings in _WHEEL_DEP_MODULES above."""
    cls = getattr(_load_module(), spec["name"], None)
    if isinstance(cls, type):
        return cls
    for dotted in _WHEEL_DEP_MODULES:
        try:
            mod = importlib.import_module(dotted)
        except ImportError:
            continue
        cls = getattr(mod, spec["name"], None)
        if isinstance(cls, type):
            return cls
    raise ImportError(
        f"candidate wheel {CANDIDATE_MODULE!r} exposes no {spec['name']!r} and "
        "no _WHEEL_DEP_MODULES spelling defines it — the port must export the "
        "dependency types the unit consumes (a flat wheel re-exports them) or "
        "the spelling must be hand-tuned (issue #50)")
'''

REF_FUNCTION_TAIL = '''

def _load_function():
    try:
        return getattr(_load_module(), FUNCTION)
    except AttributeError as e:
        raise ImportError(f"reference module {_MODULE_CANDIDATES[0]} exposes no "
                          f"{FUNCTION!r}") from e
'''

CAND_FUNCTION_TAIL = '''

_CANDIDATE_FUNCTION = @@FUNCTION_REPR@@  # wheel export name; rename if it differs


def _load_function():
    return getattr(_load_module(), _CANDIDATE_FUNCTION)
'''

REF_MODULE_TAIL = '''

def _load_functions():
    # module conformance (issue #48): every judged export, none missing
    return _require_exports(_load_module(), FUNCTIONS,
                            f"reference module {_MODULE_CANDIDATES[0]}",
                            "the unit's whole public API is the "
                            "contract (issue #48)")
'''

CAND_MODULE_TAIL = '''

def _load_functions():
    # module conformance (issue #48): every judged export, none missing
    return _require_exports(_load_module(), FUNCTIONS,
                            f"candidate module {CANDIDATE_MODULE}",
                            "the port must export the unit's whole "
                            "public API (issue #48)")
'''

REF_BACKEND = REF_HEADER + REF_IMPORT + REF_DC + REF_FUNCTION_TAIL
CAND_BACKEND = CAND_HEADER + CAND_IMPORT + CAND_DC + CAND_FUNCTION_TAIL
REF_MODULE_BACKEND = REF_HEADER + REF_IMPORT + REF_DC + REF_MODULE_TAIL
CAND_MODULE_BACKEND = CAND_HEADER + CAND_IMPORT + CAND_DC + CAND_MODULE_TAIL

GENERATOR_TEMPLATE = '''#!/usr/bin/env python3
"""gen_@@STEM@@.py — seeded corpus generator for unit @@UNIT@@ (issue #32).

Deterministic per --seed: identical seed -> byte-identical corpus (one
random.Random(seed), fixed pools, sorted keys). The reviewer's fresh-inputs
re-judgment (Stage 4 step 8) regenerates with a DIFFERENT seed:

    python gen_@@STEM@@.py --seed <fresh> --out corpus-fresh.jsonl

Rows are the documented corpus format: one raw JSON value per line = one
input, a JSON object of {param: value} for @@SURFACE@@ — NEVER pre-wrapped
in {"i", "input"} framing; oracle.py adds that envelope itself (issue #29).
Edge rows cover zero-values and signature boundaries (often the documented
error path); random rows follow the baked-in specs.

List/tuple parameters draw REALISTIC series lengths (issue #49):
random-walk series of 60-320 elements dominate — coherent OHLCV bars for
open/high/low/close dataclass items, walked floats/ints otherwise — with
a short-length minority kept as deliberate error rows (0-9 element lists
left min-length functions erroring on nearly every row, far below the
oracle's min_reference_ok_fraction).

@@INVARIANTS@@Flagged parameters generate None until hand-completed:
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
@@ENTRY_CONST@@
DEFAULT_SEED = @@SEED@@
DEFAULT_ROWS = @@ROWS@@
# {param: {"spec": <generation spec>, "has_default": bool}} — from the
# contract's type signature via scaffold.py (module mode: the UNION of the
# judged functions' parameters; has_default = defaulted in every function
# declaring it, so omitting it is always safe — issue #48)
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
# series parameters need realistic depth (issue #49): 0-9 element lists
# left min-length functions (52+ bars, 63+ values) erroring on nearly
# every row — keep a short minority as deliberate error/boundary rows
_SHORT_SIZES = [0, 1, 1, 2, 2, 3, 4, 6, 9]
_SERIES_MIN = 60
_SERIES_MAX = 320


def g_len(rng):
    """Series length: realistic lengths dominate so min-length functions
    run on most rows; the short pool stays as deliberate error/boundary
    coverage (issue #49)."""
    if rng.random() < 0.2:
        return rng.choice(_SHORT_SIZES)
    return rng.randint(_SERIES_MIN, _SERIES_MAX)


def g_walk_floats(rng, n):
    """Coherent random-walk price-like series: finite, positive, JSON-clean
    (realistic input, not iid noise — issue #49)."""
    out, x = [], rng.uniform(1.0, 1000.0)
    for _ in range(n):
        x = max(round(x + x * rng.uniform(-0.05, 0.05), 6), 0.01)
        out.append(x)
    return out


def g_walk_ints(rng, n):
    """Coherent integer random walk (counts/indices, clamped at zero)."""
    out, x = [], rng.randint(0, 1000)
    for _ in range(n):
        x = max(x + rng.randint(-3, 3), 0)
        out.append(x)
    return out


def _ohlcv_fields(item):
    """{field name: field} of an OHLCV-shaped dataclass item (open/high/
    low/close all present — volume and extras optional), else None."""
    if item.get("kind") != "dataclass":
        return None
    fields = {f["name"]: f for f in item["fields"]}
    return fields if {"open", "high", "low", "close"} <= set(fields) else None


def _ohlcv_bar(rng, close, fields):
    """One bar chained from the previous close: high/low bracket the bar,
    so the intra-row relationships hold by construction. Volume and extra
    fields stay generatable per their own specs."""
    open_ = close
    close = max(round(open_ * (1.0 + rng.uniform(-0.05, 0.05)), 6), 0.01)
    high = round(max(open_, close) * (1.0 + rng.uniform(0.0, 0.02)), 6)
    low = max(round(min(open_, close) * (1.0 - rng.uniform(0.0, 0.02)), 6), 0.01)
    ohlc = {"open": open_, "high": high, "low": low, "close": close}
    bar = {}
    for name, field in fields.items():
        spec = field["spec"]
        if name in ohlc and spec["kind"] in ("int", "float"):
            bar[name] = int(ohlc[name]) if spec["kind"] == "int" else ohlc[name]
        elif name == "volume" and spec["kind"] == "int":
            bar[name] = rng.randint(0, 10 ** 6)
        elif name == "volume" and spec["kind"] == "float":
            bar[name] = round(rng.uniform(0.0, 10.0 ** 6), 6)
        elif field.get("has_default") and rng.random() < 0.3:
            continue  # exercise the dataclass default
        else:
            bar[name] = gen(spec, rng)
    return bar, close


def g_ohlcv(rng, n, fields):
    """Coherent random-walk bars (issue #49): open chains to the previous
    close, close is walked, high/low bracket the bar."""
    bars, close = [], rng.uniform(10.0, 500.0)
    for _ in range(n):
        bar, close = _ohlcv_bar(rng, close, fields)
        bars.append(bar)
    return bars


def g_series(item, rng, n, depth):
    """A realistic n-element series of an item spec (issue #49): coherent
    OHLCV bars for OHLCV-shaped dataclass items, random-walk floats/ints
    for scalar items, recursive generation otherwise."""
    fields = _ohlcv_fields(item)
    if fields is not None:
        return g_ohlcv(rng, n, fields)
    if item["kind"] == "float":
        return g_walk_floats(rng, n)
    if item["kind"] == "int":
        return g_walk_ints(rng, n)
    return [gen(item, rng, depth + 1) for _ in range(n)]


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
        return g_series(spec["item"], rng, g_len(rng), depth)
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
            return g_series(spec["item"], rng, g_len(rng), depth)
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
    return re.sub(r"[^A-Za-z0-9_]", "_", unit_id)


def _fill_template(text: str, tokens: dict[str, str], what: str) -> str:
    """@@TOKEN@@ substitution; refuses to ship an unresolved token — a
    template bug must never become a broken artifact."""
    for token, value in tokens.items():
        text = text.replace(token, value)
    if "@@" in text:
        raise ToolError(f"internal: unresolved template token in {what}", kind="setup")
    return text


def _hand_tune_block(items: list[dict]) -> str:
    if not items:
        return " none — the signature is fully covered by the heuristic."
    return "\n" + "\n".join(f"  - {it['where']}: {it['reason']}" for it in items)


def _wheel_dep_note(derived: list[dict]) -> str:
    """The comment above _WHEEL_DEP_MODULES in the candidate adapter: the
    unit's dependency dataclasses in their python spellings, as recorded
    at scaffold time (the wheel's own spellings are unknown until it
    exists)."""
    if not derived:
        return "\n# No dependency dataclasses recorded for this unit."
    listed = ", ".join(
        f"{d['name']} ({d['module']})"
        + (f", declared dep {d['dep']}" if d["dep"] else ", UNDECLARED")
        for d in derived)
    return ("\n# Dependency dataclasses for this unit (python spellings, "
            f"recorded at scaffold time): {listed}")


def _render_adapter(*, side: str, plan: dict, unit: str, root: Path,
                    module_candidates: list[str], unit_path: str,
                    candidate_module: str | None) -> str:
    """One side of the symmetric pair (issue #48). The plan's mode picks
    the backend pair and the protocol/entry/run/load fragments: function
    mode declares one function's specs, module mode the per-function table
    plus the union of the judged parameters."""
    stem = _sanitize_stem(unit)
    if plan["mode"] == "module":
        backends = (REF_MODULE_BACKEND, CAND_MODULE_BACKEND)
        mode_tokens = {
            "@@PROTOCOL@@": PROTOCOL_MODULE,
            "@@ENTRY_DECLS@@": ENTRY_DECLS_MODULE,
            "@@RUN_DEF@@": RUN_DEF_MODULE,
            "@@LOAD_CALL@@": LOAD_CALL_MODULE,
            "@@FUNCTIONS@@": pformat(plan["functions_detail"], sort_dicts=False, width=88),
            "@@UNION_SPECS@@": pformat(plan["union_specs"], sort_dicts=False, width=88),
        }
    else:
        backends = (REF_BACKEND, CAND_BACKEND)
        scalar = {"@@FUNCTION@@": plan["function"],
                  "@@FUNCTION_REPR@@": repr(plan["function"]), "@@STEM@@": stem}

        def _frag(text: str) -> str:  # resolve fragment-local tokens up front
            for k, v in scalar.items():
                text = text.replace(k, v)
            return text
        raw_specs = {n: info["spec"] for n, info in plan["param_specs"].items()}
        mode_tokens = {
            "@@PROTOCOL@@": _frag(PROTOCOL_FUNCTION),
            "@@ENTRY_DECLS@@": _frag(ENTRY_DECLS_FUNCTION),
            "@@RUN_DEF@@": _frag(RUN_DEF_FUNCTION),
            "@@LOAD_CALL@@": LOAD_CALL_FUNCTION,
            "@@PARAM_SPECS@@": pformat(raw_specs, sort_dicts=False, width=88),
            "@@RESULT_SPEC@@": pformat(plan["result_spec"], sort_dicts=False, width=88),
        }
    if side == "ref":
        backend = (backends[0]
                   .replace("@@ROOT_REPR@@", repr(str(root)))
                   .replace("@@MODULE_CANDIDATES@@", repr(module_candidates))
                   .replace("@@UNIT_PATH_REPR@@", repr(unit_path)))
    else:
        # the wheel layout is unknown at Stage 2 (issue #50): a flat wheel
        # re-exports its dependencies in the wheel module itself; a split
        # wheel hand-tunes its spellings into _WHEEL_DEP_MODULES
        backend = (backends[1]
                   .replace("@@CANDIDATE_MODULE@@", repr(candidate_module))
                   .replace("@@ROOT_REPR@@", repr(str(root)))
                   .replace("@@WHEEL_DEP_MODULES@@", "[]")
                   .replace("@@DEP_NOTE@@",
                            _wheel_dep_note(plan.get("derived_inputs") or [])))
    if plan["mode"] == "function":
        backend = backend.replace("@@FUNCTION_REPR@@", repr(plan["function"]))
    return _fill_template(ADAPTER_TEMPLATE, {
        "@@UNIT@@": unit,
        "@@UNIT_REPR@@": repr(unit),
        "@@STEM@@": stem,
        "@@HANDTUNE_MARK@@": f" ({len(plan['hand_tune'])} item(s))" if plan["hand_tune"] else "",
        "@@HANDTUNE_LIST@@": _hand_tune_block(plan["hand_tune"]),
        "@@BACKEND@@": backend.rstrip("\n"),
        **mode_tokens,
    }, f"{side} adapter for {unit}")


def _shared_adapter_sha256(ref_text: str, cand_text: str):
    """Symmetry evidence: hash the template outside the BACKEND section.
    Both sides must agree — or the verdict would measure the adapters."""
    def shared(text: str) -> bytes:
        head, _, rest = text.partition("# ==== BACKEND")
        _, _, tail = rest.partition("# ==== END BACKEND")
        return (head + tail).encode("utf-8")
    return hashlib.sha256(shared(ref_text)).hexdigest(), \
        hashlib.sha256(shared(cand_text)).hexdigest()


def _render_generator(*, unit: str, mode: str, functions: list[str] | None,
                      specs: dict, edge_rows: list, seed: int, rows: int,
                      invariants: list[str]) -> str:
    """specs is {param: {"spec", "has_default"}} — the function's own
    parameters (function mode) or the union across the judged functions
    (module mode, has_default = defaulted in every declaring function)."""
    stem = _sanitize_stem(unit)
    flagged = sorted(n for n, info in specs.items()
                     if info["spec"]["kind"] == "unknown")
    inv_lines = "".join(f"    {line}\n" for line in invariants[:10])
    invariants_block = (f"Contract invariants (from the unit's contract.json —\n"
                        f"the edge rows should exercise them, especially the "
                        f"error paths):\n{inv_lines}\n\n" if inv_lines else "")
    if mode == "module":
        surface = (f"every judged function ({', '.join(functions)}); a row "
                   "carries the UNION of their parameters — each function "
                   "takes the row's values for its own (issue #48)")
        entry_const = (f"FUNCTIONS = {functions!r}  # judged public API "
                       "(module conformance, issue #48)")
    else:
        surface = functions[0]
        entry_const = f"FUNCTION = {functions[0]!r}"
    return _fill_template(GENERATOR_TEMPLATE, {
        "@@STEM@@": stem,
        "@@UNIT@@": unit,
        "@@UNIT_REPR@@": repr(unit),
        "@@SURFACE@@": surface,
        "@@ENTRY_CONST@@": entry_const,
        "@@SEED@@": str(seed),
        "@@ROWS@@": str(rows),
        "@@SPECS@@": pformat(specs, sort_dicts=False, width=88),
        "@@EDGE_ROWS@@": pformat(edge_rows, sort_dicts=False, width=88),
        "@@INVARIANTS@@": invariants_block,
        "@@FLAGGED@@": (", ".join(flagged) if flagged else "none"),
    }, f"generator for {unit}")


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


def _walk_spec(spec: dict, path: str = ""):
    """Yield (path, spec) for every node of a generation/rebuild spec,
    depth-first — ``bars[item]``, ``x.field``, ``d[key]``, ``u[i]``."""
    yield path, spec
    k = spec["kind"]
    if k == "dataclass":
        for f in spec["fields"]:
            yield from _walk_spec(f["spec"], f"{path}.{f['name']}")
    elif k in ("list", "set", "optional"):
        yield from _walk_spec(spec["item"], f"{path}[item]")
    elif k == "tuple" and spec.get("variadic"):
        yield from _walk_spec(spec["item"], f"{path}[item]")
    elif k in ("tuple", "union"):
        for i, it in enumerate(spec["items"]):
            yield from _walk_spec(it, f"{path}[{i}]")
    elif k == "dict":
        yield from _walk_spec(spec["key"], f"{path}[key]")
        yield from _walk_spec(spec["value"], f"{path}[value]")


def _infeasible_generation_paths(spec: dict) -> list[str]:
    """Paths at which an ``unknown`` kind sits *inside* a container or
    dataclass (``list[Frame]`` where Frame is not a @dataclass, an
    unresolvable dataclass field, ...): only None/JSON-any stand-ins can
    be generated there, so the realistic rows issue #49 requires are
    infeasible — flagged for hand-completion like an unexpressible
    top-level type. A top-level ``unknown`` is flagged by the caller, so
    it is not re-walked here (one flag per cause, never duplicates)."""
    if spec["kind"] == "unknown":
        return []
    return [p for p, s in _walk_spec(spec) if s["kind"] == "unknown"]


def _dataclass_specs_of(spec: dict, path: str = "") -> list[tuple[str, dict]]:
    """(param path, dataclass spec) for every dataclass in a spec, the
    top level included (issue #50)."""
    return [(p, s) for p, s in _walk_spec(spec, path) if s["kind"] == "dataclass"]


def _derived_inputs(param_specs: dict, unit: dict, units_by_id: dict,
                    uid: str, warnings: list[str]) -> list[dict]:
    """The derived-input record (issue #50): every dataclass a judged
    parameter needs that the unit module itself does not define — ``Bar``
    in ``simple_moving_average(bars: list[Bar], ...)`` coming from the
    unit's ``u-bars`` dependency. Each entry records the defining
    module/path, the declared ``deps[]`` unit it maps to (None = an
    undeclared dependency — the Stage-1 record may be stale, or the module
    is shared/unported), and the parameter paths that use it."""
    unit_modules = set(import_candidates_of(unit["python_path"]))
    dep_modules: dict[str, str] = {}  # dependency module name -> unit id
    for dep_id in unit.get("deps") or []:
        dep = units_by_id.get(dep_id)
        if dep is None:
            warnings.append(f"unit {uid}: deps[] names {dep_id!r}, which is not "
                            "a unit in the state file — the Stage-1 record is stale")
            continue
        for name in import_candidates_of(dep["python_path"]):
            dep_modules[name] = dep_id
    found: dict[tuple[str, str], dict] = {}
    for pname, info in param_specs.items():
        for ppath, spec in _dataclass_specs_of(info["spec"], pname):
            if spec["module"] in unit_modules:
                continue  # defined by the unit itself, not a dependency
            key = (spec["module"], spec["name"])
            entry = found.setdefault(key, {
                "name": spec["name"], "module": spec["module"],
                "path": spec.get("path"),
                "dep": dep_modules.get(spec["module"]),
                "params": [],
            })
            entry["params"].append(ppath)
    for entry in found.values():
        entry["params"] = sorted(set(entry["params"]))
    items = sorted(found.values(), key=lambda e: (e["module"], e["name"]))
    if items:
        detail = "; ".join(
            f"{it['name']} from {it['module']}"
            + (f" (declared dep {it['dep']})" if it["dep"]
               else " (NOT a declared dep — deps[] may be stale, or the module "
                    "is shared/unported)")
            + f" for {', '.join(it['params'])}"
            for it in items)
        warnings.append(
            f"unit {uid}: {len(items)} derived-input dataclass(es) (issue #50): "
            f"{detail} — each adapter resolves them on ITS OWN side at import "
            "time (reference: the python dependency module; candidate: the "
            "wheel's own class — a flat wheel re-exports it, split wheels "
            "hand-tune _WHEEL_DEP_MODULES in the candidate adapter); a side "
            "that cannot resolve one is an honest setup error, never silent []")
    return items


def _param_specs(params: dict[str, dict], ctx: FileCtx) -> tuple[dict, list[dict]]:
    """({name: {"spec", "has_default"}}, hand-tune flags) — a parameter whose
    annotation the heuristic cannot express, whose dict keys cannot
    round-trip through JSON, or whose containers/dataclasses hold a type it
    cannot generate (realistic rows infeasible — issue #49), is flagged,
    never guessed."""
    specs: dict[str, dict] = {}
    hand_tune: list[dict] = []
    for name, info in params.items():
        spec = _parse_ann(info["annotation"], ctx)
        specs[name] = {"spec": spec, "has_default": info["has_default"]}
        if spec["kind"] == "unknown":
            hand_tune.append({"where": f"param {name}",
                              "reason": spec.get("reason", spec.get("text", "unknown type"))})
        for path in _infeasible_generation_paths(spec):
            hand_tune.append({
                "where": f"param {name}{path}",
                "reason": "holds a type the heuristic cannot generate — realistic "
                          "corpus rows are infeasible (issue #49); hand-complete "
                          "the generator and the adapters",
            })
        if spec["kind"] == "dict" and spec["key"]["kind"] not in JSON_DICT_KEYS:
            hand_tune.append({"where": f"param {name} (dict keys)",
                              "reason": f"dict keys of kind {spec['key']['kind']!r} "
                                        "cannot round-trip through JSON"})
    return specs, hand_tune


def _typed_params(fn, ctx: FileCtx, where: str = "") -> tuple[dict, list[dict]]:
    """({name: {"spec", "has_default"}}, hand-tune flags) for one function;
    ``where`` locates the flags ("fn: param x" in module mode)."""
    params, notes = _signature_params(fn)
    specs, flags = _param_specs(params, ctx)
    hand_tune = [{**f, "where": where + f["where"]} for f in flags]
    hand_tune += [{"where": where + "signature", "reason": n} for n in notes]
    return specs, hand_tune


def _result_spec(fn, ctx: FileCtx, uid: str, warnings: list[str]) -> dict | None:
    """Return-annotation spec (None = unannotated); an annotation the
    heuristic cannot express warns — the adapters fall back to an honest
    _jsonify walk (unserializable outputs judge incomparable)."""
    spec = _parse_ann(fn.returns, ctx) if fn.returns else None
    if spec is not None and spec["kind"] == "unknown":
        warnings.append(f"unit {uid}: {fn.name}() return annotation "
                        f"{spec.get('text')!r} is not JSON-expressible — the "
                        "adapters fall back to an honest _jsonify walk "
                        "(unserializable outputs judge incomparable)")
    return spec


def _warn_framing_hazard(param_names, owner: str, uid: str,
                         warnings: list[str]) -> None:
    """A surface taking exactly 'i' and 'input' emits rows indistinguishable
    from the pre-wrapped stdin framing oracle.py refuses (issue #29) —
    warn, never scaffold silently."""
    if set(param_names) == {"i", "input"}:
        warnings.append(f"unit {uid}: {owner} takes exactly params 'i' and "
                        "'input' — rows are indistinguishable from pre-wrapped "
                        "framing and oracle.py will refuse the corpus "
                        "(issue #29); rename or hand-tune")


def _analyze_function_mode(tree: ast.Module, ctx: FileCtx, src_stem: str,
                           args, uid: str, warnings: list[str]) -> dict:
    """Legacy single-entry plan (issue #32): one function, one adapter pair
    judging only it (--mode function)."""
    fn, how = _pick_function(tree, src_stem, args.functions[0] if args.functions else None)
    param_specs, hand_tune = _typed_params(fn, ctx)
    result_spec = _result_spec(fn, ctx, uid, warnings)
    _warn_framing_hazard(param_specs, f"{fn.name}()", uid, warnings)
    return {
        "mode": "function", "function": fn.name, "function_source": how,
        "functions_detail": None, "union_specs": None,
        "param_specs": param_specs, "result_spec": result_spec,
        "hand_tune": hand_tune, "checked_functions": [fn.name],
    }


def _analyze_module_mode(tree: ast.Module, ctx: FileCtx, args, uid: str,
                         warnings: list[str]) -> dict:
    """Module-conformance plan (issue #48): judge the unit's WHOLE public
    API (or the --function subset) through one envelope adapter — the
    corpus is the UNION of the judged functions' parameters."""
    fns, how = _select_module_functions(tree, args.functions)
    functions_detail: dict[str, dict] = {}
    fn_param_specs: dict[str, dict] = {}
    hand_tune: list[dict] = []
    for fn in fns:
        specs, flags = _typed_params(fn, ctx, where=f"{fn.name}: ")
        hand_tune += flags
        fn_param_specs[fn.name] = specs
        functions_detail[fn.name] = {
            "params": {n: info["spec"] for n, info in specs.items()},
            "result": _result_spec(fn, ctx, uid, warnings),
        }
    union = _union_of(fn_param_specs, hand_tune)
    _warn_framing_hazard(union, "the judged functions", uid, warnings)
    # has_default = defaulted in EVERY declaring function, so a row may
    # omit the parameter without breaking any caller
    gen_specs = {n: {"spec": info["spec"], "has_default": not info["required"]}
                 for n, info in union.items()}
    return {
        "mode": "module", "function": None, "function_source": how,
        "functions_detail": functions_detail, "union_specs": union,
        "param_specs": gen_specs, "result_spec": None,
        "hand_tune": hand_tune, "checked_functions": [f.name for f in fns],
    }


def _scaffold_unit(u: dict, root: Path, project_root: Path, out: Path,
                   args, targets: set, units_by_id: dict,
                   warnings: list[str]) -> dict:
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
    if args.mode == "module":
        plan = _analyze_module_mode(tree, ctx, args, uid, warnings)
    else:
        plan = _analyze_function_mode(tree, ctx, src.stem, args, uid, warnings)
    plan["derived_inputs"] = _derived_inputs(plan["param_specs"], u,
                                             units_by_id, uid, warnings)

    contract = _load_contract(args.contract, uid, warnings) if args.contract else None
    invariants = sorted(set((contract or {}).get("invariants") or []))
    if contract is not None:
        sigs = contract.get("type_signatures", [])
        missing = [name for name in plan["checked_functions"]
                   if not any(name in s for s in sigs)]
        if missing:
            warnings.append(f"unit {uid}: judged function(s) {missing} do not appear "
                            f"in {args.contract} type_signatures — contract and "
                            "source disagree")

    stem = _sanitize_stem(uid)
    module_candidates = import_candidates_of(u["python_path"])
    resolved = src.resolve()
    rel = resolved.relative_to(root) if resolved.is_relative_to(root) else resolved
    ref_text = _render_adapter(side="ref", plan=plan, unit=uid, root=root,
                               module_candidates=module_candidates,
                               unit_path=rel.as_posix(),
                               candidate_module=args.candidate_module)
    cand_text = _render_adapter(side="cand", plan=plan, unit=uid, root=root,
                                module_candidates=module_candidates,
                                unit_path=rel.as_posix(),
                                candidate_module=args.candidate_module)
    ref_sha, cand_sha = _shared_adapter_sha256(ref_text, cand_text)
    edge_rows = _edge_rows(plan["param_specs"])
    gen_text = _render_generator(unit=uid, mode=plan["mode"],
                                 functions=plan["checked_functions"],
                                 specs=plan["param_specs"],
                                 edge_rows=edge_rows, seed=args.seed,
                                 rows=args.rows, invariants=invariants)

    files = {
        "ref_adapter": out / "adapters" / f"{stem}_ref.py",
        "cand_adapter": out / "adapters" / f"{stem}_cand.py",
        "generator": out / "corpora" / f"gen_{stem}.py",
        "corpus": out / "corpora" / f"corpus-{uid}.jsonl",
        "manifest": out / "manifests" / f"{uid}.json",
    }
    texts = {"ref_adapter": ref_text, "cand_adapter": cand_text,
             "generator": gen_text}
    targets.update(files.values())

    scaffold_block = {"tool": TOOL, "version": VERSION, "seed": args.seed,
                      "mode": plan["mode"], "function_source": plan["function_source"],
                      "candidate_module": args.candidate_module,
                      "derived_inputs": plan["derived_inputs"],
                      "adapters_shared_sha256": ref_sha}
    manifest = {
        "unit": uid,
        "mode": plan["mode"],
        "reference": {"entry": f"../adapters/{stem}_ref.py:run"},
        "candidate": {"entry": f"../adapters/{stem}_cand.py:run"},
        "corpus": f"../corpora/corpus-{uid}.jsonl",
        "scaffold": scaffold_block,
    }
    result = {
        "unit": uid, "python_path": u["python_path"],
        "module": module_name_of(u["python_path"]),
        "mode": plan["mode"], "function_source": plan["function_source"],
        "parameters": plan["param_specs"], "hand_tune": plan["hand_tune"],
        "derived_inputs": plan["derived_inputs"],
        "files": {k: str(p) for k, p in files.items()},
        "texts": texts, "manifest": manifest,
        "adapters": {"symmetric": ref_sha == cand_sha,
                     "shared_sha256": ref_sha},
        "edge_rows": len(edge_rows),
        "candidate_module": args.candidate_module,
    }
    if plan["mode"] == "module":
        checked = plan["checked_functions"]
        scaffold_block["functions"] = manifest["functions"] = checked
        result.update(functions=checked, functions_detail=plan["functions_detail"],
                      union_parameters=plan["union_specs"])
    else:
        scaffold_block["function"] = plan["function"]
        result.update(function=plan["function"], result=plan["result_spec"])
    return result


def _write_unit(result: dict) -> None:
    for key, text in result["texts"].items():
        path = Path(result["files"][key])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
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


def _module_fn_stats(recs: list) -> dict:
    """Module-mode dry-run evidence (issue #48): per-function ok/error
    counts from the {fn: value | {"__error__": ...}} envelopes of the
    reference records. This is what makes an envelope corpus auditable —
    rows are 'ok' even when a function errored, so the per-function tally
    is the honest ground truth."""
    stats: dict[str, dict] = {}
    ok_rows = 0
    for rec in recs:
        if rec is None or not rec.get("ok"):
            continue
        ok_rows += 1
        value = rec.get("value")
        if not isinstance(value, dict):
            continue
        for name, v in value.items():
            entry = stats.setdefault(name, {"ok": 0, "error": 0})
            is_err = (isinstance(v, dict) and set(v) == {"__error__"}
                      and isinstance(v["__error__"], dict))
            entry["error" if is_err else "ok"] += 1
    for entry in stats.values():
        entry["rows"] = ok_rows
    return stats


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
                "min_reference_ok_fraction": None,
                "degenerate": True, "warning": None, "first_errors": [],
                "functions": {}, "functions_all_error": [],
                "degenerate_detail": None}
    rows = [oracle._corpus_row_status(i, rec) for i, rec in enumerate(recs)]
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    gt = oracle.reference_ground_truth(recs, len(rows), oracle.merge_rules({}))
    fn_stats = _module_fn_stats(recs) if result["mode"] == "module" else {}
    all_error = sorted(n for n, s in fn_stats.items() if s["error"] and s["ok"] == 0)
    degenerate = gt["degenerate"]
    degenerate_detail = None
    if not degenerate and all_error and len(all_error) == len(fn_stats):
        # every judged function errored on every envelope — no per-function
        # parity evidence at all, the module-mode face of issue #30
        degenerate = True
        degenerate_detail = ("every judged function errored on every reference "
                             "dry-run row — the module corpus carries no parity "
                             "evidence (issue #30); fix the corpus or the "
                             "flagged parameters")
    return {
        "rows": rows, "counts": counts,
        "reference_ok": gt["reference_ok"],
        "reference_ok_fraction": gt["reference_ok_fraction"],
        "min_reference_ok_fraction": gt["min_reference_ok_fraction"],
        "degenerate": degenerate,
        "degenerate_detail": degenerate_detail,
        "warning": gt["warning"],
        "first_errors": [r for r in rows if r["status"] == "error"][:3],
        "functions": fn_stats,
        "functions_all_error": all_error if not degenerate else [],
    }


def _split_names(chunks: list[str] | None) -> list[str]:
    """--unit/--function values: repeatable flags, comma-separated too."""
    return [w for chunk in chunks or []
            for w in (p.strip() for p in chunk.split(",")) if w]


def _select_units(units: list, chunks: list[str] | None) -> list[dict]:
    """Units the user asked for (--unit repeatable / comma-separated), or
    all of them; unknown ids are a usage error, not silence."""
    wanted = _split_names(chunks)
    if not wanted:
        return units
    known = {u["id"] for u in units}
    unknown = [w for w in wanted if w not in known]
    if unknown:
        raise ToolError(f"unknown unit id(s) {unknown}; state has {sorted(known)}",
                        kind="usage")
    return [u for u in units if u["id"] in wanted]


def _classify_dry_run(result: dict, dry: dict, findings: list, warnings: list) -> None:
    """Turn the dry-run record into findings/warnings per tools/README.md.
    Findings (exit 2): hand-tune flags; a reference that cannot run or
    errors on every row (every judged function, in module mode); a
    reference-ok fraction below min_reference_ok_fraction — a corpus that
    cannot exercise the unit (issue #49), the same contract as params the
    heuristic cannot express. Warnings: non-ok rows, and a single
    all-error function."""
    unit = result["unit"]
    if result["hand_tune"]:
        findings.append({"unit": unit, "kind": "hand_tune",
                         "items": result["hand_tune"]})
    if dry.get("setup_error"):
        findings.append({
            "unit": unit, "kind": "reference_setup_error",
            "detail": f"the reference adapter failed to run, so the corpus "
                      f"could not be validated: {dry['setup_error']}"})
    elif dry["degenerate"]:
        findings.append({
            "unit": unit, "kind": "degenerate_corpus",
            "detail": dry.get("degenerate_detail") or
                      "the reference adapter errors on every corpus row — a judge "
                      "run would be hollow/INCOMPARABLE (issue #30); fix the corpus "
                      "or the flagged parameters"})
    else:
        frac = dry["reference_ok_fraction"]
        if frac is not None and frac < dry["min_reference_ok_fraction"]:
            findings.append({
                "unit": unit, "kind": "thin_ground_truth",
                "detail": f"reference ok on only {dry['reference_ok']}/"
                          f"{len(dry['rows'])} dry-run rows ({frac:.1%} < "
                          f"min_reference_ok_fraction "
                          f"{dry['min_reference_ok_fraction']}) — the corpus "
                          "cannot exercise the unit (issue #49); give the series "
                          "parameters realistic generation or hand-complete the "
                          "flagged parameters"})
        if dry["warning"]:
            warnings.append(f"unit {unit}: {dry['warning']}")
        for name in dry.get("functions_all_error") or []:
            warnings.append(f"unit {unit}: module-mode function {name!r} errored on "
                            "every reference dry-run row — its parity is unjudged "
                            "(issue #48); fix its corpus values or hand-complete "
                            "its flagged parameters")
    for status, n in sorted(dry["counts"].items()):
        if status != "ok" and n:
            warnings.append(f"unit {unit}: {n}/{len(dry['rows'])} "
                            f"reference dry-run rows {status} (some error-path "
                            "rows are coverage; a wall of identical errors is not)")


def _finish_unit(result: dict, out: Path, args, findings: list, warnings: list) -> None:
    """Write the artifacts, generate the corpus, dry-run it through
    oracle.py's runner, and record findings/warnings."""
    _write_unit(result)
    result["corpus"] = _generate_corpus(result, args.seed, args.rows)
    dry = _dry_run_corpus(result, out, args.timeout)
    result["corpus_dry_run"] = {k: v for k, v in dry.items() if k != "rows"}
    result["corpus_dry_run"]["sample_rows"] = dry["rows"][:5]
    _classify_dry_run(result, dry, findings, warnings)
    del result["texts"]


def cmd_scaffold(args) -> int:
    if not (args.state or args.state_pos):
        raise ToolError("a migration-state.json path is required (positional or --state)",
                        kind="usage")
    # --function: the module-mode judged subset, or the single
    # function-mode entry (enforced below)
    args.functions = _split_names(args.function)
    if args.mode == "function" and len(args.functions) > 1:
        raise ToolError("--mode function judges ONE function per manifest; pass "
                        "--mode module (the default) to judge several through "
                        "the conformance envelope (issue #48)", kind="usage")
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

    selected = _select_units(units, args.unit)

    # analyze every unit first, then refuse-before-write: hand-tuned
    # scaffolds must not be half-overwritten
    targets: set = set()
    units_by_id = {x["id"]: x for x in units}
    results = [_scaffold_unit(u, root, project_root, out, args, targets,
                              units_by_id, warnings)
               for u in selected]
    existing = sorted(str(p) for p in targets if p.exists())
    if existing and not args.force:
        raise ToolError(
            "refusing to overwrite existing scaffold output (hand-tuned edits "
            f"would be lost): {'; '.join(existing)} — pass --force to regenerate",
            kind="usage")

    findings: list[dict] = []
    for result in results:
        _finish_unit(result, out, args, findings, warnings)

    code = EXIT_FINDINGS if findings else EXIT_OK
    ok_units = sum(1 for r in results if not r["hand_tune"])
    hand_tune_n = sum(len(r["hand_tune"]) for r in results)
    n_fns = sum(len(r.get("functions") or []) for r in results)
    human = (f"scaffolded {len(results)} unit(s) under {out} (seed {args.seed}, "
             f"mode {args.mode}"
             + (f", {n_fns} judged function(s)" if args.mode == "module" else "")
             + f"): {ok_units} fully generated, {hand_tune_n} hand-tune flag(s)"
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
                                        "(issue #32; module conformance: issue #48)")
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
    p.add_argument("--mode", choices=("module", "function"), default="module",
                   help="module (default, issue #48): ONE manifest judges the "
                        "unit's WHOLE public API — the adapter calls every "
                        "judged function per row and returns "
                        "{fn: result | {__error__}} so per-function errors "
                        "compare instead of failing the row; function: the "
                        "legacy single-entry scaffold")
    p.add_argument("--function", action="append", metavar="NAME",
                   help="public function(s) to judge (repeatable / comma-"
                        "separated). module mode: the envelope subset "
                        "(default: every public function); function mode: "
                        "the single entry (default: the module-stem-named "
                        "public function, else the first)")
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
