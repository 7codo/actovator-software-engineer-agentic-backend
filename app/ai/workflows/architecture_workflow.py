import json
from typing import Any, Dict, Literal

from langchain.agents import create_agent
from langchain_core.messages import ToolMessage
from langchain_core.prompts import PromptTemplate
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph

from copilotkit.langgraph import RunnableConfig, copilotkit_customize_config

from app.ai.prompts import ARCHITECTURE_PROMPT, PRD_GENERATOR_PROMPT, TECH_STACK_PROMPT, USER_STORIES_GENERATOR_PROMPT
from app.ai.tools.git_tools import build_git_tools
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


def _require_sandbox_id(state: State, step_name: str) -> str:
    sandbox_id = state.get("sandbox_id")
    if not sandbox_id:
        raise ValueError(f"sandbox_id is required for {step_name}")
    return sandbox_id


async def _get_available_features_metadata(config: RunnableConfig):
    silent_config = copilotkit_customize_config(
        config, emit_messages=False, emit_tool_calls=False
    )
    list_result = await execute_specific_tool(
        "list_dir",
        {"relative_path": ".actovator/features", "recursive": True},
        silent_config,
    )
    print("list_result", list_result)
    _, files = parse_list_dir_tool_result(list_result["result"])
    print("files", files)
    available_features = []
    for prd_path in (f for f in files if f.endswith("/prd.md")):
        file_result = await execute_specific_tool(
            "read_file", {"path": prd_path}, silent_config
        )
        metadata, _ = parse_frontmatter(file_result["result"])
        available_features.append({"path": prd_path, "metadata": metadata})
    print("available_features", available_features)
    return available_features


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

async def init_step(state: State) -> Dict[str, Any]:
    return {"sandbox_id": state.get("sandbox_id")}


async def prd_generator_step(state: State, config: RunnableConfig) -> Dict[str, Any]:
    _require_sandbox_id(state, "prd_generator_step")

    model = _build_model_from_state(state)
    tools = await filtered_tools(
        state.sandbox_id,
        allowed_tools=["read_file", "create_text_file", "replace_content"],
    )

    
    available_features = await _get_available_features_metadata(config)
    print("available_features", available_features)
    system_message = PromptTemplate.from_template(PRD_GENERATOR_PROMPT).format(
        available_feature=json.dumps(available_features),
    )
    print("system_message", system_message[:50])
    agent = create_agent(model=model, system_prompt=system_message, tools=tools)
    result = await agent.ainvoke({"messages": state["messages"]}, config)
    messages = result["messages"]

    
    prd_path = None
    for msg in reversed(messages):
        if isinstance(msg, ToolMessage) and msg.name == "assign_prd_saving_completed":
            prd_path = msg.artifact.get("relative_path") if hasattr(msg, "artifact") else None
            break
    print("prd_path", prd_path)
    return {"messages": messages, "prd_path": prd_path}


async def tech_stack_expander_step(state: State, config: RunnableConfig) -> Dict[str, Any]:
    silent_config = copilotkit_customize_config(
        config, emit_messages=False, emit_tool_calls=False
    )

    packages = await execute_specific_tool(
        "read_file", {"relative_path": "package.json"}, silent_config
    )
    print("packages", packages)
    feature_prd = await execute_specific_tool(
        "read_file", {"relative_path": state.get("prd_path")}, silent_config
    )
    print("feature_prd", feature_prd)
    tech_stack = await execute_specific_tool(
        "read_file",
        {"relative_path": ".actovator/features/tech_stack.json"},
        silent_config,
    )
    print("tech_stack", tech_stack)

    model = _build_model_from_state(state)
    tools = await filtered_tools(
        state.sandbox_id,
        allowed_tools=["replace_content"],
    )
    system_message = PromptTemplate.from_template(TECH_STACK_PROMPT).format(
        tech_stack=tech_stack["result"],
        prd=feature_prd["result"],
        packages=packages["result"],
    )
    agent = create_agent(model=model, system_prompt=system_message, tools=tools)
    result = await agent.ainvoke({"messages": state["messages"]}, config)
    return {"messages": result["messages"]}


async def user_stories_generator_step(state: State, config: RunnableConfig) -> Dict[str, Any]:
    silent_config = copilotkit_customize_config(
        config, emit_messages=False, emit_tool_calls=False
    )
    prd_file_path = state.get("prd_path")
    prd_content = await execute_specific_tool(
        "read_file", {"relative_path": prd_file_path}, silent_config
    )
    print("prd_content", prd_content)
    tech_stack = await execute_specific_tool(
        "read_file",
        {"relative_path": ".actovator/features/tech_stack.json"},
        silent_config,
    )
    print("tech_stack", tech_stack)

    model = _build_model_from_state(state)
    tools = await filtered_tools(
        state.sandbox_id,
        allowed_tools=["read_file", "create_text_file", "replace_content"],
    )
    system_message = PromptTemplate.from_template(USER_STORIES_GENERATOR_PROMPT).format(
        prd=prd_content,
        tech_stack=tech_stack,
        prd_file_path=prd_file_path,
    )
    agent = create_agent(model=model, system_prompt=system_message, tools=tools)
    result = await agent.ainvoke({"messages": state["messages"]}, config)
    return {"messages": result["messages"]}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def route_to(state: State) -> Literal["tech_stack_expander_step", "__end__"]:
    # FIX: `is_instance` → `isinstance`, added missing `:`, fixed attribute access
    last_msg = state.get("messages", [])[-1] if state.get("messages") else None
    if isinstance(last_msg, ToolMessage) and last_msg.name == "assign_prd_saving_completed":
        return "tech_stack_expander_step"
    return END


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

coding_workflow = StateGraph(State)

coding_workflow.add_node("init_step", init_step)
coding_workflow.add_node("prd_generator_step", prd_generator_step)
# coding_workflow.add_node("tech_stack_expander_step", tech_stack_expander_step)  # FIX: uncommented to match routing

coding_workflow.add_edge(START, "init_step")
coding_workflow.add_edge("init_step", "prd_generator_step")
# coding_workflow.add_conditional_edges("prd_generator_step", route_to)  # FIX: removed duplicate unconditional edge
# coding_workflow.add_edge("tech_stack_expander_step", END)

coding_graph = coding_workflow.compile(checkpointer=InMemorySaver())