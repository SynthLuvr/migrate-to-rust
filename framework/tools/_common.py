"""Shared helpers for framework/tools/*.py.

Design contract (framework/tools/README.md "Anti-requirements"):
- stdlib only, deterministic, stateless between invocations
- every tool prints machine-readable JSON on stdout and embeds a
  human-readable summary; ``--human`` additionally echoes it to stderr
- exit codes: 0 = clean verdict, 1 = usage/IO error, 2 = findings/divergence,
  3 = incomparable/warning-class outcome, 4 = setup/dependency failure
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent

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


def run_child(cmd: list[str], *, cwd: str | os.PathLike | None = None,
              input_text: str | None = None, timeout: float = 120.0) -> tuple[int, str, str]:
    """Deterministic child-process helper: fixed env additions, captured
    stdout/stderr, hard timeout. Returns (returncode, stdout, stderr)."""
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTHONSTARTUP", None)
    proc = subprocess.run(
        cmd, cwd=cwd, env=env, input=input_text, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def import_tool(name: str):
    """Import a sibling tool module (e.g. bench.py importing oracle.py).
    Works whether the tools are run as scripts or imported under pytest."""
    if str(TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(TOOLS_DIR))
    return __import__(name)


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
