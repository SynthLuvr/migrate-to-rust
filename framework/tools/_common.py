"""Shared helpers for framework/tools/*.py.

Design contract (framework/tools/README.md "Anti-requirements"):
- stdlib only, deterministic, stateless between invocations
- every tool prints machine-readable JSON on stdout and embeds a
  human-readable summary; ``--human`` additionally echoes it to stderr
- stdout is ALWAYS a single JSON object — including error paths (a refusal
  is a result, not a crash); ToolError/run_cli() below provide the
  uniform error envelope
- exit codes: 0 = clean verdict, 1 = usage/IO error, 2 = findings/divergence,
  3 = incomparable/warning-class outcome, 4 = setup/dependency failure
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from itertools import count
from pathlib import Path

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_FINDINGS = 2
EXIT_INCOMPARABLE = 3
EXIT_SETUP = 4

SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "node_modules",
    "target", "dist", "build", ".mypy_cache", ".pytest_cache", ".tox",
}


def now_iso() -> str:
    """Second-precision UTC ISO-8601 with 'Z' — the journal timestamp format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path: str | os.PathLike) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path: str | os.PathLike) -> list[tuple[int, object]]:
    """Parse a JSONL file into [(line_number, value), ...]. Raises ValueError
    with file:line context on malformed lines — never silently skips."""
    out = []
    text = Path(path).read_text(encoding="utf-8")
    for n, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            out.append((n, json.loads(line)))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{n}: invalid JSON line: {exc}") from exc
    return out


