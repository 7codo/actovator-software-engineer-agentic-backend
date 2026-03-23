from langchain_core.messages import HumanMessage, AIMessage
import hashlib
import json

from e2b import AsyncSandbox
from langchain.tools import tool

from app.constants import PROJECT_PATH
from app.core.config import settings
from typing import Optional, List
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableConfig
from langchain.agents import create_agent
from app.ai.llm.models import build_model_from_state
from app.ai.resources import SANDBOX_TOOLS_DEFINITIONS
from typing import Literal
from pydantic import BaseModel
from langgraph.types import Command

# ---------------------------------------------------------------------------
# PROMPTS
# ---------------------------------------------------------------------------


CONTEXT_GATHERER_PROMPT = """
## Role
You are the Context Gatherer. Collect all information needed to complete the user task using read-only tools.

---

## Inputs
You will receive only the following input:

| Input      | Your job                                                                 |
|------------|--------------------------------------------------------------------------|
| `user_task`| Inspect the task, identify what context is needed, and gather it from scratch |

- The available API tools.
```
{api_tools_catalog}
```

---

## Workflow
Inspect your input, identify what is missing, call tools until all acceptance criteria are met, then produce a context report.

---

## Tool usage guide

| Tool                      | When to use                                                               |
|---------------------------|---------------------------------------------------------------------------|
| `list_dir`                | Start here — list the task-relevant scope, not the full recursive root    |
| `find_file`               | Locate a known file by name e.g. `global.css`, `tailwind.config.ts`       |
| `search_for_pattern`      | Search for specific patterns — avoid `*` wildcards, they overload context |
| `get_symbols_overview`    | Get high-level symbols in a file — increase depth param for more detail   |
| `find_symbol`             | Locate and return the full body of a specific symbol                      |
| `find_referencing_symbols`| Find all call sites of a symbol before updating it                        |
| `read_file`               | Last resort — only when none of the above tools are sufficient            |

---

## Rules
- Always call `get_tool_parameters` before each `execute_tool`.
- Never guess or assume missing values — use tools to find them.
- If a tool returns insufficient results, try a narrower query.
- Always check `package.json` when the task involves a package not yet in the project.

---

## Output
Produce a context report covering:
- **Files** — to create, update, or delete with their current relevant content
- **Symbols** — affected symbols and their call sites if applicable
- **Packages** — to install if missing from `package.json`
- **Constraints** — anything that limits or shapes how the task must be done
"""

EXECUTOR_PROMPT = """
## Role
You are the Executor. Optimize your workflow to complete the user task efficiently and accurately using the available tools. You are empowered to make all execution decisions based on the provided context.

---

## Inputs
You will receive the following structured inputs:

| Input                                                          | Your job                                |
|---------------------------------------------------------------|-----------------------------------------|
| `user task` + `context report` + `previous execution reports`  | Review the context and prior reports, then execute the task from scratch, avoiding redundant actions |
| `user task` + `verification report` + `previous execution reports` | Analyze the verification failure report, review prior attempts, then take a corrected, non-redundant execution path |

- `api tools catalog`: The available tools.
```
{api_tools_catalog}
```

---

## Workflow

1. Inspect all inputs, including the list of previous execution reports, to avoid duplicate or redundant work.
2. Choose a path:

| Path            | When                                            |
|-----------------|-------------------------------------------------|
| Answer directly | The task only involves reading, explaining, or describing |
| Execute         | The task requires creating, editing, or deleting code or resources |

---

## Editing Approach

Favor symbolic, high-level operations over low-level file editing when possible.

| Approach      | When                                                     |
|---------------|----------------------------------------------------------|
| Symbol-based  | When updating, creating, or deleting entire symbols      |
| File-based    | When a symbol-based update is not possible or appropriate|

---

## Conditions
1. Always call `get_tool_parameters` before each `execute_tool`.
2. After each tool call, verify its result before proceeding.
3. Install or remove required packages using the tools before editing code if needed.
4. When resuming after a `verification report`, analyze the verification failure, correct your approach, and never repeat failing or redundant actions.
5. Carefully review the list of `previous execution reports` to understand what actions were already performed and to prevent any redundant or repeated steps.


---

## Execution Report Structure

When you successfully execute, your `execution_report` must include:

- A **list of file paths changed** with:
    - Path
    - Operation type: `CREATE`, `UPDATE`, `DELETE`, or `RENAME`
    - Concise summary of the change (e.g., "Added API handler", "Refactored method A in X", etc.)
- A **list of packages** installed or removed during execution, stressing the exact action (e.g., `"INSTALLED: express"`, `"REMOVED: lodash"`).
- A summary explaining the overall execution in clear language.

---

## Rules
- If answering directly (no code/resources modified), set only `answer` and do not call any tools.
- When handling a `verification report`, focus only on what previously failed; do not repeat previous successful or unchanged steps.

"""


