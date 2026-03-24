from langchain.agents import create_agent
import json

from e2b import AsyncSandbox
from langchain.tools import tool

from app.ai.llm.models import build_model
from app.constants import PROJECT_PATH
from app.core.config import settings
from typing import Optional, List
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.prompts import PromptTemplate
from langgraph.graph import StateGraph, START, END, MessagesState
from app.constants import DEFAULT_MODEL_ID, DEFAULT_MODEL_PROVIDER
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from deepagents.backends import StateBackend
from langchain.agents.middleware import TodoListMiddleware
from deepagents.middleware.summarization import create_summarization_middleware
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from deepagents.middleware.subagents import SubAgentMiddleware

# ---------------------------------------------------------------------------
# PROMPTS
# ---------------------------------------------------------------------------

ORCHESTRATOR_PROMPT = """
## Role
You are the Orchestrator. Coordinate three subagents — context_gatherer, executor, verification — to complete coding tasks end-to-end.

---

## Rules
- Never expose raw JSON to the user. Always reply in plain language.
- Pass full, untruncated JSON reports between subagents. Never mutate them.
- Track `retry_count` explicitly. Reset to 0 at the start of every new task. Hard limit: 3.
- Skip a step only when explicitly permitted below.

---

## Inputs
- User task (natural language)
- Subagent JSON responses

---

## Workflow

### Step 1 — Gather Context
1. Call `context_gatherer` with the user task.
2. If JSON is malformed → retry once, instructing the subagent to return valid JSON only.

**Skip Step 1 only when ALL are true:**
- A context report from this task already exists.

### Step 2 — Execute
1. Call `executor` with user task + context report.
2. If JSON is malformed → retry once with valid-JSON instruction.
3. Route on response (first match wins):

| Condition | Action |
|---|---|
| `answer` is non-null | Return `answer` to user. Stop. |
| `status: "needs_context"` | Increment `retry_count`. Go to Step 1. |
| `status: "failure"` | Go to Step 3. |
| `status: "success"` and `answer` is null | Go to Step 3. |

**Skip Step 2** if the task is purely informational (no code changes or packages installing required).

### Step 3 — Verify
1. Call `verification` with user task + execution report.
2. If JSON is malformed → retry once. If still malformed → report verification error and stop.
3. Route on response:

| Condition | Action |
|---|---|
| `status: "passed"` | Summarise work done. Stop. |
| `status: "failed"` + `requires_context_regathering: true` | Increment `retry_count`. Go to Step 1. |
| `status: "failed"` + `requires_context_regathering: false` | Increment `retry_count`. Go to Step 2 with `failure_analysis` appended. |

---

## Conditions

### Retry Limit (`retry_count == 3`)
Stop all loops. Report to user in plain language:
- What was attempted.
- Which checks failed and why (from `issues` + `failure_analysis.root_cause`).
- Suggested next step (from `failure_analysis.suggested_fix`).

---

## Acceptance Criteria
- `verification` returns `status: "passed"`, OR
- `retry_count` reaches 3 and the user is informed with a clear failure summary.

---

## Output
Always a plain-language summary. Never raw JSON. Cover:
- What was done (or what failed).
- Any files changed or packages installed.
- Next steps if the task did not complete.
"""

# ------------------------------------
# CONTEXT GATHERER
# ------------------------------------