def write_json_atomic(path: str | os.PathLike, payload: object) -> None:
    """Write pretty JSON via temp-file + os.replace so readers never see a
    half-written state file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def emit(payload: dict, human: str | None = None, human_flag: bool = False) -> int:
    """Print the machine-readable JSON (always) and the human summary
    (embedded in the JSON; echoed to stderr with --human). Returns exit code
    taken from payload['summary']['exit_code'] when present."""
    payload.setdefault("tool", Path(sys.argv[0]).name if sys.argv[0] else None)
    payload.setdefault("generated_at", now_iso())
    summary = payload.setdefault("summary", {})
    if human is not None:
        summary.setdefault("human", human)
    code = summary.get("exit_code", EXIT_OK)
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    if human_flag and human:
        print(human, file=sys.stderr)
    return code


class ToolError(Exception):
    """A refusal (usage/IO/setup problem) raised mid-command. The CLI
    boundary (``run_cli``) converts it into the JSON error envelope on
    stdout instead of letting it escape as a traceback. ``kind`` ∈ io
    (filesystem/environment) · usage (bad/missing input content) ·
    setup (missing dependency, exit 4)."""

    def __init__(self, detail: str, *, kind: str = "io"):
        super().__init__(detail)
        self.detail = detail
        self.kind = kind


def emit_error(detail: str, *, kind: str = "io", tool: str | None = None,
               version: int | None = None, human_flag: bool = False) -> int:
    """Emit the uniform JSON error envelope on stdout (README "Uniform
    conventions"): ``error.kind`` + ``error.detail``, and
    ``summary.exit_code`` mirroring the process exit code — so callers
    parsing stdout always get JSON, even on failure. Returns the exit
    code."""
    exit_code = EXIT_SETUP if kind == "setup" else EXIT_ERROR
    payload: dict = {}
    if tool is not None:
        payload["tool"] = tool
    if version is not None:
        payload["version"] = version
    payload["error"] = {"kind": kind, "detail": detail}
    payload["summary"] = {"exit_code": exit_code, "human": detail}
    return emit(payload, human=detail, human_flag=human_flag)


def run_cli(main_func, argv=None, *, tool: str | None = None,
            version: int | None = None) -> int:
    """CLI boundary for every tool's ``__main__``: refusals leave as the
    JSON error envelope, never a bare traceback/stderr line. Input
    problems — missing/unreadable files (OSError), malformed JSON or bad
    values (ValueError) — are results, not crashes; genuine programming
    errors (TypeError, KeyError, ...) still traceback. Usage:
    ``sys.exit(run_cli(main, tool=TOOL, version=VERSION))``."""
    # every tool defines --human as a top-level flag; honor it on the error
    # envelope too, so `--human` echoes the summary to stderr consistently
    human_flag = "--human" in (argv if argv is not None else sys.argv[1:])
    try:
        return main_func(argv)
    except ToolError as e:
        return emit_error(e.detail, kind=e.kind, tool=tool, version=version,
                          human_flag=human_flag)
    except (OSError, ValueError) as e:
        kind = "usage" if isinstance(e, ValueError) else "io"
        return emit_error(f"{type(e).__name__}: {e}", kind=kind, tool=tool,
                          version=version, human_flag=human_flag)


# ------------------------------------------------- memory containment ----
# Issue #74 (fixes the enforcement gap behind #73): a runaway child must
# die alone inside its own cgroup scope — never a host-wide OOM that
# fails the whole session server's unit.

_SCOPE_WARN = "[run_child]"
# systemd-run's own startup failures: the scope was never created and the
# command never ran. Matched only on a scoped attempt that also produced
# no stdout (see _scope_startup_failed), so a child that legitimately ran
# and failed is never mistaken for a scoping failure — and never re-run.
_SCOPE_STARTUP_FAILURES = (
    "Failed to connect to bus",
    "Failed to start transient scope unit",
    "Failed to start transient service unit",
    "System has not been booted with systemd",
    "Trying to run as user instance",
)
_scope_seq = count()


def scope_argv(cmd: list[str], mem_limit_mb: int, *,
               unit: str | None = None,
               systemd_run: str | None = None) -> tuple[list[str], str | None]:
    """Prefix ``cmd`` with a per-child systemd user scope bounding memory:
    ``systemd-run --user --scope -p MemoryMax=<n>M -p MemorySwapMax=0
    --unit=<name> -- <cmd...>``. Returns ``(argv, unit)`` with ``unit=None``
    when scoping is unavailable (no systemd-run on PATH) — the caller then
    runs ``cmd`` unscoped, and the returned argv is ``cmd`` itself.

    ``MemoryMax`` bounds actual memory (RSS), not address space: an
    ``RLIMIT_AS`` ceiling breaks rustc/LLVM's large reservations, so cgroup
    v2 is the only ceiling that fits this harness. The named unit makes a
    breach attributable — the journal says ``run-child-….scope``, not an
    anonymous python. ``systemd_run`` pins the binary for tests (``''``
    forces the unavailable path); ``unit`` defaults to a unique derived
    name. Pure — nothing is spawned here."""
    if mem_limit_mb is None or mem_limit_mb <= 0:
        raise ValueError(f"mem_limit_mb must be a positive number of MB, "
                         f"got {mem_limit_mb!r}")
    exe = shutil.which("systemd-run") if systemd_run is None else systemd_run
    if not exe:
        return cmd, None
    name = unit or _scope_unit_name(cmd)
    return [exe, "--user", "--scope",
            "-p", f"MemoryMax={mem_limit_mb}M",
            "-p", "MemorySwapMax=0",
            f"--unit={name}", "--", *cmd], name


def _scope_unit_name(cmd: list[str]) -> str:
    """A unique, syntactically valid scope unit name derived from the
    command — the attribution trail when the kernel kills a breach."""
    exe = Path(cmd[0]).stem if cmd else "child"
    safe = re.sub(r"[^0-9A-Za-z_.-]", "_", exe)[:40].strip("_") or "child"
    return f"run-child-{safe}-{os.getpid()}-{next(_scope_seq)}"


def _scope_startup_failed(proc: subprocess.CompletedProcess) -> bool:
    """True only when systemd-run died BEFORE the command ran (scope never
    created): non-zero exit, no stdout, and one of systemd-run's own
    startup-failure messages on stderr. Only then may run_child retry
    unscoped — a child that ran is never re-run (side effects must not
    happen twice)."""
    if proc.returncode == 0 or proc.stdout:
        return False
    err = proc.stderr or ""
    return any(sig in err for sig in _SCOPE_STARTUP_FAILURES)


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the child's whole process group: run_child children are
    session leaders, so this reaps the entire subtree — grandchildren a
    bare ``proc.kill()`` would orphan (issue #73). Best-effort: a group
    that is already gone means the job is done."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        try:
            proc.kill()  # group gone or unkillable: the direct child
        except OSError:
            pass


def self_containment() -> dict:
    """Where THIS process runs, cgroup-wise: the ``containment`` block the
    driver and the chain harness report so a run's memory scope is
    attributable from the artifacts alone (issue #74). Never raises — an
    unreadable /proc reads as unknown."""
    try:
        raw = Path("/proc/self/cgroup").read_text(encoding="utf-8")
        lines = [l for l in raw.splitlines() if l.strip()]
    except OSError:
        lines = []
    full = lines[0].split(":", 2)[-1] if lines else None
    unit = lines[-1].split(":", 2)[-1].rsplit("/", 1)[-1] if lines else None
    return {"cgroup": full, "unit": unit or None,
            "in_migration_slice": bool(unit and "migration.slice" in full)}


def run_child(cmd: list[str], *, cwd: str | os.PathLike | None = None,
              input_text: str | None = None, timeout: float = 120.0,
              env_extra: dict[str, str] | None = None,
              mem_limit_mb: int | None = None) -> tuple[int, str, str]:
    """Deterministic child-process helper: fixed env additions, captured
    stdout/stderr, hard timeout. `env_extra` layers caller variables on top
    (e.g. a per-unit CARGO_TARGET_DIR) without loosening the fixed ones.
    Returns (returncode, stdout, stderr).

    Every child runs as its own session leader and a timeout (or an
    aborting exception) SIGKILLs its whole process group, so no child
    can outlive this parent as an orphan (issue #73: judge REPL
    grandchildren stayed blocked on stdin, holding RSS, after their
    judge was reaped). TimeoutExpired is raised as before, with the
    group reaped first.

    `mem_limit_mb` (issue #74) runs the child inside its own systemd user
    scope (see scope_argv) so a memory breach kills that child alone
    (SIGKILL -> exit 137) instead of a host-wide OOM failing the session
    server's unit (#73). Graceful fallback, never a hard failure: no
    systemd-run on PATH, or the scope fails to start (container/CI without
    a user manager) -> the child runs unscoped with a `[run_child]` warning
    appended to stderr. Unscoped paths (`mem_limit_mb=None`) behave exactly
    as before."""
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTHONSTARTUP", None)
    if env_extra:
        env.update(env_extra)

    def spawn(argv: list[str]) -> subprocess.CompletedProcess:
        # start_new_session: the child leads its own process group, so the
        # timeout path below can kill the whole subtree, not just this pid
        with subprocess.Popen(
                argv, cwd=cwd, env=env, text=True,
                stdin=subprocess.PIPE if input_text is not None else None,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True) as proc:
            try:
                out, err = proc.communicate(input=input_text,
                                            timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                _kill_process_group(proc)
                exc.stdout, exc.stderr = proc.communicate()
                raise
            except BaseException:  # KeyboardInterrupt &c.: no orphans
                _kill_process_group(proc)
                raise
            return subprocess.CompletedProcess(argv, proc.returncode,
                                               stdout=out, stderr=err)

    warning = None
    if mem_limit_mb is not None:
        scoped, unit = scope_argv(cmd, mem_limit_mb)
        if unit is None:
            warning = (f"{_SCOPE_WARN} systemd-run not on PATH — cannot "
                       f"scope the child to mem_limit_mb={mem_limit_mb}M; "
                       f"running unscoped (issue #74 fallback)")
        else:
            proc = spawn(scoped)
            if not _scope_startup_failed(proc):
                return proc.returncode, proc.stdout, proc.stderr
            warning = (f"{_SCOPE_WARN} memory scope failed to start "
                       f"({(proc.stderr or '').strip()[:160]}) — running "
                       f"unscoped (issue #74 fallback)")
    proc = spawn(cmd)
    if warning is None:
        return proc.returncode, proc.stdout, proc.stderr
    stderr = f"{proc.stderr}\n{warning}" if proc.stderr else warning
    return proc.returncode, proc.stdout, stderr


def iter_tree_files(root: str | os.PathLike, extensions: set[str] | None = None) -> list[Path]:
    """Sorted recursive file listing honoring SKIP_DIRS; optional extension
    filter (compared case-insensitively, with the leading dot)."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fname in sorted(filenames):
            if extensions is not None:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in extensions:
                    continue
            out.append(Path(dirpath) / fname)
    return out


def python_version_string() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def module_name_of(python_path: str) -> str:
    """Dotted module name for a source path: strip src//lib/ layout prefix
    and trailing __init__ (src/pkg/parse.py -> pkg.parse).

    Assumes the classic src-layout where ``src/`` is not part of the import
    name; use ``import_candidates_of`` to match a path against real import
    strings, which may use either spelling (issue #26)."""
    parts = list(Path(python_path).with_suffix("").parts)
    if parts and parts[0] in ("src", "lib"):
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def import_candidates_of(python_path: str) -> list[str]:
    """Dotted import names a path may be imported under, canonical first:
    the layout-stripped name (``src/pkg/parse.py`` -> ``pkg.parse``) plus,
    when a src//lib/ prefix was stripped, the un-stripped spelling
    (``src.pkg.parse``) — projects whose import root IS the top-level src/
    directory import the latter (issue #26)."""
    parts = list(Path(python_path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    full = ".".join(parts)
    stripped = module_name_of(python_path)
    if stripped and stripped != full:
        return [stripped, full]
    return [full] if full else []
