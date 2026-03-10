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
    PlANNER_PROMPT,
)
from app.constants import PROJECT_PATH
from app.ai.utils import build_model
from app.constants import DEFAULT_MODEL_ID, DEFAULT_MODEL_PROVIDER
from langchain_core.prompts import PromptTemplate
from langchain_core.messages import SystemMessage


class State(MessagesState):
    sandbox_id: str | None
    model_provider: str | None
    model_id: str | None


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
    )

    result = await research_agent.ainvoke({"messages": messages})

    return {"messages": result["messages"], "sandbox_id": tools_result.sandbox_id}


async def planner_step(state: State):
    messages = state.get("messages")
    model = _build_model_from_state(state)
    response = await model.ainvoke([SystemMessage(content=PlANNER_PROMPT), *messages])
    return {"messages": [*messages, response], "sandbox_id": state.get("sandbox_id")}


async def mini_docs_creation_step(state: State):
    messages = state.get("messages")
    sandbox_id = state.get("sandbox_id")

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
    )

    result = await docs_agent.ainvoke({"messages": messages})

    return {"messages": result["messages"], "sandbox_id": sandbox_id}


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
