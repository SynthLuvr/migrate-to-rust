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

wraps the whole run in a **chain of GLV orchestrator sessions** (the HTTP
transport ``tools/glv-subagents.md`` already defines for role
delegation): it starts session 1 with the operator's single prompt, parks
on ``GET /api/sessions/:id/wait`` until each session settles, classifies
the outcome from the state file (the single source of truth — sessions
write it only through ``state.py``), and on session-level exhaustion
spawns the successor itself:

  1. **Auto-succession.** ``run_status=budget_exhausted`` (or a session
     that settled without recording any terminal state) under the
     run-level ceilings → compose the full resume brief (state file path,
     position, escalation dossier, journal tail, run-budget remaining,
     chain history, hygiene findings), spawn the successor via
     ``POST /api/sessions``, stop the predecessor if still live, journal
     a **stop-notice** (operator informed, not involved), and reset
     ``run_status`` to ``running`` — all through ``state.py``'s validated
     writer. No human act anywhere.
  2. **Fresh session budget, shared run budget.** Every successor starts
     with a fresh session/context budget; the RUN-level ceilings
     (``autonomy.budget``: 5M tokens / 720 min by default) bind across
     the whole chain — token spend is summed through the one state file
     every session writes, wall clock accumulates per session — so
     runaway stays impossible.
  3. **Guardrails.** A depth cap (``--max-depth``, or
     ``autonomy.budget.max_chain_depth``, policy default 10) and a
     monotonic-progress guard (each successor must advance the stage or
     decide ≥1 unit, vs. the snapshot taken when it was spawned); either
     violated → ``failed_safely`` with the dossier — never an infinite
     resume loop.
  4. **Only genuine terminals end the chain:** ``complete`` /
     ``no_go_measured`` / ``failed_safely``, or the run-level ceilings
     themselves (the pre-authorized bound — the one case where
     ``budget_exhausted`` is the honest human-facing result: the operator
     must raise the ceiling). Session-level ``budget_exhausted`` never
     survives a chain while GLV is reachable.
  5. **Degraded mode.** GLV unreachable (env unset, server down) at
     chain start → refusal (exit 2) with the documented fallback: the
     pre-#25 behavior — ``pipeline.py run`` ends in ``budget_exhausted``,
     a human resumes after raising the ceiling. Unreachable MID-chain →
     the exhausted state stands, a notice says so, exit 0: a terminal
     state, never a hang.
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

Auth/transport per ``tools/glv-subagents.md``: ``GLV_BASE_URL`` +
``GLV_API_TOKEN`` (flags override), bearer on every ``/api`` path. This
tool writes the state file ONLY through ``state.py``'s save_validated
(the "state.py is the ONLY writer" discipline — succession.py composes
it, the way pipeline.py does).

Exit codes follow tools/README.md: 0 any terminal state except
failed_safely (a *result*, not a failure) · 2 findings (failed_safely, a
refusal such as "GLV unreachable at chain start", or unresolved
hygiene) · 1 usage/IO via the shared envelope.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
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
VERSION = 1
FRAMEWORK = Path(__file__).resolve().parent.parent
DEFAULT_MAX_CHAIN_DEPTH = 10
# statuses that mean a unit reached a decision (anything but pending);
# the monotonic-progress guard counts these — "≥1 unit ported/escalated"
DECIDED_UNIT_STATUSES = state_tool.RUST_LIVE_STATUSES | {"escalated", "fallback_python"}
# run_status values that legitimately end a chain (MIGRATION_PROMPT.md
# terminal-state discipline); budget_exhausted is NOT among them — it is a
# scheduling event while run-level budget remains
GENUINE_TERMINALS = ("complete", "no_go_measured", "failed_safely")
GLV_LIVE_STATUSES = ("queued", "running")
JOURNAL_TAIL_ENTRIES = 20


# ------------------------------------------------------------ transport ----

