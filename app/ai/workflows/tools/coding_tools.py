import asyncio
import shlex
import json
from datetime import datetime, timezone
from typing import Optional, List
from jsonschema import Draft7Validator
from e2b import AsyncSandbox
from langchain.tools import tool
from app.constants import PROJECT_PATH
from app.core.config import settings

# ---------------------------------------------------------------------------
# SANDBOX CONNECTION MANAGER
# ---------------------------------------------------------------------------

_SANDBOX_CACHE: dict[str, AsyncSandbox] = {}
_SANDBOX_LOCK = asyncio.Lock()

# Renew when less than this many seconds remain on the lease
_RENEW_THRESHOLD_SECONDS = 60
# How long to extend the timeout on each renewal (seconds)
_RENEW_EXTENSION_SECONDS = 300


async def get_or_connect_sandbox(sandbox_id: str) -> AsyncSandbox:
    """
    Return a cached AsyncSandbox for *sandbox_id*, creating one if needed.
    Before returning, check the remaining lease time and renew if it is
    below the threshold so long-running agent runs never hit the default
    ~5-minute expiry.
    """
    async with _SANDBOX_LOCK:
        sandbox = _SANDBOX_CACHE.get(sandbox_id)

        if sandbox is None:
            sandbox = await AsyncSandbox.connect(
                sandbox_id=sandbox_id, api_key=settings.e2b_api_key
            )
            _SANDBOX_CACHE[sandbox_id] = sandbox

        # Renew the lease when expiry is approaching.
        try:
            info = await sandbox.get_info()
            end_at: datetime = info.end_at
            if end_at.tzinfo is None:
                end_at = end_at.replace(tzinfo=timezone.utc)
            remaining = (end_at - datetime.now(timezone.utc)).total_seconds()
            if remaining < _RENEW_THRESHOLD_SECONDS:
                await sandbox.set_timeout(_RENEW_EXTENSION_SECONDS)
        except Exception:
            # Non-fatal: if the info call fails we still try to use the sandbox.
            pass

        return sandbox


class BuildSandboxToolsDefinitions:
    """
    Builds and exposes LangChain tools to fetch API tool parameter schemas by tool name.
    """

    def __init__(
        self,
        allowed_tools: Optional[List[str]] = None,
        excluded_tools: Optional[List[str]] = None,
    ) -> None:
        from app.ai.resources import SANDBOX_TOOLS_DEFINITIONS

        tools = SANDBOX_TOOLS_DEFINITIONS

        if allowed_tools:
            tools = [t for t in tools if t["name"] in allowed_tools]
        if excluded_tools:
            tools = [t for t in tools if t["name"] not in excluded_tools]

        self.tools = tools

    def get_sandbox_tools_without_params(self) -> str:
        """Returns a JSON list of tools with only name and description (no parameters)."""
        return json.dumps(
            [
                {
                    "name": t["name"],
                    "description": t["description"],
                }
                for t in self.tools
            ],
            indent=2,
        )

    def get_sandbox_tool_parameters(self, tool_name: str) -> str:
        for t in self.tools:
            if t["name"] == tool_name:
                return json.dumps(t.get("parameters", {}), indent=2)
        return json.dumps({"error": f"Tool '{tool_name}' not found"})

    def as_langchain_tools(self) -> dict:
        instance = self

        @tool
        def get_tool_parameters(tool_name: str) -> str:
            """
            Retrieves the parameter schema for a specific tool by its name.
            Use this to get the arguments definition for a tool you intend to use.

            Args:
                tool_name: The name of the tool to retrieve parameters for.

            Returns:
                A JSON string representing the tool's parameters, or an error if not found.
            """
            return instance.get_sandbox_tool_parameters(tool_name)

        return {"get_tool_parameters": get_tool_parameters}


class BuildDocsReaderTools:
    """
    Builds and exposes sandbox tools for interacting with an E2B AsyncSandbox.
    """

    def __init__(
        self,
        sdbx_id: str,
    ) -> None:
        self.sdbx_id = sdbx_id

    async def _get_sandbox(self) -> AsyncSandbox:
        return await get_or_connect_sandbox(self.sdbx_id)


