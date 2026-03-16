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

_ACTOVATOR_PATH = f"{PROJECT_PATH}/.actovator"


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

    
    async def get_host_url(port: int = 8000) -> dict:
        """Return the public HTTPS URL exposed by the sandbox on the given port.

        Args:
            port: The sandbox port to expose (default 8000).

        Returns: full https:// URL
        """
        try:
            sandbox = await _get_sandbox()
            host = sandbox.get_host(port)
            url = f"https://{host}"
            return url
        except Exception as e:
            return {"url": None, "port": port, "error": f"[{type(e).__name__}] {e}"}

    # ── Public tools ─────────────────────────────────────────────────────────
    @tool
    async def create_run_bash_script(
        script_content: str,
        script_name: str = "",
        timeout: int = 60,
    ) -> dict:
        """Write an bash script, then execute it.

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


        # ── 3. Resolve script name & digest ──────────────────────────────────
        digest = _content_digest(content)
        if not script_name:
            script_name, digest = _make_script_name(content)

        script_name = script_name.removesuffix(".sh")
        script_path = f"bashs/{script_name}.sh"


        # ── 5. Write via heredoc (handles quotes/special chars safely) ────────
        try:
            delimiter = f"HEREDOC_{hashlib.sha1(script_name.encode()).hexdigest()[:8]}"
            write_cmd = f"cat > {script_path} << '{delimiter}'\n{content}\n{delimiter}"
            await execute_shell_command(write_cmd, cwd=_ACTOVATOR_PATH)
        except Exception as e:
            return _shell_error(f"Failed to write script to {script_path}", e)

        # ── 6. Make executable ────────────────────────────────────────────────
        try:
            await execute_shell_command(
                f"chmod +x {script_path}", cwd=_ACTOVATOR_PATH
            )
        except Exception as e:
            return _shell_error(f"Failed to chmod {script_path}", e)

        # ── 7. Run ────────────────────────────────────────────────────────────
        try:
            result = await execute_shell_command(
                f"timeout {timeout} bash {script_path}", cwd=_ACTOVATOR_PATH
            )
        except Exception as e:
            return _shell_error(f"Failed to execute {script_path}", e)

        return {
            "script_path": script_path,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        }

        host = sandbox.get_host(port)
    url = f"https://{host}"

    return {
        "create_run_bash_script": create_run_bash_script,
        "execute_shell_command": execute_shell_command,
        "read_file": read_file,
        "get_host_url": get_host_url,
    }

