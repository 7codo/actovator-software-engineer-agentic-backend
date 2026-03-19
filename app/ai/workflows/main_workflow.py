import httpx
import json
from typing import Optional, List, Tuple
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableConfig
from langchain.agents import create_agent
from app.ai.prompts import COIL_FRAMEWORK_PROMPT
from app.ai.tools.sandbox_tools import build_sandbox_tools
from app.ai.utils import build_model
from app.constants import DEFAULT_MODEL_PROVIDER, DEFAULT_MODEL_ID
from app.ai.skills import SERENA_TOOLS_USAGE_SKILL
from app.utils.files_utils import build_skills_index
from langchain.tools import tool

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class State(MessagesState):
    sandbox_id: str
    model_provider: Optional[str] = None
    model_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def get_available_api_tools(
    tools_api_base_url: str,
    allowed_tools: Optional[List[str]] = None,
    excluded_tools: Optional[List[str]] = None,
) -> List[dict]:
    """
    Fetches the available tools from the tools API, filtered by allowed and excluded lists.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{tools_api_base_url}/tools", timeout=10.0)
        resp.raise_for_status()
        tools = resp.json()

    if allowed_tools:
        tools = [t for t in tools if t["name"] in allowed_tools]
    if excluded_tools:
        tools = [t for t in tools if t["name"] not in excluded_tools]
    return tools


async def _wire_system_prompt(
    system_prompt_template: str, tools_api_base_url: str
) -> Tuple[str, List[dict]]:
    """
    Prepare the system prompt containing tool catalog (name+description), plus tool guidance.
    Returns the formatted prompt string and the full catalog for later use.
    """
    skill_by_name, _ = build_skills_index([SERENA_TOOLS_USAGE_SKILL])
    api_tools_catalog = await get_available_api_tools(
        tools_api_base_url, excluded_tools=["execute_shell_command"]
    )
    api_tools_catalog_lite = [
        {"name": t["name"], "description": t["description"]} for t in api_tools_catalog
    ]

    formatted_prompt = PromptTemplate.from_template(system_prompt_template).format(
        api_tools_catalog=json.dumps(api_tools_catalog_lite, indent=2),
        api_tools_guidance=skill_by_name.get("tools_usage"),
    )
    return formatted_prompt, api_tools_catalog


def _build_get_tool_params_tool(api_tools_catalog: List[dict]):
    """
    Returns a LangChain tool to fetch API tool parameter schemas by tool name.
    """

    @tool
    def get_tool_params_by_name(tool_name: str) -> str:
        """
        Retrieves the parameter schema for a specific tool by its name.
        Use this to get the arguments definition for a tool you intend to use.

        Args:
            tool_name: The name of the tool to retrieve parameters for.

        Returns:
            A JSON string representing the tool's parameters or an error if not found.
        """
        for tool in api_tools_catalog:
            if tool["name"] == tool_name:
                return json.dumps(tool.get("parameters", {}), indent=2)
        return json.dumps({"error": "Tool not found"})

    return get_tool_params_by_name


def _require_sandbox_id(state: State) -> str:
    sandbox_id = state.get("sandbox_id")
    if not sandbox_id:
        raise ValueError("sandbox_id is required!")
    return sandbox_id


# ---------------------------------------------------------------------------
# Main Node
# ---------------------------------------------------------------------------


async def main_node(state: State, config: RunnableConfig) -> dict:
    sandbox_id = _require_sandbox_id(state)

    model_provider = state.get("model_provider") or DEFAULT_MODEL_PROVIDER
    model_id = state.get("model_id") or DEFAULT_MODEL_ID
    model = build_model(provider=model_provider, model_id=model_id)

    sandbox_tools = build_sandbox_tools(sandbox_id)
    tools_api_base_url = (await sandbox_tools["get_host_url"](8000))["url"]

    system_msg, api_tools_catalog = await _wire_system_prompt(
        COIL_FRAMEWORK_PROMPT, tools_api_base_url
    )
    get_params_tool = _build_get_tool_params_tool(api_tools_catalog)

    agent = create_agent(
        model=model,
        system_prompt=system_msg,
        tools=[
            sandbox_tools["execute_tool"],
            get_params_tool,
        ],
    )

    result = await agent.ainvoke({"messages": state.get("messages", [])}, config=config)
    return {"messages": result["messages"]}


# ---------------------------------------------------------------------------
# Workflow Setup
# ---------------------------------------------------------------------------

main_workflow = StateGraph(State)
main_workflow.add_node("main_node", main_node)
main_workflow.add_edge(START, "main_node")
main_workflow.add_edge("main_node", END)
checkpointer = InMemorySaver()
main_graph = main_workflow.compile(checkpointer=checkpointer)
