from langchain.agents import create_agent
import json
from typing import Optional, List
from jsonschema import Draft7Validator
from e2b import AsyncSandbox
from langchain.tools import tool
from pydantic import BaseModel, Field
from langgraph.types import Command
from app.ai.llm.models import build_model
from app.constants import PROJECT_PATH
from app.core.config import settings
from langgraph.graph.message import MessagesState
from langchain_core.messages import HumanMessage, AIMessage, BaseMessage
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver
from enum import Enum
from app.constants import DEFAULT_MODEL_ID, DEFAULT_MODEL_PROVIDER


# ---------------------------------------------------------------------------
# PROMPTS
# ---------------------------------------------------------------------------


# ------------------------------------
# CONTEXT GATHERER
# ------------------------------------

CONTEXT_GATHERER_PROMPT = """\
## Role
You are the **Context Gatherer**, the first agent before the Executor and Verifier agents. Explore the codebase and return a structured context report that will be passed to the Executor agent.

---

## Rules
- Always call `get_tool_parameters` before each `execute_tool` call.
- Always prefer symbolic tools for code exploration when possible.
    - For example, instead of using `read_file` to retrieve an entire file for a specific symbol, first use `get_symbols_overview` to get a symbol map (adjust depth as needed), then use `find_symbol` to fetch just the symbol's body.
- Never guess missing values — use tools to discover them.
- Only use `search_for_pattern` or `read_file` if symbolic tools cannot retrieve the required information
- Use `find_referencing_symbols` to identify every symbol in the codebase that references a given symbol. This helps prevent breaking changes to know exactly how many places depend on it and where.
- Start at the narrowest scope and widen only as needed.
- Check `package.json` when the task involves adding or removing a package.
For every file entry:
- Include only lines directly relevant to the task (target symbol body, import block, config key, type definition).
- **Max 30 lines per entry.** If longer: include the first 15 + last 15 lines with `// ... truncated ...` between them.
- Do not copy entire files. If the full file is genuinely required, write `"entire file required — N lines"` and include only the first 30 lines.
- Use exact source text — no paraphrasing.

---

## Inputs
Processing depends on the input path:

### Normal Path:
- **User task** — a natural language description of the requested change or feature.
- **Execution History** — previous changes made by the executor agent.

### Insufficient Context Path:
If the Executor agent could not proceed due to lack of information, you will receive a verification report from the Verifier agent containing these additional inputs:
- **`insufficient_reason`** — the cause cited by the Executor for why the previous context report was incomplete.
- **`previous_context_report`** — the full context report generated in the previous run.

- **Tool catalog**
```json
{api_tools_catalog}
```

---

## Workflow

### Fresh Run
1. Read the task. Identify only the files, symbols, and packages directly touched.
2. Read the execution history to be aware of previous changes.
3. For each tool call: invoke `get_tool_parameters` first, then `execute_tool`.
4. Stop as soon as you have sufficient context — do not over-collect.
5. Build and return the context report.

### Retry Run
1. Read `insufficient_reason` carefully. Treat `previous_context_report` as the base report to build on.
2. Issue only the tool calls that address what was missing.
3. Merge new findings with the previous report.

---

## Acceptance Criteria

- [ ] `get_tool_parameters` is called before every `execute_tool` call.
- [ ] Symbolic tools (`get_symbols_overview`, `find_symbol`) are used before `read_file` or `search_for_pattern`.
- [ ] `read_file` / `search_for_pattern` are only used when symbolic tools cannot retrieve the information.
- [ ] No value in the output is inferred or assumed — all values are tool-discovered.
- [ ] `find_referencing_symbols` is called for every symbol being modified or deleted.
- [ ] Exploration starts at the narrowest scope and widens only when necessary.
- [ ] `package.json` is checked whenever the task involves adding or removing a package.
- [ ] Each file entry contains only task-relevant lines, capped at 30 lines.
- [ ] Entries exceeding 30 lines use: first 15 + `// ... truncated ...` + last 15.
- [ ] If the full file is required: `"entire file required — N lines"` + first 30 lines only.
- [ ] All source text is exact — no paraphrasing.
- [ ] Only files, symbols, and packages directly touched by the task are collected.
- [ ] Execution history is read before any tool calls.
- [ ] Collection stops as soon as sufficient context exists.

**Retry Run**
- [ ] Only tool calls that address `insufficient_reason` are issued.
- [ ] New findings are merged into `previous_context_report` — not appended separately.


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
      {{
        "name": "...",
        "file": "...",
        "call_sites": ["..."]
      }}
    ],
    "packages": [
      {{
        "name": "...",
        "action": "install | remove"
      }}
    ],
    "constraints": ["..."]
  }}
}}
```
"""
# ------------------------------------
# EXECUTOR
# ------------------------------------

