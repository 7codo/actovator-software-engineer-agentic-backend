from ag_ui_langgraph.agent import HumanMessage
import httpx
import json
from typing import Optional, List, Tuple
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.checkpoint.memory import InMemorySaver
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableConfig
from langchain.agents import create_agent
from app.ai.llm.models import build_model_from_state
from app.ai.prompts import CODE_EDITOR_PROMPT, CODE_EDITOR_VERIFICATION_PROMPT
from app.ai.tools.sandbox_tools import build_sandbox_tools
from app.ai.skills import CODE_EDITING_TOOLS_SKILL
from app.ai.tools.workflow_tools import start_verification_process
from app.utils.files_utils import build_skills_index
from langchain.tools import tool
from enum import Enum
from typing import Literal
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------


class CheckResult(str, Enum):
    PASS = "✓"
    FAIL = "✗"


class PassCheck(BaseModel):
    """A single check row inside a PASS report."""

    number: int = Field(..., ge=1, description="Row index, 1-based.")
    claim: str = Field(
        ..., description="Claim extracted verbatim from execution_result."
    )
    tool_used: str = Field(
        ..., description="Name of the tool used to verify the claim."
    )
    observed_value: str = Field(
        ..., description="Exact value observed in the tool output."
    )
    result: Literal[CheckResult.PASS] = CheckResult.PASS


class FailCheck(BaseModel):
    """A single check row inside a FAIL report (may pass or fail individually)."""

    number: int = Field(..., ge=1, description="Row index, 1-based.")
    claim: str = Field(
        ..., description="Claim extracted verbatim from execution_result."
    )
    tool_used: str = Field(
        ..., description="Name of the tool used to verify the claim."
    )
    observed_value: str = Field(
        ..., description="Exact value observed in the tool output."
    )
    expected_value: str = Field(
        ..., description="Expected value derived from execution_result."
    )
    result: CheckResult = Field(..., description="✓ if values match, ✗ otherwise.")


class Failure(BaseModel):
    """Detailed breakdown for a single failing check."""

    check_number: int = Field(..., ge=1)
    what_was_claimed: str = Field(..., description="The claim from execution_result.")
    what_was_observed: str = Field(
        ..., description="Quoted value from the tool output."
    )
    discrepancy: str = Field(..., description="Exact difference — no speculation.")


class UnverifiableCheck(BaseModel):
    """A check that could not be completed."""

    check_number: int = Field(..., ge=1)
    reason: str = Field(..., description="Why the check could not be completed.")


# ---------------------------------------------------------------------------
# Report variants
# ---------------------------------------------------------------------------


class PassReport(BaseModel):
    """Verification report when every check passes."""

    status: Literal["PASS"] = "PASS"
    checks: list[PassCheck] = Field(..., min_length=1)
    summary: str = Field(
        ...,
        description=(
            "Human-readable summary, e.g. "
            "'All 3 checks passed. The observed state matches the expected state.'"
        ),
    )


class FailReport(BaseModel):
    """Verification report when one or more checks fail."""

    status: Literal["FAIL"] = "FAIL"
    checks: list[FailCheck] = Field(..., min_length=1)
    failures: list[Failure] = Field(
        default_factory=list,
        description="Detailed breakdown for every check whose result is ✗.",
    )
    unverifiable_checks: list[UnverifiableCheck] = Field(
        default_factory=list,
        description="Checks that could not be completed at all.",
    )
    summary: str = Field(
        ...,
        description=(
            "Human-readable summary, e.g. "
            "'2 of 4 checks passed. 2 failed. Do not re-run until discrepancies are resolved.'"
        ),
    )


