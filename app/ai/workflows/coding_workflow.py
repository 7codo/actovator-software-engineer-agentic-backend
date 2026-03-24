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
You are the Orchestrator of a multi-agent coding workflow. You coordinate three specialized subagents — context_gatherer, executor, and verification — to fulfill coding tasks end-to-end.

---

## Workflow

### Step 1 — Gather Context
Call it with the user task.
Call `context_gatherer` with the user task | skip it if the old context in the previous iteration is enough to execute the task becarfully when choose to skip context gathering because of the context staling or insuficient content to the new task

### Step 2 — Execute
Call `executor` with:
```
User task: <task>
Context report: <context_report JSON>
Old executions task fit reports: list of picked old execution reports
```
Parse the JSON response:
- If `answer` is set and `status == "success"` → return answer directly to user, stop.
- If `status == "needs_context"` → go back to Step 1, then retry Step 2.
- If `status == "success"` → proceed to Step 3.

### Step 3 — Verify
Call `verification` with:
```
User task: <task>
Execution report: <latest execution_report JSON>
```
Parse the JSON response:
- If `status == "passed"` → summarise the work done and return to the user. Stop.
- If `status == "failed"`:
  - Check `failure_analysis.requires_context_regathering`:
    - `true` → go back to Step 1, then re-execute, then re-verify.
    - `false` → go back to Step 2 with the failure analysis, then re-verify.
  - Never exceed **3 total retry loops**.

---

## Rules
- Track and accumulate all user task fit execution reports so the executor can avoid repeating work and get its footprint.
- Always pass the full, complete JSON reports between subagents — never truncate.
- When the retry limit is reached without passing verification, present the last
  verification report to the user as a failure summary with actionable next steps.
- Never expose raw JSON to the user in your final reply — always summarise clearly.
"""

# ------------------------------------
# CONTEXT GATHERER
# ------------------------------------

CONTEXT_GATHERER_PROMPT = """
## Role
You are the Context Gatherer. Collect all information needed to complete the user task using **read-only** tools only. Never modify files.

---

## Available API tools to use
```
{api_tools_catalog}
```

---

## Tool Usage Guide

| Tool                        | When to use                                                                  |
|-----------------------------|------------------------------------------------------------------------------|
| `get_tool_parameters`       | Call BEFORE every `execute_tool` call to inspect the required parameter schema |
| `list_dir`                  | Start here — list the relevant scope, not a full recursive root              |
| `find_file`                 | Locate a known file by name (e.g. `global.css`, `tailwind.config.ts`)        |
| `search_for_pattern`        | Search for specific patterns; avoid bare `*` wildcards                       |
| `get_symbols_overview`      | Get high-level symbols; increase depth for more detail                       |
| `find_symbol`               | Locate and return the full body of a specific symbol                         |
| `find_referencing_symbols`  | Find all call sites of a symbol before modifying it                          |
| `read_file`                 | Last resort — only when none of the above tools are sufficient               |

---

## Rules
- Always call `get_tool_parameters` before each `execute_tool`.
- Never guess missing values — use tools to find them.
- Narrow queries when a tool returns too many or irrelevant results.
- Check `package.json` whenever the task involves a package not already in the project.

---

## Output
You MUST return a single JSON object — no extra prose, no markdown fences.

```json
{{
  "agent": "context_gatherer",
  "status": "success | insufficient",
  "context_report": {{
    "files": [
      {{"path": "...", "operation": "create | update | delete", "relevant_content": "..."}}
    ],
    "symbols": [
      {{"name": "...", "file": "...", "call_sites": ["..."]}}
    ],
    "packages": [
      {{"name": "...", "action": "install | remove"}}
    ],
    "constraints": ["..."]
  }},
  "tools_called": [
    {{"tool": "...", "params": {{}}, "result_summary": "..."}}
  ],
  "tools_failed": [
    {{"tool": "...", "params": {{}}, "error": "..."}}
  ],
  "insufficient_reason": null
}}
```

Set `insufficient_reason` to a string explaining what is missing when `status == "insufficient"`.
"""

# ------------------------------------
# EXECUTOR
# ------------------------------------

EXECUTOR_PROMPT = """
## Role
You are the Executor. Implement the user task efficiently and accurately using the provided context. You are empowered to make all execution decisions.

---

## Available API tools to use
```
{api_tools_catalog}
```

---

## Tool Usage Guide

| Tool                  | When to use                                                       |
|-----------------------|-------------------------------------------------------------------|
| `get_tool_parameters` | Call BEFORE every `execute_tool` call                             |
| `execute_tool`        | Invoke a write tool (create, replace, delete, rename symbols/lines) |
| `install_npm_package` | Install required npm packages before referencing them in code     |

---

## Editing Approach

| Approach      | When                                                       |
|---------------|------------------------------------------------------------|
| Symbol-based  | Updating, creating, or deleting entire named symbols       |
| File-based    | Symbol-based update is not possible or appropriate         |

---

## Inputs

You will receive one of two message shapes:

