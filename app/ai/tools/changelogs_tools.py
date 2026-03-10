from langchain.tools import tool

from app.utils.changelogs_retriever_utils import (
    fetch_release_by_ref,
    format_release,
    parse_repo_url,
    fetch_all_releases,
    filter_releases_between,
)

import hashlib
import re
import textwrap
from datetime import datetime

from e2b import AsyncSandbox
from langchain.tools import tool
from langchain_core.tools import BaseTool

from app.constants import PROJECT_PATH
from app.core.config import settings
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


def build_changelog_tools(sdbx_id: str) -> dict[str, BaseTool]:

    async def _get_sandbox() -> AsyncSandbox:
        return await AsyncSandbox.connect(
            sandbox_id=sdbx_id, api_key=settings.e2b_api_key
        )

    async def _get_github_token() -> str:
        sandbox = await _get_sandbox()
        result = await sandbox.commands.run("echo $GITHUB_TOKEN")
        return result.stdout.strip()

    @tool
    async def search_changelogs(
        repo_url: str,
        known_version: str,
        keyword: str,
        current_version: str,
    ) -> str:
        """
        Search changelog lines containing the keyword across releases after the known version up to the currently installed version.

        Args:
            repo_url (str): The GitHub repository URL.
            known_version (str): The fully known version.
            keyword (str): Keyword to filter lines in release bodies.
            current_version (str): The currently installed version.

        Returns:
            str: Formatted string with all matching changelog lines within the version range.
        """
        try:
            github_token = await _get_github_token()
            print("github_token", github_token)
            owner, repo = parse_repo_url(repo_url)
            releases = fetch_all_releases(owner, repo, github_token)
            matched_releases = filter_releases_between(
                releases, known_version, current_version
            )
        except Exception as e:
            return f"Error fetching releases or filtering by version: {e}"

        try:
            total = len(matched_releases)
            blocks = [
                block
                for idx, release in enumerate(matched_releases, start=1)
                if (
                    block := format_release(
                        release, index=idx, total=total, query=keyword
                    )
                )
            ]
        except Exception as e:
            return f"Error while processing keyword '{keyword}': {e}"

        if blocks:
            return f"### Results for '{keyword}':\n" + "\n".join(blocks)

        return f"No matches for '{keyword}' in releases between {known_version} and {current_version}."

    return {"search_changelogs": search_changelogs}
