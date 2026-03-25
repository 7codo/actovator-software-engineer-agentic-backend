from langchain.agents import create_agent
import json
from typing import Optional, List, Literal

from e2b import AsyncSandbox
from langchain.tools import tool
from pydantic import BaseModel, Field

from app.ai.llm.models import build_model
from app.constants import PROJECT_PATH
from app.core.config import settings
from langgraph.graph.message import MessagesState
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, BaseMessage
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver

from app.constants import DEFAULT_MODEL_ID, DEFAULT_MODEL_PROVIDER


# ---------------------------------------------------------------------------
# PROMPTS
# ---------------------------------------------------------------------------

ORCHESTRATOR_PROMPT = """\
## Role
You are the Orchestrator. Coordinate three subagents — `context_gatherer`, `executor`, and `verification` — to complete coding tasks end-to-end.

---

## Inputs
- User task (natural language)
- Subagent JSON responses

---

## Rules
- Never expose raw JSON to the user. Always reply in plain language.
- Pass full, untruncated JSON reports between subagents. Never mutate them.
- Skip a step only when explicitly permitted below.
- If a subagent returns malformed JSON, retry once with an instruction to return valid JSON only.

---

## Workflow

### Step 1 — Gather Context
1. Call `context_gatherer` with the user task.
2. If the task is purely informational (no code changes or package installs required):
   - Answer the user directly if the context is sufficient.
   - Otherwise, regather context and answer once sufficient context is available. Stop.

### Step 2 — Execute
> **Skip** if the task is purely informational.

1. Call `executor` with the user task + context report.
2. Route on response (first match wins):

| Condition | Action |
|---|---|
| `status: "needs_context"` | Return to Step 1 with the executor report includes the insufficient reason and the previous `context_report` appended |
| `status: "failure"` | Stop and inform the user to contact support. |
| `status: "success"` | Go to step 3 with the executor report |

### Step 3 — Verify
1. Call `verification` with the user task + execution report.
2. Route on response:

| Condition | Action |
|---|---|
| `status: "passed"` | Summarise work done. Stop. |
| `status: "failed"` + `requires_context_regathering: true` | Return to Step 1 with the verification report includes the insufficient reason and the previous `context_report` appended |
| `status: "failed"` + `requires_context_regathering: false` | Return to Step 2 with the verification report includes the failure analysis and the original `context_report` appended. |

---

## Acceptance Criteria
A run is only considered successful when **all** of the following are true:

- [ ] No raw JSON was exposed to the user at any point.
- [ ] All JSON reports passed between subagents are full and untruncated, exactly as received.
- [ ] No step was skipped unless explicitly permitted by the workflow.
- [ ] Any malformed JSON was retried exactly once before escalating.
- [ ] Every routing decision matched the first applicable condition in the routing table.
- [ ] Context was regathered whenever a step returned insufficient reason.
- [ ] `verification` reached `status: "passed"` before the task was considered complete.

---

## Output
Return a structured decision with the following fields:

- `next_action`: one of `"context_gatherer"`, `"executor"`, `"verification"`, or `"end"`.
- `subagent_message`: the full message to send to the next subagent — include the user task and any relevant JSON reports exactly as received. Required unless `next_action` is `"end"`.
- `final_response`: a plain-language summary for the user. Required when `next_action` is `"end"`. Cover:
  - What was done (or what failed).
  - Any files changed or packages installed.
  - Next steps if the task did not complete.
"""

# ------------------------------------
# CONTEXT GATHERER
# ------------------------------------

