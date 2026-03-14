import json
from typing import Any, Dict, Literal

from langchain.agents import create_agent
from langchain_core.messages import ToolMessage, HumanMessage, AIMessage
from langchain_core.prompts import PromptTemplate
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph

from copilotkit.langgraph import RunnableConfig

from app.ai.prompts import (
    PRD_GENERATOR_PROMPT,
    TECH_STACK_PROMPT,
    USER_STORIES_GENERATOR_PROMPT,
)
from app.ai.tools.mcp_tools import execute_specific_tool, filtered_tools
from app.ai.utils import build_model
from app.constants import DEFAULT_MODEL_ID, DEFAULT_MODEL_PROVIDER
from app.utils.files_utils import parse_frontmatter, parse_list_dir_tool_result


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class State(MessagesState):
    sandbox_id: str | None
    model_provider: str | None
    model_id: str | None
    prd_path: str | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_model_from_state(state: State):

    return build_model(
        provider=state.get("model_provider", DEFAULT_MODEL_PROVIDER),
        model_id=state.get("model_id", DEFAULT_MODEL_ID),
    )


def _require_sandbox_id(
    state: State,
) -> str:
    sandbox_id = state.get("sandbox_id")
    if not sandbox_id:
        raise ValueError("sandbox_id is required")
    return sandbox_id


async def _get_available_features_metadata(sandbox_id: str):

    list_result = await execute_specific_tool(
        sandbox_id,
        "list_dir",
        {"relative_path": ".actovator/features", "recursive": True},
    )

    _, files = parse_list_dir_tool_result(list_result["result"])

    available_features = []
    for prd_path in (f for f in files if f.endswith("/prd.md")):
        file_result = await execute_specific_tool(
            sandbox_id, "read_file", {"relative_path": prd_path}
        )
        metadata, _ = parse_frontmatter(file_result["result"])
        available_features.append({"path": prd_path, "metadata": metadata})

    return available_features


def _get_relative_path_from_last_scope(messages):
    """
    Finds the last Human Message scope and extracts the relative_path
    from the AIMessage with tool_calls within that scope.
    """

    # 1. Find the index of the LAST HumanMessage
    last_human_idx = None
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            last_human_idx = i
            break

    if last_human_idx is None:
        raise ValueError("No HumanMessage found in messages")

    # 2. Slice to only the last scope
    last_scope = messages[last_human_idx:]

    # 3. Find the LAST AIMessage with tool_calls containing relative_path
    relative_path = None
    for msg in reversed(last_scope):
        if not isinstance(msg, AIMessage):
            continue

        tool_calls = (
            msg.tool_calls if hasattr(msg, "tool_calls") and msg.tool_calls else []
        )

        for tool_call in reversed(tool_calls):
            args = tool_call.get("args", {})
            if "relative_path" in args:
                relative_path = args["relative_path"]
                break

        if relative_path:
            break

    return {
        "relative_path": relative_path,
    }


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


async def init_step(state: State) -> Dict[str, Any]:
    sandbox_id = _require_sandbox_id(state)

    return {"sandbox_id": sandbox_id}


async def prd_generator_step(state: State, config: RunnableConfig) -> Dict[str, Any]:

    model = _build_model_from_state(state)

    built_tools = await filtered_tools(
        state.get("sandbox_id"),
        allowed_tools=["read_file", "create_text_file", "replace_content"],
    )

    available_features = await _get_available_features_metadata(state.get("sandbox_id"))
    print("available_features", available_features)
    system_message = PromptTemplate.from_template(PRD_GENERATOR_PROMPT).format(
        available_feature=json.dumps(available_features),
    )

    agent = create_agent(
        model=model, system_prompt=system_message, tools=built_tools.tools
    )
    result = await agent.ainvoke({"messages": state["messages"]}, config)
    messages = result["messages"]

    relative_path_result = _get_relative_path_from_last_scope(messages)
    print("relative_path_result", relative_path_result)
    print("END prd generator", len(messages))
    return {"messages": messages, "prd_path": relative_path_result["relative_path"]}


