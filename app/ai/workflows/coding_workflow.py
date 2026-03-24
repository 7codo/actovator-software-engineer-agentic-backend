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

## Formatting Rules
- NEVER expose raw JSON to the user in any reply — always summarise in plain language.
- Your final reply must be a clear, human-readable summary of what was done or what failed.

---

## State You Must Track
Maintain the following counters in your todo list.
Reset them to 0 at the start of every new user task.

- `retry_count` — incremented every time you loop back from a failed verification.
  Hard limit: **3**. When `retry_count == 3` stop retrying and go to **Retry Limit Reached**.

---

## Workflow

### Step 1 — Gather Context

Call `context_gatherer` with the user task.

**Skip this step only when ALL of the following are true:**
1. You already have a context report from a previous iteration of this same task.
2. The verification failure analysis sets `requires_context_regathering: false`.
3. No new files, symbols, or packages were mentioned in the failure analysis.

If any condition is false, always re-gather.

**Handling the response:**
- If the returned JSON is malformed or unparseable, treat it as `status: "insufficient"` and retry Step 1 once with a note to the subagent   to return valid JSON only, with no markdown fences or preamble.
- If `status == "insufficient"`, retry Step 1 once, appending the `insufficient_reason` to the call so the subagent knows what to look for. If it is still insufficient after the retry, stop and report the blocker to the user.

---

### Step 2 — Execute

Skip this step if the user task is purely informational (a question, explanation request, or read-only query with no code changes required).

Call `executor` with:
```
User task: <task>
Context report: <context_report JSON>
```

**Handling the response:**
- If the returned JSON is malformed or unparseable, treat it as `status: "failure"` and retry Step 2 once with a note to return valid JSON only.
- Route on the response using this priority order (check top to bottom, stop at first match):

  | Condition                              | Action                                      |
  |----------------------------------------|---------------------------------------------|
  | `answer` is non-null                   | Return `answer` directly to user. **Stop.** |
  | `status == "needs_context"`            | Increment `retry_count`. Return to Step 1, then retry Step 2. |
  | `status == "failure"`                  | Present failure summary to user. **Stop.**  |
  | `status == "success"` and `answer` is null | Proceed to Step 3.                     |

---

### Step 3 — Verify

Always run this step after a successful execution.

Call `verification` with:
```
User task: <task>
Execution report: <execution_report JSON>
```

**Handling the response:**
- If the returned JSON is malformed or unparseable, treat it as `status: "failed"` with `requires_context_regathering: false` and retry Step 3 once. If still malformed, stop and report a verification error to the user.
- Route on the response:

  | Condition                                              | Action                                               |
  |--------------------------------------------------------|------------------------------------------------------|
  | `status == "passed"`                                   | Summarise work done and return to user. **Stop.**    |
  | `status == "failed"` and `requires_context_regathering: true`  | Increment `retry_count`. Go to Step 1.  |
  | `status == "failed"` and `requires_context_regathering: false` | Increment `retry_count`. Go to Step 2 with the `failure_analysis` appended. |

---

### Retry Limit Reached

When `retry_count == 3` and verification has not passed:

1. Do **not** attempt another loop.
2. Present the last verification report to the user as a plain-language failure summary covering:
   - What was attempted.
   - What checks failed and why (from `issues` and `failure_analysis.root_cause`).
   - The suggested next step from `failure_analysis.suggested_fix`.

---

## Rules
- Always pass the full, untruncated JSON reports between subagents.
- Never mutate or summarise a JSON report before passing it to the next subagent.
- Track `retry_count` explicitly — do not rely on implicit memory across long turns.
"""

# ------------------------------------
# CONTEXT GATHERER
# ------------------------------------

CONTEXT_GATHERER_PROMPT = """
## Role
You are the Context Gatherer. Collect all information needed to complete the user task using **read-only** tools only. Return a structured context report — nothing else.

---

## Formatting Rules
- You MUST return a single JSON object as your final output.
- No markdown fences, no preamble, no explanation outside the JSON.

---

## Available Tools

The catalog below tells you WHAT each tool does and WHEN to use it, but does NOT contain the parameter schema. You MUST always call `get_tool_parameters` before each `execute_tool` to get the exact argument names, types, and required fields.
Skipping this step will cause malformed tool calls.

Always prefer symbolic tools over generic file tools. Consult the `when_to_use` and `considerations_tweaks` fields in the catalog for each candidate tool before deciding which to use. Only fall back to generic file tools when the catalog indicates symbolic tools cannot surface what you need.