class BuildSandboxTools:
    """
    Builds and exposes sandbox tools for interacting with an E2B AsyncSandbox.
    """

    def __init__(
        self,
        sdbx_id: str,
        tools_definitions: Optional["BuildSandboxToolsDefinitions"] = None,
    ) -> None:
        self.sdbx_id = sdbx_id
        self._tools_definitions = tools_definitions

    def _validate_tool_params(self, tool_name: str, tool_params: dict) -> Optional[str]:
        """
        Validates tool_params against the tool's parameter schema.
        Returns a human-readable error string on failure, or None if valid.
        """
        if self._tools_definitions is None:
            return None  # No definitions injected — skip validation

        raw_schema = self._tools_definitions.get_sandbox_tool_parameters(tool_name)
        schema = json.loads(raw_schema)

        if "error" in schema:
            return (
                f"Unknown tool '{tool_name}'. "
                f"Available tools: {', '.join(t['name'] for t in self._tools_definitions.tools)}"
            )

        validator = Draft7Validator(schema)
        errors = sorted(validator.iter_errors(tool_params), key=lambda e: list(e.path))

        if not errors:
            return None

        messages = []
        for err in errors:
            location = (
                " → ".join(str(p) for p in err.absolute_path)
                if err.absolute_path
                else "root"
            )
            messages.append(f"  • [{location}] {err.message}")

        required_props = schema.get("properties", {})
        hint_lines = [
            f"    - {name}: {meta.get('type', 'any')} — {meta.get('description', '')}"
            for name, meta in required_props.items()
        ]
        hint_block = (
            "\nExpected parameters:\n" + "\n".join(hint_lines) if hint_lines else ""
        )

        return (
            f"Validation failed for tool '{tool_name}':\n"
            + "\n".join(messages)
            + hint_block
        )

    async def _get_sandbox(self) -> AsyncSandbox:
        return await get_or_connect_sandbox(self.sdbx_id)

    def _shell_error(self, context: str, exc: Exception) -> dict:
        return {
            "script_path": None,
            "stdout": "",
            "stderr": f"[{type(exc).__name__}] {context}: {exc}",
            "exit_code": 1,
        }

    async def execute_shell_command(
        self,
        command: str,
        user: str = "user",
        cwd: str | None = None,
        background: bool = False,
    ):
        try:
            sandbox = await self._get_sandbox()
            return await sandbox.commands.run(
                command, user=user, cwd=cwd, background=background
            )
        except Exception as e:
            raise RuntimeError(
                f"execute_shell_command failed.\nCommand: {command}\nReason: {type(e).__name__}: {e}"
            ) from e

    async def get_host_url(self, port: int = 8000) -> dict:
        try:
            sandbox = await self._get_sandbox()
            host = sandbox.get_host(port)
            return {"url": f"https://{host}", "port": port}
        except Exception as e:
            return {"url": None, "port": port, "error": f"[{type(e).__name__}] {e}"}

    async def read_memory(self, path: str = "actovator/project_memory.md") -> str:
        try:
            sandbox = await self._get_sandbox()
            return await sandbox.files.read(path)
        except Exception:
            return ""

    @staticmethod
    def _extract_memory_section(markdown: str, heading: str) -> str:
        """Extract content under a ## heading until the next ## or end of file."""
        lines = markdown.splitlines()
        inside = False
        collected = []
        for line in lines:
            if line.strip().startswith("## ") and heading.lower() in line.lower():
                inside = True
                continue
            if inside:
                if line.startswith("## ") or line.startswith("# "):
                    break
                collected.append(line)
        return "\n".join(collected).strip()

    async def get_server_logs(self, lines_count: int = 25) -> str:
        try:
            result = await self.execute_shell_command(
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

    async def get_lint_checks(self) -> str:
        try:
            result = await self.execute_shell_command("npm run lint", cwd=PROJECT_PATH)
            return result.stdout
        except Exception as e:
            return f"[{type(e).__name__}] Failed to run lint checks: {e}"

    async def execute_tool(self, tool_name: str, tool_params: dict) -> dict:
        validation_error = self._validate_tool_params(tool_name, tool_params)
        if validation_error:
            return {
                "stdout": "",
                "stderr": validation_error,
                "exit_code": 1,
            }

        import hashlib

        payload = json.dumps(tool_params)
        digest = hashlib.sha1(payload.encode()).hexdigest()[:8]
        payload_path = f"/tmp/payload_{digest}.json"

        try:
            sandbox = await self._get_sandbox()
            await sandbox.files.write(payload_path, payload)

            host_result = await self.get_host_url(8000)
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
                f'; echo "HTTP_STATUS:$?"'
            )

            result = await self.execute_shell_command(command, cwd=PROJECT_PATH)
            return {
                "stdout": getattr(result, "stdout", ""),
                "stderr": getattr(result, "stderr", ""),
                "exit_code": getattr(result, "exit_code", 1),
            }
        except Exception as e:
            return self._shell_error(f"Failed to execute tool '{tool_name}'", e)
        finally:
            try:
                await self.execute_shell_command(f"rm -f {payload_path}")
            except Exception:
                pass

    async def manage_npm_package(
        self, package: str, action: str = "install", is_dev: bool = False
    ) -> dict:
        if action == "remove":
            command = f"npm uninstall {package}"
        else:
            flag = "--save-dev" if is_dev else "--save"
            command = f"npm install {flag} {package}"
        try:
            result = await self.execute_shell_command(command, cwd=PROJECT_PATH)
            return {
                "stdout": getattr(result, "stdout", ""),
                "stderr": getattr(result, "stderr", ""),
                "exit_code": getattr(result, "exit_code", 1),
            }
        except Exception as e:
            return self._shell_error(f"Failed to {action} npm package '{package}'", e)

    async def process_screenshot(
        self,
        url: str,
        viewport: tuple[int, int] | None = None,
        device: str | None = None,
        annotate: bool = False,
        full_page: bool = True,
        wait_timeout_ms: int = 10000,
    ) -> list | dict:
        """
        Core screenshot logic. See the @tool wrapper for parameter docs.
        """
        import time

        timestamp = int(time.time())
        screenshot_filename = f"screenshot-{timestamp}.png"

        try:
            # Always start clean
            await self.execute_shell_command("agent-browser close", user="root")

            # Open the page
            result = await self.execute_shell_command(
                f"agent-browser open {url}", user="root"
            )
            if getattr(result, "exit_code", 1) != 0:
                return {
                    "stdout": getattr(result, "stdout", ""),
                    "stderr": getattr(result, "stderr", ""),
                    "exit_code": getattr(result, "exit_code", 1),
                    "image_data": None,
                }

            # Device emulation takes precedence over viewport
            if device:
                await self.execute_shell_command(
                    f'agent-browser set device "{device}"', user="root"
                )
            elif viewport:
                width, height = viewport
                await self.execute_shell_command(
                    f"agent-browser set viewport {width} {height}", user="root"
                )

            # Safe page-load wait:
            # 1. Try networkidle with a capped timeout (some sites never reach idle)
            # 2. Fall back to document.readyState === 'complete'
            # 3. Last resort: short fixed delay
            wait_result = await self.execute_shell_command(
                f"AGENT_BROWSER_DEFAULT_TIMEOUT={wait_timeout_ms} "
                f"agent-browser wait --load networkidle",
                user="root",
            )
            if getattr(wait_result, "exit_code", 0) != 0:
                fallback = await self.execute_shell_command(
                    "agent-browser wait --fn \"document.readyState === 'complete'\"",
                    user="root",
                )
                if getattr(fallback, "exit_code", 0) != 0:
                    # Last resort — give the page a moment then continue anyway
                    await self.execute_shell_command(
                        "agent-browser wait 2000", user="root"
                    )

            # Build screenshot flags
            flags = []
            if full_page:
                flags.append("--full")
            if annotate:
                flags.append("--annotate")
            flags_str = " ".join(flags)

            result = await self.execute_shell_command(
                f"agent-browser screenshot {flags_str} {screenshot_filename}",
                user="root",
            )

            await self.execute_shell_command("agent-browser close", user="root")

            if getattr(result, "exit_code", 1) != 0:
                return {
                    "stdout": getattr(result, "stdout", ""),
                    "stderr": getattr(result, "stderr", ""),
                    "exit_code": getattr(result, "exit_code", 1),
                    "image_data": None,
                }

            b64_result = await self.execute_shell_command(
                f"base64 -w 0 {screenshot_filename}"
            )
            image_data = b64_result.stdout.strip()

            output = [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_data}"},
                },
                {
                    "type": "text",
                    "text": (
                        f"Screenshot captured: {screenshot_filename}"
                        + (f" | device={device}" if device else "")
                        + (
                            f" | viewport={viewport[0]}x{viewport[1]}"
                            if viewport and not device
                            else ""
                        )
                        + (" | annotated=true" if annotate else "")
                    ),
                },
            ]

            if annotate:
                output.append(
                    {
                        "type": "text",
                        "text": (
                            "Annotated screenshot: numbered labels (e.g. [1], [2]) "
                            "are overlaid on interactive elements. Each label [N] maps "
                            "to ref @eN for follow-up interactions."
                        ),
                    }
                )

            return output

        except Exception as e:
            return self._shell_error("process_screenshot failed", e)
        finally:
            try:
                await self.execute_shell_command(f"rm -f {screenshot_filename}")
            except Exception:
                pass

    async def execute_agent_browser(self, command: str) -> dict:

        stripped = command.strip()
        if not stripped.startswith("agent-browser"):
            return {
                "stdout": "",
                "stderr": f"Rejected: command must start with 'agent-browser', got: '{stripped[:60]}'",
                "exit_code": 1,
            }
        if "screenshot" in stripped:
            return {
                "stdout": "",
                "stderr": "Rejected: use `process_screenshot` tool instead",
                "exit_code": 1,
            }

        parts = stripped.split()
        if "--engine" not in parts:
            # Find the position after 'agent-browser'
            if parts[0] == "agent-browser":
                parts.insert(1, "--engine")
                parts.insert(2, "lightpanda")
            stripped = " ".join(parts)
        try:
            result = await self.execute_shell_command(stripped, cwd=PROJECT_PATH)
            return {
                "stdout": getattr(result, "stdout", ""),
                "stderr": getattr(result, "stderr", ""),
                "exit_code": getattr(result, "exit_code", 1),
            }
        except Exception as e:
            return self._shell_error("execute_agent_browser failed", e)

    def as_langchain_tools(self) -> dict:
        instance = self

        @tool
        async def execute_tool(tool_name: str, tool_params: dict) -> dict:
            """
            Invoke a registered sandbox tool by name.

            Args:
                tool_name: The registered name of the tool to invoke.
                tool_params: A dictionary of parameters to pass as the JSON request body.

            Returns:
                dict with 'stdout', 'stderr', and 'exit_code'.
            """
            return await instance.execute_tool(tool_name, tool_params)

        @tool
        async def manage_npm_package(
            package: str, action: str = "install", is_dev: bool = False
        ) -> dict:
            """
            Install or remove an npm package inside the project.

            **Note: prefer not to pin the package version unless required.**

            Args:
                package: The npm package name to install or remove.
                action: Either "install" (default) or "remove".
                is_dev: If True, installs as a devDependency — ignored when action is "remove".

            Returns:
                dict with 'stdout', 'stderr', and 'exit_code'.
            """
            return await instance.manage_npm_package(package, action, is_dev)

        @tool
        async def get_server_logs(lines_count: int = 25) -> str:
            """
            Fetch the latest server logs from pm2.
            Use this to detect runtime errors after code changes.

            Args:
                lines_count: Number of recent log lines to return (default 25).
            """
            return await instance.get_server_logs(lines_count)

        @tool
        async def get_lint_checks() -> str:
            """
            Run ESLint on the project and return the full output.
            Use this to detect code-quality violations after code changes.
            """
            return await instance.get_lint_checks()

        @tool
        async def execute_agent_browser(command: str) -> dict:
            """
            Execute an agent-browser CLI command inside the sandbox.
            Only accepts commands that start with 'agent-browser'.

            Args:
                command: A full agent-browser CLI command string.

            Returns:
                dict with 'stdout', 'stderr', and 'exit_code'.
            """
            return await instance.execute_agent_browser(command=command)

        @tool
        async def process_screenshot(
            url: str,
            viewport: tuple[int, int] | None = None,
            device: str | None = None,
            annotate: bool = False,
            full_page: bool = True,
            wait_timeout_ms: int = 10000,
        ) -> list:
            """
            Capture a screenshot of a URL using a headless browser (agent-browser).

            Handles device emulation, custom viewports, element annotation, and
            resilient page-load detection: first attempts network-idle (with a
            configurable timeout), falls back to document.readyState, then a
            short fixed delay — so it works even on sites that never reach idle.

            Args:
                url (str):
                    The fully-qualified URL to capture (e.g. "https://example.com").

                viewport (tuple[int, int] | None):
                    Custom browser window size as (width, height) in CSS pixels.
                    Example: (1920, 1080) for desktop, (375, 812) for a mobile width.
                    Ignored when `device` is provided (device sets its own viewport).

                device (str | None):
                    Emulate a named device — sets viewport AND user-agent together.
                    Takes precedence over `viewport`.
                    Examples: "iPhone 14", "iPad Pro", "Pixel 7", "Galaxy S21".

                annotate (bool):
                    Overlay numbered labels ([1], [2], …) on every interactive element.
                    Each label [N] maps to ref @eN for follow-up click/fill commands.
                    Useful for debugging layouts, identifying buttons, or mapping a page
                    before automation. Default: False.

                full_page (bool):
                    Capture the entire scrollable page, not just the visible viewport.
                    Default: True. Set to False for above-the-fold / viewport-only shots.

                wait_timeout_ms (int):
                    How long (ms) to wait for network idle before falling back.
                    Default: 10 000 (10 s). Raise for slow/heavy sites; lower for fast
                    ones or pages that stream indefinitely (e.g. live dashboards).

            Returns:
                list: Two or three items:
                    - image_url block  — base64-encoded PNG as a data URL.
                    - text block       — filename plus a summary of options used.
                    - text block       — (only when annotate=True) explains the label→ref
                                        mapping for follow-up interactions.

            Raises:
                Returns a dict with stdout/stderr/exit_code on browser or shell failure.
            """
            return await instance.process_screenshot(
                url,
                viewport=viewport,
                device=device,
                annotate=annotate,
                full_page=full_page,
                wait_timeout_ms=wait_timeout_ms,
            )

        return {
            "execute_tool": execute_tool,
            "manage_npm_package": manage_npm_package,
            "get_server_logs": get_server_logs,
            "get_lint_checks": get_lint_checks,
            "execute_agent_browser": execute_agent_browser,
            "process_screenshot": process_screenshot,
        }