async def tech_stack_expander_step(
    state: State, config: RunnableConfig
) -> Dict[str, Any]:
    messages = state.get("messages", [])
    print("Start tech_stack_expander_step", len(messages))
    prd_file_path = state.get("prd_path")
    print("prd_file_path", prd_file_path)
    if not prd_file_path:
        return {
            "messages": [
                *messages,
                AIMessage(
                    content="Error: Create PRD file first to generate user stories."
                ),
            ]
        }
    packages = await execute_specific_tool(
        state.get("sandbox_id"),
        "read_file",
        {"relative_path": "package.json"},
    )
    #
    feature_prd = await execute_specific_tool(
        state.get("sandbox_id"),
        "read_file",
        {"relative_path": prd_file_path},
    )
    #
    tech_stack = await execute_specific_tool(
        state.get("sandbox_id"),
        "read_file",
        {"relative_path": ".actovator/features/tech_stack.json"},
    )
    #

    model = _build_model_from_state(state)
    built_tools = await filtered_tools(
        state.get("sandbox_id"),
        allowed_tools=["replace_content"],
    )

    system_message = PromptTemplate.from_template(TECH_STACK_PROMPT).format(
        tech_stack=json.dumps(tech_stack["result"]),
        prd=feature_prd["result"],
        packages=json.dumps(packages["result"]),
    )

    agent = create_agent(
        model=model, system_prompt=system_message, tools=built_tools.tools
    )
    result = await agent.ainvoke({"messages": state["messages"]}, config)
    print("End tech_stack_expander_step", len(result["messages"]))
    return {"messages": result["messages"]}


async def user_stories_generator_step(
    state: State, config: RunnableConfig
) -> Dict[str, Any]:
    messages = state.get("messages", [])
    print("Start user_stories_generator_step", len(messages))
    prd_file_path = state.get("prd_path")
    print("prd_file_path", prd_file_path)
    if not prd_file_path:
        return {
            "messages": [
                *messages,
                AIMessage(
                    content="Error: Create PRD file first to generate user stories."
                ),
            ]
        }
    prd_content = await execute_specific_tool(
        state.get("sandbox_id"),
        "read_file",
        {"relative_path": prd_file_path},
    )

    tech_stack = await execute_specific_tool(
        state.get("sandbox_id"),
        "read_file",
        {"relative_path": ".actovator/features/tech_stack.json"},
    )

    model = _build_model_from_state(state)
    built_tools = await filtered_tools(
        state.get("sandbox_id"),
        allowed_tools=["read_file", "create_text_file", "replace_content"],
    )
    system_message = PromptTemplate.from_template(USER_STORIES_GENERATOR_PROMPT).format(
        prd=prd_content,
        tech_stack=tech_stack,
        prd_file_path=prd_file_path,
    )
    agent = create_agent(
        model=model, system_prompt=system_message, tools=built_tools.tools
    )
    result = await agent.ainvoke({"messages": messages}, config)
    print("End user_stories_generator_step", len(result["messages"]))
    return {"messages": result["messages"]}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def route_to(
    state: State,
) -> Literal[
    "tech_stack_expander_step", "__end__"
]:  # WHY: to determine where the prd generator step complete conversation with the human

    prd_path = state.get("prd_path")
    print("Routing decision - prd_path:", prd_path)
    if prd_path:
        return "tech_stack_expander_step"
    return END


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

architecture_workflow = StateGraph(State)

architecture_workflow.add_node("init_step", init_step)
architecture_workflow.add_node("prd_generator_step", prd_generator_step)
architecture_workflow.add_node(
    "user_stories_generator_step", user_stories_generator_step
)
architecture_workflow.add_node("tech_stack_expander_step", tech_stack_expander_step)

architecture_workflow.add_edge(START, "init_step")
architecture_workflow.add_edge("init_step", "prd_generator_step")
architecture_workflow.add_conditional_edges("prd_generator_step", route_to)
architecture_workflow.add_edge(
    "tech_stack_expander_step", "user_stories_generator_step"
)
architecture_workflow.add_edge("user_stories_generator_step", END)

architecture_graph = architecture_workflow.compile(checkpointer=InMemorySaver())

if __name__ == "__main__":
    import asyncio

    available_features = asyncio.run(
        _get_available_features_metadata("iigdxjs9fklw96v31hqhe")
    )
