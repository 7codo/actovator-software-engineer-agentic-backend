from enum import StrEnum
import uuid

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langchain.agents import create_agent
from app.ai.tools.mcp_tools import filtered_tools
from app.ai.tools.sandbox_tools import build_sandbox_tools
from app.ai.prompts import (
    EDITING_PROMPT,
    RESEARCH_PROMPT,
    MINIT_DOCS_CREATION_PROMPT,
    TESTING_PROMPT,
    PlANNER_PROMPT,FIX_PLANNER_PROMPT
)
from app.constants import PROJECT_PATH
from app.ai.utils import build_model
from app.constants import DEFAULT_MODEL_ID, DEFAULT_MODEL_PROVIDER
from langchain_core.prompts import PromptTemplate
from langchain_core.messages import SystemMessage
from pydantic import BaseModel


# --- Enums ---

class Operation(StrEnum):
    ADD = "ADD"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    CREATE = "CREATE"
    REPLACE = "REPLACE"
    MOVE = "MOVE"

class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


# --- Format A ---

class EditStep(BaseModel):
    step_number: int
    title: str
    operation: Operation
    target_path: str
    symbol: str | None = None
    symbol_type: str | None = None  # function / class / type / variable / component
    destination_path: str | None = None  # only for MOVE
    change_description: str
    depends_on: list[int]  # step numbers, empty = none
    breaking_change: bool
    breaking_change_detail: str | None = None  # e.g. "2 consumers updated in steps 3–4"


class PostExecutionChecklist(BaseModel):
    created_files_referenced: bool
    deleted_files_imports_removed: bool
    breaking_change_consumers_have_update_steps: bool
    no_step_modifies_before_create: bool
    no_circular_dependencies: bool


class EditPlan(BaseModel):
    source_report_summary: str
    total_steps: int
    estimated_risk_level: RiskLevel
    steps: list[EditStep]
    post_execution_checklist: PostExecutionChecklist


# --- Format B ---

class ValidationIssue(BaseModel):
    number: int
    section: str
    issue: str
    required_action: str


class ValidationFailureReport(BaseModel):
    issues: list[ValidationIssue]


# --- Top-level union ---

class EditPlanOutput(BaseModel):
    success: bool
    edit_plan: EditPlan | None = None
    validation_failure: ValidationFailureReport | None = None



#///////////////


class FileEntry(BaseModel):
    file_path: str
    reason: str


class Symbol(BaseModel):
    name: str
    file: str
    type: str
    referenced_by: list[str]
    breaking_change_risk: bool
    breaking_change_detail: str | None = None


class InvestigationReport(BaseModel):
    change_request_summary: str
    codebase_map: str
    files_to_modify: list[FileEntry]
    files_to_create: list[FileEntry]
    files_to_delete: list[FileEntry]
    symbol_analysis: list[Symbol]
    dependency_and_risk_flags: str
    recommended_edit_sequence: list[str]


#//////////
class PackageEntry(BaseModel):
    package_name: str
    known_version: str
    installed_version: str
    keywords: dict[str, str]  # keyword -> mini-doc


class PackageLookupResult(BaseModel):
    packages: list[PackageEntry]


class State(MessagesState):
    sandbox_id: str | None
    model_provider: str | None
    model_id: str | None
    investigation_report: InvestigationReport
    plan: EditPlanOutput
    mini_docs: PackageLookupResult


def _build_model_from_state(state: State):
    return build_model(
        provider=state.get("model_provider", DEFAULT_MODEL_PROVIDER),
        model_id=state.get("model_id", DEFAULT_MODEL_ID),
    )


