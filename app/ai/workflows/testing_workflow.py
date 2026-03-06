import uuid

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph

from app.ai.tools.mcp_tools import filtered_tools
from app.ai.llm.models import (
    gemini_3_flash_preview,
)
from app.ai.prompts import TESTING_PROMPT
from langchain.agents import create_agent
from app.ai.tools.sandbox_tools import build_sandbox_tools
from langchain_core.messages import HumanMessage, AIMessage


class State(MessagesState):
    sandbox_id: str | None


async def testing_step(state: State):
    messages = state.get("messages")
    sandboxId = state.get("sandbox_id")
    if not sandboxId:
        raise Exception("The sandbox id is required!")
    tools_result = await filtered_tools(
        sandboxId, allowed_tools=["execute_shell_command"]
    )
    sandbox_tools = build_sandbox_tools(sandboxId)
    # lint_checks = await sandbox_tools["get_lint_checks"]()
    # server_logs = await sandbox_tools["get_server_logs"]()

    # lint_and_server_logs = (
    #     f"Lint Checks:\n{lint_checks}\---\nnDev Server Logs:\n{server_logs}"
    # )

    agent = create_agent(
        model=gemini_3_flash_preview,
        system_prompt=TESTING_PROMPT,
        tools=[
            sandbox_tools["agent_browser"],
            sandbox_tools["get_lint_checks"],
            sandbox_tools["get_server_logs"],
        ],
    )
    result = await agent.ainvoke({"messages": messages})
    messages = result["messages"]

    return {"messages": messages, "sandbox_id": tools_result.sandbox_id}


testing_workflow = StateGraph(State)
testing_workflow.add_node(testing_step)

testing_workflow.add_edge(START, "testing_step")
testing_workflow.add_edge("testing_step", END)

checkpointer = InMemorySaver()

config = {"configurable": {"thread_id": str(uuid.uuid4())}}
testing_graph = testing_workflow.compile(checkpointer=checkpointer)