CONTEXT_GATHERER_PROMPT = """\
## Role
You are the **Context Gatherer**. Explore the codebase using read-only tools and return a structured context report that will be passed to the Executor agent.

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
- **User task** — natural language description of what needs to be done.
- **`insufficient_reason`** *(optional)* — raised by the Executor when the previous context report was incomplete.
- **`previous_context_report`** (optional) — the full context report from the previous run; present whenever `insufficient_reason` is set.
- **Tool catalog**
```json
{api_tools_catalog}
```

---

## Workflow

### Fresh Run
1. Read the task. Identify only the files, symbols, and packages directly touched.
2. For each tool call: invoke `get_tool_parameters` first, then `execute_tool`.
3. Stop as soon as you have sufficient context — do not over-collect.
4. Build and return the context report.

### Retry Run
1. Read `insufficient_reason` carefully. Treat `previous_context_report` as the base report to build on.
2. Issue only the tool calls that address what was missing.
3. Merge new findings with the previous report.

---

## Acceptance Criteria
A run is only considered successful when **all** of the following are true:

- [ ] Every `execute_tool` call was preceded by a `get_tool_parameters` call for the same tool.
- [ ] No values were guessed — all symbol names, paths, and parameters were discovered via tools.
- [ ] `search_for_pattern` or `read_file` was only used when symbolic tools were insufficient.
- [ ] `find_referencing_symbols` was called for every symbol that will be modified or removed.
- [ ] `package.json` was checked if the task involves adding or removing a dependency.
- [ ] No file entry exceeds 30 lines — truncation applied with `// ... truncated ...` where needed.
- [ ] All source content is exact — no paraphrasing or summarization of code.
- [ ] **Fresh run:** scope was identified before any tool calls; collection stopped as soon as sufficient context was reached.
- [ ] **Retry run:** only the gaps cited in `insufficient_reason` were targeted; new findings were merged into the previous report rather than replacing it.
- [ ] The final output is a single, valid JSON object matching the required schema — no prose, no extra keys.

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
You are the Executor. Implement the user task by writing files and symbols and install or remove packages based on the context report.

---

## Rules
- Always prefer symbolic tools for code editing when possible.
- Only use `create_text_file` to overwrite a file when no symbolic editing tool is suitable for the operation.
- Always call `get_tool_parameters` before each `execute_tool` call.
- Never install packages after writing code that uses them — packages always come first.
- Never repeat an action listed in `actions_already_attempted`.
- If context is missing → set `status: "needs_context"` immediately. Do not guess or partially proceed.

---

## Inputs
You will receive one of the following:

Fresh execution:
```
User task: <task>
Context report: <JSON>
```

---

Retry after verification failure:
```
User task: <task>
Context report: <JSON>
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
Depending on the state, proceed down one of the following paths:

### Fresh run — always follow this order
1. **Packages first.** Call `manage_npm_package` for every install/removal in the context report. Confirm exit code is `0` before continuing.
2. **Writes second.** Apply all file and symbol changes.
3. **Report last.** Populate `execution_report` only after all writes are complete.

### Retry run
1. Read `failure_analysis.root_cause` and `failure_analysis.suggested_fix` first. Use the `context_report` for file paths, symbol names, and package details needed to carry out the fix.
2. Check `actions_already_attempted` — do not repeat any listed action.
3. Only issue tool calls that directly address the reported failures.
4. Append every new action to `actions_attempted` in your output.
5. If the fix needs information not in the failure report → set `status: "needs_context"` immediately.

---

## Conditions

### Editing approach

| Approach | When |
|---|---|
| Symbol-based | Replacing an entire method, class, or function |
| File-based (line editing) | Changing a few lines within a larger symbol |

---

## Acceptance Criteria
A run is only considered successful when **all** of the following are true:

- [ ] No symbolic tool was bypassed in favor of `create_text_file` unless no symbolic tool was applicable.
- [ ] `get_tool_parameters` was called before every `execute_tool` invocation without exception.
- [ ] No action listed in `actions_already_attempted` was repeated.
- [ ] `status` was set to `"needs_context"` immediately upon detecting missing context — no partial writes were made.
- [ ] `context_insufficient_reason` is non-null if and only if `status` is `"needs_context"`.
- [ ] **Fresh run:** All package operations completed with exit code `0` before any file or symbol write was issued.
- [ ] **Fresh run:** `execution_report` was populated only after every write operation completed.
- [ ] **Retry run:** `failure_analysis.root_cause` and `failure_analysis.suggested_fix` were read before issuing any tool call.
- [ ] **Retry run:** Only tool calls that directly address the reported failure were issued.
- [ ] **Retry run:** Every new action taken is recorded in `actions_attempted` in the output.

---

## Output
Return a single JSON object. Nothing else.
```json
{{
  "agent": "executor",
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
You are the Verifier. Confirm the execution matches the user task intent by inspecting changed files, server logs, and lint results. Do not modify anything.

---

## Rules
- Always call `get_tool_parameters` before each `execute_tool` call, except when calling `get_server_logs` or `get_lint_checks`, which do not require a preceding `get_tool_parameters` call.
- Always prefer symbolic tools for code exploration when possible.
    - For example, instead of using `read_file` to retrieve an entire file for a specific symbol, first use `get_symbols_overview` to get a symbol map (adjust depth as needed), then use `find_symbol` to fetch just the symbol's body.
- Only use `search_for_pattern` or `read_file` if symbolic tools cannot retrieve the required information
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
- Call `get_server_logs` (default `lines_count: 25`; increase if the execution report indicates more output).
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

A run is only considered successful when **all** of th

### Rules Compliance
- [ ] Every `execute_tool` call was preceded by a `get_tool_parameters` call, **except** `get_server_logs` and `get_lint_checks`
- [ ] Symbolic tools (`get_symbols_overview`, `find_symbol`) were used for code exploration wherever applicable — `read_file` or `search_for_pattern` were only used as fallbacks
- [ ] Every `checks` and `issues` entry cites a concrete tool result in the required format (`✓/✗ [tool_name → param_summary]`)
- [ ] No check or issue was reported without a corresponding tool call and observed output
- [ ] **Step 1 – File Inspection**: Every file listed in `execution_report.files_changed` was individually inspected using an appropriate tool, and each result was classified as `✓` or `✗`
- [ ] **Step 2 – Server Logs**: `get_server_logs` was called; every line was triaged against the Log Triage table; only ERROR or CRASH lines appear in `server_log_errors`
- [ ] **Step 3 – Lint**: `get_lint_checks` was called; only error-level violations (not warnings) appear in `lint_violations`
- [ ] **Step 4 – Symbol Verification**: Every entry in `execution_report.symbols_modified` was confirmed to exist with its expected signature, checked for lint errors, and verified to have at least one call site (unless newly created)
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


class OrchestratorDecision(BaseModel):
    next_action: Literal["context_gatherer", "executor", "verification", "end"] = Field(
        description=(
            "The next step in the workflow. Use 'end' only after verification passes "
            "or to deliver a final informational answer."
        )
    )
    subagent_message: Optional[str] = Field(
        default=None,
        description=(
            "Full message to pass to the next subagent. Must include the user task "
            "and any relevant JSON reports exactly as received. Required unless "
            "next_action is 'end'."
        ),
    )
    final_response: Optional[str] = Field(
        default=None,
        description=(
            "Plain-language summary for the user. Required when next_action is 'end'. "
            "Cover what was done, files changed, packages installed, and next steps "
            "if the task did not complete."
        ),
    )


# ---------------------------------------------------------------------------
# GRAPH STATE
# ---------------------------------------------------------------------------


class AgentState(MessagesState):
    sandbox_id: str
    model_id: Optional[str]
    model_provider: Optional[str]
    # Set by the orchestrator, consumed by subagent nodes
    next_action: Optional[str]
    subagent_message: Optional[str]


# ---------------------------------------------------------------------------
# ORCHESTRATOR NODE
# ---------------------------------------------------------------------------


async def orchestrator_node(state: AgentState, config: RunnableConfig) -> dict:
    """
    Reads the full message history (user task + any subagent reports already
    appended), calls the model with structured output, and writes the routing
    decision back into the state.
    """
    model = build_model(
        model_id=state.get("model_id") or DEFAULT_MODEL_ID,
        provider=state.get("model_provider") or DEFAULT_MODEL_PROVIDER,
    )
    structured_model = model.with_structured_output(OrchestratorDecision)

    messages = [SystemMessage(content=ORCHESTRATOR_PROMPT)] + list(
        state["orchestration_messages"]
    )
    decision: OrchestratorDecision = await structured_model.ainvoke(messages)

    updates: dict = {
        "next_action": decision.next_action,
        "subagent_message": decision.subagent_message,
    }

    # When the workflow ends, surface the final response as an AI message so
    # the caller sees it in the messages list.
    if decision.next_action == "end" and decision.final_response:
        updates["messages"] = [
            *state.get("messages"),
            AIMessage(content=decision.final_response),
        ]
        updates["orchestration_messages"] = [AIMessage(content=decision.final_response)]

    return updates


def route_orchestrator(state: AgentState) -> str:
    """Edge function: routes to the chosen subagent node or END."""
    return state.get("next_action") or "end"


# ---------------------------------------------------------------------------
# SHARED SUBAGENT RUNNER
# ---------------------------------------------------------------------------


async def _run_subagent(
    *,
    system_prompt: str,
    tools: list,
    state: AgentState,
    agent_name: str,
    messages: List[BaseMessage],
) -> dict:
    """
    Creates a stateless subagent, invokes it with the orchestrator-composed
    message, and returns the last message appended to the state under the
    subagent's name so the orchestrator can read the full history on its
    next turn.
    """
    model = build_model(
        model_id=state.get("model_id") or DEFAULT_MODEL_ID,
        provider=state.get("model_provider") or DEFAULT_MODEL_PROVIDER,
    )
    agent = create_agent(model, system_prompt=system_prompt, tools=tools)

    subagent_message = state.get("subagent_message") or ""
    result = await agent.ainvoke({"messages": [HumanMessage(content=subagent_message)]})

    last_content = result["messages"][-1].content
    # Append as an AIMessage named after the subagent so the orchestrator can
    # identify which agent produced which report in the shared message history.
    return {
        "orchestration_messages": [AIMessage(content=last_content, name=agent_name)],
        "messages": [*messages, AIMessage(content=last_content, name=agent_name)],
    }


# ---------------------------------------------------------------------------
# SUBAGENT NODES
# ---------------------------------------------------------------------------


async def context_gatherer_node(state: AgentState, config: RunnableConfig) -> dict:
    sandbox_builder = BuildSandboxTools(state["sandbox_id"])
    lc_tools = sandbox_builder.as_langchain_tools()

    read_definitions = BuildSandboxToolsDefinitions(allowed_tools=READ_ONLY_TOOLS)
    read_get_params = read_definitions.as_langchain_tools()["get_tool_parameters"]

    system_prompt = PromptTemplate.from_template(CONTEXT_GATHERER_PROMPT).format(
        api_tools_catalog=read_definitions.get_sandbox_tools_without_params()
    )

    return await _run_subagent(
        system_prompt=system_prompt,
        tools=[lc_tools["execute_tool"], read_get_params],
        state=state,
        agent_name="context_gatherer",
        messages=state.get("messages"),
    )


async def executor_node(state: AgentState, config: RunnableConfig) -> dict:
    sandbox_builder = BuildSandboxTools(state["sandbox_id"])
    lc_tools = sandbox_builder.as_langchain_tools()

    write_definitions = BuildSandboxToolsDefinitions(allowed_tools=WRITE_TOOLS)
    write_get_params = write_definitions.as_langchain_tools()["get_tool_parameters"]

    system_prompt = PromptTemplate.from_template(EXECUTOR_PROMPT).format(
        api_tools_catalog=write_definitions.get_sandbox_tools_without_params()
    )

    return await _run_subagent(
        system_prompt=system_prompt,
        tools=[
            lc_tools["execute_tool"],
            write_get_params,
            lc_tools["manage_npm_package"],
        ],
        state=state,
        agent_name="executor",
        messages=state.get("messages"),
    )


async def verification_node(state: AgentState, config: RunnableConfig) -> dict:
    sandbox_builder = BuildSandboxTools(state["sandbox_id"])
    lc_tools = sandbox_builder.as_langchain_tools()

    read_definitions = BuildSandboxToolsDefinitions(allowed_tools=READ_ONLY_TOOLS)
    read_get_params = read_definitions.as_langchain_tools()["get_tool_parameters"]

    system_prompt = PromptTemplate.from_template(VERIFICATION_PROMPT).format(
        api_tools_catalog=read_definitions.get_sandbox_tools_without_params()
    )

    return await _run_subagent(
        system_prompt=system_prompt,
        tools=[
            lc_tools["execute_tool"],
            read_get_params,
            lc_tools["get_server_logs"],
            lc_tools["get_lint_checks"],
        ],
        state=state,
        agent_name="verification",
        messages=state.get("messages"),
    )


# ---------------------------------------------------------------------------
# GRAPH ASSEMBLY
# ---------------------------------------------------------------------------

coding_workflow = StateGraph(AgentState)

coding_workflow.add_node("orchestrator", orchestrator_node)
coding_workflow.add_node("context_gatherer", context_gatherer_node)
coding_workflow.add_node("executor", executor_node)
coding_workflow.add_node("verification", verification_node)

coding_workflow.add_edge(START, "orchestrator")

coding_workflow.add_conditional_edges(
    "orchestrator",
    route_orchestrator,
    {
        "context_gatherer": "context_gatherer",
        "executor": "executor",
        "verification": "verification",
        "end": END,
    },
)

# Every subagent returns control to the orchestrator after completing.
coding_workflow.add_edge("context_gatherer", "orchestrator")
coding_workflow.add_edge("executor", "orchestrator")
coding_workflow.add_edge("verification", "orchestrator")

coding_graph = coding_workflow.compile(checkpointer=InMemorySaver())


# ---------------------------------------------------------------------------
# ENTRY POINT (unchanged utility)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    write_definitions = BuildSandboxToolsDefinitions(allowed_tools=WRITE_TOOLS)
    result = write_definitions.get_sandbox_tools_without_params()
    with open("output/updated_json_write_tools.json", "w") as f:
        f.write(result)