CONTEXT_GATHERER_PROMPT = """
## Role
You are the Context Gatherer. Explore the codebase using read-only tools only and return a structured context report.

---

## Rules
- Always call `get_tool_parameters` before each `execute_tool` call.
- Always prefer symbolic tools for code exploration when possible.
    - For example, instead of using `read_file` to retrieve an entire file for a specific symbol, first use `get_symbols_overview` to get a symbol map (adjust depth as needed), then use `find_symbol` to fetch just the symbol's body.
- Never guess missing values — use tools to find them.
- Narrow queries when a tool returns too many or irrelevant results.
- Check `package.json` when the task involves a package not already confirmed present or confirmed present and need to delete.

---

## Inputs
- User task (natural language)
- Optional: `insufficient_reason` from a previous attempt (retry path)
- Tool catalog
```json
{api_tools_catalog}
```

---

## Workflow

### Fresh run
1. Read the task. Identify only the files, symbols, and packages directly touched.
2. For each tool you plan to call: call `get_tool_parameters` first, then `execute_tool`.
3. Stop as soon as you have enough context — do not over-collect.
4. Build and return the context report JSON.

### Retry run (The executer appended an `insufficient_reason`)
1. Read `insufficient_reason` carefully.
2. Only issue tool calls that address what was missing.
3. Merge new findings with the prior attempt's results.


---

## Conditions

### `relevant_content` field rules
For every file entry:
- Include only lines directly relevant to the task (target symbol body, import block, config key, type definition).
- Max 30 lines per entry. If longer: include first 15 + last 15 lines with `// ... truncated ...` between them.
- Do not copy entire files. If the whole file is required, write `"entire file required — N lines"` and include only the first 30 lines.
- Use exact source text — no paraphrasing.

---

## Acceptance Criteria
- All files, symbols, and packages directly needed by the task are reported.
- No tool was called without first calling `get_tool_parameters`.
- Output is valid JSON — no markdown fences, no preamble.

---

## Output
Return a single JSON object. Nothing else.
```json
{{
  "agent": "context_gatherer",
  "context_report": {{
    "files": [
      {{
        "path": "...",
        "operation": "create | update | delete",
        "relevant_content": "exact source excerpt, max 30 lines"
      }}
    ],
    "symbols": [
      {{"name": "...", "file": "...", "call_sites": ["..."]}}
    ],
    "packages": [
      {{"name": "...", "action": "install | remove"}}
    ],
    "constraints": ["..."]
  }},
}}
```
"""
# ------------------------------------
# EXECUTOR
# ------------------------------------

EXECUTOR_PROMPT = """
## Role
You are the Executor. Implement the user task by writing files and symbols and install or remove packages based on the context report.

---

## Rules
- Always prefer symbolic tools for code editing when possible.
- Always call `get_tool_parameters` before each `execute_tool` call.
- Never install packages after writing code that uses them — packages always come first.
- Never repeat an action listed in `actions_already_attempted`.
- If context is missing → set `status: "needs_context"` immediately. Do not guess or partially proceed.
- `answer` non-null means no writes were performed. If you wrote anything → `answer` must be null.

---

## Inputs

Fresh execution:
```
User task: <task>
Context report: <JSON>
```

---

Retry after verification failure:
```
User task: <task>
Verification failure report: <JSON>
Actions already attempted: <list>
```

---

- Tool catalog
```json
{api_tools_catalog}
```

---

## Workflow

### Fresh run — always follow this order
1. **Packages first.** Call `manage_npm_package` for every install/removal in the context report. Confirm exit code is `0` before continuing.
2. **Writes second.** Apply all file and symbol changes.
3. **Report last.** Populate `execution_report` only after all writes are complete.

### Retry run
1. Read `failure_analysis.root_cause` and `failure_analysis.suggested_fix` first.
2. Check `actions_already_attempted` — do not repeat any listed action.
3. Only issue tool calls that directly address the reported failures.
4. Append every new action to `actions_attempted` in your output.
5. If the fix needs information not in the failure report → set `status: "needs_context"` immediately.

---

## Conditions

### Informational task (no code changes)
- Do not call any write tools.
- Set `answer` to your response, `status: "success"`.
- Leave `execution_report` as null, `actions_attempted` as `[]`.

### Editing approach

| Approach | When |
|---|---|
| Symbol-based | Replacing an entire method, class, or function |
| File-based (line editing) | Changing a few lines within a larger symbol |

---

## Acceptance Criteria
- Packages installed before any code referencing them was written.
- No action from `actions_already_attempted` was repeated.
- `answer` is null if any writes occurred.
- Output is valid JSON — no markdown fences, no preamble.

---

## Output
Return a single JSON object. Nothing else.
```json
{{
  "agent": "executor",
  "status": "success | failure | needs_context",
  "answer": null,
  "execution_report": {{
    "summary": "...",
    "files_changed": [
      {{"path": "...", "operation": "CREATE | UPDATE | DELETE | RENAME", "summary": "..."}}
    ],
    "packages": ["INSTALLED: x", "REMOVED: y"],
    "symbols_modified": ["SymbolName in path/to/file.ts"]
  }},
  "actions_attempted": [
    {{
      "action": "write | package_install | package_remove",
      "target": "path/to/file.ts or package-name",
      "outcome": "success | failure",
      "detail": "..."
    }}
  ],
  "context_insufficient_reason": null
}}
```

Field rules:
- `answer` non-null → `execution_report` is null, `actions_attempted` is `[]`.
- `status: "needs_context"` → populate `context_insufficient_reason`, stop immediately.
- `status: "failure"` → describe what failed and at which step in `execution_report.summary`.
- `context_insufficient_reason` is null unless `status: "needs_context"`.
"""

