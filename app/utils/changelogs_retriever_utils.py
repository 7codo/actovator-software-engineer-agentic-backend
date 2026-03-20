import sys
import re
import json
import argparse
import urllib.request
import urllib.error
from typing import Optional


# ── helpers ──────────────────────────────────────────────────────────────────


def parse_repo_url(url: str) -> tuple[str, str]:
    """Extract owner and repo name from a GitHub URL."""
    url = url.rstrip("/").removesuffix(".git")
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+)", url)
    if not match:
        raise ValueError(f"Could not parse GitHub repo URL: {url}")
    return match.group(1), match.group(2)


def normalize_version(version: str) -> str:
    """Strip leading 'v' so comparisons are consistent."""
    return version.lstrip("v")


# Patterns that indicate an unstable / pre-release version
_UNSTABLE_RE = re.compile(
    r"(canary|alpha|beta|rc|nightly|dev|experimental|next|preview|insiders?)"
    r"",
    re.IGNORECASE,
)


def is_stable_version(tag: str) -> bool:
    """Return True only if the tag looks like a stable release (no pre-release keywords)."""
    return not bool(_UNSTABLE_RE.search(tag))


def version_tuple(v: str) -> tuple[int, ...]:
    """Convert '18.2.0' → (18, 2, 0) for sorting/comparison."""
    try:
        return tuple(int(x) for x in normalize_version(v).split("."))
    except ValueError:
        return (0,)


def gh_request(url: str, token: Optional[str] = None) -> list | dict:
    """Make a GitHub API request and return parsed JSON."""
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "changelog-fetcher/1.0")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def fetch_all_releases(
    owner: str, repo: str, token: Optional[str] = None
) -> list[dict]:
    """Fetch every release from the GitHub Releases API (handles pagination)."""
    releases = []
    page = 1
    print("Fetching releases from GitHub API", end="", flush=True)

    while True:
        url = (
            f"https://api.github.com/repos/{owner}/{repo}/releases"
            f"?per_page=100&page={page}"
        )
        try:
            data = gh_request(url, token)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise ValueError(f"Repo '{owner}/{repo}' not found on GitHub.")
            elif e.code == 403:
                raise ValueError(
                    "GitHub API rate-limited. Pass --token to increase the limit."
                )
            elif e.code == 422:
                # GitHub caps pagination at 1000 results (page 10 × per_page 100)
                # 422 means we've gone past the available range — stop gracefully
                break
            raise

        if not data:
            break

        releases.extend(data)
        print(".", end="", flush=True)
        page += 1

    total_fetched = len(releases)
    if total_fetched >= 1000:
        print(
            "\n  ⚠️  GitHub API cap hit (1000 releases max). Older releases may be missing."
        )
    print(f" {total_fetched} releases fetched.\n")
    return releases


def filter_releases_between(
    releases: list[dict],
    old_version: str,
    new_version: str,
) -> list[dict]:
    """
    Return releases whose version is BETWEEN old_version (exclusive)
    and new_version (inclusive), sorted oldest → newest.
    """
    old_t = version_tuple(old_version)
    new_t = version_tuple(new_version)

    if old_t >= new_t:
        raise ValueError(
            f"old_version ({old_version}) must be lower than new_version ({new_version})."
        )

    result = []
    skipped = []
    for r in releases:
        tag = r.get("tag_name", "")
        v_t = version_tuple(tag)
        if old_t < v_t <= new_t:
            if is_stable_version(tag) and not r.get("prerelease", False):
                result.append(r)
            else:
                skipped.append(tag)

    if skipped:
        print(
            f"  ⏭️  Skipped {len(skipped)} unstable release(s): {', '.join(skipped)}\n"
        )

    # Sort oldest to newest
    result.sort(key=lambda r: version_tuple(r.get("tag_name", "")))
    return result