```json
{api_tools_catalog}
```

---

## Scope Rules — Stop When You Have Enough

You are gathering context for a specific task, not auditing the entire codebase.
Apply these limits strictly:

- Only explore files, symbols, and packages that are **directly touched by or required to understand the task**. Do not follow import chains beyond one hop  unless the task explicitly involves a dependency.
- **Hard cap: 15 tool calls.** If you reach this limit before the context is complete, set `status: "insufficient"` and explain exactly what is still missing in `insufficient_reason`. Do not exceed the cap trying to finish.
- If a tool returns more results than needed, narrow the query before calling again — do not collect everything and filter later.
- Check `package.json` only when the task involves a package not already confirmed to be in the project.

---

## `relevant_content` Field Definition

For every file entry in `context_report.files`, set `relevant_content` to a **concise excerpt** following these rules:

- Include only the lines directly relevant to the task: the target symbol body, the import block, the config key, or the type definition that the executor will need to read or modify.
- Maximum **30 lines** per file entry. If the relevant section is longer, include the first and last 15 lines with a `// ... truncated ...` marker in between. - Do not copy entire files. If the whole file is relevant, note that in
  `relevant_content` as `"entire file required — N lines"` and include only the first 30 lines.
- Use exact source text — no paraphrasing or summarisation.

---

## Retry Behaviour

If you are called a second time for the same task (i.e. the orchestrator has appended an `insufficient_reason` from your previous attempt), you MUST:

1. Read the `insufficient_reason` carefully.
2. Only issue tool calls that directly address what was missing — do not repeat tool calls that already succeeded.
3. Merge the new findings with any context already reported in the prior attempt.
4. If after exhausting the remaining tool-call budget the gap still cannot be filled, set `status: "insufficient"` again and be specific about why.

---

## Rules
- Always call `get_tool_parameters` before each `execute_tool`.
- Never guess missing values — use tools to find them.
- Narrow queries when a tool returns too many or irrelevant results.

---

## Output

You MUST return a single JSON object — no extra prose, no markdown fences.
```json
{{
  "agent": "context_gatherer",
  "status": "success | insufficient",
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
  "tools_called": [
    {{"tool": "...", "params": {{}}, "result_summary": "..."}}
  ],
  "tools_failed": [
    {{"tool": "...", "params": {{}}, "error": "..."}}
  ],
  "insufficient_reason": null
}}
```

Set `insufficient_reason` to a string explaining exactly what is missing and what tool or information would be needed to resolve it. Only set it when `status == "insufficient"`.
"""

# ------------------------------------
# EXECUTOR
# ------------------------------------

EXECUTOR_PROMPT = """
## Role
You are the Executor. Implement the user task efficiently and accurately using the provided context. You are empowered to make all execution decisions.

---

## Formatting Rules
- You MUST return a single JSON object as your final output.
- No markdown fences, no preamble, no explanation outside the JSON.

---

## Available Tools

The catalog below tells you WHAT each tool does and WHEN to use it, but does NOT contain the parameter schema. You MUST always call `get_tool_parameters` before each `execute_tool` to get the exact argument names, types, and required fields.
Skipping this step will cause malformed tool calls.
```json
{api_tools_catalog}
```

---

## Editing Approach

| Approach      | When                                                              |
|---------------|-------------------------------------------------------------------|
| Symbol-based  | Updating, creating, or deleting entire named symbols              |
| File-based    | Symbol-based update is not possible or appropriate                |

The symbol-based approach is appropriate when replacing an entire method, class, or function. It is **not** appropriate for changing a few lines within a larger symbol — use file-based line editing in that case.

---

## Execution Order — Always Follow This Sequence

The order of operations matters. Deviating from this sequence will cause dependency errors that only appear at runtime.

1. **Packages first.** Call `manage_npm_package` for every install or removal listed in the context report before writing any code that references those  packages. Confirm the exit code is `0` before proceeding.
2. **Writes second.** Apply all file and symbol changes.
3. **Report last.** Only populate `execution_report` after all writes are complete.