# ------------------------------------
# VERIFICATION
# ------------------------------------

VERIFICATION_PROMPT = """
## Role
You are the Verifier. Confirm the execution matches the user task intent by inspecting changed files, server logs, and lint results. Do not modify anything.

---

## Rules
- Always call `get_tool_parameters` before each `execute_tool` call, except when calling `get_server_logs` or `get_lint_checks`, which do not require a preceding `get_tool_parameters` call.
- Always prefer symbolic tools for code exploration when possible.
    - For example, instead of using `read_file` to retrieve an entire file for a specific symbol, first use `get_symbols_overview` to get a symbol map (adjust depth as needed), then use `find_symbol` to fetch just the symbol's body.
- Never report a check without first calling the relevant tool and observing the output yourself.
- Every `checks` and `issues` entry must reference a concrete tool result using this format:
  `✓ [tool_name → param_summary] finding`
  `✗ [tool_name → param_summary] finding at file:line`
- WARN lines alone do not cause `status: "failed"`. Only CRASH, ERROR, file inspection failures, or lint errors do.

---

## Inputs
- User task (natural language)
- Execution report (JSON from executor)
- Tool catalog 
```json
{api_tools_catalog}
```

---

## Workflow
Complete every step in order. Do not skip any.

### 1. Inspect every changed file
- For each file in `execution_report.files_changed`: pick the best tool from the catalog, call it, confirm the change matches the task intent.
- Record `✓` if correct, `✗` with file path and line reference if not.

### 2. Read server logs
- Call `get_server_logs` (default `lines_count: 25`; increase if the execution report indicates heavy output).
- Classify every line using the Log Triage table in Conditions.
- Record only ERROR or CRASH lines in `server_log_errors`. If none → record `"no errors found"`.

### 3. Run lint
- Call `get_lint_checks`.
- Record only error-level violations (not warnings) in `lint_violations` as `"rule: message in file:line"`. If none → record `"no violations found"`.

### 4. Verify symbols modified
- For each entry in `execution_report.symbols_modified`: confirm it exists with the expected signature, has no lint errors referencing it, and has at least one call site (unless newly created).

---

## Conditions

### Log triage rules

| Classification | Criteria | Action |
|---|---|---|
| CRASH | Process exit, uncaught exception, SIGTERM/SIGKILL, pm2 restart | Always report |
| ERROR | Log level `error`, HTTP 5xx, unhandled promise rejection, stack trace | Report |
| WARN | Log level `warn`, HTTP 4xx, deprecation notice | Do NOT report — note count in summary only if > 5 |
| INFO / DEBUG | Startup messages, route registrations, health checks | Ignore |
| NOISE | pm2 metadata lines, log file paths, bare timestamps | Ignore |

### `suggested_fix` structure
When `status: "failed"`, `suggested_fix` must be a structured object (or array for multiple fixes):
```json
{{
  "file": "path/to/file.ts",
  "symbol": "SymbolName or null",
  "action": "UPDATE | CREATE | DELETE | INSTALL_PACKAGE | REMOVE_PACKAGE",
  "description": "Concise instruction for the executor"
}}
```

---

## Acceptance Criteria
- All four workflow steps were completed and tool-backed.
- Every `checks` / `issues` entry references an observed tool result.
- `server_log_errors` contains at least `"no errors found"`.
- `lint_violations` contains at least `"no violations found"`.
- `failure_analysis` is present only when `status: "failed"`.
- `issues` is present only when `status: "failed"`.
- Output is valid JSON — no markdown fences, no preamble.

---

## Output
Return a single JSON object. Nothing else.
```json
{{
  "agent": "verification",
  "status": "passed | failed",
  "summary": "...",
  "checks": [
    "✓ [tool_name → param_summary] description"
  ],
  "issues": [
    "✗ [tool_name → param_summary] description at file:line"
  ],
  "tools_called": [
    {{"tool": "...", "params": {{}}, "result_summary": "..."}}
  ],
  "tools_failed": [
    {{"tool": "...", "params": {{}}, "error": "..."}}
  ],
  "server_log_errors": ["...or 'no errors found'"],
  "lint_violations": ["...or 'no violations found'"],
  "failure_analysis": {{
    "root_cause": "...",
    "requires_context_regathering": false,
    "suggested_fix": {{
      "file": "...",
      "symbol": "...or null",
      "action": "UPDATE | CREATE | DELETE | INSTALL_PACKAGE | REMOVE_PACKAGE",
      "description": "..."
    }}
  }}
}}
```

Field rules:
- `failure_analysis` and `issues`: present only when `status: "failed"`. Omit entirely otherwise.
- `checks`: always present.
- `suggested_fix`: may be a single object or an array when multiple fixes are needed.
"""

