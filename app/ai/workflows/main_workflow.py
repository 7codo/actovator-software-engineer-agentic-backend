import httpx
from typing import Optional

from deepagents import create_deep_agent
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableConfig
from langchain.agents import create_agent
from app.ai.prompts import COIL_FRAMEWORK_PROMPT
from app.ai.tools.sandbox_tools import build_sandbox_tools
from app.ai.utils import build_model
from app.constants import DEFAULT_MODEL_PROVIDER, DEFAULT_MODEL_ID


async def get_available_api_tools(
    tools_api_base_url: str,
    allowed_tools: Optional[list[str]] = None,
    excluded_tools: Optional[list[str]] = None,
) -> list[dict]:
    """
    Fetch and filter available API tools from the given base URL.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{tools_api_base_url}/tools", timeout=10.0)
        resp.raise_for_status()
        available_api_tools: list[dict] = resp.json()
    if allowed_tools:
        available_api_tools = [
            t for t in available_api_tools if t["name"] in allowed_tools
        ]
    if excluded_tools:
        available_api_tools = [
            t for t in available_api_tools if t["name"] not in excluded_tools
        ]
    return available_api_tools


class State(MessagesState):
    sandbox_id: str
    model_provider: Optional[str] = None
    model_id: Optional[str] = None


def _require_sandbox_id(state: State) -> str:
    sandbox_id = state.get("sandbox_id")
    if not sandbox_id:
        raise ValueError("sandbox_id is required!")
    return sandbox_id


async def main_node(state: State, config: RunnableConfig) -> dict:

    sandbox_id = _require_sandbox_id(state, "mini_docs_creation_step")
    model_provider = state.get("model_provider") or DEFAULT_MODEL_PROVIDER
    model_id = state.get("model_id") or DEFAULT_MODEL_ID

    model = build_model(provider=model_provider, model_id=model_id)
    sandbox_tools = build_sandbox_tools(sandbox_id)

    # Get the sandbox tool API base URL and fetch available API tools.
    tools_api_base_url = await sandbox_tools["get_host_url"](8000)
    print("tools_api_base_url", tools_api_base_url)
    available_api_tools = await get_available_api_tools(
        tools_api_base_url["url"], allowed_tools=["read_file", "list_file"]
    )

    # Compose system prompt.
    print("available_api_tools", available_api_tools)

    def dump_tools(tools):
        import json

        # Escape { and } so PromptTemplate doesn't treat them as variables
        return json.dumps(tools, indent=2)

    print("dump_tools(available_api_tools)", dump_tools(available_api_tools))
    system_message = PromptTemplate.from_template(COIL_FRAMEWORK_PROMPT).format(
        available_api_tools=dump_tools(available_api_tools),
        available_bash_commands="No bash commands related to this task",
    )
    print("system_message", system_message[:5])
    # Create and run deep agent
    agent = create_agent(
        model=model,
        system_prompt=system_message,
        tools=[sandbox_tools["create_run_bash_script"]],
    )
    result = await agent.ainvoke(
        {"messages": state.get("messages", [])},
        config=config,
    )

    return {"messages": result["messages"]}


# Compose workflow graph
main_workflow = StateGraph(State)
main_workflow.add_node("main_node", main_node)
main_workflow.add_edge(START, "main_node")
main_workflow.add_edge("main_node", END)
checkpointer = InMemorySaver()
main_graph = main_workflow.compile(checkpointer=checkpointer)
