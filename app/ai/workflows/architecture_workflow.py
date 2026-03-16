import json
import logging
from typing import Any, Dict, Literal

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import PromptTemplate
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph

from copilotkit.langgraph import RunnableConfig, copilotkit_emit_message

from app.ai.prompts import (
    PRD_GENERATOR_PROMPT,
    TECH_STACK_PROMPT,
    USER_STORIES_GENERATOR_PROMPT,
)
from app.ai.tools.mcp_tools import execute_specific_tool, filtered_tools
from app.ai.utils import build_model
from app.constants import DEFAULT_MODEL_ID, DEFAULT_MODEL_PROVIDER
from app.utils.files_utils import parse_frontmatter, parse_list_dir_tool_result

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FEATURES_DIR = ".actovator/features"
TECH_STACK_PATH = f"{FEATURES_DIR}/tech_stack.json"
PACKAGE_JSON_PATH = "package.json"


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


def _require_sandbox_id(state: State) -> str:
    sandbox_id = state.get("sandbox_id")
    if not sandbox_id:
        raise ValueError("sandbox_id is required")
    return sandbox_id


async def _read_sandbox_file(sandbox_id: str, relative_path: str) -> str:
    result = await execute_specific_tool(
        sandbox_id, "read_file", {"relative_path": relative_path}
    )
    return result["result"]


async def _invoke_agent(
    state: State,
    config: RunnableConfig,
    system_prompt: str,
    messages: list,
    allowed_tools: list[str],
    exclude_last_human_message: bool = False,
) -> list:
    model = _build_model_from_state(state)
    built_tools = await filtered_tools(state.get("sandbox_id"), allowed_tools=allowed_tools)
    agent = create_agent(model=model, system_prompt=system_prompt, tools=built_tools.tools)
    result = await agent.ainvoke({"messages": messages}, config)
    result_messages = result["messages"]
    if exclude_last_human_message:
        last_human_idx = next(
            (i for i in range(len(result_messages) - 1, -1, -1) if isinstance(result_messages[i], HumanMessage)),
            None,
        )
        if last_human_idx is not None:
            result_messages = result_messages[:last_human_idx] + result_messages[last_human_idx + 1:]
    return result_messages


async def _get_available_features_metadata(sandbox_id: str) -> list[dict]:
    list_result = await execute_specific_tool(
        sandbox_id,
        "list_dir",
        {"relative_path": FEATURES_DIR, "recursive": True},
    )
    _, files = parse_list_dir_tool_result(list_result["result"])

    features = []
    for prd_path in (f for f in files if f.endswith("/prd.md")):
        file_result = await execute_specific_tool(
            sandbox_id, "read_file", {"relative_path": prd_path}
        )
        metadata, _ = parse_frontmatter(file_result["result"])
        features.append({"path": prd_path, "metadata": metadata})

    return features


def _extract_prd_path_from_messages(messages: list) -> str | None:
    """Return the `relative_path` arg from the last tool call in the last human scope."""
    last_human_idx = next(
        (i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)),
        None,
    )
    if last_human_idx is None:
        raise ValueError("No HumanMessage found in messages")

    for msg in reversed(messages[last_human_idx:]):
        if not isinstance(msg, AIMessage):
            continue
        for tool_call in reversed(getattr(msg, "tool_calls", []) or []):
            if "relative_path" in tool_call.get("args", {}):
                return tool_call["args"]["relative_path"]

    return None


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