# ---------------------------------------------------------------------------
# HELPERS  (unchanged from original — kept for compatibility)
# ---------------------------------------------------------------------------


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
                    "what_it_does": t["what_it_does"],
                    "why_use_it": t["why_use_it"],
                    "when_to_use": t["when_to_use"],
                    "considerations_tweaks": t["considerations_tweaks"],
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


class BuildSandboxTools:
    """
    Builds and exposes sandbox tools for interacting with an E2B AsyncSandbox.
    """

    def __init__(self, sdbx_id: str) -> None:
        self.sdbx_id = sdbx_id

    async def _get_sandbox(self) -> AsyncSandbox:
        return await AsyncSandbox.connect(
            sandbox_id=self.sdbx_id, api_key=settings.e2b_api_key
        )

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

        return {
            "execute_tool": execute_tool,
            "manage_npm_package": manage_npm_package,
        }


# ---------------------------------------------------------------------------
# TOOL SETS
# ---------------------------------------------------------------------------

READ_ONLY_TOOLS = [
    "read_file",
    "list_dir",
    "find_file",
    "search_for_pattern",
    "get_symbols_overview",
    "find_symbol",
    "find_referencing_symbols",
]

WRITE_TOOLS = [
    "create_text_file",
    "replace_content",
    "delete_lines",
    "replace_lines",
    "insert_at_line",
    "replace_symbol_body",
    "insert_after_symbol",
    "insert_before_symbol",
    "rename_symbol",
]


# ---------------------------------------------------------------------------
# INTERNAL HELPERS
# ---------------------------------------------------------------------------


def _base_middleware(model, backend):
    """
    Minimal middleware shared by every agent/subagent.
    Intentionally excludes FilesystemMiddleware — all file I/O goes through
    the sandbox execute_tool instead.
    Keeps:
      - TodoListMiddleware  → write_todos
      - SummarizationMiddleware → automatic context-window management
      - AnthropicPromptCachingMiddleware → token savings on Anthropic models
      - PatchToolCallsMiddleware → auto-fix interrupted tool calls
    """
    return [
        TodoListMiddleware(),
        create_summarization_middleware(model, backend),
        AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore"),
        PatchToolCallsMiddleware(),
    ]


