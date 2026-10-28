#!/usr/bin/env python3
"""succession.py — the session-chain harness (issue #25).

``budget_exhausted`` at the SESSION level (the orchestrator's context
window gives out; the motivating run 20261028_4 died at stage 4/7 with
~600k of 5,000,000 run tokens spent) is "awaiting input" in disguise: the
run parks and a human must notice and restart a successor by hand. That
breaks the autonomy philosophy — "ask" is not one of the agent's moves —
so this tool makes succession a *scheduling event in code*:

    python framework/tools/succession.py chain \
        --state runs/<project>/migration-state.json \
        --prompt-file runs/<project>/first-prompt.md

It wraps the whole run in a **chain of GLV orchestrator sessions** (the
HTTP transport ``tools/glv-subagents.md`` already defines for role
delegation): it starts session 1 with the operator's single prompt, parks
on ``GET /api/sessions/:id/wait`` until each session settles, classifies
the outcome from the state file (the single source of truth — sessions
write it only through ``state.py``), and on session-level exhaustion
spawns the successor itself.

  1. **Auto-succession.** ``run_status=budget_exhausted`` (or a session
     that settled without recording any terminal state) under the
     run-level ceilings → compose the full resume brief, spawn the
     successor via ``POST /api/sessions``, stop the predecessor if still
     live, journal a **stop-notice** (operator informed, not involved),
     and reset ``run_status`` to ``running`` — all through ``state.py``'s
     validated writer. No human act anywhere.
  2. **Fresh session budget, shared run budget.** Every successor starts
     with a fresh session/context budget; the RUN-level ceilings
     (``autonomy.budget``) bind across the whole chain — token spend sums
     through the one state file every session writes, wall clock
     accumulates per session — so runaway stays impossible.
  3. **Guardrails.** A depth cap (``--max-depth``, or
     ``autonomy.budget.max_chain_depth``, policy default 10) and a
     monotonic-progress guard (each successor must advance the stage or
     decide ≥1 unit, vs. the snapshot taken when it was spawned); either
     violated → ``failed_safely`` with the dossier — never an infinite
     resume loop.
  4. **Only genuine terminals end the chain:** ``complete`` /
     ``no_go_measured`` / ``failed_safely``, or the run-level ceilings
     themselves (the one case where ``budget_exhausted`` is the honest
     human-facing result: the operator must raise the ceiling).
  5. **Self-bootstrapped, self-healing transport (issue #47).** GLV is
     unconditional — no alternate operating mode, no second transport,
     no human provisioning step. Before session 1 the harness probes
     ``GET /api/openapi.json``, starts ``glv serve`` itself when nothing
     answers, and mints a token named ``run-<project>-s<n>`` for every
     chain session (``glv token list`` is the audit trail), exporting
     ``GLV_BASE_URL``/``GLV_API_TOKEN`` itself. A 401 run (the 30-day
     token expiry) re-mints and continues; a connection loss mid-chain
     revives the server and retries. Only a transport that cannot be
     revived ends the chain — ``failed_safely`` with a journaled
     stop-notice (the operator is informed, never involved); re-running
     the chain re-bootstraps and adopts any live session.
  6. **Resume hygiene at every succession** (the 20261028_4 lesson: the
     state recorded ``rust_path: crates/wicksmith-core/src/u_bars.rs``
     while the file lived at ``src/u_bars.rs``): every unit's recorded
     paths are verified against the target root; an unambiguous mismatch
     (exactly one candidate on disk) is repaired through ``state.py`` —
     journaled — and anything unresolved rides in the successor's brief
     as an explicit warning. ``succession.py hygiene`` runs the check
     standalone.

Restartable anywhere (the harness itself can die): a re-invocation adopts
the last unsettled session from ``session_chain.sessions[]`` and
re-classifies before spawning anything.

Subcommands
-----------
  chain    drive the run as a successor-session chain over GLV (above)
  brief    compose (and print, no side effects) the brief the next
           session — first or successor — would receive
  hygiene   verify/repair the state file's recorded paths (issue #25
           resume-hygiene); exit 2 while anything is unresolved

Auth/transport per ``tools/glv-subagents.md`` § *GLV bootstrap*
(issue #47): the harness ensures the server itself and mints one named
token per chain session, exporting ``GLV_BASE_URL``/``GLV_API_TOKEN``
(explicit flags/env are honored as configuration); bearer on every
``/api`` path. This
tool writes the state file ONLY through ``state.py``'s save_validated
(the "state.py is the ONLY writer" discipline — succession.py composes
it, the way pipeline.py does).

Exit codes follow tools/README.md: 0 any terminal state except
failed_safely (a *result*, not a failure) · 2 findings (failed_safely —
including a GLV transport that could not be revived — or unresolved
hygiene) · 1 usage/IO via the shared envelope.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state as state_tool  # noqa: E402  — the ONLY writer, composed not duplicated
import pipeline as pipeline_tool  # noqa: E402  — ceilings + report composed, not copied
from _common import (  # noqa: E402
    EXIT_FINDINGS, EXIT_OK, ToolError, emit, now_iso, run_cli,
)

TOOL = "succession.py"
VERSION = 2
FRAMEWORK = Path(__file__).resolve().parent.parent
DEFAULT_MAX_CHAIN_DEPTH = 10
# statuses that mean a unit reached a decision (anything but pending);
# the monotonic-progress guard counts these — "≥1 unit ported/escalated"
DECIDED_UNIT_STATUSES = state_tool.RUST_LIVE_STATUSES | {"escalated", "fallback_python"}
# run_status values that legitimately end a chain (MIGRATION_PROMPT.md
# terminal-state discipline); budget_exhausted is NOT among them — it is a
# scheduling event while run-level budget remains
GENUINE_TERMINALS = ("complete", "no_go_measured", "failed_safely")
# run_status values that warrant an auto-succession: the session's own
# budget guard fired (budget_exhausted) or it settled without recording
# any terminal state (None/pending/running — the context window killed it
# first); in all of them the RUN-level bound decides what happens next
SUCCEED_ELIGIBLE = (None, "pending", "running", "budget_exhausted")
# finished runs a fresh chain must refuse to resume — failed_safely is
# deliberately absent: a chain may take a failed_safely run over
FINISHED_RUN_STATUSES = ("complete", "no_go_measured")
GLV_LIVE_STATUSES = ("queued", "running")
JOURNAL_TAIL_ENTRIES = 20
DEFAULT_SESSION_ONE_PROMPT = (
    "Run the Python->Rust migration per the framework's "
    "MIGRATION_PROMPT.md; the state file path is recorded above.")


# ------------------------------------------------------------ transport ----

class GlvError(ToolError):
    """GLV transport failure (HTTP error). kind='io': surfaces to the
    chain's self-healing / park path — never a crash."""

    def __init__(self, detail: str):
        super().__init__(detail, kind="io")


class GlvAuthError(GlvError):
    """HTTP 401 — the token expired (30-day lifetime) or is invalid.
    Never a stop, never a human ask: the transport re-mints the named
    token and retries (issue #47)."""


class GlvUnreachableError(GlvError):
    """Connection-level failure — nothing answered. Never a stop: the
    transport owns the server lifecycle, revives `glv serve`, and
    retries (issue #47)."""


def _multipart(fields: dict[str, str]) -> tuple[bytes, str]:
    """Encode form fields as multipart/form-data (POST /api/sessions is a
    multipart endpoint per tools/glv-subagents.md)."""
    boundary = "succession-" + uuid4().hex
    parts = []
    for key, value in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n")
    parts.append(f"--{boundary}--\r\n")
    return "".join(parts).encode("utf-8"), f"multipart/form-data; boundary={boundary}"


