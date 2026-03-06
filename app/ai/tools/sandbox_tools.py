import asyncio
from typing import Any

from e2b import AsyncSandbox
from langchain.tools import tool
from langchain_core.tools import BaseTool

from app.core.config import settings
from app.constants import PROJECT_PATH


def build_sandbox_tools(sdbx_id: str) -> dict[str, BaseTool]:

    async def _get_sandbox() -> AsyncSandbox:
        """Reusable helper to connect to the sandbox."""
        return await AsyncSandbox.connect(
            sandbox_id=sdbx_id, api_key=settings.e2b_api_key
        )

    @tool
    async def get_server_logs(lines_count: int = 25) -> str:
        """Fetch and return Next.js development server logs from the sandbox.

        Args:
            lines_count (int): Number of log lines to retrieve. Defaults to 25.

        Returns:
            str: Server logs output.
        """
        sandbox = await _get_sandbox()
        result = await sandbox.commands.run(
            f"pm2 logs project --raw --time --lines {lines_count} --nostream"
        )
        SKIP = ("[TAILING]", "/home/user/.pm2/logs/")

        lines = result.stdout.splitlines()
        filtered = [line for line in lines if not any(line.startswith(p) for p in SKIP)]
        return "\n".join(filtered).strip()

    @tool
    async def get_lint_checks() -> str:
        """Run and return ESLint results for the Next.js project in the sandbox.

        Returns:
            str: Lint check output.
        """
        sandbox = await _get_sandbox()
        result = await sandbox.commands.run("npm run lint", cwd=PROJECT_PATH)
        return result.stdout

    @tool
    async def agent_browser(command: str) -> str:
        """Controls the sandbox browser via agent-browser CLI. Call this to open, inspect, or interact with the app UI.

        Args:
            command (str): The agent-browser subcommand and its arguments, e.g. "open http://localhost:3000" or "screenshot /tmp/debug.png".

        Returns:
            str: output from the agent-browser command.
        """
        sandbox = await _get_sandbox()
        result = await sandbox.commands.run(f"agent-browser {command}", user="root")
        return result.stdout

    # @tool
    # async def open_browser_with_localhost_path(url: str) -> str:
    #     """Opens a browser and navigates to the given URL. Call this to inspect the app at a specific path.

    #     Args:
    #         url (str): The full URL to open, e.g. http://localhost:3000/dashboard.

    #     Returns:
    #         str: Browser console output captured after page load.
    #     """
    #     sandbox = await _get_sandbox()
    #     result = await sandbox.commands.run(
    #         f"agent-browser open {url}",
    #         user="root",
    #     )
    #     return result.stdout

    # @tool
    # async def close_browser() -> str:
    #     """Closes the sandbox browser. Call this after finishing browser inspection to free resources.

    #     Returns:
    #         str: Confirmation output from the close command.
    #     """
    #     sandbox = await _get_sandbox()
    #     result = await sandbox.commands.run(
    #         "agent-browser close",
    #         user="root",
    #     )
    #     return result.stdout

    return {
        "get_server_logs": get_server_logs,
        "get_lint_checks": get_lint_checks,
        "agent_browser": agent_browser,
        # "open_browser_with_localhost_path": open_browser_with_localhost_path,
        # "close_browser": close_browser,
    }


async def _main() -> None:
    sandbox_tools = build_sandbox_tools("igjgy8c0rxosyitmur8x0")

    # print("--- get_server_logs ---")
    # logs: Any = await sandbox_tools["get_server_logs"].ainvoke({"lines_count": 25})
    # print(logs)

    # print("--- get_lint_checks ---")
    # lint: Any = await sandbox_tools["get_lint_checks"].ainvoke({})
    # print(lint)

    print("--- get_console_messages ---")
    console: Any = await sandbox_tools["get_console_messages"]()
    print(console)


if __name__ == "__main__":
    asyncio.run(_main())