async def codebase_research_step(state: State):
    messages = state.get("messages")
    sandbox_id = state.get("sandbox_id")

    if not sandbox_id:
        raise ValueError("sandbox_id is required for codebase_research_step")

    model = _build_model_from_state(state)
    template = PromptTemplate.from_template(RESEARCH_PROMPT)
    sandbox_tools = build_sandbox_tools(sandbox_id)
    packages = await sandbox_tools["read_file"](path=f"{PROJECT_PATH}/package.json")
    system_message = template.format(packages=packages)
    print("system_message", system_message)
    tools_result = await filtered_tools(
        sandbox_id,
        allowed_tools=[
            "find_referencing_symbols",
            "find_symbol",
            "get_symbols_overview",
            # "search_for_pattern",
            "find_file",
            "list_dir",
        ],
        
    )

    research_agent = create_agent(
        system_prompt=system_message,
        tools=tools_result.tools,
        model=model,
        response_format=InvestigationReport
    )

    result = await research_agent.ainvoke({"messages": messages})

    return {"investigation_report": result["structured_response"], "sandbox_id": tools_result.sandbox_id}


async def planner_step(state: State):
    messages = state.get("messages")
    investigation_report = state.get("investigation_report")
    model = _build_model_from_state(state)

    planner_input = [
        SystemMessage(content=PlANNER_PROMPT),
        SystemMessage(content=f"Investigation Report:\n{investigation_report.model_dump_json(indent=2)}"),
        *messages,
    ]

    response = await model.with_structured_output(EditPlanOutput).ainvoke(planner_input)
    return {"plan": response, "sandbox_id": state.get("sandbox_id")}


async def mini_docs_creation_step(state: State):
    messages = state.get("messages")
    sandbox_id = state.get("sandbox_id")
    investigation_report = state.get("investigation_report")
    plan = state.get("plan")

    if not sandbox_id:
        raise ValueError("sandbox_id is required for mini_docs_creation_step")

    model = build_model(
        provider="google_genai", model_id="gemini-3-pro-preview"
    )  ## use the same model the coding agent will use
    sandbox_tools = build_sandbox_tools(sandbox_id)

    docs_agent = create_agent(
        system_prompt=MINIT_DOCS_CREATION_PROMPT,
        tools=[sandbox_tools["search_changelogs"]],
        model=model,
        response_format=PackageLookupResult,
    )

    result = await docs_agent.ainvoke({
        "messages": [
            *messages,
            SystemMessage(content=f"Investigation Report:\n{investigation_report.model_dump_json(indent=2)}"),
            SystemMessage(content=f"Edit Plan:\n{plan.model_dump_json(indent=2)}"),
        ]
    })

    return {"mini_docs": result["structured_response"], "sandbox_id": sandbox_id}


async def fix_planner_step(state: State):
    messages = state.get("messages")
    plan = state.get("plan")
    mini_docs = state.get("mini_docs")
    model = _build_model_from_state(state)

    fix_planner_input = [
        SystemMessage(content=FIX_PLANNER_PROMPT),
        SystemMessage(content=f"Edit Plan:\n{plan.model_dump_json(indent=2)}"),
        SystemMessage(content=f"Mini Docs:\n{mini_docs.model_dump_json(indent=2)}"),
        *messages,
    ]

    response = await model.with_structured_output(EditPlanOutput).ainvoke(fix_planner_input)
    return {"plan": response, "sandbox_id": state.get("sandbox_id")}
# Graph definition
coding_workflow = StateGraph(State)

coding_workflow.add_node("codebase_research_step", codebase_research_step)
coding_workflow.add_node("planner_step", planner_step)
coding_workflow.add_node("mini_docs_creation_step", mini_docs_creation_step)

coding_workflow.add_edge(START, "mini_docs_creation_step")
# coding_workflow.add_edge("codebase_research_step", "planner_step")
coding_workflow.add_edge("mini_docs_creation_step", END)

checkpointer = InMemorySaver()
config = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": 100}
coding_graph = coding_workflow.compile(checkpointer=checkpointer)

# if __name__ == "__main__":
#     import asyncio

#     initial_state = State(messages=[HumanMessage(content="create a middleware")], sandbox_id="i1k8r48s8mln547h1upxa")
#     result = asyncio.run(codebase_research_step(initial_state))
#     print(result["messages"])