class GlvClient:
    """The minimal GLV JSON-API surface the chain needs (cheat sheet:
    tools/glv-subagents.md; source of truth: GET /api/openapi.json)."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, *, data: bytes | None = None,
                 headers: dict | None = None) -> dict:
        req = urllib.request.Request(
            f"{self.base_url}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {self.token}", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            if e.code == 401:
                raise GlvAuthError(
                    f"GLV {method} {path}: HTTP 401: {detail}") from e
            raise GlvError(f"GLV {method} {path}: HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise GlvUnreachableError(
                f"GLV {method} {path}: no answer from the server: {e}") from e

    def start_session(self, *, name: str, prompt: str,
                      working_dir: str | None = None,
                      model: str | None = None) -> dict:
        fields = {"name": name, "prompt": prompt}
        if working_dir:
            fields["working_dir"] = working_dir
        if model:
            fields["model"] = model
        data, ctype = _multipart(fields)
        return self._request("POST", "/api/sessions", data=data,
                             headers={"Content-Type": ctype})

    def wait_settled(self, session_id: str, *, timeout_ms: int = 60000) -> dict:
        """Long-poll until the session settles (status outside
        queued|running); a 202 body just means call again."""
        while True:
            out = self._request(
                "GET", f"/api/sessions/{session_id}/wait?timeout={timeout_ms}")
            if out.get("status") not in GLV_LIVE_STATUSES:
                return out

    def stop(self, session_id: str) -> dict:
        return self._request("POST", f"/api/sessions/{session_id}/stop")


# ---------------------------------------- self-bootstrap (issue #47) ------

DEFAULT_GLV_URL = "http://localhost:3000"
OPENAPI_PATH = "/api/openapi.json"
TOKEN_RE = re.compile(r"glv_at_[A-Za-z0-9_\-]+")
SERVE_STARTUP_TIMEOUT_S = 60.0


def _glv_cli_path() -> str | None:
    """The glv CLI (``GLV_CLI`` overrides the PATH lookup)."""
    return os.environ.get("GLV_CLI") or shutil.which("glv")


def _probe_server(base_url: str, *, timeout: float = 5.0) -> bool:
    """True when the server answers ``/api/openapi.json`` at all — a 401
    JSON body counts: proving the server is UP is the probe's job;
    proving we may talk to it is the token's."""
    req = urllib.request.Request(f"{base_url.rstrip('/')}{OPENAPI_PATH}")
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _serve_start() -> bool:
    """Start ``glv serve`` detached (``-c/--max-concurrent``, default 6 —
    the run queue shared by every served agent). Returns True when the
    CLI exists and the process launched; the log lands in a temp file so
    a failed start is diagnosable from the tool output."""
    cli = _glv_cli_path()
    if not cli:
        return False
    fd, log = tempfile.mkstemp(prefix="glv-serve-", suffix=".log")
    os.close(fd)
    with open(log, "wb") as log_fh:
        subprocess.Popen(  # the operator-installed CLI, by resolved path
            [cli, "serve"], stdin=subprocess.DEVNULL,
            stdout=log_fh, stderr=subprocess.STDOUT,
            start_new_session=True)
    return True


def _ensure_server(base_url: str, *,
                   wait_s: float = SERVE_STARTUP_TIMEOUT_S,
                   probe=None, serve_start=None) -> str:
    """``'up'`` when the server answers; otherwise start ``glv serve``
    and wait for it. Raises ToolError(kind='io') when nothing answers
    and the server cannot be started — an environment failure reported
    as such (install glv); the answer is never a second transport."""
    probe = probe or _probe_server
    if probe(base_url):
        return "up"
    if not (serve_start or _serve_start)():
        raise ToolError(
            f"nothing is serving at {base_url} and the glv CLI is not on "
            f"PATH, so the server cannot be started — install glv "
            f"(github.com/SynthLuvr/glv); GLV is the only transport "
            f"(issue #47)", kind="io")
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if probe(base_url):
            return "started"
    raise ToolError(
        f"`glv serve` was started but {base_url} still answers nothing "
        f"after {wait_s:.0f}s — check the glv-serve-*.log temp file",
        kind="io")


