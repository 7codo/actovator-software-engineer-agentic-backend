import re
import json
import urllib.request
import urllib.error
from typing import Callable, Optional


# ── helpers ──────────────────────────────────────────────────────────────────


def parse_repo_url(url: str) -> tuple[str, str]:
    """Extract owner and repo name from a GitHub URL."""
    url = url.rstrip("/").removesuffix(".git")
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+)", url)
    if not match:
        raise ValueError(f"Could not parse GitHub repo URL: {url}")
    return match.group(1), match.group(2)


def normalize_version(version: str) -> str:
    """
    Strip any non-numeric prefix so comparisons are consistent.
    Handles plain 'v' prefixes ('v2.1.0' → '2.1.0') as well as
    arbitrary package prefixes ('shadcn@4.1.1' → '4.1.1').
    """
    # Drop everything up to and including the last '@' or 'v'
    version = re.sub(r"^.*[@v]", "", version)
    return version


_UNSTABLE_RE = re.compile(
    r"(canary|alpha|beta|rc|nightly|dev|experimental|next|preview|insiders?)",
    re.IGNORECASE,
)


def is_stable_version(tag: str) -> bool:
    """Return True only if the tag looks like a stable release."""
    return not bool(_UNSTABLE_RE.search(tag))


def version_tuple(v: str) -> tuple[int, ...]:
    """Convert '18.2.0' → (18, 2, 0) for sorting/comparison."""
    try:
        return tuple(int(x) for x in normalize_version(v).split("."))
    except ValueError:
        return (0,)


# ── API ───────────────────────────────────────────────────────────────────────


