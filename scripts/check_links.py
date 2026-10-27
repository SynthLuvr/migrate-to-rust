#!/usr/bin/env python3
"""Check that every markdown link in the repo resolves.

Relative links (any extension, with or without a ``#`` anchor) must point at
something that exists; an anchor on a ``.md`` target must match a heading in
that file (GitHub-style slug). External ``http(s)`` URLs and ``mailto:``
links are ignored by default.

With ``--external``, unique http(s) URLs are additionally HEAD-checked;
hosts known to block automated checks can be skipped with ``--allow-host``.
This mode is best-effort and meant for the optional, non-blocking CI job.

Code fences are skipped so example snippets cannot produce phantom links.

Stdlib only. Non-zero exit on any broken relative link (and, with
``--external``, on any unreachable non-allowlisted URL).

Run locally: python scripts/check_links.py [--external [--allow-host HOST ...]]
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

# [label](target) and ![alt](target); the target may be <wrapped> and/or
# carry a "title".
LINK_RE = re.compile(r"(!?)\[([^\]\[]*)\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+[\"'][^)]*[\"'])?\s*\)")
FENCE_RE = re.compile(r"^(```+|~~~+).*$")
HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
REDIRECT_CODES = {301, 302, 303, 307, 308}
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) migrate-to-rust-link-check/1.0"
)


def strip_code_fences(text: str) -> str:
    """Blank out fenced blocks (``` or ~~~) so links inside example
    snippets are not checked."""
    lines = text.splitlines()
    in_fence = False
    fence_mark = ""
    for i, line in enumerate(lines):
        m = FENCE_RE.match(line)
        if m and not in_fence:
            fence_mark = m.group(1)[0] * 3
            in_fence = True
        elif in_fence and line.strip().startswith(fence_mark):
            in_fence = False
        elif in_fence:
            lines[i] = ""
    return "\n".join(lines)


def github_slug(heading: str) -> str:
    """GitHub's anchor slug: lowercase, drop punctuation except - and _,
    spaces become dashes."""
    slug = re.sub(r"[^\w\- ]", "", heading.lower())
    return slug.strip().replace(" ", "-")


def heading_slugs(markdown_path: Path) -> set[str]:
    return {
        github_slug(m.group(1))
        for m in HEADING_RE.finditer(markdown_path.read_text(encoding="utf-8"))
    }


def check_relative(source: Path, target: str) -> str | None:
    """Return an error string for a broken relative link, else None."""
    path_part, _, anchor = target.partition("#")
    if not path_part:  # same-document anchor
        if anchor and anchor not in heading_slugs(source):
            return f"anchor #{anchor} matches no heading in {source}"
        return None
    resolved = (source.parent / path_part).resolve()
    if not resolved.exists():
        return f"{target} — no such file or directory (from {source})"
    if anchor and resolved.suffix == ".md" and anchor not in heading_slugs(resolved):
        return f"anchor #{anchor} matches no heading in {resolved}"
    return None


def head_check(url: str, timeout: float = 15.0) -> str | None:
    """Return an error string if the URL is unreachable, else None.

    Redirects are followed manually (urllib does not follow 308 before
    Python 3.11) and a refused HEAD (405/501) is retried with GET.
    """
    current, hops = url, 0
    while True:
        req = urllib.request.Request(
            current, method="HEAD", headers={"User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return None if resp.status == 200 else f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            if exc.code in REDIRECT_CODES and exc.headers.get("Location"):
                current = urllib.request.urljoin(current, exc.headers["Location"])
                hops += 1
                if hops > 5:
                    return "too many redirects"
                continue
            if exc.code in (405, 501):  # HEAD refused; retry with GET
                return get_check(current, timeout)
            return f"HTTP {exc.code}"
        except Exception as exc:  # any transport error (DNS, TLS, timeout, ...)
            return f"{type(exc).__name__}: {exc}"


def get_check(url: str, timeout: float) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return None if resp.status == 200 else f"HTTP {resp.status}"
    except Exception as exc:  # any transport error
        return f"{type(exc).__name__}: {exc}"


def collect_links(md_files: list[Path]) -> tuple[list[str], set[str], int]:
    """Classify every link target in every markdown file.

    Returns (errors, external_urls, relative_count): errors are broken
    relative links, external_urls the set of unique http(s) targets.
    """
    errors: list[str] = []
    external: set[str] = set()
    relative = 0
    for source in md_files:
        text = strip_code_fences(source.read_text(encoding="utf-8"))
        for m in LINK_RE.finditer(text):
            target = m.group(3)
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1]
            if target.startswith("mailto:"):
                continue
            if target.startswith(("http://", "https://")):
                external.add(target)
                continue
            relative += 1
            err = check_relative(source, target)
            if err:
                errors.append(f"{source}: {err}")
    return errors, external, relative


def fail(messages: list[str]) -> None:
    for msg in messages:
        print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def check_external(urls: set[str], allow_hosts: list[str]) -> None:
    allowed = set(allow_hosts)
    skipped = sorted(u for u in urls if urlsplit(u).netloc in allowed)
    to_check = sorted(u for u in urls if urlsplit(u).netloc not in allowed)
    for url in skipped:
        print(f"skip (allowlisted host): {url}")
    bad = []
    for i, url in enumerate(to_check, 1):
        err = head_check(url)
        print(f"[{i}] {'FAIL ' + err if err else 'ok  '} {url}")
        if err:
            bad.append(f"{url}: {err}")
        time.sleep(0.2)  # be a polite client
    if bad:
        fail(bad)
    print(f"external spot-check: {len(to_check)} URLs reachable")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--external",
        action="store_true",
        help="also HEAD-check unique http(s) URLs (best-effort, allowlistable)",
    )
    parser.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="HOST",
        help="host to skip in --external mode (repeatable)",
    )
    args = parser.parse_args()

    md_files = sorted(p for p in REPO_ROOT.rglob("*.md") if ".git" not in p.parts)
    errors, external, relative = collect_links(md_files)
    print(
        f"checked {len(md_files)} markdown files: "
        f"{relative - len(errors)}/{relative} internal links ok, "
        f"{len(external)} unique external"
    )
    if errors:
        fail(errors)
    if args.external:
        check_external(external, args.allow_host)


if __name__ == "__main__":
    main()