def search_in_body(body: str, query: str, tag: str, url: str) -> list[str]:
    """
    Return lines from body that contain the query keyword (case-insensitive).
    Each matched line is annotated with the release tag and URL as a reference.
    """
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    matches = []
    for line in body.splitlines():
        if pattern.search(line):
            # Highlight the matched keyword with >>> markers
            highlighted = pattern.sub(lambda m: f">>>{m.group()}<<<", line.strip())
            matches.append(f"  {highlighted}\n    ↳ ref: {tag}  {url}")
    return matches


def fetch_release_by_ref(
    owner: str,
    repo: str,
    ref: str,
    token: Optional[str] = None,
) -> dict:
    """
    Fetch a single release by its tag name (ref) from the GitHub API.
    e.g. ref = 'v18.2.0' or '18.2.0'
    """
    # Try with and without leading 'v'
    tags_to_try = [ref, f"v{ref}" if not ref.startswith("v") else ref.lstrip("v")]
    for tag in tags_to_try:
        url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
        try:
            return gh_request(url, token)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            elif e.code == 403:
                raise ValueError(
                    "GitHub API rate-limited. Pass --token to increase the limit."
                )
            raise
    raise ValueError(
        f"Release '{ref}' not found in {owner}/{repo}.\n"
        f"  Make sure the tag exists at: https://github.com/{owner}/{repo}/releases"
    )


def format_release(
    release: dict,
    index: Optional[int] = None,
    total: Optional[int] = None,
    query: Optional[str] = None,
) -> str:
    """
    Format and return a human-readable changelog block for a GitHub release.

    Args:
        release (dict): The release data.
        index (Optional[int]): Optional index of this release in a sequence.
        total (Optional[int]): Optional total number of releases in sequence.
        query (Optional[str]): If provided, filter and annotate only lines from the body
                               matching this keyword.

    Returns:
        str: A formatted changelog string block, or an empty string if filtering found no matches.
    """
    tag = release.get("tag_name", "unknown")
    name = release.get("name") or tag
    published = (release.get("published_at") or "")[:10]  # YYYY-MM-DD
    url = release.get("html_url", "")
    raw_body = release.get("body") or ""
    body = raw_body.strip() if raw_body.strip() else "*(No changelog provided)*"

    if index is not None and total is not None:
        if query:
            header = f"[{index}/{total}] - {published}"
        else:
            header = f"[{index}/{total}]  {name}  —  {published} - {url}"
    else:
        header = f"{name}  —  {published} - {url}"

    divider = "---"

    if query:
        hits = search_in_body(body, query, tag, url)
        if not hits:
            return ""  # suppress releases with no matches
        body_output = "\n".join(hits)
    else:
        body_output = body

    formatted = f"\n{divider}\n  {header}\n{body_output}\n{divider}\n"
    return formatted


def write_output(content: str, output_path: Optional[str]) -> None:
    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"✅  Saved to {output_path}")
    else:
        print(content)