class BuildGitTools:
    """
    Builds and exposes git tools in E2B AsyncSandbox.
    """

    def __init__(
        self,
        sdbx_id: str,
    ) -> None:
        self.sdbx_id = sdbx_id

    async def _get_sandbox(self) -> AsyncSandbox:
        return await get_or_connect_sandbox(self.sdbx_id)

    def _shell_error(self, context: str, exc: Exception) -> dict:
        return {
            "script_path": None,
            "stdout": "",
            "stderr": f"[{type(exc).__name__}] {context}: {exc}",
            "exit_code": 1,
        }

    async def execute_shell_command(
        self,
        command: str,
        user: str = "user",
        cwd: str | None = None,
        background: bool = False,
    ):
        try:
            sandbox = await self._get_sandbox()
            return await sandbox.commands.run(
                command, user=user, cwd=cwd, background=background
            )
        except Exception as e:
            raise RuntimeError(
                f"execute_shell_command failed.\nCommand: {command}\nReason: {type(e).__name__}: {e}"
            ) from e

    async def ensure_git_remote_origin(
        self, remote_url: Optional[str] = None, repo_name: Optional[str] = None
    ) -> dict:

        res = await self.execute_shell_command(
            "git rev-parse --is-inside-work-tree", cwd=PROJECT_PATH
        )
        if res.exit_code != 0 or res.stdout.strip() != "true":
            init_res = await self.execute_shell_command("git init", cwd=PROJECT_PATH)
            if init_res.exit_code != 0:
                return self._shell_error(
                    "error-initialising-repo",
                    Exception(
                        init_res.stderr or "Unknown error initialising git repository."
                    ),
                )

        # 2. Return early if an origin remote already exists.
        async def _get_origin_url():
            try:
                origin_res = await self.execute_shell_command(
                    "git remote get-url origin", cwd=PROJECT_PATH
                )
                if origin_res.exit_code == 0:
                    return origin_res.stdout.strip()
            except Exception:
                pass
            return None

        existing = await _get_origin_url()
        if existing is not None:
            return {
                "status": "already-exists",
                "remote_url": existing,
                "created_repo": None,
                "error": "",
            }

        # 3. Add a caller-supplied remote URL.
        if remote_url:
            res = await self.execute_shell_command(
                f"git remote add origin {remote_url}", cwd=PROJECT_PATH
            )
            if res.exit_code != 0:
                return self._shell_error(
                    "error-adding-remote-url",
                    Exception(res.stderr or "Unknown error adding remote URL."),
                )
            return {
                "status": "set-remote-url-supplied",
                "remote_url": remote_url,
                "created_repo": None,
                "error": "",
            }

        # 4. Create a new GitHub repo with the CLI.
        # Resolve repo_name: if absent or blank, derive it from the sandbox directory.
        name = None
        if repo_name:
            name = repo_name.strip() if isinstance(repo_name, str) else ""

        if not name:
            # Append a short uuid4 to ensure uniqueness

            basename_res = await self.execute_shell_command(
                "basename $(pwd)", cwd=PROJECT_PATH
            )
            base_name = (
                basename_res.stdout.strip() if basename_res.exit_code == 0 else ""
            )
            name = f"{base_name}-actovator-{self.sdbx_id}" if base_name else ""
        if not name:
            return self._shell_error(
                "error-creating-remote-url",
                Exception(
                    "Could not determine a repository name: provide repo_name or ensure PROJECT_PATH is a named directory."
                ),
            )
        res = await self.execute_shell_command(
            f"gh repo create {name} --private --source=. --remote=origin",
            cwd=PROJECT_PATH,
        )
        origin_url = await _get_origin_url()
        if res.exit_code == 0 and origin_url:
            return {
                "status": "created-remote-url",
                "remote_url": origin_url,
                "created_repo": name,
                "error": "",
            }
        return self._shell_error(
            "error-creating-remote-url",
            Exception(res.stderr or "Unknown error creating remote URL via gh CLI."),
        )

    async def commit_changes(self, message: str) -> dict:
        try:
            await self.execute_shell_command("git add -A", cwd=PROJECT_PATH)
            result = await self.execute_shell_command(
                f"git commit -m {shlex.quote(message)}", cwd=PROJECT_PATH, user="root"
            )
            if result.exit_code != 0:
                return {"ok": False, "stdout": result.stdout, "stderr": result.stderr}
            push = await self.execute_shell_command(
                "git push origin HEAD", cwd=PROJECT_PATH
            )
            return {
                "ok": push.exit_code == 0,
                "stdout": push.stdout,
                "stderr": push.stderr,
            }
        except Exception as e:
            return self._shell_error("commit_changes failed", e)

    def as_langchain_tools(self) -> dict:
        instance = self

        @tool
        async def commit_changes(message: str) -> dict:
            """
            Stage all changes, commit with the given message, and push to origin HEAD.

            Args:
                message: Imperative commit message, max 72 characters.

            Returns:
                dict with 'ok', 'stdout', and 'stderr'.
            """
            return await instance.commit_changes(message)

        return {"commit_changes": commit_changes}


async def test():
    git_builder = BuildGitTools("iti5z2kpx0ssvodih6xlq")
    await git_builder.ensure_git_remote_origin()

    git_lc_tools = git_builder.as_langchain_tools()
    result = await git_lc_tools["commit_changes"](message="first commit")
    print(result)


if __name__ == "__main__":
    import asyncio

    asyncio.run(test())