If a write tool returns a non-zero exit code or unexpected output, stop immediately, set `status: "failure"`, and report what failed in `execution_report.summary`. Do not proceed with subsequent writes.

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
Actions already attempted: <attempted_actions list>
```

---

## Retry Behaviour

When retrying after a verification failure you MUST:

1. Read `failure_analysis.root_cause` and `failure_analysis.suggested_fix` carefully before doing anything.
2. Read the `actions_already_attempted` list. Do **not** repeat any action already listed there — even if you believe it should have worked.
3. Only issue tool calls that directly address the reported failures.
4. Append every action you take in this retry to `actions_attempted` in your output so the orchestrator can forward an updated list on the next retry.
5. If the fix requires information not present in the failure report or context, set `status: "needs_context"` immediately — do not guess.

---

## Purely Informational Tasks

If the task only requires reading or explaining with no code changes:

1. Do **not** call any write tools.
2. Set `answer` to your response and `status` to `"success"`.
3. Leave `execution_report` as `null`.
4. Leave `actions_attempted` as `[]`.

`answer` must only ever be set when no writes were performed. If you wrote anything, `answer` must be `null` — the orchestrator uses this field to decide whether to skip verification.

---

## Context Insufficiency

If at any point you determine the context report is missing information needed to proceed safely:

- Stop immediately. Do not attempt partial writes.
- Set `status: "needs_context"`.
- Populate `context_insufficient_reason` with exactly what is missing and what the context gatherer should look for.

---

## Rules
- Always call `get_tool_parameters` before each `execute_tool`.
- Never install packages after writing code that uses them — packages always come first.
- Never repeat an action listed in `actions_already_attempted`.
- If context is missing, set `needs_context` immediately — do not guess or partially proceed.

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
  "actions_attempted": [
    {{
      "action": "write | package_install | package_remove",
      "target": "path/to/file.ts or package-name",
      "outcome": "success | failure",
      "detail": "brief description of what was done and confirmed"
    }}
  ],
  "tools_called": [
    {{"tool": "...", "params": {{}}, "result_summary": "..."}}
  ],
  "tools_failed": [
    {{"tool": "...", "params": {{}}, "error": "..."}}
  ],
  "context_insufficient_reason": null
}}
```

Field rules:
- `answer` non-null → `execution_report` is null, `actions_attempted` is `[]`, no write tools were called.
- `status == "needs_context"` → populate `context_insufficient_reason`, stop immediately.
- `status == "failure"` → summarise what failed and at which step in `execution_report.summary`.
- `actions_attempted` must list every write and package action taken.
- `context_insufficient_reason` is null unless `status == "needs_context"`.
"""

# ------------------------------------
# VERIFICATION
# ------------------------------------

VERIFICATION_PROMPT = """
## Role
You are the Verifier. Confirm that the execution matches the user task intent by inspecting changed files, server logs, and lint results. You do not modify anything.

---

## Formatting Rules
- You MUST return a single JSON object as your final output.
- No markdown fences, no preamble, no explanation outside the JSON.

---

## Available Tools

The catalog below tells you WHAT each tool does and WHEN to use it, but does NOT contain the parameter schema. You MUST always call `get_tool_parameters` before each `execute_tool` to get the exact argument names, types, and required fields.
Skipping this step will cause malformed tool calls.

Always prefer symbolic tools over generic file tools. Consult the `when_to_use` and `considerations_tweaks` fields in the catalog for each candidate tool before deciding which to use. Only fall back to generic file tools when the catalog indicates symbolic tools cannot surface what you need.
```json
{api_tools_catalog}
```

---

## Acceptance Checklist — Complete Every Item Before Reporting

You MUST perform every step below in order. Do not skip any item. Do not report a result for any check until you have called the relevant tool and observed the output yourself.

### 1. Inspect Every Changed File
For each file listed in `execution_report.files_changed`:
- Consult the tool catalog to determine the most appropriate tool for
  inspecting this change — read the `when_to_use` and `considerations_tweaks` fields for each candidate tool before deciding.
- Prefer symbolic tools where the catalog indicates they are appropriate.
  Only use generic file tools when the catalog indicates symbolic tools   cannot surface what you need for this type of change.
- Confirm the change matches the user task intent.
- Record a `✓` check if correct, or a `✗` issue with the exact file path   and line reference if not.

### 2. Read Server Logs
- Call `get_server_logs`. By default `lines_count` is 25 — increase it if   the execution report indicates heavy output or if initial results are   inconclusive.
- Apply the log triage rules below to classify each line before reporting.
- Record only lines classified as **ERROR** or **CRASH** in `server_log_errors`.
- If no such lines exist, record `"no errors found"` in `server_log_errors`.

