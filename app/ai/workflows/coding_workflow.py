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
)
from app.ai.utils import build_model
from app.constants import DEFAULT_MODEL_ID


class State(MessagesState):
    sandbox_id: str | None
    model_provider: str | None
    model_id: str | None


def _build_model_from_state(state: State):
    return build_model(
        provider=state.get("model_provider", DEFAULT_MODEL_ID),
        model_id=state.get("model_id", DEFAULT_MODEL_ID),
    )


async def codebase_research_step(state: State):
    messages = state.get("messages")
    sandbox_id = state.get("sandbox_id")

    if not sandbox_id:
        raise ValueError("sandbox_id is required for codebase_research_step")

    model = _build_model_from_state(state)

    tools_result = await filtered_tools(
        sandbox_id,
        allowed_tools=[
            "find_referencing_symbols",
            "find_symbol",
            "get_symbols_overview",
            "search_for_pattern",
            "find_file",
            "list_dir",
        ],
    )

    research_agent = create_agent(
        system_prompt=RESEARCH_PROMPT,
        tools=tools_result.tools,
        model=model,
    )

    result = await research_agent.ainvoke({"messages": messages})

    return {"messages": result["messages"], "sandbox_id": tools_result.sandbox_id}


async def planner_step(state: State):
    messages = state.get("messages")
    model = _build_model_from_state(state)
    response = await model.ainvoke(messages)
    return {"messages": [*messages, response], "sandbox_id": state.get("sandbox_id")}


async def mini_docs_creation_step(state: State):
    messages = state.get("messages")
    sandbox_id = state.get("sandbox_id")

    if not sandbox_id:
        raise ValueError("sandbox_id is required for mini_docs_creation_step")

    model = _build_model_from_state(state)
    sandbox_tools = build_sandbox_tools(sandbox_id)

    docs_agent = create_agent(
        system_prompt=MINIT_DOCS_CREATION_PROMPT,
        tools=[sandbox_tools["search_changelogs"]],
        model=model,
    )

    result = await docs_agent.ainvoke({"messages": messages})

    return {"messages": result["messages"], "sandbox_id": sandbox_id}




# Graph definition
coding_workflow = StateGraph(State)

coding_workflow.add_node("codebase_research_step", codebase_research_step)
coding_workflow.add_node("planner_step", planner_step)
coding_workflow.add_node("mini_docs_creation_step", mini_docs_creation_step)

coding_workflow.add_edge(START, "codebase_research_step")
coding_workflow.add_edge("codebase_research_step", "planner_step")
coding_workflow.add_edge("planner_step", END)

checkpointer = InMemorySaver()
config = {"configurable": {"thread_id": str(uuid.uuid4())}}
coding_graph = coding_workflow.compile(checkpointer=checkpointer)