class GlvError(ToolError):
    """GLV transport failure (unreachable / HTTP error). kind='io': the
    caller decides between degraded mode and a refusal — never a crash."""

    def __init__(self, detail: str):
        super().__init__(detail, kind="io")


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
            raise GlvError(f"GLV {method} {path}: HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise GlvError(f"GLV {method} {path} unreachable: {e}") from e

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


def _client_from(args) -> GlvClient:
    url = args.glv_url or os.environ.get("GLV_BASE_URL") or ""
    token = args.glv_token or os.environ.get("GLV_API_TOKEN") or ""
    if not (url and token):
        raise ToolError(
            "GLV transport is not configured: set GLV_BASE_URL and "
            "GLV_API_TOKEN (or --glv-url/--glv-token) per "
            "tools/glv-subagents.md — `glv serve` on the server host, "
            "`glv token create` for the token. Without GLV the successor "
            "chain cannot run: fall back to the pre-#25 degraded mode "
            "(pipeline.py run ends in budget_exhausted; a human resumes "
            "after raising the ceiling).", kind="usage")
    return GlvClient(url, token)


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
    """Monotonic-progress guard: stage advance, or ≥1 unit newly decided
    (ported/divergent_explained/escalated/fallback_python/retired —
    anything but pending). Guards against infinite resume loops of a
    successor that keeps exhausting without moving the run."""
    if state.get("stage", 0) > (snapshot.get("stage") or 0):
        return True
    counts = snapshot.get("unit_status") or {}
    decided_then = sum(counts.get(s, 0) for s in DECIDED_UNIT_STATUSES)
    decided_now = sum(1 for u in state.get("units", [])
                      if u.get("status") in DECIDED_UNIT_STATUSES)
    return decided_now > decided_then


def _journal_write(state: dict, path: Path, changes: list, events: list,
                   note_field: str) -> dict:
    """Write through state.py (the ONLY writer); a rejection keeps the
    file as-is and records the failure (the chain still reports)."""
    try:
        state_tool.save_validated(state, path, actor=TOOL, changes=changes)
    except ValueError as e:
        events.append({"event": "journal-write-rejected", "field": note_field,
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


def _append_session(state: dict, path: Path, entry: dict, events: list) -> dict:
    """Append one orchestrator session record (spawn time) and keep
    depth/max_depth coherent — one validated write."""
    chain = json.loads(json.dumps(state.get("session_chain") or {}))
    sessions = chain.get("sessions") or []
    old_depth = chain.get("depth", 0)
    chain["sessions"] = sessions + [entry]
    chain["depth"] = max(0, len(chain["sessions"]) - 1)
    # the cap in force when this session was appended; appends only happen
    # while len(sessions) <= cap, so depth <= max_depth holds by construction
    chain["max_depth"] = (int(entry.pop("_max_depth", 0))
                          or chain.get("max_depth") or DEFAULT_MAX_CHAIN_DEPTH)
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
    session = _chain_session(state, index) or {}
    label = f"session_chain.sessions[{session.get('id', index)}]"
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

def _roots_for(state: dict, state_path: Path, extra: str | None = None) -> list[Path]:
    """Candidate roots a recorded relative path may resolve against, most
    specific first: an explicit root, the preflight-recorded target root,
    then the state file's neighborhood (runs/<project>/ inside the target
    repo is the documented layout)."""
    roots: list[Path] = []

    def add(value) -> None:
        if not value:
            return
        p = Path(str(value)).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        if p.is_dir() and p not in roots:
            roots.append(p)

    add(extra)
    add(((state.get("preflight") or {}).get("target") or {}).get("root"))
    add(state_path.parent)
    add(state_path.parent.parent)
    add(Path.cwd())
    return roots


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


def resume_hygiene(state: dict, state_path: Path, *, apply: bool = True,
                   root_override: str | None = None) -> tuple[dict, list[dict]]:
    """Verify every recorded path at succession time (issue #25
    resume-hygiene). Returns (state, findings); each finding carries
    action ∈ ok | repaired | would-repair | unresolved | skipped.
    Repairs go through state.py's validated writer (journaled) and only
    fire when exactly ONE candidate exists on disk — ambiguity is
    reported, never guessed. Python paths are the target's own layout:
    reported, never rewritten."""
    findings: list[dict] = []
    roots = _roots_for(state, state_path, root_override)
    if not roots:
        findings.append({"check": "roots", "unit": None, "recorded": None,
                         "found": None, "action": "unresolved",
                         "detail": "no candidate root exists on disk"})
        return state, findings

    # the basename search only ever runs inside the TARGET repo(s) — an
    # explicit --root or the preflight-recorded root (at most the state
    # file's own directory, the runs/<project>/ layout) — never the cwd or
    # loose parent directories: a basename like u_bars.rs matches far too
    # widely outside the project to stay unambiguous
    search_roots: list[Path] = []
    for cand in (root_override,
                 ((state.get("preflight") or {}).get("target") or {}).get("root"),
                 str(state_path.parent)):
        if not cand:
            continue
        p = Path(str(cand)).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        if p.is_dir() and p not in search_roots:
            search_roots.append(p)

    def check_path(rel, unit: str | None, field: str) -> dict:
        if not rel:
            return {"check": field, "unit": unit, "recorded": rel,
                    "found": None, "action": "skipped",
                    "detail": "not recorded"}
        found = _exists_under(str(rel), roots)
        if found is not None:
            return {"check": field, "unit": unit, "recorded": rel,
                    "found": str(found), "action": "ok", "detail": ""}
        return {"check": field, "unit": unit, "recorded": rel, "found": None,
                "action": "unresolved",
                "detail": "not found under any candidate root"}

    findings.append(check_path(
        (state.get("project") or {}).get("python_path"), None,
        "project.python_path"))

    for u in state.get("units") or []:
        uid = u.get("id")
        # python paths: verify + report only (the target's own layout)
        findings.append(check_path(u.get("python_path"), uid,
                                   f"units[{uid}].python_path"))
        rust = u.get("rust_path")
        if rust is None:
            continue
        if _exists_under(str(rust), roots) is not None:
            findings.append({"check": f"units[{uid}].rust_path", "unit": uid,
                             "recorded": rust, "found": rust,
                             "action": "ok", "detail": ""})
            continue
        # mechanical re-spellings, then a bounded same-basename search —
        # deduped by ABSOLUTE path so one file found under two roots (the
        # target root and the state dir inside it) stays one candidate
        candidates: list[str] = [a for a in _rust_path_alternatives(str(rust))
                                 if _exists_under(a, roots) is not None]
        hits: dict[Path, str] = {}
        base = Path(str(rust)).name
        for root in search_roots:
            try:
                for hit in root.rglob(base):
                    if hit.is_file():
                        hits.setdefault(hit.resolve(), str(hit.relative_to(root)))
            except OSError:
                continue
        for rel in _rust_path_alternatives(str(rust)):
            for root in search_roots:
                abs_hit = root / rel
                if abs_hit.is_file():
                    hits.setdefault(abs_hit.resolve(), rel)
        candidates.extend(hits.values())
        uniq = sorted(set(candidates))
        if len(uniq) == 1:
            if not apply:
                findings.append({
                    "check": f"units[{uid}].rust_path", "unit": uid,
                    "recorded": rust, "found": uniq[0],
                    "action": "would-repair",
                    "detail": "exactly one candidate exists on disk"})
                continue
            old_rust = u.get("rust_path")
            u["rust_path"] = uniq[0]
            try:
                state_tool.save_validated(
                    state, state_path, actor=TOOL,
                    changes=[(uid, f"units[{uid}].rust_path", old_rust,
                              uniq[0])])
                findings.append({
                    "check": f"units[{uid}].rust_path", "unit": uid,
                    "recorded": old_rust, "found": uniq[0],
                    "action": "repaired",
                    "detail": "exactly one candidate exists on disk; "
                              "repaired through state.py (journaled)"})
                state = _load_state(state_path)
            except ValueError as e:
                u["rust_path"] = old_rust
                findings.append({
                    "check": f"units[{uid}].rust_path", "unit": uid,
                    "recorded": old_rust, "found": None,
                    "action": "unresolved",
                    "detail": f"repair rejected by state validation: {e}"})
        else:
            findings.append({
                "check": f"units[{uid}].rust_path", "unit": uid,
                "recorded": rust, "found": None, "action": "unresolved",
                "detail": (f"{len(uniq)} candidate(s) on disk "
                           f"({', '.join(uniq[:3])})" if uniq
                           else "no candidate found under any candidate root")})
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
    ]
    lines.append(f"- pending units (leaf-first order): {', '.join(pending)}"
                 if pending else "- no pending units")
    lines += [
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
        *[f"- s{s.get('index')} **{s.get('name')}** ({s.get('id')}): "
          f"{s.get('outcome') or 'in flight'}"
          + (f" — {s.get('succession_reason')}" if s.get("succession_reason")
             else "")
          for s in sessions],
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
        f"- this run is chained (tools/succession.py, max depth {max_depth}): "
        "if your session budget exhausts before the run reaches a terminal "
        "state, record `budget_exhausted` and stop — a successor session "
        "continues from this state file automatically; never ask a human",
        "",
        "---",
        "",
    ])
    return header + prompt_text


def _next_brief(state: dict, state_path: Path, *, policy: dict, args,
                hygiene_findings: list[dict] | None = None) -> tuple[str, int]:
    """The brief for the NEXT session: fresh start (the operator's single
    prompt) vs. successor resume (composed from the state file)."""
    max_tokens, max_minutes, max_depth = _ceilings(state, policy, args)
    index = len(_chain_block(state).get("sessions") or []) + 1
    if index == 1:
        prompt_text = (Path(args.prompt_file).read_text(encoding="utf-8")
                       if getattr(args, "prompt_file", None) else
                       "Run the Python->Rust migration per the framework's "
                       "MIGRATION_PROMPT.md; the state file path is recorded "
                       "above.")
        return compose_start_brief(prompt_text=prompt_text, state=state,
                                   state_path=state_path,
                                   max_depth=max_depth), index
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
    tokens = pipeline_tool.tokens_spent(state)
    wall = _chain_block(state).get("wall_clock_minutes") or 0.0
    sessions = _chain_block(state).get("sessions") or []
    if run_status == "budget_exhausted" or run_status in (None, "pending",
                                                          "running"):
        # the session believed it exhausted (its guard fired) or settled
        # without recording any terminal state (the context window killed
        # it first) — either way, first ask whether the RUN-level bound hit
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
    return {"kind": "terminal", "run_status": run_status,
            "reason": f"unexpected run_status {run_status!r} — treated as "
                      f"terminal; inspect the state file"}


# ---------------------------------------------------------------- chain ---

def _start_session(state: dict, path: Path, client: GlvClient, brief: str,
                    events: list, *, project: str, working_dir: str,
                    model: str | None, max_depth: int,
                    succession_reason: str | None,
                    snapshot: dict) -> tuple[dict, int]:
    """Start one GLV orchestrator session with the brief and record it in
    the chain (does NOT wait — the caller may need to journal the
    succession and reset run_status before the successor starts working).
    Returns the reloaded state + session index."""
    index = len(_chain_block(state).get("sessions") or []) + 1
    name = f"run-{project}-s{index}"
    started = client.start_session(name=name, prompt=brief,
                                   working_dir=working_dir, model=model)
    sid = started.get("id") or f"unknown-{index}"
    entry = {"index": index, "id": sid, "name": name,
             "started_at": now_iso(),
             "succession_reason": succession_reason,
             "progress_snapshot": snapshot,
             "_max_depth": max_depth}
    return _append_session(state, path, entry, events), index


def _settle(state: dict, path: Path, client: GlvClient, index: int,
            events: list) -> dict:
    session = _chain_session(state, index) or {}
    settled = client.wait_settled(session.get("id") or "")
    # the session JUST wrote the state file (its last act); re-read before
    # journaling the settle bookkeeping or this write would clobber it
    state = _load_state(path)
    state = _update_session(state, path, index,
                            {"settled_at": now_iso(),
                             "glv_status": settled.get("status")}, events)
    state = _accumulate_wall_clock(state, path, index, events)
    run_status = _load_state(path).get("run_status")
    state = _update_session(state, path, index, {"outcome": run_status},
                            events)
    events.append({"event": "session-settled", "index": index,
                   "glv_status": settled.get("status"),
                   "outcome": run_status})
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


def cmd_chain(args) -> int:
    policy = pipeline_tool.load_policy(args.policy)
    path = Path(args.state)
    state = _load_state(path)
    sessions = _chain_block(state).get("sessions") or []

    if not sessions and state.get("run_status") in ("complete", "no_go_measured"):
        return _refusal(
            f"run_status={state['run_status']} is a finished run — start a "
            f"fresh run (state.py init) to migrate again; a chain resumes "
            f"budget_exhausted/failed_safely/interrupted runs, not finished "
            f"ones", args.human, file=str(path))

    max_tokens, max_minutes, max_depth = _ceilings(state, policy, args)
    events: list[dict] = []
    project = (state.get("project") or {}).get("name") or "project"
    working_dir = args.working_dir or \
        (((state.get("preflight") or {}).get("target") or {}).get("root")
         or str(path.parent))

    if args.dry_run:
        state, findings = resume_hygiene(state, path, apply=False)
        settled = [s for s in _chain_block(state).get("sessions") or []
                   if isinstance(s, dict) and s.get("settled_at")]
        decision = None
        if settled:
            decision = _classify(state, max_tokens=max_tokens,
                                 max_minutes=max_minutes,
                                 max_depth=max_depth)
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

    try:
        client = _client_from(args)  # refusal (exit 2) when GLV is unconfigured
    except ToolError as e:
        return _refusal(str(e), args.human, file=str(path))

    terminal_reason: str | None = None
    degraded_reason: str | None = None

    while True:
        state = _load_state(path)
        sessions = _chain_block(state).get("sessions") or []
        if not sessions:
            # session 1: the operator's single prompt — not a succession.
            # Resumable statuses flip to running first (same rule as
            # pipeline.run), journaled through state.py.
            if state.get("run_status") in (None, "pending", "budget_exhausted",
                                           "failed_safely"):
                state = _set_run_status(state, path, "running", events,
                                        "chain session 1 starting")
            brief, _ = _next_brief(state, path, policy=policy, args=args)
            try:
                state, _ = _start_session(state, path, client, brief, events,
                                          project=project,
                                          working_dir=working_dir,
                                          model=args.model,
                                          max_depth=max_depth,
                                          succession_reason=None,
                                          snapshot=_progress_snapshot(state))
                state = _settle(state, path, client,
                                len(_chain_block(state)["sessions"]), events)
            except GlvError as e:
                return _refusal(
                    f"GLV unreachable, the chain cannot start ({e}). "
                    f"Degraded mode (pre-#25 behavior): run pipeline.py run "
                    f"directly — it ends in budget_exhausted and a human "
                    f"resumes after raising the ceiling; or start `glv "
                    f"serve` and re-run this command.", args.human,
                    file=str(path))
            continue
        last = sessions[-1] or {}
        if not last.get("settled_at"):
            # the harness itself died mid-session: adopt the live session
            # (never abandon it), wait it out, then re-classify
            try:
                state = _settle(state, path, client, last.get("index"),
                                events)
            except GlvError as e:
                return _refusal(
                    f"chain interrupted mid-session and GLV is unreachable "
                    f"({e}); check `glv sessions` / `glv view "
                    f"{last.get('id')}` on the server host, then re-run this "
                    f"command — it adopts the live session", args.human,
                    file=str(path))
            continue

        decision = _classify(state, max_tokens=max_tokens,
                             max_minutes=max_minutes, max_depth=max_depth)
        events.append({"event": "classified", "kind": decision["kind"],
                       "reason": decision["reason"]})

        if decision["kind"] == "terminal":
            terminal_reason = decision["reason"]
            break
        if decision["kind"] == "run_level_exhausted":
            if state.get("run_status") != "budget_exhausted":
                state = _set_run_status(state, path, "budget_exhausted",
                                        events, decision["reason"])
            terminal_reason = decision["reason"]
            break
        if decision["kind"] in ("no_progress", "depth_cap"):
            state = _set_run_status(state, path, "failed_safely", events,
                                    decision["reason"])
            terminal_reason = decision["reason"]
            break

        # succeed: hygiene -> brief -> spawn -> stop-notice + reset
        state, findings = resume_hygiene(state, path)
        for f in findings:
            if f.get("action") == "repaired":
                events.append({"event": "hygiene-repaired", **f})
        brief, index = _next_brief(state, path, policy=policy, args=args,
                                   hygiene_findings=findings)
        try:
            state, _ = _start_session(state, path, client, brief, events,
                                      project=project, working_dir=working_dir,
                                      model=args.model, max_depth=max_depth,
                                      succession_reason=decision["reason"],
                                      snapshot=_progress_snapshot(state))
        except GlvError as e:
            # GLV died mid-chain: degraded mode — the exhausted state stands
            degraded_reason = str(e)
            if state.get("run_status") != "budget_exhausted":
                state = _set_run_status(
                    state, path, "budget_exhausted", events,
                    f"session ended non-terminal and GLV is unreachable "
                    f"({e}) — degraded mode")
            state = _append_notice(
                state, path,
                f"succession FAILED at depth {len(sessions)}: GLV unreachable "
                f"({e}); degraded mode — budget_exhausted stands, resume per "
                f"tools/glv-subagents.md once the server is back", events)
            terminal_reason = (f"GLV unreachable at succession: {e} — "
                               f"degraded mode; the run parks in "
                               f"budget_exhausted (the pre-#25 behavior)")
            break
        predecessor = sessions[-1] or {}
        if predecessor.get("glv_status") in GLV_LIVE_STATUSES:
            try:
                client.stop(predecessor.get("id"))
                events.append({"event": "predecessor-stopped",
                               "id": predecessor.get("id")})
            except GlvError as e:
                events.append({"event": "predecessor-stop-failed",
                               "id": predecessor.get("id"),
                               "detail": str(e)})
        successor = _chain_session(state, index) or {}
        state = _append_notice(
            state, path,
            f"stop-notice: s{predecessor.get('index')} "
            f"{predecessor.get('name')} ({predecessor.get('id')}) settled "
            f"{predecessor.get('outcome')!r} at stage {state.get('stage')} — "
            f"successor s{index} {successor.get('name')} "
            f"({successor.get('id')}) spawned automatically with full resume "
            f"context; run_status reset to running; operator informed, not "
            f"involved", events)
        state = _set_run_status(state, path, "running", events,
                                "successor session spawned by the chain")
        events.append({"event": "succession", "to_session": index,
                       "depth": _chain_block(state).get("depth")})
        try:
            state = _settle(state, path, client, index, events)
        except GlvError as e:
            # the successor is in flight and GLV died mid-wait: degraded
            # mode — the operator checks the session server-side
            degraded_reason = str(e)
            state = _append_notice(
                state, path,
                f"successor s{index} was in flight when GLV became "
                f"unreachable ({e}); degraded mode — check `glv view "
                f"{(_chain_session(state, index) or {}).get('id')}` on the "
                f"server host, then re-run the chain (it adopts the session)",
                events)
            state = _set_run_status(
                state, path, "budget_exhausted", events,
                "successor in flight when GLV became unreachable — "
                "degraded mode")
            terminal_reason = (f"GLV unreachable while successor s{index} "
                               f"was in flight: {e} — degraded mode; the run "
                               f"parks in budget_exhausted (the pre-#25 "
                               f"behavior)")
            break

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
             + (f" [DEGRADED: {degraded_reason}]" if degraded_reason else "")
             + f"; report: {report_path}")
    emit({"tool": TOOL, "version": VERSION, "file": str(path),
          "run_status": run_status,
          "degraded": degraded_reason is not None,
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
    p.add_argument("--glv-url", help="override GLV_BASE_URL")
    p.add_argument("--glv-token", help="override GLV_API_TOKEN")
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