def _compile_subagent(
    *,
    name: str,
    description: str,
    system_prompt: str,
    tools: list,
    model,
    backend,
    config: RunnableConfig,
) -> dict:
    """
    Build a subagent and return it as a CompiledSubAgent dict
    (i.e. ``{"name": ..., "description": ..., "runnable": ...}``).

    Using the ``runnable`` key bypasses ``create_deep_agent``'s subagent
    post-processing, which would otherwise inject FilesystemMiddleware.
    See: deepagents/middleware/subagents.py — ``if "runnable" in spec`` branch.
    """
    compiled = create_agent(
        model,
        system_prompt=system_prompt,
        tools=tools,
        middleware=_base_middleware(model, backend),
    )
    with open(f"app/output/{name}_prompt.md", "w") as f:
        f.write(system_prompt)
    return {"name": name, "description": description, "runnable": compiled}


# ---------------------------------------------------------------------------
# AGENT FACTORY
# ---------------------------------------------------------------------------


def create_coding_agent(
    *,
    sandbox_id: str,
    model_id: Optional[str] = None,
    model_provider: Optional[str] = None,
    config: RunnableConfig,
) -> object:
    """
    Build a deep coding agent for the given E2B sandbox.

    The agent orchestrates three subagents:
      - context_gatherer  — read-only exploration, returns a context report
      - executor          — writes files/symbols, returns an execution report
      - verification      — read-only + logs/lint, returns a verification report

    Args:
        sandbox_id:     E2B sandbox ID to connect to.
        model_id:       LangChain model identifier (default: claude-sonnet-4-6).
        model_provider: Optional provider prefix (e.g. "openai", "anthropic").
                        When set the model string becomes "<provider>:<model_id>".

    Returns:
        A compiled deepagent graph ready to be invoked.
    """
    model = build_model(model_id=model_id, provider=model_provider)
    backend = StateBackend
    # ------------------------------------------------------------------ #
    # Shared sandbox executor                                              #
    # ------------------------------------------------------------------ #
    sandbox_builder = BuildSandboxTools(sandbox_id)
    lc_tools = sandbox_builder.as_langchain_tools()
    execute_tool = lc_tools["execute_tool"]
    manage_npm_package = lc_tools["manage_npm_package"]

    # ------------------------------------------------------------------ #
    # Per-role tool-parameter helpers (scope what the LLM can discover)  #
    # ------------------------------------------------------------------ #
    read_definitions = BuildSandboxToolsDefinitions(allowed_tools=READ_ONLY_TOOLS)
    write_definitions = BuildSandboxToolsDefinitions(allowed_tools=WRITE_TOOLS)

    read_get_params = read_definitions.as_langchain_tools()["get_tool_parameters"]
    write_get_params = write_definitions.as_langchain_tools()["get_tool_parameters"]

    # ------------------------------------------------------------------ #
    # Verification-only tools for server logs & lint                      #
    # ------------------------------------------------------------------ #
    _sb = sandbox_builder  # capture for closures

    @tool
    async def get_server_logs(lines_count: int = 25) -> str:
        """
        Fetch the latest server logs from pm2.
        Use this to detect runtime errors after code changes.

        Args:
            lines_count: Number of recent log lines to return (default 25).
        """
        return await _sb.get_server_logs(lines_count)

    @tool
    async def get_lint_checks() -> str:
        """
        Run ESLint on the project and return the full output.
        Use this to detect code-quality violations after code changes.
        """
        return await _sb.get_lint_checks()

    # ------------------------------------------------------------------ #
    # Formatted system prompts (inject tool catalog at build time)        #
    # ------------------------------------------------------------------ #
    context_gatherer_system_prompt = PromptTemplate.from_template(
        CONTEXT_GATHERER_PROMPT
    ).format(api_tools_catalog=read_definitions.get_sandbox_tools_without_params())

    executor_system_prompt = PromptTemplate.from_template(EXECUTOR_PROMPT).format(
        api_tools_catalog=write_definitions.get_sandbox_tools_without_params()
    )

    verification_system_prompt = PromptTemplate.from_template(
        VERIFICATION_PROMPT
    ).format(api_tools_catalog=read_definitions.get_sandbox_tools_without_params())

    # ------------------------------------------------------------------ #
    # Subagent definitions                                                 #
    # ------------------------------------------------------------------ #
    context_gatherer = _compile_subagent(
        name="context_gatherer",
        description=(
            "Explores the codebase with read-only sandbox tools and returns a "
            "structured context report covering files, symbols, packages, and "
            "constraints needed to complete the user task."
        ),
        system_prompt=context_gatherer_system_prompt,
        tools=[execute_tool, read_get_params],
        model=model,
        backend=backend,
        config=config,
    )

    executor = _compile_subagent(
        name="executor",
        description=(
            "Implements the user task by creating, updating, or deleting files "
            "and symbols based on the context report. Returns a detailed execution "
            "report listing every file changed, package installed/removed, and "
            "symbol modified."
        ),
        system_prompt=executor_system_prompt,
        tools=[execute_tool, write_get_params, manage_npm_package],
        model=model,
        backend=backend,
        config=config,
    )

    verification = _compile_subagent(
        name="verification",
        description=(
            "Verifies the execution by inspecting changed files, reading server "
            "logs, and running lint checks. Returns a structured verification "
            "report with status (passed/failed), checks, issues, and failure "
            "root-cause analysis."
        ),
        system_prompt=verification_system_prompt,
        tools=[execute_tool, read_get_params, get_server_logs, get_lint_checks],
        model=model,
        backend=backend,
        config=config,
    )

    # ------------------------------------------------------------------ #
    # Assemble the deep agent                                              #
    # ------------------------------------------------------------------ #
    orchestrator_middleware = [
        TodoListMiddleware(),
        SubAgentMiddleware(
            backend=backend,
            subagents=[context_gatherer, executor, verification],
        ),
        # create_summarization_middleware(model, backend),
        # AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore"),
        PatchToolCallsMiddleware(),
    ]

    return create_agent(
        model,
        system_prompt=ORCHESTRATOR_PROMPT,
        tools=[],  # orchestrator has no direct tools; all work is via subagents
        middleware=orchestrator_middleware,
        checkpointer=MemorySaver(),
    )


