import json
from langgraph.types import Command
from app.ai.tools.files_tools import get_agent_browser_skill
from langchain_core.messages import HumanMessage
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver
from app.ai.workflows.prompts.coding_prompts import (
    USING_SYMBOLIC_TOOLS_GUIDE,
    CONTEXT_GATHERER_PROMPT,
    EXECUTOR_PROMPT,
    VERIFICATION_PROMPT,
    E2E_TESTING_PROMPT,
    MEMORY_AGENT_PROMPT,
    GIT_AGENT_PROMPT,
    DESIGN_SYSTEM_CREATOR_PROMPT,
)
from app.ai.workflows.tools.coding_tools import (
    BuildSandboxToolsDefinitions,
    BuildSandboxTools,
    BuildGitTools,
)
from app.ai.workflows.helpers.coding_helpers import (
    READ_ONLY_TOOLS,
    WRITE_TOOLS,
    GIT_TOOLS,
    AgentState,
    _run_subagent,
    extract_json_content,
)


async def context_gatherer_node(state: AgentState, config: RunnableConfig) -> dict:
    read_definitions = BuildSandboxToolsDefinitions(allowed_tools=READ_ONLY_TOOLS)
    read_get_params = read_definitions.as_langchain_tools()["get_tool_parameters"]
    sandbox_builder = BuildSandboxTools(
        state["sandbox_id"], tools_definitions=read_definitions
    )
    memory = await sandbox_builder.read_memory()
    project_ecosystem = sandbox_builder._extract_memory_section(memory, "Ecosystem")
    project_structure = sandbox_builder._extract_memory_section(memory, "Structure")
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
    project_ecosystem = sandbox_builder._extract_memory_section(memory, "Ecosystem")
    coding_conventions = sandbox_builder._extract_memory_section(memory, "Conventions")
    architecture = sandbox_builder._extract_memory_section(memory, "Architecture")
    preferences = sandbox_builder._extract_memory_section(memory, "Preferences")

    system_prompt = PromptTemplate.from_template(EXECUTOR_PROMPT).format(
        api_tools_catalog=write_definitions.get_sandbox_tools_without_params(),
        project_ecosystem=project_ecosystem,
        coding_conventions=coding_conventions,
        architecture=architecture,
        preferences=preferences,
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
        goto="memory",
    )


async def memory_node(state: AgentState, config: RunnableConfig) -> Command:

    git_definitions = BuildSandboxToolsDefinitions(allowed_tools=GIT_TOOLS)
    read_get_params = git_definitions.as_langchain_tools()["get_tool_parameters"]
    sandbox_builder = BuildSandboxTools(
        state["sandbox_id"], tools_definitions=git_definitions
    )
    lc_tools = sandbox_builder.as_langchain_tools()
    system_prompt = PromptTemplate.from_template(MEMORY_AGENT_PROMPT).format(
        api_tools_catalog=git_definitions.get_sandbox_tools_without_params(),
        using_symbolic_tools_guide=USING_SYMBOLIC_TOOLS_GUIDE,
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
        ],
        state=state,
        agent_name="memory",
        messages=messages_input,
    )

    return Command(
        update={"messages": result["messages"]},
        goto=END,
    )


async def git_node(state: AgentState, config: RunnableConfig) -> Command:
    git_builder = BuildGitTools(state["sandbox_id"])
    await git_builder.ensure_git_remote_origin()

    git_lc_tools = git_builder.as_langchain_tools()

    user_message = state.get("user_message")
    executor_report = state.get("executor_report")

    messages_input = [
        HumanMessage(
            f"User Task: {user_message.content}\nExecution Report: {executor_report}"
        )
    ]

    result = await _run_subagent(
        system_prompt=GIT_AGENT_PROMPT,
        tools=[
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


async def screenshot_testing_node(state: AgentState, config: RunnableConfig) -> Command:
    sandbox_builder = BuildSandboxTools(state["sandbox_id"])
    lc_tools = sandbox_builder.as_langchain_tools()

    result = await _run_subagent(
        system_prompt="""You have the ability to use agent-browser cli, open and take screenshot nothing else; agent-browser open http://localhotst:3000; then use `process_screenshot` tool to take screenshot and process it""",
        tools=[
            get_agent_browser_skill,
            lc_tools["execute_agent_browser"],
            lc_tools["process_screenshot"],
        ],
        state=state,
        agent_name="testing",
        messages=state.get("messages"),
    )

    return {"messages": result["messages"]}


async def interactively_node(state: AgentState, config: RunnableConfig) -> Command:
    sandbox_builder = BuildSandboxTools(state["sandbox_id"])
    lc_tools = sandbox_builder.as_langchain_tools()

    result = await _run_subagent(
        system_prompt=DESIGN_SYSTEM_CREATOR_PROMPT,
        tools=[
            get_agent_browser_skill,
            lc_tools["execute_agent_browser"],
            lc_tools["process_screenshot"],
        ],
        state=state,
        agent_name="testing",
        messages=state.get("messages"),
    )

    return {"messages": result["messages"]}


# ---------------------------------------------------------------------------
# GRAPH ASSEMBLY
# ---------------------------------------------------------------------------

coding_workflow = StateGraph(AgentState)

coding_workflow.add_node("context_gatherer", context_gatherer_node)
coding_workflow.add_node("executor", executor_node)
coding_workflow.add_node("verification", verification_node)
coding_workflow.add_node("e2e_testing", e2e_testing_node)
coding_workflow.add_node("memory", memory_node)
# coding_workflow.add_node("testing", screenshot_testing_node)
coding_workflow.add_node("testing", interactively_node)


coding_workflow.add_edge(START, "testing")
coding_workflow.add_edge("testing", END)
# coding_workflow.add_edge(START, "context_gatherer")
# coding_workflow.add_edge("context_gatherer", "executor")
# coding_workflow.add_edge("executor", "verification")
# coding_workflow.add_edge("e2e_testing", "git")
coding_graph = coding_workflow.compile(checkpointer=InMemorySaver())
