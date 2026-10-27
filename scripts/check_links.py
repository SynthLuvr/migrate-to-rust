#!/usr/bin/env python3
"""Check that every markdown link in the repo resolves.

Relative links (files, directories, any extension, with or without a ``#``
anchor) must point at something that exists; an anchor on a ``.md`` target
must match a heading in that file (GitHub-style slug). External ``http(s)``
URLs and ``mailto:`` links are ignored by default.

With ``--external``, unique http(s) URLs are additionally HEAD-checked
(redirects, including 308, are followed; HEAD-unfriendly servers get a GET
retry). Hosts known to block automated checks can be skipped with
``--allow-host`` (e.g. ``dfrws.org`` 403s from some networks — see the link
issue). This mode is best-effort and meant for the optional, non-blocking CI
job.

Code fences are skipped so example snippets cannot produce phantom links.

Stdlib only. Non-zero exit on any broken relative link (and, in
``--external`` mode, on any unreachable non-allowlisted URL).

Run locally:  python scripts/check_links.py [--external [--allow-host HOST ...]]
"""

import argparse
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent

# [label](target), also ![alt](target); target may be <wrapped> and/or carry
# a "title". Skip bare-footnote/heading patterns via the bracket content.
LINK_RE = re.compile(r"(!?)\[([^\]\[]*)\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+[\"'][^)]*[\"'])?\s*\)")
FENCE_RE = re.compile(r"^(```+|~~~+).*$")
HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
REDIRECT_CODES = {301, 302, 303, 307, 308}
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) migrate-to-rust-link-check/1.0"
)


def fail(messages: list[str]) -> None:
    for msg in messages:
        print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def strip_code_fences(text: str) -> str:
    """Blank out fenced blocks (``` or ~~~) so links inside examples
    (shell commands, YAML, prompts) are not checked."""
    lines, in_fence, fence_mark = text.splitlines(), False, ""
    for i, line in enumerate(lines):
        m = FENCE_RE.match(line)
        if m and not in_fence:
            in_fence, fence_mark = True, m.group(1)[0] * 3
        elif in_fence and line.strip().startswith(fence_mark):
            in_fence = False
        elif in_fence:
            lines[i] = ""
    return "\n".join(lines)


def github_slug(heading: str) -> str:
    """GitHub's anchor slug: lowercase, drop punctuation except - and _,
    spaces to dashes."""
    slug = re.sub(r"[^\w\- ]", "", heading.lower(), flags=re.UNICODE)
    return slug.strip().replace(" ", "-")


def heading_slugs(markdown_path: Path) -> set[str]:
    slugs: set[str] = []
    for m in HEADING_RE.finditer(markdown_path.read_text(encoding="utf-8")):
        slugs.append(github_slug(m.group(1)))
    return set(slugs)


def check_relative(source: Path, target_raw: str) -> str | None:
    """Return an error string for a broken relative link, else None."""
    path_part = target_raw.split("#", 1)[0]
    anchor = target_raw.partition("#")[2] or None
    if not path_part:  # same-document anchor
        if anchor and anchor not in heading_slugs(source):
            return f"anchor #{anchor} matches no heading in {source}"
        return None
    resolved = (source.parent / path_part).resolve()
    if not resolved.exists():
        return f"{target_raw} — no such file or directory (from {source})"
    if anchor and resolved.suffix == ".md":
        if anchor not in heading_slugs(resolved):
            return f"anchor #{anchor} matches no heading in {resolved}"
    return None


def head_check(url: str, timeout: float = 15.0) -> str | None:
    """Return an error string if the URL is unreachable, else None.

    Follows redirects manually (Python < 3.11 urllib does not follow 308)
    and retries once with GET when HEAD is refused (405/501).
    """
    current, hops = url, 0
    while True:
        req = urllib.request.Request(
            current, method="HEAD", headers={"User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    return None
                return f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            if exc.code in REDIRECT_CODES and exc.headers.get("Location"):
                current = urllib.request.urljoin(current, exc.headers["Location"])
                hops += 1
                if hops > 5:
                    return "too many redirects"
                continue
            if exc.code in (405, 501):  # HEAD refused; retry with GET
                return head_check_get(current, timeout)
            return f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001 — report any transport error
            return f"{type(exc).__name__}: {exc}"


def head_check_get(url: str, timeout: float) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return None if resp.status == 200 else f"HTTP {resp.status}"
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--external",
        action="store_true",
        help="also HEAD-check unique http(s) URLs (best-effort, allowlistable)",
    )
    ap.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="HOST",
        help="host to skip in --external mode (repeatable)",
    )
    args = ap.parse_args()

    md_files = sorted(
        p for p in REPO_ROOT.rglob("*.md") if ".git" not in p.parts
    )
    errors: list[str] = []
    external: set[str] = set()
    links = 0

    for source in md_files:
        text = strip_code_fences(source.read_text(encoding="utf-8"))
        for m in LINK_RE.finditer(text):
            raw = m.group(3)
            if raw.startswith("<") and raw.endswith(">"):
                raw = raw[1:-1]
            links += 1
            if raw.startswith("mailto:"):
                continue
            if raw.startswith(("http://", "https://")):
                external.add(raw)
                continue
            err = check_relative(source, raw)
            if err:
                errors.append(f"{source}: {err}")

    for err in errors:
        print(f"FAIL: {err}", file=sys.stderr)

    print(
        f"checked {links} links across {len(md_files)} markdown files: "
        f"{links - len(errors)} relative ok, {len(external)} unique external"
    )
    if errors:
        fail(errors)

    if args.external:
        allowed = set(args.allow_host)
        skipped = sorted(u for u in external if urlsplit(u).netloc in allowed)
        for u in skipped:
            print(f"skip (allowlisted host): {u}")
        bad = []
        for i, url in enumerate(sorted(external - set(skipped)), 1):
            err = head_check(url)
            print(f"[{i}] {'FAIL ' + err if err else 'ok  '} {url}")
            if err:
                bad.append(f"{url}: {err}")
            time.sleep(0.2)  # be a polite client
        if bad:
            fail(bad)
        print(f"external spot-check: {len(external) - len(skipped)} URLs reachable")


if __name__ == "__main__":
    main()