async def prd_generator_step(state: State, config: RunnableConfig) -> Dict[str, Any]:
    sandbox_id = _require_sandbox_id(state)

    await copilotkit_emit_message(config, "Scanning existing features...")
    available_features = await _get_available_features_metadata(sandbox_id)
    logger.debug("available_features: %s", available_features)

    await copilotkit_emit_message(config, "Generating PRD...")

    system_prompt = PromptTemplate.from_template(PRD_GENERATOR_PROMPT).format(
        available_feature=json.dumps(available_features),
    )
    messages = await _invoke_agent(
        state,
        config,
        system_prompt=system_prompt,
        messages=state["messages"],
        allowed_tools=["read_file", "create_text_file", "replace_content"],
    )

    prd_path = _extract_prd_path_from_messages(messages)
    logger.debug("prd_path resolved to: %s", prd_path)

    if prd_path:
        await copilotkit_emit_message(config, f"PRD saved to {prd_path}")

    return {"messages": messages, "prd_path": prd_path}


async def tech_stack_expander_step(state: State, config: RunnableConfig) -> Dict[str, Any]:
    sandbox_id = _require_sandbox_id(state)
    prd_path = state.get("prd_path")

    if not prd_path:
        return {"messages": [*state["messages"], AIMessage(content="Error: Create PRD file first.")]}

    await copilotkit_emit_message(config, "Reading package.json...")
    packages = await _read_sandbox_file(sandbox_id, PACKAGE_JSON_PATH)

    await copilotkit_emit_message(config, "Reading PRD...")
    feature_prd = await _read_sandbox_file(sandbox_id, prd_path)

    await copilotkit_emit_message(config, "Reading tech stack...")
    tech_stack = await _read_sandbox_file(sandbox_id, TECH_STACK_PATH)

    await copilotkit_emit_message(config, "Analysing tech stack...")
    system_prompt = PromptTemplate.from_template(TECH_STACK_PROMPT).format(
        tech_stack=json.dumps(tech_stack),
        packages=json.dumps(packages),
    )
    messages = await _invoke_agent(
        state,
        config,
        system_prompt=system_prompt,
        messages=[HumanMessage(f"PRD Content:\n{feature_prd}")],
        allowed_tools=["replace_content"],
        exclude_last_human_message=True,
    
    )
    await copilotkit_emit_message(config, "Tech stack updated.")
    return {"messages": messages}


async def user_stories_generator_step(state: State, config: RunnableConfig) -> Dict[str, Any]:
    sandbox_id = _require_sandbox_id(state)
    prd_path = state.get("prd_path")

    if not prd_path:
        return {"messages": [*state["messages"], AIMessage(content="Error: Create PRD file first.")]}

    await copilotkit_emit_message(config, "Reading PRD...")
    prd_content = await _read_sandbox_file(sandbox_id, prd_path)

    await copilotkit_emit_message(config, "Reading tech stack...")
    tech_stack = await _read_sandbox_file(sandbox_id, TECH_STACK_PATH)

    await copilotkit_emit_message(config, "Generating user stories...")
    system_prompt = PromptTemplate.from_template(USER_STORIES_GENERATOR_PROMPT).format(
        tech_stack=tech_stack,
        prd_file_path=prd_path,
    )
    messages = await _invoke_agent(
        state,
        config,
        system_prompt=system_prompt,
        messages=[HumanMessage(f"PRD Content:\n{prd_content}")],
        allowed_tools=["read_file", "create_text_file", "replace_content"],
        exclude_last_human_message=True,
    )
    await copilotkit_emit_message(config, "User stories ready.")
    return {"messages": messages}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def route_after_prd(state: State) -> Literal["tech_stack_expander_step", "__end__"]:
    return "tech_stack_expander_step" if state.get("prd_path") else END


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

_workflow = StateGraph(State)

_workflow.add_node("prd_generator_step", prd_generator_step)
_workflow.add_node("tech_stack_expander_step", tech_stack_expander_step)
_workflow.add_node("user_stories_generator_step", user_stories_generator_step)

_workflow.add_edge(START, "prd_generator_step")
_workflow.add_conditional_edges("prd_generator_step", route_after_prd)
_workflow.add_edge("tech_stack_expander_step", "user_stories_generator_step")
_workflow.add_edge("user_stories_generator_step", END)

architecture_graph = _workflow.compile(checkpointer=InMemorySaver())