EXECUTOR_PROMPT = """\
## Role
You are the Executor. Based on the context from the context_gatherer agent, implement the user task by applying the necessary file and symbol edits, and installing or removing packages as specified in the context report.

---

## Rules
- Always prefer symbolic tools for code editing when possible.
- Only use `create_text_file` to overwrite a file when no symbolic editing tool is suitable for the operation.
- Always call `get_tool_parameters` before each `execute_tool` call.
- Never install packages after writing code that uses them — packages always come first.
- If context is missing → set `status: "needs_context"` immediately. Do not guess or partially proceed.

---

## Inputs
Process the provided inputs according to which path is present.

**Path 01: Fresh task**
Input format:
```
User task: <task>
Context report: <JSON>
```

**Path 02: Verification failure**
Input format:
```
User task: <task>
Previous Context report: <JSON>
Verification failure report: <JSON>
```

- Tool catalog
```json
{api_tools_catalog}
```

---

## Workflow
Depending on the state, proceed down one of the following paths:

### Fresh run — always follow this order
1. **Packages first.** Call `manage_npm_package` for every install/removal in the context report. Confirm exit code is `0` before continuing.
2. **Writes second.** Apply all file and symbol changes.
3. **Report last.** Populate `execution_report` only after all writes are complete.

### Retry run
1. Read verification report first. Use the `context_report` for file paths, symbol names, and package details needed to carry out the fix.
2. Only issue tool calls that directly address the reported failures.
3. Append every new action to `actions_attempted` in your output.
4. If the fix needs information not in the failure report → set `status: "needs_context"` immediately.

---

## Conditions

### Editing approach

| Approach | When |
|---|---|
| Symbol-based | Replacing an entire method, class, or function |
| File-based (line editing) | Changing a few lines within a larger symbol |

---

## Acceptance Criteria
- [ ] Symbolic editing tools are used whenever applicable; `create_text_file` is only used when no symbolic tool fits
- [ ] `get_tool_parameters` is called before every `execute_tool` call — no exceptions
- [ ] Packages are installed/removed before any code is written — never after
- [ ] Missing context triggers immediate `status: "needs_context"` — no partial execution or guessing
- [ ] All package operations run first and return exit code `0` before any file writes begin
- [ ] All file/symbol changes are applied only after packages succeed
- [ ] `execution_report` is populated only after all writes are complete
- [ ] verification report are read before any tool calls
- [ ] Only tool calls that directly address the reported failure are issued
---

## Output
Return a single JSON object. Nothing else.
```json
{{
  "status": "success | failure | needs_context",
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
- `status: "needs_context"` → populate `context_insufficient_reason`, stop immediately.
- `status: "failure"` → describe what failed and at which step in `execution_report.summary`.
- `context_insufficient_reason` is null unless `status: "needs_context"`.
"""

# ------------------------------------
# VERIFICATION
# ------------------------------------

