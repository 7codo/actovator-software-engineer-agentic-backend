import re
import asyncio
from typing import Any

from e2b import AsyncSandbox
from langchain.tools import tool
from langchain_core.tools import BaseTool

from app.core.config import settings
from app.constants import CDP_PORT, PROJECT_PATH


def _insert_cdp_flag(cmd: str) -> str:
    # Pattern finds all "agent-browser" (optionally with spaces after) 
    # not already followed directly by "--cdp"
    def replacer(match):
        # Only insert if --cdp isn't already the next arg
        head = match.group(0)
        after = cmd[match.end():]
        # If already has --cdp after "agent-browser", skip
        if after.lstrip().startswith("--cdp"):
            return head
        return f"{head} --cdp {CDP_PORT}"
    # Replace all cases of "agent-browser" not already with --cdp after
    pattern = r"\bagent-browser\b(?!\s+--cdp)"
    return re.sub(pattern, replacer, cmd)

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
    async def run_agent_browser_command(command: str) -> str:
        """
        Executes agent-browser CLI commands in the correct sandbox environment.

        Use this tool instead of a generic shell command runner for all agent-browser operations,
        as it provides the required privileges and sets the working directory appropriately.

        **Note:** You can use this tool to create an agent-browser bash file and execute it.

        Args:
            command (str): The full agent-browser CLI command with arguments,
                e.g., "agent-browser open http://localhost:3000".

        Returns:
            str: Output produced by the agent-browser command.
        """
        sandbox = await _get_sandbox()
        browser_workspace = f"{PROJECT_PATH}/.actovator/tests/browser"

        adjusted_command = _insert_cdp_flag(command)

        result = await sandbox.commands.run(
            adjusted_command, user="root", cwd=browser_workspace
        )
        return result.stdout

    async def execute_shell_command(
        command: str,
        user: str | None = None,
        cwd: str | None = None,
        background: bool = False
    ) -> str:
        """
        Executes an arbitrary shell command in the sandbox.

        Args:
            command (str): The shell command to execute.
            user (str, optional): User context in which to run the command ("user" or "root"). Defaults to "user".
            cwd (str, optional): Working directory for the command. If not specified, use the project root.
            background (bool, optional): Whether to run the command in the background.

        Returns:
            str: Output of the shell command.
        """
        sandbox = await _get_sandbox()

        result = await sandbox.commands.run(command, user=user, cwd=cwd, background=background)
        return result.stdout

    return {
        "get_server_logs": get_server_logs,
        "get_lint_checks": get_lint_checks,
        "run_agent_browser_command": run_agent_browser_command,
        "execute_shell_command": execute_shell_command,
    }


async def _main() -> None:
    # sandbox = await AsyncSandbox.connect(
    #         sandbox_id="i5puwhqrasfuvocwi6rqi", api_key=settings.e2b_api_key
    #     )
    sandbox_tools = build_sandbox_tools("i5puwhqrasfuvocwi6rqi")
    # result = await sandbox.commands.run(
    #     "apt-get install -y "
    #     "libcairo2 libpango-1.0-0 libpangocairo-1.0-0 "
    #     "libatk1.0-0 libatk-bridge2.0-0 libcups2 "
    #     "libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 "
    #     "libxfixes3 libxrandr2 libgbm1 libasound2 "
    #     "libnspr4 libnss3 libx11-6 libxcb1 libxext6 "
    #     "libxss1 libxtst6 fonts-liberation libappindicator3-1 "
    #     "libu2f-udev libvulkan1", user="root"
    # )
    result =  await sandbox_tools["execute_shell_command"]("pkill -f 'chrome-linux64/chrome'", user="root")
    print("result", result)

    # print("--- get_server_logs ---")
    # logs: Any = await sandbox_tools["get_server_logs"].ainvoke({"lines_count": 25})
    # print(logs)

    # print("--- get_lint_checks ---")
    # lint: Any = await sandbox_tools["get_lint_checks"].ainvoke({})
    # print(lint)

    # print("--- get_console_messages ---")
    # console: Any = await sandbox_tools["get_console_messages"]()
    # print(console)


if __name__ == "__main__":
    asyncio.run(_main())