VerificationReport = PassReport | FailReport


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class State(MessagesState):
    sandbox_id: str
    model_provider: Optional[str] = None
    model_id: Optional[str] = None
    user_task: str | None = None
    execution_result: str | None = None
    verification_report: VerificationReport | None = None


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
    skill_by_name, _ = build_skills_index([CODE_EDITING_TOOLS_SKILL])
    api_tools_catalog = await get_available_api_tools(
        tools_api_base_url, excluded_tools=["execute_shell_command"]
    )
    api_tools_catalog_lite = [
        {"name": t["name"], "description": t["description"]} for t in api_tools_catalog
    ]

    formatted_prompt = PromptTemplate.from_template(system_prompt_template).format(
        api_tools_catalog=json.dumps(api_tools_catalog_lite, indent=2),
        api_tools_guidance=skill_by_name.get("code_editing_tools"),
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
        for tool in api_tools_catalog:  # noqa: F402
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

    model = build_model_from_state(state)

    sandbox_tools = build_sandbox_tools(sandbox_id)
    tools_api_base_url = (await sandbox_tools["get_host_url"](8000))["url"]

    system_msg, api_tools_catalog = await _wire_system_prompt(
        CODE_EDITOR_PROMPT, tools_api_base_url
    )
    get_params_tool = _build_get_tool_params_tool(api_tools_catalog)

    agent = create_agent(
        model=model,
        system_prompt=system_msg,
        tools=[
            sandbox_tools["execute_tool"],
            get_params_tool,
            start_verification_process,
        ],
    )

    previous_report = state.get("verification_report")
    user_task = state.get("user_task")
    if previous_report is not None:
        messages = [
            HumanMessage(
                content=(
                    f"User Task:\n{user_task}\n\n"
                    f"Previous Verification Report (failed attempt):\n{previous_report}\n\n"
                    "Please fix the discrepancies and then provide a fresh execution log for verification."
                )
            )
        ]
    else:
        messages = state.get("messages", [])
    result = await agent.ainvoke({"messages": messages}, config=config)
    return {
        "messages": result["messages"],
        "verification_report": None,
        "user_task": None,
    }


async def verification_node(state: State, config: RunnableConfig) -> dict:
    sandbox_id = _require_sandbox_id(state)

    model = build_model_from_state(state)

    sandbox_tools = build_sandbox_tools(sandbox_id)
    tools_api_base_url = (await sandbox_tools["get_host_url"](8000))["url"]

    api_tools_catalog = await get_available_api_tools(
        tools_api_base_url,
        allowed_tools=[
            "list_dir",
            "find_file",
            "search_for_pattern",
            "get_symbols_overview",
            "find_symbol",
            "find_referencing_symbols",
            "execute_shell_command",
        ],
    )
    api_tools_catalog_lite = [
        {"name": t["name"], "description": t["description"]} for t in api_tools_catalog
    ]
    formatted_prompt = PromptTemplate.from_template(
        CODE_EDITOR_VERIFICATION_PROMPT
    ).format(
        api_tools_catalog=json.dumps(api_tools_catalog_lite, indent=2),
    )
    get_params_tool = _build_get_tool_params_tool(api_tools_catalog)

    agent = create_agent(
        model=model,
        system_prompt=formatted_prompt,
        tools=[
            sandbox_tools["execute_tool"],
            get_params_tool,
        ],
        response_format=VerificationReport,
    )

    result = await agent.ainvoke(
        {
            "messages": [
                HumanMessage(
                    f"Execution Result: {state.get('execution_result')}\nUser Task: {state.get('user_task')}"
                )
            ]
        },
        config=config,
    )
    verification_report = result["structured_response"]
    return {"messages": result["messages"], "verification_report": verification_report}


# ---------------------------------------------------------------------------
# Workflow Setup
# ---------------------------------------------------------------------------


def route_after_verification_step(state: State):
    if isinstance(state.get("verification_report"), PassReport):
        return END
    elif isinstance(state.get("verification_report"), FailReport):
        return "main_node"


coding_workflow = StateGraph(State)
coding_workflow.add_node("main_node", main_node)
coding_workflow.add_node("verification_node", verification_node)
coding_workflow.add_edge(START, "main_node")
coding_workflow.add_conditional_edge("verification_node", route_after_verification_step)

checkpointer = InMemorySaver()
coding_graph = coding_workflow.compile(checkpointer=checkpointer)
