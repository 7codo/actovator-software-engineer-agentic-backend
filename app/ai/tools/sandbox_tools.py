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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BROWSER_WORKSPACE = f"{PROJECT_PATH}/.actovator/tests/e2e"

_AGENT_BROWSER_CMD_RE = re.compile(r"^\s*agent-browser\b", re.MULTILINE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _add_shebang(content: str) -> str:
    if not content.startswith("#!"):
        return "#!/usr/bin/env bash\nset -euo pipefail\n\n" + content
    return content


def _make_script_name(content: str) -> tuple[str, str]:
    """Return (script_name_stem, sha1_digest) derived from content."""
    digest = hashlib.sha1(content.encode()).hexdigest()[:8]
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    return f"script_{ts}_{digest}", digest


def _content_digest(content: str) -> str:
    return hashlib.sha1(content.encode()).hexdigest()[:8]


def _shell_error(context: str, exc: Exception) -> dict:
    """Uniform error dict for tools that return a dict."""
    return {
        "script_path": None,
        "stdout": "",
        "stderr": f"[{type(exc).__name__}] {context}: {exc}",
        "exit_code": 1,
    }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_sandbox_tools(sdbx_id: str) -> dict[str, BaseTool]:

    async def _get_sandbox() -> AsyncSandbox:
        return await AsyncSandbox.connect(
            sandbox_id=sdbx_id, api_key=settings.e2b_api_key
        )

    # not exposed as a @tool ─────────────────────────

    async def execute_shell_command(
        command: str,
        user: str = "root",
        cwd: str | None = None,
        background: bool = False,
    ):
        """Run an arbitrary shell command inside the sandbox and return the result object."""
        try:
            sandbox = await _get_sandbox()
            return await sandbox.commands.run(
                command, user=user, cwd=cwd, background=background
            )
        except Exception as e:
            raise RuntimeError(
                f"execute_shell_command failed.\n"
                f"Command: {command}\n"
                f"Reason: {type(e).__name__}: {e}"
            ) from e

    async def read_file(path: str) -> str:
        """Read a file's content from the sandbox."""
        try:
            sandbox = await _get_sandbox()
            file_content = await sandbox.files.read(path)
            return file_content
        except Exception as e:
            raise RuntimeError(
                f"read_file failed.\nReason: {type(e).__name__}: {e}"
            ) from e

    # ── Public tools ─────────────────────────────────────────────────────────

    @tool
    async def get_server_logs(lines_count: int = 25) -> str:
        """Query the agent's training knowledge for the last fully known version of a package.

        Args:
            package: The name of the package to query (e.g. "langchain", "fastapi").

        Returns:
            The last known version string in x.x.x format.
        """

        try:
            sandbox = await _get_sandbox()
            result = await sandbox.commands.run(
                f"pm2 logs project --raw --time --lines {lines_count} --nostream"
            )
        except Exception as e:
            return f"[{type(e).__name__}] Failed to fetch server logs: {e}"

        skip_prefixes = ("[TAILING]", "/home/user/.pm2/logs/")
        lines = [
            line
            for line in result.stdout.splitlines()
            if not any(line.startswith(p) for p in skip_prefixes)
        ]
        return "\n".join(lines).strip()

    @tool
    async def get_lint_checks() -> str:
        """Run ESLint on the Next.js project and return the results."""
        try:
            sandbox = await _get_sandbox()
            result = await sandbox.commands.run("npm run lint", cwd=PROJECT_PATH)
            return result.stdout
        except Exception as e:
            return f"[{type(e).__name__}] Failed to run lint checks: {e}"

    @tool
    async def run_agent_browser_command(command: str) -> str:
        """Execute an agent-browser CLI command inside the sandbox.

        Validates that the command starts with 'agent-browser' before running.

        Args:
            command: Full agent-browser command, e.g. "agent-browser goto http://localhost:3000".

        Returns:
            stdout of the command, or an error string on failure.
        """
        try:
            stripped = command.strip()
            if not stripped.startswith("agent-browser"):
                raise ValueError(
                    f"Invalid command: '{stripped}'. "
                    "Only 'agent-browser' commands are supported. "
                    "Example: 'agent-browser open http://localhost:3000'"
                )

            sandbox = await _get_sandbox()
            result = await sandbox.commands.run(
                stripped, user="root", cwd=_BROWSER_WORKSPACE
            )

            if result.exit_code != 0:
                raise RuntimeError(
                    f"Command failed (exit {result.exit_code}).\n"
                    f"Stderr: {result.stderr or '(none)'}\n"
                    f"Stdout: {result.stdout or '(none)'}"
                )

            return result.stdout

        except (ValueError, RuntimeError) as e:
            return f"[{type(e).__name__}] {e}"
        except Exception as e:
            return f"[Unexpected Error] {type(e).__name__}: {e}"

    @tool
    async def run_browser_agent_bash_script(
        script_content: str,
        script_name: str = "",
        timeout: int = 60,
    ) -> dict:
        """Write an agent-browser bash script to bashs/, then execute it.

        The script must contain at least one top-level 'agent-browser' command.
        A shebang is prepended automatically when absent.

        Args:
            script_content: Full bash script body.
            script_name:    Filename stem (no .sh extension). Auto-generated when omitted.
            timeout:        Hard kill timeout in seconds (default 60).

        Returns:
            {
                "script_path": str,   # path written inside the sandbox
                "stdout":      str,
                "stderr":      str,
                "exit_code":   int,
            }
        """
        # ── 1. Normalise content ─────────────────────────────────────────────
        content = _add_shebang(textwrap.dedent(script_content).strip())

        # ── 2. Validate agent-browser usage ──────────────────────────────────
        if not _AGENT_BROWSER_CMD_RE.search(content):
            return {
                "script_path": None,
                "stdout": "",
                "stderr": (
                    "Validation error: script must contain at least one "
                    "'agent-browser <command>' call as the main command "
                    "(not as a subcommand or argument)."
                ),
                "exit_code": 1,
            }

        # ── 3. Resolve script name & digest ──────────────────────────────────
        digest = _content_digest(content)
        if not script_name:
            script_name, digest = _make_script_name(content)

        script_name = script_name.removesuffix(".sh")
        script_path = f"bashs/{script_name}.sh"

        # ── 4. Ensure target directory exists ────────────────────────────────
        try:
            await execute_shell_command("mkdir -p bashs", cwd=_BROWSER_WORKSPACE)
        except Exception as e:
            return _shell_error("Failed to create bashs/ directory", e)

        # ── 5. Write via heredoc (handles quotes/special chars safely) ────────
        try:
            delimiter = f"HEREDOC_{hashlib.sha1(script_name.encode()).hexdigest()[:8]}"
            write_cmd = f"cat > {script_path} << '{delimiter}'\n{content}\n{delimiter}"
            await execute_shell_command(write_cmd, cwd=_BROWSER_WORKSPACE)
        except Exception as e:
            return _shell_error(f"Failed to write script to {script_path}", e)

        # ── 6. Make executable ────────────────────────────────────────────────
        try:
            await execute_shell_command(
                f"chmod +x {script_path}", cwd=_BROWSER_WORKSPACE
            )
        except Exception as e:
            return _shell_error(f"Failed to chmod {script_path}", e)

        # ── 7. Run ────────────────────────────────────────────────────────────
        try:
            result = await execute_shell_command(
                f"timeout {timeout} bash {script_path}", cwd=_BROWSER_WORKSPACE
            )
        except Exception as e:
            return _shell_error(f"Failed to execute {script_path}", e)

        return {
            "script_path": script_path,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        }

    return {
        "get_server_logs": get_server_logs,
        "get_lint_checks": get_lint_checks,
        "run_agent_browser_command": run_agent_browser_command,
        "run_browser_agent_bash_script": run_browser_agent_bash_script,
        "execute_shell_command": execute_shell_command,
        "read_file": read_file,
    }


async def _main() -> None:
    # sandbox = await AsyncSandbox.connect(
    #         sandbox_id="i5puwhqrasfuvocwi6rqi", api_key=settings.e2b_api_key
    #     )
    sandbox_tools = build_sandbox_tools(
        "iigdxjs9fklw96v31hqhe"
    )  # i9na59ivfvvyq3ha8qsct created directly
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
    # result = await sandbox_tools["search_changelogs"](
    #     repo_url="https://github.com/vercel/next.js",
    #     known_version="15.1.0",
    #     search="middleware",
    #     package_name="next",
    # )
    result = await sandbox_tools["execute_shell_command"](
        command="gh auth status", cwd=PROJECT_PATH
    )
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
    import asyncio

    asyncio.run(_main())
