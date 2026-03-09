from langchain.tools import tool

from app.utils.changelogs_retriever_utils import (
    fetch_release_by_ref,
    format_release,
    parse_repo_url,
    fetch_all_releases,
    filter_releases_between,
)


@tool
async def fetch_release_by_reference(repo_url: str, ref: str) -> str:
    """
    Fetch and format the changelog details for a specific release by tag/ref.

    Args:
        repo_url (str): The GitHub repository URL.
        ref (str): The release tag (e.g., 'v18.2.0' or '18.2.0').

    Returns:
        str: The formatted changelog block for the release.
    """
    owner, repo = parse_repo_url(repo_url)
    release = fetch_release_by_ref(owner, repo, ref, None)
    block = format_release(release)
    return block


# @tool
# async def fetch_release_lines_by_search(repo_url: str, ref: str, search: str) -> str:
#     """
#     Fetch and return only the lines from a release's changelog body that match a keyword.

#     Args:
#         repo_url (str): The GitHub repository URL.
#         ref (str): The release tag (e.g., 'v18.2.0' or '18.2.0').
#         search (str): Keyword to filter for in the release body.

#     Returns:
#         str: Formatted string of matching lines with release references.
#     """
#     owner, repo = parse_repo_url(repo_url)
#     release = fetch_release_by_ref(owner, repo, ref, None)
#     block = format_release(release, index=1, total=1, query=search)
#     return block


# @tool
# async def search_release_lines_in_range(
#     repo_url: str, old_version: str, new_version: str, search: str
# ) -> str:
#     """
#     Search across releases between two versions, returning matching changelog lines containing a keyword.

#     Args:
#         repo_url (str): The GitHub repository URL.
#         old_version (str): The starting version (exclusive).
#         new_version (str): The ending version (inclusive).
#         search (str): Keyword to filter for in all release bodies.

#     Returns:
#         str: Formatted string matching lines from all release changelogs in range.
#     """
#     owner, repo = parse_repo_url(repo_url)
#     releases = fetch_all_releases(owner, repo)
#     matched_releases = filter_releases_between(releases, old_version, new_version)
#     blocks = []
#     for i, release in enumerate(matched_releases, start=1):
#         block = format_release(
#             release, index=i, total=len(matched_releases), query=search
#         )
#         if block:
#             blocks.append(block)
#     if blocks:
#         return "\n".join(blocks)
#     else:
#         return f"No matches for '{search}' in releases between {old_version} and {new_version}."