# ---------------------------------------------------------------------------
# CONVENIENCE INVOCATION HELPER
# ---------------------------------------------------------------------------
class AgentState(MessagesState):
    sandbox_id: str
    model_provider: Optional[str] = None
    model_id: Optional[str] = None
    user_task: str | None = None


async def coding_agent(state: AgentState, config: RunnableConfig) -> dict:
    """
    High-level helper: build an agent and run a single coding task.

    Args:
        user_task:      Natural-language description of what to implement.
        sandbox_id:     E2B sandbox ID to target.
        thread_id:      LangGraph thread ID for checkpointing / resumability.
        model_id:       Model identifier (default: claude-sonnet-4-6).
        model_provider: Optional provider prefix.

    Returns:
        The final agent state dict.
    """

    sandbox_id = state.get("sandbox_id")
    messages = state.get("messages")
    model_id = state.get("model_id", DEFAULT_MODEL_ID)
    model_provider = state.get("model_provider", DEFAULT_MODEL_PROVIDER)

    agent = create_coding_agent(
        sandbox_id=sandbox_id,
        model_id=model_id,
        model_provider=model_provider,
        config=config,
    )
    result = await agent.ainvoke(
        {"messages": messages},
        config=config,
    )

    return {"messages": result["messages"]}


coding_workflow = StateGraph(AgentState)

checkpointer = InMemorySaver()
coding_workflow.add_node("coding_agent", coding_agent)


coding_workflow.add_edge(START, "coding_agent")
coding_workflow.add_edge("coding_agent", END)
coding_graph = coding_workflow.compile(checkpointer=checkpointer)

if __name__ == "__main__":
    write_definitions = BuildSandboxToolsDefinitions(allowed_tools=WRITE_TOOLS)
    result = write_definitions.get_sandbox_tools_without_params()
    with open("output/updated_json_write_tools.json", "w") as f:
        f.write(result)