VERIFICATION_PROMPT = """\
## Role
You are the Verifier. You're the third agent runs after the context gatherer and the excutor agents, Confirm the execution matches the user task intent by inspecting changed files, server logs, and lint results. Do not modify anything.

---

## Rules
- Always call `get_tool_parameters` before each `execute_tool` call, except when calling `get_server_logs` or `get_lint_checks`, which do not require a preceding `get_tool_parameters` call.
- Always prefer symbolic tools for code exploration when possible.
    - For example, instead of using `read_file` to retrieve an entire file for a specific symbol, first use `get_symbols_overview` to get a symbol map (adjust depth as needed), then use `find_symbol` to fetch just the symbol's body.
- Only use `search_for_pattern` or `read_file` if symbolic tools cannot retrieve the required information
- Never report a check with assumptions alwats confirming by calling the relevant tool and observing the output yourself.
- WARN lines alone do not cause `status: "failed"`. Only CRASH, ERROR, file inspection failures, or lint errors do.

---

## Inputs
- User task description (in natural language)
- Execution report (JSON)
- Context report (executor's context)

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
- Call `get_server_logs` (default `lines_count: 25`; increase if the execution report indicates more output).
- Classify every line using the Log Triage table in Conditions.
- Record only ERROR or CRASH lines in `server_log_errors`. If none → record `"no errors found"`.

### 3. Run lint
- Call `get_lint_checks`.
- Record only error-level violations (not warnings) in `lint_violations` as `"rule: message in file:line"`. If none → record `"no violations found"`.

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

---

## Acceptance Criteria

- [ ] `get_tool_parameters` is called before every `execute_tool` call — except `get_server_logs` and `get_lint_checks`, which never require it
- [ ] Symbolic tools are used for code exploration first; `search_for_pattern` or `read_file` are only used when symbolic tools cannot retrieve the needed information
- [ ] No check is reported based on assumptions — every check is confirmed by calling the relevant tool and observing its output
- [ ] WARN lines alone do not cause `status: "failed"` — only CRASH, ERROR, file inspection failures, or lint errors do
- [ ] Every changed file in `execution_report.files_changed` is inspected — none are skipped
- [ ] Each file inspection uses the most appropriate tool from the catalog
- [ ] `get_server_logs` is called with `lines_count` adjusted if the execution report indicates high output volume
- [ ] Every log line is classified using the triage table; only ERROR and CRASH lines are recorded in `server_log_errors`
- [ ] WARN lines are not recorded — their count is noted in `summary` only if > 5
- [ ] `get_lint_checks` is called and only error-level violations are recorded in `lint_violations` — warnings are excluded
- [ ] Steps are completed in order: file inspection → server logs → lint

---

## Output
Return a single JSON object. Nothing else.
```json
{{
  "status": "passed | failed",
  "summary": "...",
  "checks": [
    "✓ step done description"
  ],
  "issues": [
    "✗ step done description"
  ],
  "server_log_errors": ["...or 'no errors found'"],
  "lint_violations": ["...or 'no violations found'"],
  "failure_analysis": {{
    "root_cause": "...",
    "requires_context_regathering": false,
  }}
}}
```

Field rules:
- `failure_analysis` and `issues`: present only when `status: "failed"`. Omit entirely otherwise.
- `checks`: always present.
"""

