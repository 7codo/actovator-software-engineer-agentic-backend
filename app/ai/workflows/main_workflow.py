import os
import httpx
from typing import Literal

from tavily import TavilyClient
from deepagents import create_deep_agent

from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import MessagesState
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableConfig

from your_module import (          # adjust to your actual imports
    build_model,
    build_sandbox_tools,
    get_host_url,
    COIL_FRAMEWORK_PROMPT,
    DEFAULT_MODEL_PROVIDER,
    DEFAULT_MODEL_ID,
)


# ── State ──────────────────────────────────────────────────────────────────────

class State(MessagesState):
    sandbox_id: str
    model_provider: str | None = None
    model_id: str | None = None


# ── Main node ──────────────────────────────────────────────────────────────────

async def main_node(state: State, config: RunnableConfig) -> dict:
    model_provider = state.get("model_provider", DEFAULT_MODEL_PROVIDER)
    model_id       = state.get("model_id",       DEFAULT_MODEL_ID)
    sandbox_id     = state["sandbox_id"]

    # Build model & sandbox tools
    model         = build_model(provider=model_provider, model_id=model_id)
    sandbox_tools = build_sandbox_tools(sandbox_id)

    # Discover available API tools from the sidecar service
    tools_api_base_url = await get_host_url["get_host_url"](8000)
    ALLOWED_TOOLS = ["list_dir", "read_file"]
    EXCLUDED_TOOLS = []
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{tools_api_base_url}/tools", timeout=10.0)
        resp.raise_for_status()
        available_api_tools: list[dict] = resp.json()

    if ALLOWED_TOOLS and len(ALLOWED_TOOLS) > 0:
        available_api_tools = [t for t in available_api_tools if t["name"] in ALLOWED_TOOLS]
    if EXCLUDED_TOOLS and len(EXCLUDED_TOOLS) > 0:
        available_api_tools = [t for t in available_api_tools if t["name"] not in EXCLUDED_TOOLS]   # adjust shape as needed

    # Build system prompt
    system_message = PromptTemplate.from_template(COIL_FRAMEWORK_PROMPT).format(
        available_api_tools=available_api_tools,
        available_bash_commands="No bash commands related to this task",
    )

    # Create and invoke the deep agent
    agent  = create_deep_agent(
        model=model,
        system_prompt=system_message,
        tools=[sandbox_tools["create_run_bash_script"]],
    )
    result = await agent.ainvoke(
        {"messages": state.get("messages", [])},
        config=config,
    )

    return {"messages": result["messages"]}


# ── Graph ──────────────────────────────────────────────────────────────────────

main_workflow = StateGraph(State)
main_workflow.add_node("main_node", main_node)
main_workflow.add_edge(START, "main_node")
main_workflow.add_edge("main_node", END)

checkpointer  = InMemorySaver()
coding_graph  = main_workflow.compile(checkpointer=checkpointer)