def gh_request(url: str, token: Optional[str] = None) -> list | dict:
    """Make a GitHub API request and return parsed JSON."""
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "changelog-fetcher/1.0")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def fetch_releases_in_range(
    owner: str,
    repo: str,
    old_version: str,
    new_version: str,
    token: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> list[dict]:
    """
    Fetch releases from the GitHub API between old_version (exclusive)
    and new_version (inclusive).

    Stops paginating as soon as a page contains a release older than
    old_version — GitHub returns releases newest-first, so everything
    after that point is out of range.

    Args:
        owner:       GitHub repo owner.
        repo:        GitHub repo name.
        old_version: Lower bound (exclusive).
        new_version: Upper bound (inclusive).
        token:       Optional GitHub personal access token.
        progress:    Optional callback for progress messages, e.g. print.

    Returns:
        Releases in the range, sorted oldest → newest, pre-releases excluded.
    """
    old_t = version_tuple(old_version)
    new_t = version_tuple(new_version)

    if old_t >= new_t:
        raise ValueError(
            f"old_version ({old_version}) must be lower than new_version ({new_version})."
        )

    def emit(msg: str) -> None:
        if progress:
            progress(msg)

    in_range: list[dict] = []
    skipped: list[str] = []
    page = 1
    done = False

    emit("Fetching releases from GitHub API")

    while not done:
        url = (
            f"https://api.github.com/repos/{owner}/{repo}/releases"
            f"?per_page=100&page={page}"
        )
        try:
            page_data: list[dict] = gh_request(url, token)  # type: ignore[assignment]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise ValueError(f"Repo '{owner}/{repo}' not found on GitHub.")
            elif e.code == 403:
                raise ValueError(
                    "GitHub API rate-limited. Pass a token to increase the limit."
                )
            elif e.code == 422:
                # Past the 1 000-release API cap — stop gracefully.
                break
            raise

        if not page_data:
            break

        emit(".")

        for release in page_data:
            tag = release.get("tag_name", "")
            v_t = version_tuple(tag)

            if v_t > new_t:
                # Newer than our upper bound — skip but keep paginating.
                continue

            if v_t <= old_t:
                # Older than (or equal to) our lower bound.
                # Because the API is newest-first, every subsequent release
                # on this page and all following pages will also be out of
                # range, so we can stop immediately.
                done = True
                break

            # Release is within (old_t, new_t].
            if is_stable_version(tag) and not release.get("prerelease", False):
                in_range.append(release)
            else:
                skipped.append(tag)

        page += 1

    total_fetched = len(in_range) + len(skipped)
    if total_fetched >= 1000:
        emit(
            "\n  ⚠️  GitHub API cap hit (1 000 releases max). "
            "Older releases may be missing."
        )

    if skipped:
        emit(f"\n  ⏭️  Skipped {len(skipped)} unstable release(s): {', '.join(skipped)}")

    emit(f"\n{len(in_range)} release(s) found in range.\n")

    in_range.sort(key=lambda r: version_tuple(r.get("tag_name", "")))
    return in_range


def fetch_release_by_ref(
    owner: str,
    repo: str,
    ref: str,
    token: Optional[str] = None,
) -> dict:
    """
    Fetch a single release by its tag name from the GitHub API.
    Tries both 'v{ref}' and bare '{ref}' forms.
    """
    tags_to_try = [ref, f"v{ref}" if not ref.startswith("v") else ref.lstrip("v")]
    for tag in tags_to_try:
        url = f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
        try:
            return gh_request(url, token)  # type: ignore[return-value]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            elif e.code == 403:
                raise ValueError(
                    "GitHub API rate-limited. Pass a token to increase the limit."
                )
            raise

    raise ValueError(
        f"Release '{ref}' not found in {owner}/{repo}.\n"
        f"  Make sure the tag exists: https://github.com/{owner}/{repo}/releases"
    )


# ── formatting ────────────────────────────────────────────────────────────────


def search_in_body(body: str, query: str, tag: str, url: str) -> list[str]:
    """
    Return lines from body that contain the query keyword (case-insensitive),
    each annotated with the release tag and URL.
    """
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    matches = []
    for line in body.splitlines():
        if pattern.search(line):
            highlighted = pattern.sub(lambda m: f">>>{m.group()}<<<", line.strip())
            matches.append(f"  {highlighted}\n    ↳ ref: {tag}  {url}")
    return matches


def format_release(
    release: dict,
    index: Optional[int] = None,
    total: Optional[int] = None,
    query: Optional[str] = None,
) -> str:
    """
    Format a human-readable changelog block for a single GitHub release.

    Returns an empty string when query is set and no lines match.
    """
    tag = release.get("tag_name", "unknown")
    name = release.get("name") or tag
    published = (release.get("published_at") or "")[:10]
    url = release.get("html_url", "")
    raw_body = (release.get("body") or "").strip()
    body = raw_body or "*(No changelog provided)*"

    if index is not None and total is not None:
        header = (
            f"[{index}/{total}] - {published}"
            if query
            else f"[{index}/{total}]  {name}  —  {published} - {url}"
        )
    else:
        header = f"{name}  —  {published} - {url}"

    if query:
        hits = search_in_body(body, query, tag, url)
        if not hits:
            return ""
        body_output = "\n".join(hits)
    else:
        body_output = body

    return f"\n---\n  {header}\n{body_output}\n---\n"


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json

    OWNER, REPO = "shadcn-ui", "ui"
    OLD, NEW = "2.1.7", "4.1.1"

    print(f"\n[4] fetch_releases_in_range  ({OLD} → {NEW})")
    releases = fetch_releases_in_range(OWNER, REPO, OLD, NEW, progress=print)
    # with open("app/output/releases.json", "w") as f:
    #     f.write(json.dumps(releases, indent=2))
    blocks = []
    for idx, release in enumerate(releases, start=1):
        block = format_release(release, index=idx, total=len(releases))
        blocks.append(block)
    with open("app/output/formatted_changelogs.md", "w") as f:
        f.write("\n\n\n---\n\n\n".join(blocks))
    # print(f"  Retrieved {len(releases)} release(s): "
    #       f"{[r['tag_name'] for r in releases]}")

    # # 5. fetch_release_by_ref ── fetch a single release by tag
    # print(f"\n[5] fetch_release_by_ref  (tag: {NEW})")
    # single = fetch_release_by_ref(OWNER, REPO, NEW)
    # print(f"  name        : {single.get('name')}")
    # print(f"  published_at: {single.get('published_at', '')[:10]}")
    # print(f"  url         : {single.get('html_url')}")

    # # 6. format_release ── render a full changelog block
    # print(f"\n[6] format_release  (full body, no query)")
    # if releases:
    #     block = format_release(releases[0], index=1, total=len(releases))
    #     # Print only the first 20 lines to keep the output readable
    #     lines = block.splitlines()
    #     print("\n".join(lines[:20]))
    #     if len(lines) > 20:
    #         print(f"  ... ({len(lines) - 20} more lines)")

    # # 7. format_release with query ── filter body to matching lines only
    # print(f"\n[7] format_release  (query='bug')")
    # if releases:
    #     block = format_release(releases[0], index=1, total=len(releases), query="bug")
    #     print(block if block else "  (no lines matched 'bug' in this release)")

    # # 8. search_in_body ── low-level keyword search within a body string
    # print("\n[8] search_in_body")
    # sample_body = (
    #     "### Bug fixes\n"
    #     "- Fixed a crash in `gh pr view`\n"
    #     "- Improved error messages\n"
    #     "- Another bug workaround for edge case\n"
    # )
    # hits = search_in_body(sample_body, "bug", tag="v2.41.0",
    #                       url="https://github.com/cli/cli/releases/tag/v2.41.0")
    # for hit in hits:
    #     print(hit)

    # print("\n" + "=" * 60)