# ── main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Fetch GitHub changelogs between two versions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command")

    # ── subcommand: range (default) ──
    range_parser = subparsers.add_parser(
        "range",
        help="Fetch changelogs between two versions (default mode)",
    )
    range_parser.add_argument("repo_url", help="GitHub repository URL")
    range_parser.add_argument(
        "old_version", help="Starting version (exclusive), e.g. 17.0.0"
    )
    range_parser.add_argument(
        "new_version", help="Ending version (inclusive),  e.g. 18.0.0"
    )
    range_parser.add_argument(
        "--token", "-t", default=None, help="GitHub personal access token"
    )
    range_parser.add_argument(
        "--output", "-o", default=None, help="Save output to a file"
    )
    range_parser.add_argument(
        "--search",
        "-s",
        default=None,
        metavar="KEYWORD",
        help="Only show lines matching this keyword (with release reference)",
    )

    # ── subcommand: ref ──
    ref_parser = subparsers.add_parser(
        "ref",
        help="Fetch changelog for a single specific release tag",
    )
    ref_parser.add_argument("repo_url", help="GitHub repository URL")
    ref_parser.add_argument("ref", help="Release tag to fetch, e.g. v18.2.0 or 18.2.0")
    ref_parser.add_argument(
        "--token", "-t", default=None, help="GitHub personal access token"
    )
    ref_parser.add_argument(
        "--output", "-o", default=None, help="Save output to a file"
    )
    ref_parser.add_argument(
        "--search",
        "-s",
        default=None,
        metavar="KEYWORD",
        help="Only show lines matching this keyword",
    )

    # Support legacy positional usage: script.py <url> <old> <new>
    # (no subcommand given — fall back to 'range')
    args, unknown = parser.parse_known_args()
    if args.command is None:
        # Re-parse as 'range' with positional args injected
        sys.argv.insert(1, "range")
        args = parser.parse_args()

    # ── parse repo URL ──
    try:
        owner, repo = parse_repo_url(args.repo_url)
    except ValueError as e:
        print(f"❌  {e}", file=sys.stderr)
        sys.exit(1)

    # ════════════════════════════════════════════════
    #  MODE A — single ref
    # ════════════════════════════════════════════════
    if args.command == "ref":
        print(f"\n📦  {owner}/{repo}")
        print(f"🔍  Fetching changelog for ref: {args.ref}\n")
        try:
            release = fetch_release_by_ref(owner, repo, args.ref, args.token)
        except ValueError as e:
            print(f"❌  {e}", file=sys.stderr)
            sys.exit(1)
        except urllib.error.URLError as e:
            print(f"❌  Network error: {e}", file=sys.stderr)
            sys.exit(1)

        block = format_release(release, 1, 1, query=args.search)
        if not block:
            print(f"⚠️  No lines matched '{args.search}' in {args.ref}.")
            sys.exit(0)

        divider = "━" * 70
        output = (
            f"\n{divider}\n"
            f"  📌  {owner}/{repo}  —  ref: {release.get('tag_name')}\n"
            f"{divider}\n"
            f"{block}\n"
            f"{divider}\n  End of changelog\n{divider}\n"
        )
        write_output(output, args.output)
        return

    # ════════════════════════════════════════════════
    #  MODE B — version range
    # ════════════════════════════════════════════════
    old_v = normalize_version(args.old_version)
    new_v = normalize_version(args.new_version)

    print(f"\n📦  {owner}/{repo}")
    print(f"🔍  Fetching changes from v{old_v}  →  v{new_v}")
    if args.search:
        print(f"🔎  Filtering lines matching: '{args.search}'")
    print()

    try:
        releases = fetch_all_releases(owner, repo, args.token)
        matching = filter_releases_between(releases, old_v, new_v)
    except ValueError as e:
        print(f"❌  {e}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"❌  Network error: {e}", file=sys.stderr)
        sys.exit(1)

    if not matching:
        print(
            f"⚠️  No stable releases found between v{old_v} and v{new_v}.\n"
            f"   Check that the versions are correct and the repo uses GitHub Releases."
        )
        sys.exit(0)

    total = len(matching)
    divider = "━" * 70
    summary_header = (
        f"\n{divider}\n"
        f"  ✅  Found {total} stable release(s) between v{old_v} and v{new_v}\n"
        f"  Repo : https://github.com/{owner}/{repo}\n"
        f"{divider}\n"
    )

    output_parts = [summary_header]
    matched_releases = 0
    for i, release in enumerate(matching, start=1):
        block = format_release(release, i, total, query=args.search)
        if block:
            output_parts.append(block)
            matched_releases += 1

    if args.search and matched_releases == 0:
        print(f"⚠️  No lines matched '{args.search}' across {total} release(s).")
        sys.exit(0)

    if args.search:
        output_parts.insert(
            1,
            f"\n  🔎  Showing only lines matching '{args.search}' "
            f"({matched_releases}/{total} releases had matches)\n",
        )

    output_parts.append(f"\n{divider}\n  End of changelog\n{divider}\n")
    write_output("".join(output_parts), args.output)


if __name__ == "__main__":
    main()