# ---------------------------------------------------------------------------
# HELPERS
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

        return {
            "execute_tool": execute_tool,
            "manage_npm_package": manage_npm_package,
            "get_server_logs": get_server_logs,
            "get_lint_checks": get_lint_checks,
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
# STRUCTURED OUTPUT SCHEMA
# ---------------------------------------------------------------------------


class Status(str, Enum):
    passed = "passed"
    failed = "failed"


class Action(str, Enum):
    update = "UPDATE"
    create = "CREATE"
    delete = "DELETE"
    install_package = "INSTALL_PACKAGE"
    remove_package = "REMOVE_PACKAGE"


class FailureAnalysis(BaseModel):
    root_cause: str
    requires_context_regathering: bool = False


class VerificationReport(BaseModel):
    status: Status
    summary: str
    checks: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    server_log_errors: list[str] = Field(default_factory=list)
    lint_violations: list[str] = Field(default_factory=list)
    failure_analysis: Optional[FailureAnalysis] = None


# ---------------------------------------------------------------------------
# GRAPH STATE
# ---------------------------------------------------------------------------


class AgentState(MessagesState):
    sandbox_id: str
    model_id: Optional[str]
    model_provider: Optional[str]
    retry_count: int = 0
    verification_report: Optional[str] = None
    context_report: Optional[str] = None
    user_message: Optional[HumanMessage] = None
    executor_messages: List[str] = []


# ---------------------------------------------------------------------------
# SHARED SUBAGENT RUNNER
# ---------------------------------------------------------------------------


async def _run_subagent(
    *,
    state: AgentState,
    agent_name: str,
    system_prompt: str,
    messages: list[BaseMessage] = [],
    structured_output: Optional[BaseModel] = None,
    tools: list,
) -> dict:
    model = build_model(
        model_id=state.get("model_id") or DEFAULT_MODEL_ID,
        provider=state.get("model_provider") or DEFAULT_MODEL_PROVIDER,
    )

    if structured_output:
        agent = create_agent(
            model,
            system_prompt=system_prompt,
            tools=tools,
            response_format=structured_output,
            name=agent_name,
        )
    else:
        agent = create_agent(
            model,
            system_prompt=system_prompt,
            tools=tools,
            name=agent_name,
        )

    result = await agent.ainvoke({"messages": messages})

    return result


# ---------------------------------------------------------------------------
# SUBAGENT NODES
# ---------------------------------------------------------------------------


async def context_gatherer_node(state: AgentState, config: RunnableConfig) -> dict:
    read_definitions = BuildSandboxToolsDefinitions(allowed_tools=READ_ONLY_TOOLS)
    read_get_params = read_definitions.as_langchain_tools()["get_tool_parameters"]
    sandbox_builder = BuildSandboxTools(
        state["sandbox_id"], tools_definitions=read_definitions
    )
    lc_tools = sandbox_builder.as_langchain_tools()

    system_prompt = PromptTemplate.from_template(CONTEXT_GATHERER_PROMPT).format(
        api_tools_catalog=read_definitions.get_sandbox_tools_without_params()
    )
    messages = state.get("messages")
    verification_report = state.get("verification_report")
    context_report = state.get("context_report")
    executor_messages = state.get("executor_messages")

    user_message = next(
        (
            message
            for message in reversed(messages)
            if isinstance(message, HumanMessage)
        ),
        None,
    )
    if user_message is None:
        raise Exception("User task is required!")

    if verification_report is not None:
        messages_input = [
            HumanMessage(
                f"User Task: {user_message.content}\n"
                f"Verification Report: {verification_report}\n"
                f"Previous Context Report: {context_report}"
            )
        ]
    else:
        if executor_messages is None:
            messages_input = [HumanMessage(f"User Task: {user_message.content}")]
        else:
            messages_input = [
                HumanMessage(
                    f"User Task: {user_message.content}\n\n\n---\n\n\nExecutions History: {'\n---\n'.join(executor_messages)}"
                )
            ]

    result = await _run_subagent(
        system_prompt=system_prompt,
        tools=[lc_tools["execute_tool"], read_get_params],
        state=state,
        agent_name="context_gatherer",
        messages=messages_input,
    )
    context_report = result["messages"][-1].content
    return {
        "messages": result["messages"],
        "user_message": user_message,
        "context_report": context_report,
    }


async def executor_node(state: AgentState, config: RunnableConfig) -> dict:
    write_definitions = BuildSandboxToolsDefinitions(allowed_tools=WRITE_TOOLS)
    write_get_params = write_definitions.as_langchain_tools()["get_tool_parameters"]
    sandbox_builder = BuildSandboxTools(
        state["sandbox_id"], tools_definitions=write_definitions
    )
    lc_tools = sandbox_builder.as_langchain_tools()

    system_prompt = PromptTemplate.from_template(EXECUTOR_PROMPT).format(
        api_tools_catalog=write_definitions.get_sandbox_tools_without_params()
    )
    context_report = state.get("context_report")
    verification_report = state.get("verification_report")
    user_message = state.get("user_message")
    if context_report is None or user_message is None:
        raise Exception("Context report and user task are required!")

    if verification_report is not None:
        messages_input = [
            HumanMessage(
                f"User Task: {user_message.content}\n"
                f"Verification Report: {verification_report}\n"
                f"Context Report: {context_report}"
            )
        ]
    else:
        messages_input = [
            HumanMessage(
                f"User Task: {user_message.content}\n---\nContext Report: {context_report}"
            )
        ]

    result = await _run_subagent(
        system_prompt=system_prompt,
        tools=[
            lc_tools["execute_tool"],
            write_get_params,
            lc_tools["manage_npm_package"],
        ],
        state=state,
        messages=messages_input,
        agent_name="executor",
    )

    executor_messages = state.get("executor_messages") or []  # FIX: guard against None
    return {
        "messages": result["messages"],
        "executor_messages": [*executor_messages, result["messages"][-1].content],
    }


async def verification_node(state: AgentState, config: RunnableConfig) -> dict:
    read_definitions = BuildSandboxToolsDefinitions(allowed_tools=READ_ONLY_TOOLS)
    read_get_params = read_definitions.as_langchain_tools()["get_tool_parameters"]
    sandbox_builder = BuildSandboxTools(
        state["sandbox_id"], tools_definitions=read_definitions
    )
    lc_tools = sandbox_builder.as_langchain_tools()

    system_prompt = PromptTemplate.from_template(VERIFICATION_PROMPT).format(
        api_tools_catalog=read_definitions.get_sandbox_tools_without_params()
    )

    user_message = state.get("user_message")
    context_report = state.get("context_report")
    retry_count = state.get("retry_count")
    messages = state.get("messages", [])
    execution_report = next(
        (m for m in reversed(messages) if isinstance(m, AIMessage)),
        None,
    )
    messages_input = [
        HumanMessage(
            f"User Task: {user_message.content}\n"
            f"Context Report: {context_report}\n"
            f"Execution Report: {execution_report.content if execution_report else ''}"
        )
    ]

    result = await _run_subagent(
        system_prompt=system_prompt,
        tools=[
            lc_tools["execute_tool"],
            read_get_params,
            lc_tools["get_server_logs"],
            lc_tools["get_lint_checks"],
        ],
        state=state,
        agent_name="verification",
        structured_output=VerificationReport,
        messages=messages_input,
    )

    structured_response = result["structured_response"]
    verification_report = None
    next_node = END
    MAX_RETRIES = 3
    if structured_response.status == Status.failed and retry_count < MAX_RETRIES:
        retry_count = retry_count + 1
        verification_report = structured_response.model_dump_json()
        next_node = "executor"
        if (
            structured_response.failure_analysis
            and structured_response.failure_analysis.requires_context_regathering
        ):
            next_node = "context_gatherer"
    else:
        retry_count = 0

    return Command(
        update={
            "messages": result["messages"],
            "verification_report": verification_report,
            "retry_count": retry_count,
        },
        goto=next_node,
    )


# ---------------------------------------------------------------------------
# GRAPH ASSEMBLY
# ---------------------------------------------------------------------------

coding_workflow = StateGraph(AgentState)

coding_workflow.add_node("context_gatherer", context_gatherer_node)
coding_workflow.add_node("executor", executor_node)
coding_workflow.add_node("verification", verification_node)

# FIX: edge names must match the registered node names above (no "_node" suffix)
coding_workflow.add_edge(START, "context_gatherer")
coding_workflow.add_edge("context_gatherer", "executor")
coding_workflow.add_edge("executor", "verification")
coding_graph = coding_workflow.compile(checkpointer=InMemorySaver())

# if __name__ == "__main__":
#     import asyncio

#     read_definitions = BuildSandboxToolsDefinitions(allowed_tools=READ_ONLY_TOOLS)
#     result = read_definitions.get_sandbox_tool_parameters("find_symbol")
#     print("result", result)