VERIFICATION_PROMPT = """
## Role
You are the Verification node. Confirm that the execution matches the user task intent by inspecting the code, server logs, and lint results.
You do not modify anything.

---

## Inputs
- `user_task`: The original task.
- `execution_reports`: Summary of what was executed.
- `server_logs`: Current dev server output.
- `lint_checks`: Current lint results.
- `api_tools_catalog`: Read-only tools to inspect files and symbols.
```
{api_tools_catalog}
```

---

## Workflow
Read the execution report, server logs, and lint results, then use read-only tools to inspect the affected files if needed.

---

## Tool usage guide

| Tool                      | When to use                                                              |
|---------------------------|--------------------------------------------------------------------------|
| `list_dir`                | Confirm where new or modified files are located                          |
| `find_file`               | Check if a specific file exists before reading it                        |
| `get_symbols_overview`    | Get high-level symbols in a file — increase depth param for more detail  |
| `find_symbol`             | Locate and return the full body of a specific symbol                     |
| `find_referencing_symbols`| Get all symbols linked to a given symbol                                 |
| `read_file`               | Last resort — only when none of the above tools are sufficient           |

---

## Rules
- No speculation — every claim must reference an observed result.
- If server logs show errors, report them explicitly.
- If lint checks show violations, report them explicitly.

---

## Acceptance Criteria
- [ ] Server logs are checked for runtime errors.
- [ ] Lint checks are checked for violations.
- [ ] Affected files are inspected to confirm changes are correct.
- [ ] Summary clearly states whether the task was completed successfully or not.
- [ ] Every claim references an observed result.
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
        """
        Loads SANDBOX_TOOLS_DEFINITIONS filtered by allowed and excluded lists.
        Both filters can be applied independently; allowed_tools is applied first.
        """
        tools = SANDBOX_TOOLS_DEFINITIONS

        if allowed_tools:
            tools = [t for t in tools if t["name"] in allowed_tools]
        if excluded_tools:
            tools = [t for t in tools if t["name"] not in excluded_tools]

        self.tools = tools

    def get_sandbox_tools_without_params(self) -> str:
        """Returns a JSON list of tools with only name and description (no parameters)."""
        api_tools_catalog_lite = [
            {"name": t["name"], "description": t["description"]} for t in self.tools
        ]
        return json.dumps(api_tools_catalog_lite, indent=2)

    def get_sandbox_tool_parameters(self, tool_name: str) -> str:
        """
        Retrieves the parameter schema for a specific tool by its name.
        Use this to get the arguments definition for a tool you intend to use.

        Args:
            tool_name: The name of the tool to retrieve parameters for.

        Returns:
            A JSON string representing the tool's parameters, or an error if not found.
        """
        for t in self.tools:
            if t["name"] == tool_name:
                return json.dumps(t.get("parameters", {}), indent=2)
        return json.dumps({"error": f"Tool '{tool_name}' not found"})

    def as_langchain_tools(self) -> dict:
        """
        Returns LangChain @tool-decorated callables bound to this instance.
        """
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


# ---


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

    # ------------------------------------------------------------------ #
    # Core tools                                                           #
    # ------------------------------------------------------------------ #

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
                f"execute_shell_command failed.\n"
                f"Command: {command}\n"
                f"Reason: {type(e).__name__}: {e}"
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

    # ------------------------------------------------------------------ #
    # LangChain export                                                     #
    # ------------------------------------------------------------------ #

    def as_langchain_tools(self) -> dict:
        """Returns LangChain @tool-decorated callables bound to this instance."""
        instance = self

        @tool
        async def execute_tool(tool_name: str, tool_params: dict) -> dict:
            """
            Use this to invoke a registered tool.
            Call it when you need to trigger a specific tool by name with a given set of parameters.

            Args:
                tool_name: The registered name of the tool to invoke.
                tool_params: A dictionary of parameters to pass as the JSON request body.

            Returns:
                dict:
                    - 'stdout' (str): The raw response body returned by the tool endpoint.
                    - 'stderr' (str): Error details if the request or execution failed.
                    - 'exit_code' (int): 0 on success, non-zero on failure.
            """
            return await instance.execute_tool(tool_name, tool_params)

        @tool
        async def install_npm_package(package: str, is_dev: bool = False) -> dict:
            """
            Use this to install an npm package inside the project.
            Call it when you need to add a new dependency to the project before using it in code.

            **Note: it prefers to not point the package' version**

            Args:
                package: The npm package name to install
                is_dev: If True, installs as a devDependency using --save-dev (default: False).

            Returns:
                dict:
                    - 'stdout' (str): The npm install output on success.
                    - 'stderr' (str): Error details if the installation failed.
                    - 'exit_code' (int): 0 on success, non-zero on failure.
            """
            return await instance.install_npm_package(package, is_dev)

        return {
            "execute_tool": execute_tool,
            "install_npm_package": install_npm_package,
        }


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class VerificationReport(BaseModel):
    status: Literal["passed", "failed"]
    summary: str
    checks: list[str]
    issues: list[str]


class ExecutorDecision(BaseModel):
    execution_report: str | None
    answer: str | None


class AgentState(MessagesState):
    sandbox_id: str
    model_provider: Optional[str] = None
    model_id: Optional[str] = None
    user_task: str | None = None
    context_report: str | None = None
    execution_reports: list[str] = []
    verification_report: VerificationReport | None = None
    retry_count: int = 0


async def context_gatherer(state: AgentState, config: RunnableConfig) -> AgentState:
    model = build_model_from_state(state)
    sdbx_id = state.get("sandbox_id")
    messages = state.get("messages")
    user_task = next(
        (
            message
            for message in reversed(messages)
            if isinstance(message, HumanMessage)
        ),
        None,  # Default value if no HumanMessage is found
    )
    print("context_gatherer::user_task", user_task.content)
    # Sandbox tools definitions building
    read_only_tools = [
        "read_file",
        "list_dir",
        "find_file",
        "search_for_pattern",
        "get_symbols_overview",
        "find_symbol",
        "find_referencing_symbols",
    ]
    sandbox_tools_definitions_builder = BuildSandboxToolsDefinitions(
        allowed_tools=read_only_tools
    )
    get_tool_parameters_tool = sandbox_tools_definitions_builder.as_langchain_tools()[
        "get_tool_parameters"
    ]

    # sandbox tools building
    sandbox_tools_builder = BuildSandboxTools(sdbx_id)
    execute_tool = sandbox_tools_builder.as_langchain_tools()["execute_tool"]

    # format the system prompt
    formatted_system_prompt = PromptTemplate.from_template(
        CONTEXT_GATHERER_PROMPT
    ).format(
        api_tools_catalog=sandbox_tools_definitions_builder.get_sandbox_tools_without_params()
    )
    with open("output/context_gatherer_prompt.md", "w") as f:
        f.write(formatted_system_prompt)
    # create the agent
    agent = create_agent(
        model=model,
        system_prompt=formatted_system_prompt,
        tools=[
            execute_tool,
            get_tool_parameters_tool,
        ],
    )
    input_messages = [HumanMessage(content=f"User task: {user_task.content}")]

    print("context_gatherer::input_messages", input_messages, "\n\n")
    result = await agent.ainvoke({"messages": input_messages}, config=config)

    return {
        "messages": result["messages"],
        "context_report": result["messages"][-1].content,
        "user_task": user_task.content,
        "retry_count": 0,
    }


async def executor(state: AgentState, config: RunnableConfig) -> AgentState:
    model = build_model_from_state(state)
    sdbx_id = state.get("sandbox_id")
    messages = state.get("messages")
    user_task = state.get("user_task")

    context_report = state.get("context_report")
    execution_reports = state.get("execution_reports")
    verification_report = state.get("verification_report")
    print("executor::user_task", user_task)

    print("executor::context_report", context_report)
    print("executor::verification_report", verification_report)
    # Sandbox tools definitions building
    write_tools = [
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
    sandbox_tools_definitions_builder = BuildSandboxToolsDefinitions(
        allowed_tools=write_tools
    )
    get_tool_parameters_tool = sandbox_tools_definitions_builder.as_langchain_tools()[
        "get_tool_parameters"
    ]

    # sandbox tools building
    sandbox_tools_builder = BuildSandboxTools(sdbx_id)
    langchain_tools = sandbox_tools_builder.as_langchain_tools()
    execute_tool = langchain_tools["execute_tool"]
    install_npm_package_tool = langchain_tools["install_npm_package"]

    # format the system prompt
    formatted_system_prompt = PromptTemplate.from_template(EXECUTOR_PROMPT).format(
        api_tools_catalog=sandbox_tools_definitions_builder.get_sandbox_tools_without_params()
    )
    with open("output/executor_prompt.md", "w") as f:
        f.write(formatted_system_prompt)
    # create the agent
    agent = create_agent(
        model=model,
        system_prompt=formatted_system_prompt,
        tools=[execute_tool, get_tool_parameters_tool, install_npm_package_tool],
        structured_output=ExecutorDecision,
    )
    input_messages = [
        HumanMessage(
            content=f"User Task: {user_task}\nContext report: {context_report}\nPrevious execution reports: {'\n---\n'.join(execution_reports)}"
        )
    ]

    if verification_report:
        input_messages = [
            HumanMessage(
                content=f"User Task: {user_task}\nVerification failure report: {verification_report.model_dump_json()}\nPrevious execution reports:\n```{'\n---\n'.join(execution_reports)}```"
            ),
        ]

    print("executor::input_messages", input_messages)
    result = await agent.ainvoke(
        {"messages": input_messages},
        config=config,
    )

    output = result["structured_output"]
    print("executor::structured_output", output, "\n\n")
    messages = result["messages"]
    if output.answer:
        messages.append(AIMessage(content=output.answer))

    next_node = "verification"  ## when it executes
    if output.answer:
        next_node = END

    if output.execution_report:
        execution_reports = [*execution_reports, output.execution_report]

    return Command(
        update={
            "messages": messages,
            "execution_reports": execution_reports,
            "verification_report": None,
        },
        goto=next_node,
    )


async def verification(state: AgentState, config: RunnableConfig) -> AgentState:
    model = build_model_from_state(state)
    sdbx_id = state.get("sandbox_id")
    user_task = state.get("user_task")
    execution_reports = state.get("execution_reports")
    retry_count = state.get("retry_count", 0)
    print("verification::user_task", user_task)
    print("verification::execution_reports", execution_reports)
    print("verification::retry_count", retry_count)
    # read-only tools — same set as context gatherer
    read_only_tools = [
        "read_file",
        "list_dir",
        "find_file",
        "get_symbols_overview",
        "find_symbol",
        "find_referencing_symbols",
    ]
    sandbox_tools_definitions_builder = BuildSandboxToolsDefinitions(
        allowed_tools=read_only_tools
    )
    get_tool_parameters_tool = sandbox_tools_definitions_builder.as_langchain_tools()[
        "get_tool_parameters"
    ]
    sandbox_tools_builder = BuildSandboxTools(sdbx_id)
    execute_tool = sandbox_tools_builder.as_langchain_tools()["execute_tool"]
    server_logs = await sandbox_tools_builder.get_server_logs()
    lint_checks = await sandbox_tools_builder.get_lint_checks()
    formatted_system_prompt = PromptTemplate.from_template(VERIFICATION_PROMPT).format(
        api_tools_catalog=sandbox_tools_definitions_builder.get_sandbox_tools_without_params()
    )
    with open("output/verification_prompt.md", "w") as f:
        f.write(formatted_system_prompt)
    agent = create_agent(
        model=model,
        system_prompt=formatted_system_prompt,
        tools=[execute_tool, get_tool_parameters_tool],
        structured_output=VerificationReport,
    )

    result = await agent.ainvoke(
        {
            "messages": [
                HumanMessage(
                    content=f"User task: {user_task}\n"
                    f"Execution report: {execution_reports[-1]}\n"
                    f"Server logs: {server_logs}\n"
                    f"Lint checks: {lint_checks}"
                )
            ]
        },
        config=config,
    )
    output = result["structured_output"]
    print("verification::structured_output", output, "\n\n")
    messages = result["messages"]
    next_node = END
    if output.status == "failed":
        if retry_count >= 3:
            next_node = END
        else:
            next_node = "executor"

    return Command(
        update={
            "messages": messages,
            "verification_report": output,
            "retry_count": retry_count + 1 if output.status == "failed" else 0,
        },
        goto=next_node,
    )


coding_workflow = StateGraph(AgentState)

checkpointer = InMemorySaver()
coding_workflow.add_node("executor", executor)
coding_workflow.add_node("context_gatherer", context_gatherer)
coding_workflow.add_node("verification", verification)

coding_workflow.add_edge(START, "context_gatherer")
coding_workflow.add_edge("context_gatherer", "executor")
coding_graph = coding_workflow.compile(checkpointer=checkpointer)