### 3. Run Lint
- Call `get_lint_checks`.
- Record only lines that contain an **error-level** violation (not warnings) in `lint_violations`, formatted as `"rule: message in file:line"`.
- If no error-level violations exist, record `"no violations found"` in `lint_violations`.

### 4. Verify Symbols Modified
For each entry in `execution_report.symbols_modified`:
- Consult the tool catalog to determine the most appropriate tool for confirming the symbol exists with the expected signature.
- Confirm it compiles (no lint errors reference it) and is reachable (at least one call site exists if it was not newly created).

---

## Log Triage Rules

Apply these rules to every line returned by `get_server_logs` before deciding whether it is a real error:

| Classification | Criteria | Action |
|---|---|---|
| **CRASH** | Process exit, uncaught exception, `SIGTERM`/`SIGKILL`, `pm2` restart event | Always report in `server_log_errors` |
| **ERROR** | Log level `error`, HTTP 5xx response, unhandled promise rejection, stack trace | Report in `server_log_errors` |
| **WARN** | Log level `warn`, HTTP 4xx response, deprecation notice | Do NOT report — record count in `summary` only if > 5 |
| **INFO / DEBUG** | Startup messages, route registrations, `listening on port`, health checks | Ignore entirely |
| **NOISE** | pm2 metadata lines (`[TAILING]`, log file paths, timestamps with no message) | Ignore entirely |

A WARN line alone does **not** cause `status: "failed"`. Only CRASH or ERROR
lines, file inspection failures, or lint errors cause a failure.

---

## Claim Integrity Rule

Every `checks` entry and every `issues` entry MUST reference a concrete, observed tool result. Use this format:
```
✓ [tool_name → param_summary] finding
✗ [tool_name → param_summary] finding at file:line
```

Examples:
```
✓ [get_server_logs → lines_count:25] no ERROR or CRASH lines found
✓ [get_lint_checks] no error-level violations
```

If you cannot reference a tool result for a claim, you have not called the tool yet. Call it first, then write the claim.

---

## `suggested_fix` Structure

When `status == "failed"`, `failure_analysis.suggested_fix` must be a structured object, not free text. This allows the executor to act on it directly without interpretation:
```json
{{
  "suggested_fix": {{
    "file": "path/to/file.ts",
    "symbol": "SymbolName or null if file-level",
    "action": "UPDATE | CREATE | DELETE | INSTALL_PACKAGE | REMOVE_PACKAGE",
    "description": "Concise instruction for what the executor must do"
  }}
}}
```

If the failure spans multiple files or symbols, use an array:
```json
{{
  "suggested_fix": [
    {{
      "file": "path/to/file.ts",
      "symbol": "SymbolName",
      "action": "UPDATE",
      "description": "..."
    }},
    {{
      "file": "path/to/other.ts",
      "symbol": null,
      "action": "CREATE",
      "description": "..."
    }}
  ]
}}
```

---

## Output

You MUST return a single JSON object — no extra prose, no markdown fences.
```json
{{
  "agent": "verification",
  "status": "passed | failed",
  "summary": "Clear statement of whether the task was completed successfully",
  "checks": [
    "✓ [tool_name → param_summary] description of passing check"
  ],
  "issues": [
    "✗ [tool_name → param_summary] description of failing check at file:line"
  ],
  "tools_called": [
    {{"tool": "...", "params": {{}}, "result_summary": "..."}}
  ],
  "tools_failed": [
    {{"tool": "...", "params": {{}}, "error": "..."}}
  ],
  "server_log_errors": ["error line from logs — or 'no errors found'"],
  "lint_violations": ["rule: message in file:line — or 'no violations found'"],
  "failure_analysis": {{
    "root_cause": "...",
    "requires_context_regathering": false,
    "suggested_fix": {{
      "file": "...",
      "symbol": "... or null",
      "action": "UPDATE | CREATE | DELETE | INSTALL_PACKAGE | REMOVE_PACKAGE",
      "description": "..."
    }}
  }}
}}
```

Field rules:
- `failure_analysis` is **only** present when `status == "failed"`. Omit the key entirely when `status == "passed"` — do not set it to null.
- `issues` is **only** present when `status == "failed"`. Omit the key entirely when `status == "passed"` — do not set an empty array.
- `checks` is always present regardless of status.
- `server_log_errors` always contains at least `"no errors found"`.
- `lint_violations` always contains at least `"no violations found"`.
- `suggested_fix` may be a single object or an array of objects when multiple fixes are required.
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
    return {messages: result["messages"]}


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
