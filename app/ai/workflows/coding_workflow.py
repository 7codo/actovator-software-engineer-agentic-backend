import re
import shlex
import json
from langchain.agents import create_agent
from typing import Optional, List
from jsonschema import Draft7Validator
from e2b import AsyncSandbox
from langchain.tools import tool
from pydantic import BaseModel
from langgraph.types import Command
from app.ai.llm.models import build_model
from app.ai.tools.files_tools import get_agent_browser_skill
from app.constants import PROJECT_PATH
from app.core.config import settings
from langgraph.graph.message import MessagesState
from langchain_core.messages import HumanMessage, BaseMessage
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver
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

## Project Context
- **Project Ecosystem:** {project_ecosystem}
- **Project Structure:** {project_structure}

---

## Inputs
Processing depends on the input path:

### Normal Path:
- **User task** — a natural language description of the requested change or feature.
- **Latest Execution report** — latest execution report.

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

## Project Context
- **Project Ecosystem:** {project_ecosystem}
- **Coding Conventions:** {coding_conventions}

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
- Your FINAL response must be a single valid JSON object. Do not include markdown code fences, preamble, or any text outside the JSON object.
- - When the input is an E2E failure report, focus verification exclusively on the failures described in that report — do not re-verify unrelated files.

---

## Inputs