def _mint_token_via_cli(name: str, *, cli: str | None = None) -> str:
    """``glv token create --name <name>`` — the CLI path, for a host with
    filesystem access to the GLV data dir. The raw token is printed
    once (only its SHA-256 hash is stored server-side)."""
    cli = cli or _glv_cli_path()
    if not cli:
        raise ToolError("the glv CLI is not on PATH — `glv token create` "
                        "needs it", kind="io")
    try:
        proc = subprocess.run(
            [cli, "token", "create", "--name", name],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise ToolError(f"glv token create --name {name} failed to run: "
                        f"{e}", kind="io") from e
    if proc.returncode != 0:
        raise ToolError(
            f"glv token create --name {name} exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout).strip()[:300]}", kind="io")
    match = TOKEN_RE.search(proc.stdout)
    if not match:
        raise ToolError(
            f"glv token create printed no raw token (expected glv_at_… on "
            f"stdout): {proc.stdout.strip()[:300]}", kind="io")
    return match.group(0)


def _mint_token_via_loopback(base_url: str, name: str) -> str:
    """Loopback ``POST /api/tokens`` with no credential — the path for a
    session with network reach but no filesystem access to the data dir.
    Works only when the server runs ``glv serve
    --allow-token-bootstrap`` (loopback-only; never behind a reverse
    proxy — loopback detection is the security boundary)."""
    body = json.dumps({"name": name}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/tokens", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        raise ToolError(
            f"loopback POST /api/tokens refused (HTTP {e.code}: {detail}) — "
            f"the server was not started with --allow-token-bootstrap; "
            f"mint via the glv CLI on a host with data-dir access instead",
            kind="io") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ToolError(f"loopback POST /api/tokens: no answer from the "
                        f"server: {e}", kind="io") from e
    match = TOKEN_RE.search(raw)
    if not match:
        raise ToolError(f"POST /api/tokens returned no raw token "
                        f"(expected glv_at_…): {raw[:300]}", kind="io")
    return match.group(0)


def _mint_token(base_url: str, name: str) -> str:
    """Mint this session's own token (issue #47): the CLI on a host with
    data-dir access, else the loopback bootstrap endpoint. Either way it
    is the session's own act — never a human prerequisite."""
    if _glv_cli_path():
        return _mint_token_via_cli(name)
    return _mint_token_via_loopback(base_url, name)


class GlvTransport:
    """GLV, self-bootstrapped and self-healing (issue #47): the harness
    ensures the server (probe → start ``glv serve`` → wait), mints one
    named token per chain session (``run-<project>-s<n>`` — the
    ``glv token list`` audit trail), re-mints on a 401 run, and revives
    a lost server mid-chain. Explicit ``--glv-url``/``--glv-token`` (or
    ``GLV_*`` env) are honored as configuration — and still self-heal:
    there is no alternate transport to fall back to."""

    HEAL_ATTEMPTS = 3

    def __init__(self, *, base_url: str | None = None,
                 token: str | None = None, project: str = "project",
                 events: list | None = None):
        self.base_url = (base_url or os.environ.get("GLV_BASE_URL")
                         or DEFAULT_GLV_URL)
        self.url_explicit = bool(base_url or os.environ.get("GLV_BASE_URL"))
        self.explicit_token = token or os.environ.get("GLV_API_TOKEN") or None
        self.project = project
        self.events = events if events is not None else []
        self.token = self.explicit_token
        self.client = None
        self.minted: list[str] = []
        self.current_name: str | None = None

    # -- bootstrap ----------------------------------------------

    def _export_env(self) -> None:
        os.environ["GLV_BASE_URL"] = self.base_url
        os.environ["GLV_API_TOKEN"] = self.token

    def _build_client(self) -> None:
        self.client = GlvClient(self.base_url, self.token)

    def begin_session(self, *, index: int, name: str) -> None:
        """The session's first act (issue #47), unconditional: server
        ensured, this session's own named token minted, env exported,
        client built. Called before every session start and before
        adopting a live session — no human step anywhere."""
        self.current_name = name
        if self.client is None:
            if self.explicit_token is not None and self.url_explicit:
                # fully configured via flags/env: trusted as-is; the first
                # call proves it, self-heal fixes it (re-mint on 401)
                self.events.append(
                    {"event": "glv-bootstrap", "mode": "configured",
                     "base_url": self.base_url})
            elif self.explicit_token is not None:
                # a token but no URL: the server it belongs to must
                # already exist — starting a fresh local one would mint
                # against the wrong data dir
                if not _probe_server(self.base_url):
                    raise ToolError(
                        f"GLV_API_TOKEN/--glv-token is set but nothing "
                        f"answers at {self.base_url} — set GLV_BASE_URL to "
                        f"the server that token belongs to (issue #47)",
                        kind="io")
                self.events.append(
                    {"event": "glv-bootstrap", "mode": "configured",
                     "base_url": self.base_url})
            else:
                status = _ensure_server(self.base_url)
                self.token = _mint_token(self.base_url, name)
                self.minted.append(name)
                self._export_env()
                self.events.append(
                    {"event": "glv-bootstrap", "mode": "self",
                     "base_url": self.base_url, "server": status,
                     "token_name": name})
            self._build_client()
            return
        if self.explicit_token is None and name not in self.minted:
            # every chain session mints its own named token — one
            # run-<project>-s<n> per session in `glv token list`
            self.token = _mint_token(self.base_url, name)
            self.minted.append(name)
            self._export_env()
            self._build_client()
            self.events.append({"event": "glv-token-minted",
                                "token_name": name})

    # -- self-healing -------------------------------------------

    def _remint(self) -> None:
        """A 401 run (the 30-day expiry): a fresh token under the same
        session name, then retry — re-auth is never a stop."""
        name = self.current_name or f"run-{self.project}-s?"
        self.token = _mint_token(self.base_url, name)
        self.minted.append(name)
        self._export_env()
        self._build_client()
        self.events.append({"event": "glv-reauth", "token_name": name})

    def _revive(self) -> None:
        """Connection loss: the harness owns the server lifecycle — bring
        it back (probe → start → wait) and rebuild the client. A token
        lives in the server's data dir and survives the restart; if it
        does not, the next 401 re-mints."""
        _ensure_server(self.base_url)
        self._build_client()
        self.events.append({"event": "glv-server-revived",
                            "base_url": self.base_url})

    def _call(self, method: str, *args, **kwargs):
        if self.client is None:
            raise ToolError("GLV transport not bootstrapped — call "
                            "begin_session() first", kind="io")
        last: Exception | None = None
        for _ in range(self.HEAL_ATTEMPTS):
            try:
                return getattr(self.client, method)(*args, **kwargs)
            except GlvAuthError as e:
                last = e
                self._remint()
            except GlvUnreachableError as e:
                last = e
                self._revive()
        raise ToolError(
            f"the GLV transport did not recover after "
            f"{self.HEAL_ATTEMPTS} self-healing attempts ({last}) — the "
            f"chain ends failed_safely; re-running succession.py chain "
            f"re-bootstraps the transport and adopts any live session",
            kind="io")

    # -- the GlvClient surface, healed --------------------------

    def start_session(self, **kw) -> dict:
        return self._call("start_session", **kw)

    def wait_settled(self, session_id: str, **kw) -> dict:
        return self._call("wait_settled", session_id, **kw)

    def stop(self, session_id: str) -> dict:
        return self._call("stop", session_id)

    def public(self) -> dict:
        """Audit-trail view for the tool's JSON output — token NAMES
        only; a raw token is never emitted."""
        return {"base_url": self.base_url,
                "token_source": ("explicit" if self.explicit_token
                                 else "self-minted"),
                "tokens_minted": list(self.minted)}


# ------------------------------------------------------------- loading ----

def _load_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ToolError(f"state {path}: {e}", kind="io") from e
    except json.JSONDecodeError as e:
        raise ToolError(f"state {path}: invalid JSON: {e}", kind="usage") from e
    if not isinstance(state, dict):
        raise ToolError(f"state {path}: must contain a JSON object", kind="usage")
    return state


def _chain_block(state: dict) -> dict:
    chain = state.get("session_chain")
    return chain if isinstance(chain, dict) else {}


def _ceilings(state: dict, policy: dict, args) -> tuple[int | None, int | None, int]:
    """(max_tokens, max_wall_clock_minutes, max_chain_depth) — same
    precedence as pipeline.run: the state's autonomy block wins over the
    policy; an explicit --max-depth wins over both."""
    budget = {**(policy.get("budget") or {}),
              **(((state.get("autonomy") or {}).get("budget")) or {})}
    max_tokens = budget.get("max_tokens")
    max_minutes = budget.get("max_wall_clock_minutes")
    max_depth = (args.max_depth or budget.get("max_chain_depth")
                 or DEFAULT_MAX_CHAIN_DEPTH)
    return max_tokens, max_minutes, int(max_depth)


def _minutes_between(start_iso: str, end_iso: str) -> float:
    try:
        start = datetime.fromisoformat(str(start_iso).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(end_iso).replace("Z", "+00:00"))
        return max(0.0, (end - start).total_seconds() / 60.0)
    except (ValueError, TypeError):
        return 0.0


# ------------------------------------------------------ chain bookkeeping --

def _progress_snapshot(state: dict) -> dict:
    """The observable state a successor must advance: the stage plus the
    per-status unit counts (and token spend, for the record)."""
    counts = Counter(u.get("status", "?") for u in state.get("units", []))
    return {"stage": state.get("stage"),
            "unit_status": dict(counts),
            "tokens_spent": pipeline_tool.tokens_spent(state)}


def _progressed_since(state: dict, snapshot: dict) -> bool:
    """Monotonic-progress guard: the stage advanced, or ≥1 unit newly
    decided vs. the spawn snapshot. Kills infinite resume loops of a
    successor that keeps exhausting without moving the run."""
    if state.get("stage", 0) > (snapshot.get("stage") or 0):
        return True
    counts = snapshot.get("unit_status") or {}
    decided_then = sum(counts.get(s, 0) for s in DECIDED_UNIT_STATUSES)
    decided_now = sum(1 for u in state.get("units", [])
                      if u.get("status") in DECIDED_UNIT_STATUSES)
    return decided_now > decided_then


def _journal_write(state: dict, path: Path, changes: list, events: list,
                   field: str) -> dict:
    """Write through state.py (the ONLY writer); a rejection keeps the
    file as-is and records the failure (the chain still reports)."""
    try:
        state_tool.save_validated(state, path, actor=TOOL, changes=changes)
    except ValueError as e:
        events.append({"event": "journal-write-rejected", "field": field,
                       "detail": str(e)})
    return _load_state(path)


def _set_run_status(state: dict, path: Path, status: str, events: list,
                    reason: str) -> dict:
    old = state.get("run_status")
    if old == status:
        return state
    state["run_status"] = status
    state = _journal_write(state, path,
                           [(None, "run_status", old, status)], events,
                           "run_status")
    events.append({"event": "run-status", "from": old, "to": status,
                   "reason": reason})
    return state


def _chain_session(state: dict, index: int) -> dict | None:
    for s in _chain_block(state).get("sessions") or []:
        if isinstance(s, dict) and s.get("index") == index:
            return s
    return None


def _append_session(state: dict, path: Path, entry: dict, events: list,
                    max_depth: int) -> dict:
    """Append one orchestrator session record (spawn time) and keep
    depth/max_depth coherent — one validated write."""
    chain = json.loads(json.dumps(state.get("session_chain") or {}))
    sessions = chain.get("sessions") or []
    old_depth = chain.get("depth", 0)
    chain["sessions"] = sessions + [entry]
    chain["depth"] = max(0, len(chain["sessions"]) - 1)
    # the cap in force when this session was appended; appends only happen
    # while len(sessions) <= cap, so depth <= max_depth holds by construction
    chain["max_depth"] = (max_depth or chain.get("max_depth")
                          or DEFAULT_MAX_CHAIN_DEPTH)
    state["session_chain"] = chain
    events.append({"event": "session-started", "index": entry["index"],
                   "id": entry["id"], "name": entry["name"],
                   "succession": bool(entry.get("succession_reason"))})
    return _journal_write(
        state, path,
        [(None, "session_chain.sessions",
          f"<list:{len(sessions)} items>",
          f"<list:{len(sessions) + 1} items>"),
         (None, "session_chain.depth", old_depth, chain["depth"])],
        events, "session_chain")


def _update_session(state: dict, path: Path, index: int, updates: dict,
                    events: list) -> dict:
    label = (f"session_chain.sessions["
             f"{(_chain_session(state, index) or {}).get('id', index)}]")
    for i, s in enumerate(state.get("session_chain", {}).get("sessions", [])):
        if isinstance(s, dict) and s.get("index") == index:
            state["session_chain"]["sessions"][i] = {**s, **updates}
            break
    return _journal_write(state, path, [(None, label, None, updates)],
                          events, label)


def _append_notice(state: dict, path: Path, notice: str, events: list) -> dict:
    """The stop-notice: the operator-informing artifact of every
    succession — journaled into the state file (single source of truth)
    and echoed in the run report and this tool's JSON output."""
    chain = json.loads(json.dumps(state.get("session_chain") or {}))
    count = len(chain.get("notices") or [])
    chain["notices"] = list(chain.get("notices") or []) + [notice]
    # a notice may be the FIRST chain write (e.g. the transport died
    # before session 1 could start): keep the block coherent for
    # state.py's validation — sessions/depth/max_depth present and
    # consistent, so the journaled notice cannot be rejected
    chain.setdefault("sessions", [])
    chain.setdefault("depth", max(0, len(chain["sessions"]) - 1))
    chain.setdefault("max_depth", DEFAULT_MAX_CHAIN_DEPTH)
    state["session_chain"] = chain
    events.append({"event": "stop-notice", "notice": notice})
    return _journal_write(
        state, path,
        [(None, "session_chain.notices", f"<list:{count} items>",
          f"<list:{count + 1} items>")],
        events, "session_chain.notices")


def _accumulate_wall_clock(state: dict, path: Path, index: int,
                           events: list) -> dict:
    """Run-level wall clock is the SUM over every session in the chain —
    successors get a fresh session budget, never a fresh run budget."""
    session = _chain_session(state, index) or {}
    minutes = _minutes_between(session.get("started_at") or "",
                               session.get("settled_at") or "")
    chain = state.get("session_chain") or {}
    old = chain.get("wall_clock_minutes", 0.0)
    chain["wall_clock_minutes"] = round(float(old) + minutes, 2)
    state["session_chain"] = chain
    return _journal_write(
        state, path,
        [(None, "session_chain.wall_clock_minutes", old,
          chain["wall_clock_minutes"])],
        events, "session_chain.wall_clock_minutes")


# -------------------------------------------------------- resume hygiene --

def _existing_dirs(values) -> list[Path]:
    """Normalize candidate values to existing absolute directories,
    deduped, first-seen order kept."""
    out: list[Path] = []
    for value in values:
        if not value:
            continue
        p = Path(str(value)).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        if p.is_dir() and p not in out:
            out.append(p)
    return out


def _roots_for(state: dict, state_path: Path,
               extra: str | None = None) -> list[Path]:
    """Candidate roots a recorded relative path may resolve against, most
    specific first: an explicit root, the preflight-recorded target root,
    then the state file's neighborhood (runs/<project>/ inside the target
    repo is the documented layout)."""
    return _existing_dirs([
        extra,
        ((state.get("preflight") or {}).get("target") or {}).get("root"),
        state_path.parent,
        state_path.parent.parent,
        Path.cwd(),
    ])


def _search_roots_for(state: dict, state_path: Path,
                      root_override: str | None) -> list[Path]:
    """Roots for the same-basename candidate search: TARGET repo(s) only
    — an explicit --root or the preflight-recorded root, at most the
    state file's own directory (the runs/<project>/ layout) — never the
    cwd or loose parent directories: a bare basename matches far too
    widely outside the project to stay unambiguous."""
    return _existing_dirs([
        root_override,
        ((state.get("preflight") or {}).get("target") or {}).get("root"),
        str(state_path.parent),
    ])


def _exists_under(rel: str, roots: list[Path]) -> Path | None:
    for root in roots:
        candidate = root / rel
        if candidate.is_file() or candidate.is_dir():
            return candidate
    return None


def _rust_path_alternatives(rust_path: str) -> list[str]:
    """Recorded-but-missing rust paths most often mis-record the crate
    prefix: ``crates/<crate>/src/u.rs`` vs the workspace-member layout
    ``src/u.rs`` (run 20261028_4). Only mechanical re-spellings — never a
    guess about where a file *should* live."""
    parts = Path(rust_path).parts
    if len(parts) >= 3 and parts[0] == "crates":
        return [str(Path(*parts[2:]))]  # strip crates/<crate>/
    return []


def _finding(check: str, unit, recorded, found, action: str,
             detail: str) -> dict:
    return {"check": check, "unit": unit, "recorded": recorded,
            "found": found, "action": action, "detail": detail}


def _path_finding(check: str, unit, rel, roots: list[Path]) -> dict:
    """Verify one recorded relative path: ok / skipped (not recorded) /
    unresolved (missing under every candidate root)."""
    if not rel:
        return _finding(check, unit, rel, None, "skipped", "not recorded")
    found = _exists_under(str(rel), roots)
    if found is None:
        return _finding(check, unit, rel, None, "unresolved",
                        "not found under any candidate root")
    return _finding(check, unit, rel, str(found), "ok", "")


def _basename_matches_under(root: Path, base: str) -> dict[Path, str]:
    """Same-basename files under one root as {absolute: root-relative}; an
    unreadable tree contributes nothing (OSError can fire mid-walk)."""
    matches: dict[Path, str] = {}
    try:
        matches = {hit.resolve(): str(hit.relative_to(root))
                   for hit in root.rglob(base) if hit.is_file()}
    except OSError:
        pass
    return matches


def _rust_replacement_candidates(rust: str, roots: list[Path],
                                 search_roots: list[Path]) -> list[str]:
    """Replacement candidates for a missing rust_path: the mechanical
    re-spellings that exist on disk, plus same-basename files under the
    search roots — deduped by ABSOLUTE path so one file found under two
    roots (the target root and the state dir inside it) stays one
    candidate."""
    candidates = {alt for alt in _rust_path_alternatives(rust)
                  if _exists_under(alt, roots) is not None}
    hits: dict[Path, str] = {}
    for root in search_roots:
        for abs_path, rel in _basename_matches_under(
                root, Path(rust).name).items():
            hits.setdefault(abs_path, rel)
    for alt in _rust_path_alternatives(rust):
        for root in search_roots:
            if (root / alt).is_file():
                hits.setdefault((root / alt).resolve(), alt)
    return sorted(candidates | set(hits.values()))


def _repair_rust_path(state: dict, state_path: Path, unit: dict,
                      replacement: str) -> None:
    """Rewrite one rust_path through state.py's validated writer
    (journaled); on rejection the in-memory unit is rolled back so state
    and file stay in step."""
    uid = unit.get("id")
    old = unit.get("rust_path")
    unit["rust_path"] = replacement
    try:
        state_tool.save_validated(
            state, state_path, actor=TOOL,
            changes=[(uid, f"units[{uid}].rust_path", old, replacement)])
    except ValueError:
        unit["rust_path"] = old
        raise


def _unit_path_findings(state: dict, state_path: Path, unit: dict, *,
                        roots: list[Path], search_roots: list[Path],
                        apply: bool) -> tuple[list[dict], dict]:
    """Hygiene findings for one unit. python_path is the target's own
    layout — reported, never rewritten; a rust_path repair fires only
    when exactly ONE candidate exists on disk (ambiguity is reported,
    never guessed). Returns (findings, state) — state is reloaded after a
    journaled repair."""
    uid = unit.get("id")
    findings = [_path_finding(f"units[{uid}].python_path", uid,
                              unit.get("python_path"), roots)]
    rust = unit.get("rust_path")
    if rust is None:
        return findings, state
    if _exists_under(str(rust), roots) is not None:
        return findings + [_finding(f"units[{uid}].rust_path", uid,
                                    rust, rust, "ok", "")], state
    candidates = _rust_replacement_candidates(str(rust), roots, search_roots)
    if len(candidates) != 1:
        detail = (f"{len(candidates)} candidate(s) on disk "
                  f"({', '.join(candidates[:3])})" if candidates
                  else "no candidate found under any candidate root")
        return findings + [_finding(f"units[{uid}].rust_path", uid, rust,
                                    None, "unresolved", detail)], state
    if not apply:
        return findings + [_finding(
            f"units[{uid}].rust_path", uid, rust, candidates[0],
            "would-repair", "exactly one candidate exists on disk")], state
    try:
        _repair_rust_path(state, state_path, unit, candidates[0])
    except ValueError as e:
        detail = f"repair rejected by state validation: {e}"
        return findings + [_finding(f"units[{uid}].rust_path", uid, rust,
                                    None, "unresolved", detail)], state
    return findings + [_finding(
        f"units[{uid}].rust_path", uid, rust, candidates[0], "repaired",
        "exactly one candidate exists on disk; repaired through state.py "
        "(journaled)")], _load_state(state_path)


def resume_hygiene(state: dict, state_path: Path, *, apply: bool = True,
                   root_override: str | None = None) -> tuple[dict, list[dict]]:
    """Verify every recorded path at succession time (issue #25
    resume-hygiene). Returns (state, findings); each finding carries
    action ∈ ok | repaired | would-repair | unresolved | skipped."""
    roots = _roots_for(state, state_path, root_override)
    if not roots:
        return state, [_finding("roots", None, None, None, "unresolved",
                                "no candidate root exists on disk")]
    search_roots = _search_roots_for(state, state_path, root_override)
    project_path = (state.get("project") or {}).get("python_path")
    findings = [_path_finding("project.python_path", None, project_path,
                              roots)]
    # iterated exactly once over the loaded units: a mid-loop reload (a
    # journaled repair) must not restart or duplicate the walk
    for unit in state.get("units") or []:
        unit_findings, state = _unit_path_findings(
            state, state_path, unit, roots=roots, search_roots=search_roots,
            apply=apply)
        findings += unit_findings
    return state, findings


def _unresolved(findings: list[dict]) -> list[dict]:
    return [f for f in findings
            if str(f.get("action", "")).startswith("unresolved")]


# ------------------------------------------------------------- briefing ---

def _dossier_lines(state: dict) -> list[str]:
    escalated = [u for u in state.get("units", [])
                 if u.get("status") == "escalated"]
    deferred = [g for g in state.get("human_gates", [])
                if g.get("state") in ("open", "deferred")]
    lines: list[str] = []
    if not escalated and not deferred:
        lines.append("_No escalations — the queue is empty._")
    for u in escalated:
        lines.append(f"- unit **{u.get('id')}** ({u.get('python_path')}): "
                     f"python fallback live; {u.get('notes') or 'see state file'}")
    for g in deferred:
        lines.append(f"- gate **{g.get('gate')}** [{g.get('state')}]: "
                     f"{g.get('note') or 'see state file'}")
    return lines


def _session_line(s: dict) -> str:
    return (f"- s{s.get('index')} **{s.get('name')}** ({s.get('id')}): "
            f"{s.get('outcome') or 'in flight'}"
            + (f" — {s.get('succession_reason')}" if s.get("succession_reason")
               else ""))


def compose_resume_brief(*, state: dict, state_path: Path, successor_index: int,
                         max_depth: int, max_tokens: int | None,
                         max_minutes: int | None, hygiene_findings: list[dict],
                         framework: Path = FRAMEWORK) -> str:
    """The successor's complete resume context: everything a fresh session
    needs to continue the SAME run with zero human input (state file path,
    position, run-level budget remaining, escalation dossier, journal
    tail, chain history, hygiene findings, termination contract)."""
    tokens = pipeline_tool.tokens_spent(state)
    wall = _chain_block(state).get("wall_clock_minutes") or 0
    counts = Counter(u.get("status", "?") for u in state.get("units", []))
    pending = [u.get("id") for u in state.get("units", [])
               if u.get("status") == "pending"]
    journal = state.get("journal") or []
    sessions = _chain_block(state).get("sessions") or []

    tokens_line = (f"- tokens: {tokens} / {max_tokens} "
                   f"({max(0, max_tokens - tokens)} remaining)"
                   if max_tokens is not None else
                   f"- tokens: {tokens} spent (no run-level ceiling recorded)")
    wall_line = (f"- wall clock: {wall:.1f} / {max_minutes} min "
                 f"({max(0, max_minutes - wall):.1f} min remaining)"
                 if max_minutes is not None else
                 f"- wall clock: {wall:.1f} min spent (no run-level ceiling "
                 f"recorded)")

    lines = [
        f"# Successor session {successor_index} — migration resume "
        f"(chain depth {successor_index - 1}/{max_depth})",
        "",
        "You are the successor of an exhausted orchestrator session. The run "
        "itself is NOT finished — your predecessor spent its session "
        "(context-window) budget, and the chain harness "
        "(tools/succession.py) spawned you to continue the SAME run. Your "
        "session budget is fresh; the RUN-level ceilings below still bind "
        "and are summed across every session in this chain.",
        "",
        "## Resume contract",
        "",
        f"- state file (the single source of truth): `{state_path.resolve()}`",
        "- run_status has been reset to `running` by the chain harness",
        f"- framework checkout: `{framework}` — read "
        f"`{framework / 'MIGRATION_PROMPT.md'}` first; per-stage checklists "
        f"in `{framework / 'stages'}`",
        "- record every transition through `tools/state.py` (it is the ONLY "
        "writer of the state file); never edit migration-state.json directly",
        "- re-bootstrap GLV yourself from this cold start, before any "
        "chain work: the runbook's GLV bootstrap section — "
        f"`{framework / 'tools' / 'glv-subagents.md'}` § \"GLV bootstrap\" "
        "(ensure `glv serve` is up, mint this session's own token with "
        "`glv token create --name run-<project>-s<n>`, export "
        "GLV_BASE_URL/GLV_API_TOKEN, re-mint on a 401 run); GLV is the "
        "only transport — never assume the predecessor's environment "
        "survives it",
        "- the agent never asks a human mid-run, at any level — gates resolve "
        "in code (autonomy/policy.json) or the run ends with the dossier",
        "",
        "## Position",
        "",
        f"- stage: {state.get('stage')}/7 · run_status at hand-off: "
        f"{state.get('run_status')}",
        f"- autonomy: {state_tool.autonomy_level(state)}",
        f"- units: {len(state.get('units', []))} — "
        + (", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "none"),
        f"- pending units (leaf-first order): {', '.join(pending)}"
        if pending else "- no pending units",
        "",
        "## Run-level budget (across the whole chain — the sum of all sessions)",
        "",
        tokens_line,
        wall_line,
        "",
        "## Escalation dossier",
        "",
        *_dossier_lines(state),
        "",
        f"## Journal tail (last {JOURNAL_TAIL_ENTRIES} transitions)",
        "",
        *[f"- {e.get('timestamp')} {e.get('field')}: "
          f"{e.get('from')!r} -> {e.get('to')!r} ({e.get('actor')})"
          for e in journal[-JOURNAL_TAIL_ENTRIES:]],
        "",
        "## Session chain so far",
        "",
        *[_session_line(s) for s in sessions],
        "",
        "## Resume hygiene (verified at succession time)",
        "",
    ]
    repairs = [f for f in hygiene_findings if f.get("action") in
               ("repaired", "would-repair")]
    lines += ([f"- repaired: {f['check']}: `{f['recorded']}` -> "
               f"`{f['found']}` (journaled)" for f in repairs]
              or ["- no repairs needed"])
    lines += [f"- WARNING unresolved: {f['check']}: recorded "
              f"`{f['recorded']}` ({f.get('detail') or 'not found on disk'}) "
              f"— verify before relying on it"
              for f in _unresolved(hygiene_findings)]
    lines += [
        "",
        "## Termination contract",
        "",
        "- end the run in `complete` / `no_go_measured` / `failed_safely` — "
        "recorded in the state file through the terminal-state discipline",
        "- `budget_exhausted` only when the RUN-level ceilings above actually "
        "bind (the harness will chain another successor); your own session "
        "budget ending is exactly that signal, never a reason to stop early "
        "or leave work half-recorded",
        "- every unit ends ported / divergent_explained / escalated (fallback "
        "live) / fallback_python — never silently pending",
        "",
    ]
    return "\n".join(lines)


def compose_start_brief(*, prompt_text: str, state: dict, state_path: Path,
                        max_depth: int, framework: Path = FRAMEWORK) -> str:
    """Session 1 rides the operator's single prompt; the harness prepends
    only the chain context every session shares."""
    header = "\n".join([
        "# Migration run — orchestrator session 1 of the successor chain",
        "",
        f"- state file (the single source of truth): `{state_path.resolve()}`",
        f"- framework checkout: `{framework}`",
        f"- first act, before any stage work: bootstrap GLV yourself per "
        f"the runbook — `{framework / 'tools' / 'glv-subagents.md'}` § "
        f"\"GLV bootstrap\" (ensure `glv serve` is up, mint this session's "
        f"own token with `glv token create --name run-<project>-s1`, "
        f"export GLV_BASE_URL/GLV_API_TOKEN, re-mint on a 401 run); GLV "
        f"is the only transport",
        f"- this run is chained (tools/succession.py, max depth {max_depth}): "
        "if your session budget exhausts before the run reaches a terminal "
        "state, record `budget_exhausted` and stop — a successor session "
        "continues from this state file automatically; never ask a human",
        "",
        "---",
        "",
    ])
    return header + prompt_text


def _session_one_prompt(args) -> str:
    prompt_file = getattr(args, "prompt_file", None)
    if not prompt_file:
        return DEFAULT_SESSION_ONE_PROMPT
    return Path(prompt_file).read_text(encoding="utf-8")


def _next_brief(state: dict, state_path: Path, *, policy: dict, args,
                hygiene_findings: list[dict] | None = None) -> tuple[str, int]:
    """The brief for the NEXT session: fresh start (the operator's single
    prompt) vs. successor resume (composed from the state file)."""
    max_tokens, max_minutes, max_depth = _ceilings(state, policy, args)
    index = len(_chain_block(state).get("sessions") or []) + 1
    if index == 1:
        return compose_start_brief(
            prompt_text=_session_one_prompt(args), state=state,
            state_path=state_path, max_depth=max_depth), index
    return compose_resume_brief(
        state=state, state_path=state_path, successor_index=index,
        max_depth=max_depth, max_tokens=max_tokens, max_minutes=max_minutes,
        hygiene_findings=hygiene_findings or []), index


# --------------------------------------------------------- classification --

def _classify(state: dict, *, max_tokens: int | None, max_minutes: int | None,
              max_depth: int) -> dict:
    """Decide what the chain does with the last settled session. Pure
    function of the state file + the ceilings — deterministic, testable.

    kind: terminal (chain ends, genuine) · run_level_exhausted (chain
    ends; budget_exhausted is the honest result — the pre-authorized
    bound is spent) · no_progress / depth_cap (chain ends failed_safely)
    · succeed (spawn the successor)."""
    run_status = state.get("run_status")
    if run_status in GENUINE_TERMINALS:
        return {"kind": "terminal", "run_status": run_status,
                "reason": f"session settled in the genuine terminal state "
                          f"{run_status!r} — chain ends"}
    if run_status not in SUCCEED_ELIGIBLE:
        return {"kind": "terminal", "run_status": run_status,
                "reason": f"unexpected run_status {run_status!r} — treated as "
                          f"terminal; inspect the state file"}
    tokens = pipeline_tool.tokens_spent(state)
    wall = _chain_block(state).get("wall_clock_minutes") or 0.0
    sessions = _chain_block(state).get("sessions") or []
    if max_tokens is not None and tokens > max_tokens:
        return {"kind": "run_level_exhausted",
                "reason": f"run-level token ceiling reached across the "
                          f"chain: {tokens} > {max_tokens} over "
                          f"{len(sessions)} session(s) — the "
                          f"pre-authorized budget is spent"}
    if max_minutes is not None and wall > max_minutes:
        return {"kind": "run_level_exhausted",
                "reason": f"run-level wall-clock ceiling reached across "
                          f"the chain: {wall:.1f} > {max_minutes} min "
                          f"over {len(sessions)} session(s)"}
    if len(sessions) >= 2:
        snapshot = (sessions[-1] or {}).get("progress_snapshot") or {}
        if not _progressed_since(state, snapshot):
            return {"kind": "no_progress",
                    "reason": "successor session settled "
                              f"({run_status!r}) after advancing no "
                              "observable state (stage or ≥1 unit "
                              "decided) — chain terminated to prevent "
                              "an infinite resume loop"}
    if len(sessions) + 1 > max_depth:
        return {"kind": "depth_cap",
                "reason": f"chain depth cap {max_depth} reached with the "
                          f"run unfinished ({run_status!r})"}
    return {"kind": "succeed",
            "reason": f"session-level exhaustion ({run_status!r}) under "
                      f"the run-level ceilings — successor spawns "
                      f"automatically"}


# ---------------------------------------------------------------- chain ---

def _start_session(state: dict, path: Path, transport: GlvTransport,
                   brief: str, events: list, *, project: str,
                   working_dir: str, model: str | None, max_depth: int,
                   succession_reason: str | None,
                   snapshot: dict) -> tuple[dict, int]:
    """Start one GLV orchestrator session with the brief and record it in
    the chain (does NOT wait — the caller may need to journal the
    succession and reset run_status before the successor starts working).
    The transport is (re)bootstrapped here first — server ensured + this
    session's own named token minted (issue #47). Returns the reloaded
    state + session index."""
    index = len(_chain_block(state).get("sessions") or []) + 1
    name = f"run-{project}-s{index}"
    transport.begin_session(index=index, name=name)
    started = transport.start_session(name=name, prompt=brief,
                                      working_dir=working_dir, model=model)
    sid = started.get("id") or f"unknown-{index}"
    entry = {"index": index, "id": sid, "name": name,
             "started_at": now_iso(),
             "succession_reason": succession_reason,
             "progress_snapshot": snapshot}
    return _append_session(state, path, entry, events, max_depth), index


def _settle(state: dict, path: Path, transport: GlvTransport, index: int,
            events: list) -> dict:
    session = _chain_session(state, index) or {}
    settled = transport.wait_settled(session.get("id") or "")
    # the session JUST wrote the state file (its last act); re-read before
    # journaling the settle bookkeeping or this write would clobber it
    state = _load_state(path)
    state = _update_session(state, path, index,
                            {"settled_at": now_iso(),
                             "glv_status": settled.get("status")}, events)
    state = _accumulate_wall_clock(state, path, index, events)
    state = _update_session(state, path, index,
                            {"outcome": state.get("run_status")}, events)
    events.append({"event": "session-settled", "index": index,
                   "glv_status": settled.get("status"),
                   "outcome": state.get("run_status")})
    return state


def _refusal(reason: str, human_flag: bool, **extra) -> int:
    payload = {"tool": TOOL, "version": VERSION, "error": reason,
               "summary": {"exit_code": EXIT_FINDINGS, "human": reason}}
    payload.update(extra)
    emit(payload, human_flag=human_flag)
    return EXIT_FINDINGS


def _public_chain(state: dict) -> dict:
    chain = _chain_block(state)
    return {"depth": chain.get("depth", 0),
            "max_depth": chain.get("max_depth"),
            "wall_clock_minutes": chain.get("wall_clock_minutes", 0),
            "sessions": chain.get("sessions") or [],
            "notices": chain.get("notices") or []}


def _dry_run_chain(args, state: dict, path: Path, policy: dict,
                   ceilings: tuple[int | None, int | None, int]) -> int:
    """Compose the next session's brief + the guardrail plan without
    contacting GLV or writing anything."""
    max_tokens, max_minutes, max_depth = ceilings
    state, findings = resume_hygiene(state, path, apply=False)
    settled = [s for s in _chain_block(state).get("sessions") or []
               if isinstance(s, dict) and s.get("settled_at")]
    decision = None
    if settled:
        decision = _classify(state, max_tokens=max_tokens,
                             max_minutes=max_minutes, max_depth=max_depth)
        if decision["kind"] == "terminal":
            human = (f"dry-run: the chain is already finished — "
                     f"{decision['reason']}")
            emit({"tool": TOOL, "version": VERSION, "file": str(path),
                  "dry_run": True, "finished": True,
                  "classification": decision, "hygiene": findings,
                  "summary": {"exit_code": EXIT_OK, "human": human}},
                 human_flag=args.human)
            return EXIT_OK
    brief, index = _next_brief(state, path, policy=policy, args=args,
                               hygiene_findings=findings)
    human = (f"dry-run: next is session s{index} of max {max_depth}; "
             f"ceilings tokens={max_tokens or '∞'} "
             f"wall={max_minutes or '∞'} min; "
             f"{len(_unresolved(findings))} unresolved hygiene finding(s)")
    emit({"tool": TOOL, "version": VERSION, "file": str(path),
          "dry_run": True, "next_session": index, "max_depth": max_depth,
          "ceilings": {"max_tokens": max_tokens,
                       "max_wall_clock_minutes": max_minutes},
          "classification": decision,
          "hygiene": findings, "brief": brief,
          "summary": {"exit_code": EXIT_OK, "human": human}},
         human_flag=args.human)
    return EXIT_OK


def _run_first_session(state: dict, path: Path, transport: GlvTransport,
                       policy: dict, args, events: list, *, project: str,
                       working_dir: str, max_depth: int) -> dict:
    """Session 1: the operator's single prompt, not a succession.
    Resumable statuses flip to running first (the same rule as
    pipeline.run), journaled through state.py."""
    if state.get("run_status") in (None, "pending", "budget_exhausted",
                                   "failed_safely"):
        state = _set_run_status(state, path, "running", events,
                                "chain session 1 starting")
    brief, _ = _next_brief(state, path, policy=policy, args=args)
    state, _ = _start_session(state, path, transport, brief, events,
                              project=project, working_dir=working_dir,
                              model=args.model, max_depth=max_depth,
                              succession_reason=None,
                              snapshot=_progress_snapshot(state))
    return _settle(state, path, transport,
                   len(_chain_block(state)["sessions"]), events)


def _stop_predecessor_if_live(transport: GlvTransport, predecessor: dict,
                              events: list) -> None:
    if predecessor.get("glv_status") not in GLV_LIVE_STATUSES:
        return
    try:
        transport.stop(predecessor.get("id"))
        events.append({"event": "predecessor-stopped",
                       "id": predecessor.get("id")})
    except (GlvError, ToolError) as e:
        events.append({"event": "predecessor-stop-failed",
                       "id": predecessor.get("id"), "detail": str(e)})


def _record_succession(state: dict, path: Path, predecessor: dict,
                       successor: dict, events: list) -> dict:
    """The stop-notice (operator informed, not involved) plus the
    run_status reset that lets the successor record into the same run."""
    state = _append_notice(
        state, path,
        f"stop-notice: s{predecessor.get('index')} "
        f"{predecessor.get('name')} ({predecessor.get('id')}) settled "
        f"{predecessor.get('outcome')!r} at stage {state.get('stage')} — "
        f"successor s{successor.get('index')} {successor.get('name')} "
        f"({successor.get('id')}) spawned automatically with full resume "
        f"context; run_status reset to running; operator informed, not "
        f"involved", events)
    state = _set_run_status(state, path, "running", events,
                            "successor session spawned by the chain")
    events.append({"event": "succession", "to_session": successor.get("index"),
                   "depth": _chain_block(state).get("depth")})
    return state


def _park_transport_dead(state: dict, path: Path, error: ToolError,
                         events: list) -> tuple[dict, str]:
    """The transport could not be self-healed (issue #47): the chain ends
    failed_safely — a genuine terminal state with the dossier, never a
    park and never a hand-off document. The stop-notice INFORMS the
    operator; it never asks them, and there is no alternate transport to
    fall back to. Re-running the chain re-bootstraps GLV from scratch
    and adopts any live session (restartable anywhere)."""
    reason = f"the GLV transport could not be revived: {error}"
    state = _set_run_status(state, path, "failed_safely", events, reason)
    state = _append_notice(
        state, path,
        f"stop-notice: chain ended failed_safely — {reason}; re-run "
        f"tools/succession.py chain to re-bootstrap GLV (serve + a fresh "
        f"named token) and adopt any live session", events)
    return state, reason


def _apply_decision(decision: dict, state: dict, path: Path,
                    events: list) -> tuple[dict, str | None]:
    """Apply a chain-ending classification; None means 'succeed' — the
    caller spawns the successor."""
    kind, reason = decision["kind"], decision["reason"]
    if kind == "terminal":
        return state, reason
    if kind == "run_level_exhausted":
        if state.get("run_status") != "budget_exhausted":
            state = _set_run_status(state, path, "budget_exhausted", events,
                                    reason)
        return state, reason
    if kind in ("no_progress", "depth_cap"):
        return _set_run_status(state, path, "failed_safely", events,
                               reason), reason
    return state, None


def cmd_chain(args) -> int:
    policy = pipeline_tool.load_policy(args.policy)
    path = Path(args.state)
    state = _load_state(path)
    sessions = _chain_block(state).get("sessions") or []

    if not sessions and state.get("run_status") in FINISHED_RUN_STATUSES:
        return _refusal(
            f"run_status={state['run_status']} is a finished run — start a "
            f"fresh run (state.py init) to migrate again; a chain resumes "
            f"budget_exhausted/failed_safely/interrupted runs, not finished "
            f"ones", args.human, file=str(path))

    ceilings = _ceilings(state, policy, args)
    if args.dry_run:
        return _dry_run_chain(args, state, path, policy, ceilings)

    events: list[dict] = []
    project = (state.get("project") or {}).get("name") or "project"
    working_dir = args.working_dir or \
        (((state.get("preflight") or {}).get("target") or {}).get("root")
         or str(path.parent))
    max_tokens, max_minutes, max_depth = ceilings
    # GLV is the unconditional transport and this harness bootstraps it
    # itself (issue #47): probe GET /api/openapi.json, start `glv serve`
    # when nothing answers, mint run-<project>-s<n> per chain session —
    # no alternate mode, no human provisioning step, anywhere
    transport = GlvTransport(base_url=args.glv_url, token=args.glv_token,
                             project=project, events=events)
    terminal_reason: str | None = None

    try:
        while True:
            state = _load_state(path)
            sessions = _chain_block(state).get("sessions") or []

            if not sessions:
                state = _run_first_session(state, path, transport, policy,
                                           args, events, project=project,
                                           working_dir=working_dir,
                                           max_depth=max_depth)
                continue

            last = sessions[-1] or {}
            if not last.get("settled_at"):
                # the harness itself died mid-session: adopt the live session
                # (never abandon it), wait it out, then re-classify
                transport.begin_session(
                    index=last.get("index") or 1,
                    name=last.get("name")
                    or f"run-{project}-s{last.get('index') or 1}")
                state = _settle(state, path, transport, last.get("index"),
                                events)
                continue

            decision = _classify(state, max_tokens=max_tokens,
                                 max_minutes=max_minutes, max_depth=max_depth)
            events.append({"event": "classified", "kind": decision["kind"],
                           "reason": decision["reason"]})
            state, ending = _apply_decision(decision, state, path, events)
            if ending is not None:
                terminal_reason = ending
                break

            # succeed: hygiene -> brief -> spawn -> stop-notice + reset -> settle
            state, findings = resume_hygiene(state, path)
            events += [{"event": "hygiene-repaired", **f}
                       for f in findings if f.get("action") == "repaired"]
            brief, index = _next_brief(state, path, policy=policy, args=args,
                                       hygiene_findings=findings)
            state, index = _start_session(
                state, path, transport, brief, events, project=project,
                working_dir=working_dir, model=args.model,
                max_depth=max_depth, succession_reason=decision["reason"],
                snapshot=_progress_snapshot(state))
            _stop_predecessor_if_live(transport, last, events)
            successor = _chain_session(state, index) or {}
            state = _record_succession(state, path, last, successor, events)
            state = _settle(state, path, transport, index, events)
    except ToolError as e:
        # the transport could not be self-healed (server dead with no way
        # to start it, a mint that cannot run): a genuine terminal with
        # the dossier — never a park, never a hand-off to a human
        state = _load_state(path)
        state, terminal_reason = _park_transport_dead(state, path, e, events)

    report = pipeline_tool.build_report(state, policy)
    report_path = pipeline_tool.report_path_for(path)
    report_path.write_text(report, encoding="utf-8")
    run_status = state.get("run_status", "pending")
    code = EXIT_FINDINGS if run_status == "failed_safely" else EXIT_OK
    chain_pub = _public_chain(state)
    human = (f"chain finished: run_status={run_status} after "
             f"{len(chain_pub['sessions'])} session(s) "
             f"(depth {chain_pub['depth']}/{max_depth})"
             + (f" — {terminal_reason}" if terminal_reason else "")
             + f"; report: {report_path}")
    emit({"tool": TOOL, "version": VERSION, "file": str(path),
          "run_status": run_status,
          "glv": transport.public(),
          "chain": chain_pub,
          "events": events, "report_path": str(report_path),
          "summary": {"exit_code": code, "human": human}},
         human_flag=args.human)
    return code


# ------------------------------------------------------------- commands ---

def cmd_brief(args) -> int:
    policy = pipeline_tool.load_policy(args.policy)
    path = Path(args.state)
    state = _load_state(path)
    state, findings = resume_hygiene(state, path, apply=False)
    brief, index = _next_brief(state, path, policy=policy, args=args,
                               hygiene_findings=findings)
    _, _, max_depth = _ceilings(state, policy, args)
    human = (f"brief for session s{index} (max depth {max_depth}); "
             f"{len(_unresolved(findings))} unresolved hygiene finding(s)")
    emit({"tool": TOOL, "version": VERSION, "file": str(path),
          "next_session": index, "brief": brief, "hygiene": findings,
          "summary": {"exit_code": EXIT_OK, "human": human}},
         human_flag=args.human)
    return EXIT_OK


def cmd_hygiene(args) -> int:
    path = Path(args.state)
    state = _load_state(path)
    state, findings = resume_hygiene(state, path, apply=not args.no_apply,
                                     root_override=args.root)
    unresolved = _unresolved(findings)
    code = EXIT_FINDINGS if unresolved else EXIT_OK
    human = (f"resume hygiene: {len(findings)} check(s), "
             f"{sum(1 for f in findings if f.get('action') == 'repaired')} "
             f"repaired, {len(unresolved)} unresolved")
    emit({"tool": TOOL, "version": VERSION, "file": str(path),
          "applied": not args.no_apply, "findings": findings,
          "unresolved": unresolved,
          "summary": {"exit_code": code, "human": human}},
         human_flag=args.human)
    return code


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=TOOL, description=__doc__.splitlines()[0])
    ap.add_argument("--human", action="store_true",
                    help="echo human summary to stderr")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser(
        "chain", help="drive the run as a successor-session chain over GLV "
                      "(issue #25): session-level budget_exhausted spawns "
                      "the successor automatically; only genuine terminals "
                      "— or the run-level ceilings — end the chain")
    p.add_argument("state")
    p.add_argument("--prompt-file",
                   help="the operator's single prompt for session 1 "
                        "(markdown); ignored once the chain exists — "
                        "successor briefs are composed from the state file")
    p.add_argument("--working-dir",
                   help="GLV working_dir per session (default: the "
                        "preflight-recorded target root, else the state "
                        "file's directory)")
    p.add_argument("--model", help="GLV model pin (resolve pins via "
                                   "GET /api/models, never free-typed)")
    p.add_argument("--max-depth", type=int,
                   help="chain depth cap (default: autonomy.budget."
                        "max_chain_depth, policy default 10)")
    p.add_argument("--policy", help="override autonomy/policy.json")
    p.add_argument("--glv-url", help="override GLV_BASE_URL (default: the "
                                     "harness self-bootstraps — probe/start "
                                     "`glv serve` per tools/"
                                     "glv-subagents.md § GLV bootstrap)")
    p.add_argument("--glv-token", help="override GLV_API_TOKEN (default: "
                                       "mint run-<project>-s<n> per chain "
                                       "session — the `glv token list` "
                                       "audit trail)")
    p.add_argument("--dry-run", action="store_true",
                   help="compose the next session's brief + guardrail plan "
                        "without contacting GLV or writing anything")
    p.set_defaults(func=cmd_chain)

    p = sub.add_parser("brief",
                       help="compose the next session's brief (first or "
                            "successor) without side effects")
    p.add_argument("state")
    p.add_argument("--prompt-file", help="session-1 prompt (start brief)")
    p.add_argument("--policy")
    p.add_argument("--max-depth", type=int)
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("hygiene",
                       help="verify/repair recorded paths in the state file "
                            "(the succession-time resume check); exit 2 "
                            "while anything is unresolved")
    p.add_argument("state")
    p.add_argument("--root", help="explicit target root to resolve "
                                  "relative paths against (default: "
                                  "preflight.target.root, then the state "
                                  "file's neighborhood)")
    p.add_argument("--no-apply", action="store_true",
                   help="verify only; never write repairs")
    p.set_defaults(func=cmd_hygiene)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(run_cli(main, tool=TOOL, version=VERSION))