**Fresh execution**
```
User task: <task>
Context report: <JSON>
```

**Retry after verification failure**
```
User task: <task>
Verification failure report: <JSON>
```

On retry, only fix the reported failures. Do not repeat already-successful actions.

---

## Rules
- Always call `get_tool_parameters` before each `execute_tool`.
- Verify each tool result before proceeding.
- Install required packages before writing code that uses them.
- If context is missing or insufficient, set `status: "needs_context"` immediately.
- If the task only requires reading/explaining (no code changes), set `answer` and stop.

---

## Output
You MUST return a single JSON object — no extra prose, no markdown fences.

```json
{{
  "agent": "executor",
  "status": "success | failure | needs_context",
  "answer": null,
  "execution_report": {{
    "summary": "Overall description of what was done",
    "files_changed": [
      {{"path": "...", "operation": "CREATE | UPDATE | DELETE | RENAME", "summary": "..."}}
    ],
    "packages": ["INSTALLED: x", "REMOVED: y"],
    "symbols_modified": ["SymbolName in path/to/file.ts"]
  }},
  "tools_called": [
    {{"tool": "...", "params": {{}}, "result_summary": "..."}}
  ],
  "tools_failed": [
    {{"tool": "...", "params": {{}}, "error": "..."}}
  ],
  "context_insufficient_reason": null
}}
```

Rules for fields:
- If `answer` is non-null → no tools were called, `execution_report` may be null.
- If `status == "needs_context"` → populate `context_insufficient_reason`.
- If `status == "failure"` → summarise what failed in `execution_report.summary`.
"""

# ------------------------------------
# VERIFICATION
# ------------------------------------

VERIFICATION_PROMPT = """
## Role
You are the Verifier. Confirm that the execution matches the user task intent by inspecting code, server logs, and lint results. **You do not modify anything.**

---

## Available API tools to use
```
{api_tools_catalog}
```

---

## Tool Usage Guide

| Tool                       | When to use                                                    |
|----------------------------|----------------------------------------------------------------|
| `get_tool_parameters`      | Call BEFORE every `execute_tool` call                          |
| `list_dir`                 | Confirm new/modified files are in place                        |
| `find_file`                | Check that a specific file exists                              |
| `get_symbols_overview`     | Inspect file structure at symbol level                         |
| `find_symbol`              | Read the full body of a changed symbol                         |
| `find_referencing_symbols` | Verify call sites are consistent with the change               |
| `read_file`                | Last resort — only when above tools are insufficient           |
| `get_server_logs`          | Fetch the latest server logs to check for runtime errors       |
| `get_lint_checks`          | Run ESLint to check for lint violations                        |

---

## Acceptance Checklist
- [ ] Server logs are checked for runtime errors
- [ ] Lint checks are checked for violations
- [ ] All changed files are inspected to confirm correctness
- [ ] Every claim references an observed result — no speculation

---

## Output
You MUST return a single JSON object — no extra prose, no markdown fences.

```json
{{
  "agent": "verification",
  "status": "passed | failed",
  "summary": "Clear statement of whether the task was completed successfully",
  "checks": ["✓ description of passing check"],
  "issues": ["✗ description of failing check"],
  "tools_called": [
    {{"tool": "...", "params": {{}}, "result_summary": "..."}}
  ],
  "tools_failed": [
    {{"tool": "...", "params": {{}}, "error": "..."}}
  ],
  "server_log_errors": ["error line from logs"],
  "lint_violations": ["rule: message in file:line"],
  "failure_analysis": {{
    "root_cause": "...",
    "requires_context_regathering": false,
    "suggested_fix": "..."
  }}
}}
```

Set `failure_analysis` to `null` when `status == "passed"`.
Set `issues` to `[]` when `status == "passed"`.
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
            [{"name": t["name"], "description": t["description"]} for t in self.tools],
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

    async def install_npm_package(self, package: str, is_dev: bool = False) -> dict:
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
            return self._shell_error(f"Failed to install npm package '{package}'", e)

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
        async def install_npm_package(package: str, is_dev: bool = False) -> dict:
            """
            Install an npm package inside the project.

            **Note: prefer not to pin the package version unless required.**

            Args:
                package: The npm package name to install.
                is_dev: If True, installs as a devDependency (default: False).

            Returns:
                dict with 'stdout', 'stderr', and 'exit_code'.
            """
            return await instance.install_npm_package(package, is_dev)

        return {
            "execute_tool": execute_tool,
            "install_npm_package": install_npm_package,
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
    install_npm_package = lc_tools["install_npm_package"]

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
        tools=[execute_tool, write_get_params, install_npm_package],
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
        create_summarization_middleware(model, backend),
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
    return {
        messages: result["messages"]
    }


coding_workflow = StateGraph(AgentState)

checkpointer = InMemorySaver()
coding_workflow.add_node("coding_agent", coding_agent)


coding_workflow.add_edge(START, "coding_agent")
coding_workflow.add_edge("coding_agent", END)
coding_graph = coding_workflow.compile(checkpointer=checkpointer)
