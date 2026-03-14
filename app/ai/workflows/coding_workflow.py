import uuid

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph, MessagesState
from langchain_core.messages import HumanMessage, SystemMessage, BaseMessage
from langchain_core.prompts import PromptTemplate
from langchain.agents import create_agent
from app.ai.tools.mcp_tools import execute_specific_tool, filtered_tools
from app.ai.tools.sandbox_tools import build_sandbox_tools
from app.ai.prompts import (
    EDITING_PROMPT,
    MINI_DOCS_CREATION_PROMPT,
    PlANNER_PROMPT,
    RESEARCH_PROMPT,
)
from app.ai.utils import build_model
from app.constants import DEFAULT_MODEL_ID, DEFAULT_MODEL_PROVIDER, PROJECT_PATH
from ai.tools.models_tools import get_coding_agent_known_package_version
from ai.tools.changelogs_tools import build_changelog_tools
from .testing_workflow import testing_graph


class State(MessagesState):
    sandbox_id: str
    user_story: str
    model_provider: str | None = None
    model_id: str | None = None
    mini_docs: str | None = None
    research_report: str | None = None
    plan: str | None = None
    coding_messages: list[BaseMessage] = []


def _build_model_from_state(state: State):
    return build_model(
        provider=state.get("model_provider", DEFAULT_MODEL_PROVIDER),
        model_id=state.get("model_id", DEFAULT_MODEL_ID),
    )


def _require_sandbox_id(state: State, step_name: str) -> str:
    sandbox_id = state.get("sandbox_id")
    if not sandbox_id:
        raise ValueError(f"sandbox_id is required for {step_name}")
    return sandbox_id


async def init_step(state: State) -> dict:
    return {
        "sandbox_id": state.get("sandbox_id"),
    }


async def mini_docs_creation_step(state: State) -> dict:
    sandbox_id = _require_sandbox_id(state, "mini_docs_creation_step")
    user_story = state.get("user_story")
    model = _build_model_from_state(state)
    sandbox_tools = build_sandbox_tools(sandbox_id)
    packages = await sandbox_tools["read_file"](path=f"{PROJECT_PATH}/package.json")
    changelog_tools = build_changelog_tools(sandbox_id)

    system_message = PromptTemplate.from_template(MINI_DOCS_CREATION_PROMPT).format(
        packages=packages,
    )
    docs_agent = create_agent(
        system_prompt=system_message,
        tools=[
            changelog_tools["search_changelogs"],
            get_coding_agent_known_package_version,
        ],
        model=model,
    )

    result = await docs_agent.ainvoke({"messages": [HumanMessage(content=user_story)]})
    mini_docs = result["messages"][-1].content

    return {
        "messages": result["messages"],
        "mini_docs": mini_docs,
    }


async def codebase_research_step(state: State) -> dict:
    sandbox_id = _require_sandbox_id(state, "codebase_research_step")
    user_story = state.get("user_story")
    print("user_story", user_story)
    print("user_story", type(user_story))
    model = build_model(
        provider=state.get("model_provider", DEFAULT_MODEL_PROVIDER),
        model_id=state.get("model_id", "gemini-3-pro-preview"),
    )
    sandbox_tools = build_sandbox_tools(sandbox_id)
    packages = await sandbox_tools["read_file"](path=f"{PROJECT_PATH}/package.json")
    tech_stack = await sandbox_tools["read_file"](
        path=f"{PROJECT_PATH}/.actovator/features/tech_stack.json"
    )

    system_message = PromptTemplate.from_template(RESEARCH_PROMPT).format(
        packages=packages,
        tech_stack=tech_stack,
    )
    tools_result = await filtered_tools(
        sandbox_id,
        allowed_tools=[
            "find_referencing_symbols",
            "find_symbol",
            "get_symbols_overview",
            "find_file",
            "list_dir",
            "search_for_pattern",
        ],
    )
    research_agent = create_agent(
        system_prompt=system_message,
        tools=tools_result.tools,
        model=model,
    )

    result = await research_agent.ainvoke(
        {"messages": [HumanMessage(content=user_story)]}
    )
    result_messages = result["messages"]
    if result_messages and isinstance(result_messages[-1], HumanMessage):
        result_messages = result_messages[:-1]
    return {
        "messages": result_messages,
        "research_report": result["messages"][-1].content,
    }


async def planner_step(state: State) -> dict:
    model = _build_model_from_state(state)

    context = SystemMessage(
        content=f"{state.get('mini_docs', '')}\n\n{state.get('research_report', '')}"
    )
    response = await model.ainvoke(
        [
            SystemMessage(content=PlANNER_PROMPT),
            context,
            HumanMessage(content=state.get("user_task")),
        ]
    )

    return {
        "messages": [*state.get("messages"), response],
        "plan": response.content,
        "sandbox_id": state.get("sandbox_id"),
    }


async def editing_step(state: State) -> dict:
    model = _build_model_from_state(state)

    tools_result = await filtered_tools(
        state.sandbox_id,
        execluded_tools=[
            "search_for_pattern",
            "active_language_server",
            "restart_language_server",
            "execute_shell_command",
        ],
    )
    editor_agent = create_agent(
        system_prompt=EDITING_PROMPT,
        tools=tools_result.tools,
        model=model,
    )
    result = await editor_agent.ainvoke(
        [
            HumanMessage(
                content=f"{state.get('user_task')}\n\nPlan:\n{state.get('plan')}"
            ),
        ]
    )

    return {
        "messages": result["messages"],
        "coding_messages": result["messages"],
    }


async def testing_step(state: State) -> dict:
    messages = state.get("coding_messages", [])

    result = await testing_graph.ainvoke(
        [
            *messages,
            HumanMessage(content=f"{state.get('user_task')}"),
        ]
    )

    return {
        "messages": result["messages"],
    }


coding_workflow = StateGraph(State)

coding_workflow.add_node("init_step", init_step)
coding_workflow.add_node("codebase_research_step", codebase_research_step)
# coding_workflow.add_node("planner_step", planner_step)
# coding_workflow.add_node("mini_docs_creation_step", mini_docs_creation_step)
# coding_workflow.add_node("editing_step", editing_step)

## Running in parellel
coding_workflow.add_edge(START, "init_step")
# coding_workflow.add_edge("init_step", "codebase_research_step")
# coding_workflow.add_edge("init_step", "mini_docs_creation_step")
## after they done
# coding_workflow.add_edge("mini_docs_creation_step", "planner_step")
coding_workflow.add_edge("init_step", "codebase_research_step")
# coding_workflow.add_edge("codebase_research_step", "planner_step")
# coding_workflow.add_edge("planner_step", "editing_step")
coding_workflow.add_edge("codebase_research_step", END)

checkpointer = InMemorySaver()
config = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 100}
coding_graph = coding_workflow.compile(checkpointer=checkpointer)