### Normal Path
- User task description (in natural language)
- Execution report (JSON)
- Context report (executor's context)

### E2E Failure Path
- E2E testing failed report (JSON) — provided when e2e testing did not pass

- Tool catalog 
```json
{api_tools_catalog}
```

---

## Workflow
Determine which path applies, then complete every step in order. Do not skip any.

### Path A — Normal Run
1. Inspect every changed file
   - For each file in `execution_report.files_changed`: pick the best tool from the catalog, call it, confirm the change matches the task intent.
   - Record `✓` if correct, `✗` with file path and line reference if not.
2. Read server logs
   - Call `get_server_logs` (default `lines_count: 25`; increase if the execution report indicates more output).
   - Classify every line using the Log Triage table in Conditions.
   - Record only ERROR or CRASH lines in `server_log_errors`. If none → record `"no errors found"`.
3. Run lint
   - Call `get_lint_checks`.
   - Record only error-level violations (not warnings) in `lint_violations` as `"rule: message in file:line"`. If none → record `"no violations found"`.

### Path B — E2E Failure Run
1. Read the E2E failed report. Identify which scenarios failed and their `root_cause`.
2. Inspect only the files implicated by `failure_analysis.affected_files` in the E2E report.
3. Read server logs — same rules as Path A step 2.
4. Run lint — same rules as Path A step 3.
5. Set `failure_analysis.requires_context_regathering` based on whether the fix requires information beyond what the current context report covers.

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
- [ ] The final response is a single valid JSON object — no markdown fences, no leading or trailing text

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

# ------------------------------------
# E2E TESTING
# ------------------------------------

E2E_TESTING_PROMPT = """\
## Role
You are a Senior E2E Test Architect Agent. Expert in browser automation, user journey modeling, and `agent-browser` CLI.
Methodical and minimal — never add steps that don't validate a user-facing requirement.
State assumptions explicitly when inputs are ambiguous.
The application under test is a Next.js project with App Router architecture.

---

## Inputs
- `execution_report`: file changes from the code editor
- `user_task`: description of what the user was building or fixing

---

## Rules
- Call `get_agent_browser_skill` once at the start — always, before writing any test
- Call `execute_agent_browser` to run each scenario — one call per scenario, never batch
- Derive the URL path from changed files (e.g. `src/app/login/page.tsx` → `/login`)
- If you attempt to derive the correct URL path and do not succeed after a maximum of 3 tries, return a failed report and stop further attempts

**Scenarios:**
- Always include: 1 happy path + at least 1 edge/failure case
- Keep steps minimal — only what directly validates a requirement

---

## Workflow
1. **Load skill** — call `get_agent_browser_skill`
2. **Extract journey** — infer user actions from `execution_report` + `user_task`
3. **State assumptions** — list any ambiguities and how you resolved them
4. **Write scenarios** — happy path first, then edge cases
5. **Execute** — run each scenario via `execute_agent_browser`, one at a time
6. **Report** — return the structured JSON output below

---

## Acceptance Criteria

- [ ] `get_agent_browser_skill` was called before any test was written
- [ ] Each scenario runs in a separate `execute_agent_browser` call — no batching
- [ ] No unit tests or implementation-level assertions present
- [ ] Every step maps to a visible user action or UI state
- [ ] URL path is derived from changed files — not hardcoded or assumed
- [ ] If correct URL path cannot be derived after at most 3 attempts, a failed report is returned and no further attempts are made
- [ ] At least 1 happy path and 1 edge/failure case are defined
- [ ] Each step is minimal — removed if it doesn't validate a requirement
- [ ] All ambiguities are listed and resolved before test writing begins
- [ ] User journey is explicitly extracted — not inferred silently
- [ ] Assumptions section is non-empty or states "no ambiguities found"
- [ ] Happy path written before edge cases
- [ ] Each scenario executed individually — verified by separate shell calls
- [ ] Output is valid JSON — no extra text, no markdown wrapper

---

## Output
Return a single JSON object. Nothing else.
```json
{
  "status": "passed | failed",
  "summary": "...",
  "scenarios": [
    {
      "name": "...",
      "type": "happy_path | edge_case",
      "steps": ["..."],
      "result": "passed | failed",
      "detail": "..."
    }
  ],
  "failure_analysis": {
    "root_cause": "...",
    "affected_paths": ["..."],
    "url_path_resolution_attempts": [
      "Attempt 1: ...",
      "Attempt 2: ...",
      "Attempt 3: ..."
    ]
  }
}
```

**Field rules:**
- `failure_analysis` — include only when `status: "failed"`, omit otherwise.
    - If status is `"failed"` specifically due to inability to derive the correct URL path after 3 tries, the `failure_analysis` should explain the attempts under `url_path_resolution_attempts`.
- `scenarios` — always present; minimum 1 `happy_path` + 1 `edge_case`
"""

# ADD after E2E_TESTING_PROMPT
GIT_AGENT_PROMPT = """\
## Role
You are the Memory & Commit Agent. You run after all code changes have been verified and tested.
Your responsibilities:
1. Update `.actovator/memory.json` with durable learnings from the completed task.
2. Commit and push all changes to the remote repository.

---

## Rules
- Before invoking any `execute_tool` action, you must first call `get_tool_parameters` to retrieve its parameters. This step is not required for the `commit_changes` tool.
- Only read and write `.actovator/memory.json` — never investigate or modify any other file.
- Never delete existing memory entries — only append or update.
- Never change root-level keys — only modify their `data` arrays or `description` values.
- Only update keys where the task produced new, factual, durable information.
- Keep entries concise and factual — no opinions or speculation.
- Prefer symbolic tools (`get_symbols_overview`, `find_symbol`, `replace_symbol_body`, `insert_after_symbol`, `insert_before_symbol`) over `replace_content`. Use `replace_content` only when symbolic tools cannot perform the edit.
- Commit message must be a single imperative sentence, max 72 characters.

---

## Supported Memory Keys
| Key | What to store |
|---|---|
| `project_ecosystem` | Framework, language, router, root, styling, ui_library |
| `project_structure` | New directories, entry points, file location conventions |
| `architecture_decisions` | Design choices, trade-offs, rejected alternatives |
| `coding_conventions` | Naming rules, file patterns, formatting preferences |
| `user_requests` | This task appended as a completed entry with auto-incremented id |
| `user_preferences` | Preferred patterns, libraries, avoided approaches |

---

## Memory Schema Reference
```json
{{
  "project_ecosystem": {{
    "description": "Core stack identity of the project",
    "data": [{{ "framework": "...", "language": "...", "router": "...", "root": "...", "styling": "...", "ui_library": "..." }}]
  }},
  "project_structure": {{
    "description": "Key directories, entry points, and file location conventions",
    "data": [{{ "path": "...", "role": "..." }}]
  }},
  "architecture_decisions": {{
    "description": "Key design choices, trade-offs, and rejected alternatives",
    "data": [{{ "decision": "...", "reason": "..." }}]
  }},
  "coding_conventions": {{
    "description": "Naming rules, file patterns, and formatting preferences",
    "data": []
  }},
  "user_requests": {{
    "description": "Tasks and instructions history with status",
    "data": [{{ "id": 1, "request": "...", "status": "done" }}]
  }},
  "user_preferences": {{
    "description": "Preferred patterns, libraries, and rejected approaches",
    "data": [{{ "prefer": "...", "avoid": "..." }}]
  }}
}}
```

---

## Inputs
- `user_task`: the original user request
- `execution_report`: JSON summary of files changed, packages installed, and symbols modified

---

- **Tool catalog**
```json
{api_tools_catalog}
```

---

## Workflow
1. Inspect `.actovator/memory.json` — use `get_symbols_overview` first, then `find_symbol` for specific bodies.
2. Identify which keys the task affects.
3. Merge new information, preserving all unrelated keys exactly as-is.
4. Append this task to `user_requests.data` with the next sequential `id` and `"status": "done"`.
5. Apply updates using symbolic tools; fall back to `replace_content` only when necessary.
6. Call `commit_changes` with a concise imperative commit message.

---

### Acceptance Criteria
- [ ] Every `execute_tool` call is preceded by a `get_tool_parameters` call for the same tool (except `commit_changes`).
- [ ] No file other than `.actovator/memory.json` is read from or written to during execution.
- [ ] The final state of `memory.json` contains all pre-existing entries — none removed.
- [ ] All root-level keys present before the task remain present and unchanged in name after the task.
- [ ] Only keys directly relevant to the completed task have modified `data` arrays or `description` values.
- [ ] No entry in any `data` array contains subjective language, opinions, or speculative content.
- [ ] Symbolic tools are used for all edits where applicable; `replace_content` used only when no symbolic tool could perform the same edit.
- [ ] The commit message is a single imperative sentence of 72 characters or fewer.
- [ ] `get_symbols_overview` is called on `memory.json` before any write operation is attempted.
- [ ] `find_symbol` is used to inspect any specific key body before it is modified.
- [ ] A new entry is appended to `user_requests.data` with an `id` one greater than the current maximum and `"status": "done"`.
- [ ] `commit_changes` is the final tool call and is invoked exactly once.

---

Respond in plain text. Summarize:
- Which memory keys were updated and what was added
- The commit message used
- Any issues encountered
```
"""

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n\s*```", re.DOTALL)


def extract_json_content(message: BaseMessage) -> str:
    content = message.content

    # Normalize multi-part provider responses (e.g. Gemini)
    if isinstance(content, list):
        content = next(
            (block["text"] for block in content if block.get("type") == "text"),
            "",
        )

    content = content.strip()

    # Priority 1: markdown fence — validate before returning
    match = _JSON_FENCE_RE.search(content)
    if match:
        candidate = match.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass  # fence content wasn't valid JSON — fall through

    # Priority 2: try the whole string directly
    try:
        json.loads(content)
        return content
    except json.JSONDecodeError:
        pass

    # Priority 3: scan for every '{' and try to parse outward from it.
    # Prefer the longest valid match (outermost object wins over a nested one).
    best: str | None = None
    for start in (i for i, ch in enumerate(content) if ch == "{"):
        # Walk backwards from the end to find the matching close brace
        for end in range(len(content), start, -1):
            if content[end - 1] != "}":
                continue
            candidate = content[start:end]
            try:
                json.loads(candidate)
                if best is None or len(candidate) > len(best):
                    best = candidate
                break  # longest match from this start found; try the next '{'
            except json.JSONDecodeError:
                continue

    if best is not None:
        return best

    # Nothing parsed — return raw content and let the caller surface the error
    return content


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
        self._sandbox: Optional[AsyncSandbox] = None

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
        if self._sandbox is None:
            self._sandbox = await AsyncSandbox.connect(
                sandbox_id=self.sdbx_id, api_key=settings.e2b_api_key
            )
        return self._sandbox

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

    async def read_memory(self, path: str = ".actovator/memory.json") -> dict:
        try:
            sandbox = await self._get_sandbox()
            raw = await sandbox.files.read(path)
            return json.loads(raw)
        except Exception:
            return {}

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

    async def execute_agent_browser(self, command: str) -> dict:
        """
        Execute an agent-browser CLI command inside the sandbox.
        Only accepts commands that start with 'agent-browser'.

        Args:
            command: A full agent-browser CLI command string.

        Returns:
            dict with 'stdout', 'stderr', and 'exit_code'.
        """
        stripped = command.strip()
        if not stripped.startswith("agent-browser"):
            return {
                "stdout": "",
                "stderr": f"Rejected: command must start with 'agent-browser', got: '{stripped[:60]}'",
                "exit_code": 1,
            }
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

        return {
            "execute_tool": execute_tool,
            "manage_npm_package": manage_npm_package,
            "get_server_logs": get_server_logs,
            "get_lint_checks": get_lint_checks,
            "execute_agent_browser": execute_agent_browser,
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
        self._sandbox: Optional[AsyncSandbox] = None

    async def _get_sandbox(self) -> AsyncSandbox:
        if self._sandbox is None:
            self._sandbox = await AsyncSandbox.connect(
                sandbox_id=self.sdbx_id, api_key=settings.e2b_api_key
            )
        return self._sandbox

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
                f"git commit -m {shlex.quote(message)}", cwd=PROJECT_PATH
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

GIT_TOOLS = [
    "get_symbols_overview",
    "find_symbol",
    "replace_symbol_body",
    "insert_after_symbol",
    "insert_before_symbol",
    "replace_content",
    "rename_symbol",
]

# ---------------------------------------------------------------------------
# STRUCTURED OUTPUT SCHEMA
# ---------------------------------------------------------------------------


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
    e2e_testing_report: Optional[str] = None
    user_message: Optional[HumanMessage] = None
    executor_report: Optional[str] = None


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
    memory = await sandbox_builder.read_memory()
    project_ecosystem = json.dumps(memory.get("project_ecosystem", {}), indent=2)
    project_structure = json.dumps(memory.get("project_structure", {}), indent=2)
    lc_tools = sandbox_builder.as_langchain_tools()

    system_prompt = PromptTemplate.from_template(CONTEXT_GATHERER_PROMPT).format(
        api_tools_catalog=read_definitions.get_sandbox_tools_without_params(),
        project_ecosystem=project_ecosystem,
        project_structure=project_structure,
    )
    messages = state.get("messages")
    verification_report = state.get("verification_report")
    context_report = state.get("context_report")
    executor_report = state.get("executor_report")

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
        if executor_report is None:
            messages_input = [HumanMessage(f"User Task: {user_message.content}")]
        else:
            messages_input = [
                HumanMessage(
                    f"User Task: {user_message.content}\n---\nExecution Report: {executor_report}"
                )
            ]

    result = await _run_subagent(
        system_prompt=system_prompt,
        tools=[lc_tools["execute_tool"], read_get_params],
        state=state,
        agent_name="context_gatherer",
        messages=messages_input,
    )
    context_report = extract_json_content(result["messages"][-1])
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
    memory = await sandbox_builder.read_memory()
    project_ecosystem = json.dumps(memory.get("project_ecosystem", {}), indent=2)
    coding_conventions = json.dumps(memory.get("coding_conventions", {}), indent=2)

    system_prompt = PromptTemplate.from_template(EXECUTOR_PROMPT).format(
        api_tools_catalog=write_definitions.get_sandbox_tools_without_params(),
        project_ecosystem=project_ecosystem,
        coding_conventions=coding_conventions,
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

    executor_report = extract_json_content(result["messages"][-1])
    return {"messages": result["messages"], "executor_report": executor_report}


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
    executor_report = state.get("executor_report")
    e2e_testing_report = state.get("e2e_testing_report")
    if e2e_testing_report:
        messages_input = [
            HumanMessage(f"E2E testing failed report: {e2e_testing_report}")
        ]
    else:
        messages_input = [
            HumanMessage(
                f"User Task: {user_message.content}\n"
                f"Context Report: {context_report}\n"
                f"Execution Report: {executor_report}"
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
        messages=messages_input,
    )

    raw = extract_json_content(result["messages"][-1])
    report = json.loads(raw)

    MAX_RETRIES = 3
    verification_report = None
    next_node = "e2e_testing"

    status_failed = report.get("status") == "failed"
    exhausted_retries = retry_count >= MAX_RETRIES

    if status_failed and not exhausted_retries:
        retry_count += 1
        verification_report = raw
        requires_regathering = (report.get("failure_analysis") or {}).get(
            "requires_context_regathering"
        )
        next_node = "context_gatherer" if requires_regathering else "executor"
    elif exhausted_retries:
        return Command(
            update={
                "retry_count": 0,
                "messages": result["messages"],
            },
            goto=END,
        )

    return Command(
        update={
            "messages": result["messages"],
            "verification_report": verification_report,
            "retry_count": retry_count,
        },
        goto=next_node,
    )


async def e2e_testing_node(state: AgentState, config: RunnableConfig) -> Command:
    sandbox_builder = BuildSandboxTools(state["sandbox_id"])
    lc_tools = sandbox_builder.as_langchain_tools()

    user_message = state.get("user_message")

    executor_report = state.get("executor_report")

    messages_input = [
        HumanMessage(
            f"User Task: {user_message.content}\nExecution Report: {executor_report}"
        )
    ]
    result = await _run_subagent(
        system_prompt=E2E_TESTING_PROMPT,
        tools=[
            get_agent_browser_skill,
            lc_tools["execute_agent_browser"],
        ],
        state=state,
        agent_name="e2e_testing",
        messages=messages_input,
    )

    raw = extract_json_content(result["messages"][-1])
    report = json.loads(raw)

    if report.get("status") == "failed":
        return Command(
            update={
                "messages": result["messages"],
                "e2e_testing_report": raw,
            },
            goto="verification",
        )

    return Command(
        update={"messages": result["messages"]},
        goto="git",
    )


async def git_node(state: AgentState, config: RunnableConfig) -> Command:
    git_builder = BuildGitTools(state["sandbox_id"])
    await git_builder.ensure_git_remote_origin()

    git_lc_tools = git_builder.as_langchain_tools()

    git_definitions = BuildSandboxToolsDefinitions(allowed_tools=GIT_TOOLS)
    read_get_params = git_definitions.as_langchain_tools()["get_tool_parameters"]
    sandbox_builder = BuildSandboxTools(
        state["sandbox_id"], tools_definitions=git_definitions
    )
    lc_tools = sandbox_builder.as_langchain_tools()
    system_prompt = PromptTemplate.from_template(GIT_AGENT_PROMPT).format(
        api_tools_catalog=git_definitions.get_sandbox_tools_without_params()
    )

    user_message = state.get("user_message")
    executor_report = state.get("executor_report")

    messages_input = [
        HumanMessage(
            f"User Task: {user_message.content}\nExecution Report: {executor_report}"
        )
    ]

    result = await _run_subagent(
        system_prompt=system_prompt,
        tools=[
            lc_tools["execute_tool"],
            read_get_params,
            git_lc_tools["commit_changes"],
        ],
        state=state,
        agent_name="git",
        messages=messages_input,
    )

    return Command(
        update={"messages": result["messages"]},
        goto=END,
    )


# ---------------------------------------------------------------------------
# GRAPH ASSEMBLY
# ---------------------------------------------------------------------------

coding_workflow = StateGraph(AgentState)

coding_workflow.add_node("context_gatherer", context_gatherer_node)
coding_workflow.add_node("executor", executor_node)
coding_workflow.add_node("verification", verification_node)
coding_workflow.add_node("e2e_testing", e2e_testing_node)
coding_workflow.add_node("git", git_node)


coding_workflow.add_edge(START, "context_gatherer")
coding_workflow.add_edge("context_gatherer", "executor")
coding_workflow.add_edge("executor", "verification")
coding_graph = coding_workflow.compile(checkpointer=InMemorySaver())
