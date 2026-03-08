from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field, Literal

from app.ai.tools.files_tools import load_agent_browser_commands_ref
from app.ai.tools.mcp_tools import filtered_tools
from app.ai.prompts import E2E_TESTING_PROMPT, TESTING_PROMPT
from langchain.agents import create_agent
from app.ai.tools.sandbox_tools import build_sandbox_tools
from langchain.chat_models import init_chat_model
from app.constants import CDP_PORT


class Issue(BaseModel):
    source: Literal["lint", "server_logs", "browser_console"]  # noqa: F821
    severity: Literal["error", "warning", "info"]  # noqa: F821
    description: str
    recommendation: str


class Report(BaseModel):
    summary: str = Field(
        description="2–4 sentence plain-English overview of environment health."
    )
    issues: list[Issue] = Field(default_factory=list)
    skipped_steps: list[str] = Field(
        default_factory=list, description="Step name and reason, if any were skipped."
    )


class QAReport(BaseModel):
    """Structured output for the testing agent."""

    report: Report
    route_to_e2e_testing_agent: bool = Field(
        description="True if frontend changes detected (Next.js pages, UI components, etc.), False for backend-only changes."
    )


class State(MessagesState):
    sandbox_id: str | None


def _get_sandbox_id(state: State) -> str:
    sandbox_id = state.get("sandbox_id")
    if not sandbox_id:
        raise ValueError("sandbox_id is required in state.")
    return sandbox_id


def _build_model():
    return init_chat_model("google_genai:gemini-3-flash-preview")


async def testing_step(state: State, config: RunnableConfig) -> dict:
    messages = state.get("messages")
    sandbox_id = _get_sandbox_id(state)
    sandbox_tools = build_sandbox_tools(sandbox_id)

    agent = create_agent(
        model=_build_model(),
        tools=[
            sandbox_tools["get_lint_checks"],
            sandbox_tools["get_server_logs"],
            sandbox_tools["run_agent_browser_command"],
        ],
        system_prompt=TESTING_PROMPT,
        response_format=QAReport,
    )

    result = await agent.ainvoke({"messages": messages}, config)
    new_messages = result["messages"]  # it holds the report output as a tool message

    return {
        "messages": new_messages,
        "sandbox_id": sandbox_id,
    }


async def e2e_testing_step(state: State, config: RunnableConfig) -> dict:
    messages = state.get("messages")
    sandbox_id = _get_sandbox_id(state)
    sandbox_tools = build_sandbox_tools(sandbox_id)

    await sandbox_tools["execute_shell_command"](
        f"/root/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome "
        f"--remote-debugging-port={CDP_PORT} --no-sandbox --disable-gpu --headless=new",
        user="root",
        background=True,
    )
    await sandbox_tools["execute_shell_command"](
        f"agent-browser connect {CDP_PORT}", user="root"
    )
    agent = create_agent(
        model=_build_model(),
        tools=[
            sandbox_tools["run_agent_browser_command"],
            sandbox_tools["run_browser_agent_bash_script"],
            load_agent_browser_commands_ref,
        ],
        system_prompt=E2E_TESTING_PROMPT,
    )
    print("Running E2E testing agent...")
    print("input messages", messages)
    result = await agent.ainvoke({"messages": messages}, config)
    print("E2E Testing report:", result)
    await sandbox_tools["execute_shell_command"](
        "pkill -f 'chrome-linux64/chrome' && agent-browser close", user="root"
    )

    return {"messages": result["messages"], "sandbox_id": sandbox_id}


def route_after_testing(state: State) -> str:
    return "e2e_testing_step" if state.get("route_to_e2e") else END


# Graph definition
testing_workflow = StateGraph(State)
testing_workflow.add_node(testing_step)
testing_workflow.add_node(e2e_testing_step)

testing_workflow.add_edge(START, "testing_step")
testing_workflow.add_conditional_edges("testing_step", route_after_testing)
testing_workflow.add_edge("e2e_testing_step", END)

testing_graph = testing_workflow.compile(checkpointer=InMemorySaver())
