from langchain.tools import tool

from app.utils.changelogs_retriever_utils import (
    format_release,
    parse_repo_url,
    fetch_all_releases,
    filter_releases_between,
)


from e2b import AsyncSandbox
from langchain_core.tools import BaseTool

from app.core.config import settings


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
