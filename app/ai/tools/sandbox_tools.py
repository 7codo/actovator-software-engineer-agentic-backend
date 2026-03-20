import hashlib
import json
import textwrap
from datetime import datetime

from e2b import AsyncSandbox
from langchain.tools import tool
from langchain_core.tools import BaseTool

from app.constants import PROJECT_PATH
from app.core.config import settings

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ACTOVATOR_PATH = f"{PROJECT_PATH}/.actovator"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _add_shebang(content: str) -> str:
    """Ensure content starts with a bash shebang and safe bash config."""
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


def build_sandbox_tools(sdbx_id: str) -> dict[str, BaseTool | callable]:
    async def _get_sandbox() -> AsyncSandbox:
        return await AsyncSandbox.connect(
            sandbox_id=sdbx_id, api_key=settings.e2b_api_key
        )

    async def execute_shell_command(
        command: str,
        user: str = "user",
        cwd: str | None = None,
        background: bool = False,
    ):
        """Run a shell command inside the sandbox and return the result object."""
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
            return await sandbox.files.read(path)
        except Exception as e:
            raise RuntimeError(
                f"read_file failed.\nReason: {type(e).__name__}: {e}"
            ) from e

    async def get_host_url(port: int = 8000) -> dict:
        """Return the public HTTPS URL exposed by the sandbox on the given port.
        Args:
            port: The sandbox port to expose (default 8000).

        Returns: dict: {'url': ..., 'port': ..., 'error': ... (optional)}
        """
        try:
            sandbox = await _get_sandbox()
            host = sandbox.get_host(port)
            url = f"https://{host}"
            return {"url": url, "port": port}
        except Exception as e:
            return {"url": None, "port": port, "error": f"[{type(e).__name__}] {e}"}

    async def get_server_logs(lines_count: int = 25) -> str:
        """Query the agent's training knowledge for the last fully known version of a package.

        Args:
            package: The name of the package to query (e.g. "langchain", "fastapi").

        Returns:
            The last known version string in x.x.x format.
        """

        try:
            
            result = await execute_shell_command(
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
    
    async def get_lint_checks() -> str:
        """Run ESLint on the Next.js project and return the results."""
        try:
            sandbox = await _get_sandbox()
            result = await execute_shell_command("npm run lint", cwd=PROJECT_PATH)
            return result.stdout
        except Exception as e:
            return f"[{type(e).__name__}] Failed to run lint checks: {e}"

    @tool
    async def run_bash_script(
        script_content: str,
        script_name: str | None = None,
        timeout: int = 60,
    ) -> dict:
        """
        Execute a bash script inside the sandbox and return its output.
        The script is written to disk, run, and deleted automatically.
        A shebang and `set -euo pipefail` are prepended when absent.

        Args:
            script_content: Full bash script body.
            script_name:    Filename stem (no .sh extension). Auto-generated when omitted.
            timeout:        Hard kill timeout in seconds (default 60).

        Returns:
            {
                "script_path": str,   # path used during execution (already deleted)
                "stdout":      str,
                "stderr":      str,
                "exit_code":   int,
            }
        """
        # Normalize script content and name
        content = _add_shebang(textwrap.dedent(script_content).strip())
        digest = _content_digest(content)
        if not script_name:
            script_name, digest = _make_script_name(content)
        script_name = script_name.removesuffix(".sh")
        script_path = f"bashs/{script_name}.sh"

        # Write script to sandbox
        try:
            delimiter = f"HEREDOC_{hashlib.sha1(script_name.encode()).hexdigest()[:8]}"
            write_cmd = f"cat > {script_path} << '{delimiter}'\n{content}\n{delimiter}"
            await execute_shell_command(write_cmd, cwd=_ACTOVATOR_PATH)
        except Exception as e:
            return _shell_error(f"Failed to write script to {script_path}", e)

        # Make script executable
        try:
            await execute_shell_command(f"chmod +x {script_path}", cwd=_ACTOVATOR_PATH)
        except Exception as e:
            return _shell_error(f"Failed to chmod {script_path}", e)

        # Run the script, then delete it
        try:
            result = await execute_shell_command(
                f"timeout {timeout} bash {script_path}", cwd=_ACTOVATOR_PATH
            )
        except Exception as e:
            return _shell_error(f"Failed to execute {script_path}", e)
        finally:
            try:
                await execute_shell_command(f"rm -f {script_path}", cwd=_ACTOVATOR_PATH)
            except Exception:
                pass  # Deletion failure is non-fatal

        return {
            "script_path": script_path,
            "stdout": getattr(result, "stdout", ""),
            "stderr": getattr(result, "stderr", ""),
            "exit_code": getattr(result, "exit_code", 1),
        }
    
    @tool
    async def execute_tool(
        tool_name: str,
        tool_params: dict,
    ) -> dict:
        """
        Execute a tool via the Tools API using a POST request.

        Args:
            tool_name: The name of the tool to execute.
            tool_params: Dictionary of parameters to pass to the tool.

        Returns:
            {
                "stdout": str,    # The response body from the tool execution.
                "stderr": str,    # Error message if execution failed.
                "exit_code": int, # 0 for success, non-zero for failure.
            }
        """
        payload = json.dumps(tool_params)
        digest = hashlib.sha1(payload.encode()).hexdigest()[:8]
        payload_path = f"/tmp/payload_{digest}.json"

        try:
            sandbox = await _get_sandbox()
            await sandbox.files.write(payload_path, payload)

            host_result = await get_host_url(8000)
            tools_api_base_url = host_result["url"]
            if not tools_api_base_url:
                return {
                    "stdout": "",
                    "stderr": f"Could not get tools API base URL: {host_result.get('error')}",
                    "exit_code": 1,
                }
            base_url = tools_api_base_url.rstrip("/")
            url = f"{base_url}/tools/{tool_name}"
            command = (
                f"curl -sS -X POST {url} "          
                f"-H 'Content-Type: application/json' "
                f"-d '@{payload_path}'"
                f"; echo \"HTTP_STATUS:$?\""         
            )

            result = await execute_shell_command(command, cwd=PROJECT_PATH)
            return {
                "stdout": getattr(result, "stdout", ""),
                "stderr": getattr(result, "stderr", ""),
                "exit_code": getattr(result, "exit_code", 1),
            }
        except Exception as e:
            return _shell_error(f"Failed to execute tool '{tool_name}'", e)
        finally:
            try:
                await execute_shell_command(f"rm -f {payload_path}")
            except Exception:
                pass

    return {
        "run_bash_script": run_bash_script,
        "execute_shell_command": execute_shell_command,
        "read_file": read_file,
        "get_host_url": get_host_url,
        "execute_tool": execute_tool,
        "get_server_logs": get_server_logs,
        "get_lint_checks": get_lint_checks,
    }


# Test and demo code
async def test():
    sandbox_tools = build_sandbox_tools("im74m3gz6bpyyq4sn7qk7")
    result = await sandbox_tools["execute_tool"](
        tool_name="read_file", tool_params={"relative_path": "package.json"}
    )
    print(result)


if __name__ == "__main__":
    import asyncio

    asyncio.run(test())
