import httpx
import json
from typing import Optional, List, Tuple
from deepagents.backends.utils import create_file_data
from deepagents import create_deep_agent
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableConfig
from langchain.agents import create_agent
from app.ai.prompts import COIL_FRAMEWORK_PROMPT, COIL_VERIFICATION_PROMPT
from app.ai.tools.sandbox_tools import build_sandbox_tools
from app.ai.utils import build_model
from app.constants import DEFAULT_MODEL_PROVIDER, DEFAULT_MODEL_ID, PROJECT_PATH
from app.ai.skills import SERENA_TOOLS_USAGE_SKILL
from app.utils.files_utils import build_skills_index
from app.ai.tools.files_tools import load_skill
from langchain.tools import tool
from langchain_core.messages import SystemMessage, HumanMessage

class State(MessagesState):
    sandbox_id: str
    model_provider: Optional[str] = None
    model_id: Optional[str] = None


async def get_available_api_tools(
    tools_api_base_url: str,
    allowed_tools: Optional[List[str]] = None,
    excluded_tools: Optional[List[str]] = None,
) -> List[dict]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{tools_api_base_url}/tools", timeout=10.0)
        resp.raise_for_status()
        available_api_tools: List[dict] = resp.json()

    if allowed_tools:
        available_api_tools = [t for t in available_api_tools if t["name"] in allowed_tools]
    if excluded_tools:
        available_api_tools = [t for t in available_api_tools if t["name"] not in excluded_tools]

    return available_api_tools


async def _wire_system_prompt(system_prompt: str, tools_api_base_url: str) -> Tuple[str, List[dict]]:
    """
    Wires the system prompt with a summarized tool catalog (name and description only).
    Returns the formatted prompt and the full catalog for tool usage.
    """
    skill_by_name, _ = build_skills_index([SERENA_TOOLS_USAGE_SKILL])

    # 1. Fetch the full catalog
    api_tools_catalog = await get_available_api_tools(
        tools_api_base_url, excluded_tools=["execute_shell_command"]
    )
    
    # 2. Create a filtered version containing only name and description
    api_tools_catalog_lite = [
        {"name": t["name"], "description": t["description"]} 
        for t in api_tools_catalog
    ]

    # 3. Format prompt with the lite catalog
    formatted_prompt = PromptTemplate.from_template(system_prompt).format(
        api_tools_catalog=json.dumps(api_tools_catalog_lite, indent=2),
        tools_api_base_url=tools_api_base_url,
        api_tools_guidance=skill_by_name.get("tools_usage"),
        project_path=PROJECT_PATH,
    )
    
    # 4. Return both the prompt and the full catalog
    return formatted_prompt, api_tools_catalog


async def _build_verification_tool(model, bash_tool, system_message: str):
    @tool
    async def call_verification_expert(execution_result: str, user_task: str) -> str:
        """
        Independently verify that the claimed execution result satisfies the original task.

        Args:
            execution_result: what the executor claims it did
            user_task: the original task that was executed
        Returns:
            VERIFIED or FAILED: <reason>.
        """
        agent = create_agent(
            model=model,
            system_prompt=system_message,
            tools=[bash_tool],
        )
        response = await agent.ainvoke({
            "messages": [HumanMessage(content=f"User Task: {user_task}\nClaimed result: {execution_result}")]
        })
        return response["messages"][-1].content

    return call_verification_expert


def _build_get_tool_params_tool(api_tools_catalog: List[dict]):
    """
    Factory to create a tool that retrieves parameters for a specific tool by name.
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
                # Return only the parameters section
                return json.dumps(tool.get("parameters", {}), indent=2)
        return json.dumps({"error": "Tool not found"})

    return get_tool_params_by_name


def _require_sandbox_id(state: State) -> str:
    sandbox_id = state.get("sandbox_id")
    if not sandbox_id:
        raise ValueError("sandbox_id is required!")
    return sandbox_id


async def main_node(state: State, config: RunnableConfig) -> dict:
    sandbox_id = _require_sandbox_id(state)

    model_provider = state.get("model_provider") or DEFAULT_MODEL_PROVIDER
    model_id = state.get("model_id") or DEFAULT_MODEL_ID
    model = build_model(provider=model_provider, model_id=model_id)

    sandbox_tools = build_sandbox_tools(sandbox_id)
    tools_api_base_url: str = (await sandbox_tools["get_host_url"](8000))["url"]

    # Wire verification prompt (passing lite catalog to prompt)
    verification_system_message, verification_catalog = await _wire_system_prompt(COIL_VERIFICATION_PROMPT, tools_api_base_url)
    with open("verificatio_system_message.md", "w") as f:
        f.write(verification_system_message + "\n")
        
    verification_tool = await _build_verification_tool(
        model=model,
        bash_tool=sandbox_tools["run_bash_script"],
        system_message=verification_system_message,
    )

    # Wire main prompt (passing lite catalog to prompt, retaining full catalog)
    main_system_message, main_catalog = await _wire_system_prompt(COIL_FRAMEWORK_PROMPT, tools_api_base_url)
    with open("main_system_message.md", "w") as f:
        f.write(main_system_message + "\n")
    # raise Exception("shut")
    # Create the new tool using the full catalog
    get_params_tool = _build_get_tool_params_tool(main_catalog)

    agent = create_agent(
        model=model,
        system_prompt=main_system_message,
        tools=[
            sandbox_tools["run_bash_script"], 
            verification_tool,
            get_params_tool  # Add the new tool here
        ],
    )

    result = await agent.ainvoke(
        {"messages": state.get("messages", [])},
        config=config,
    )
    return {"messages": result["messages"]}


main_workflow = StateGraph(State)
main_workflow.add_node("main_node", main_node)
main_workflow.add_edge(START, "main_node")
main_workflow.add_edge("main_node", END)
checkpointer = InMemorySaver()
main_graph = main_workflow.compile(checkpointer=checkpointer)