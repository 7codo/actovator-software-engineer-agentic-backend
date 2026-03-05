import uuid

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
# from app.ai.agents.coding_agents import build_coding_agent, build_testing_agent
from app.ai.tools.mcp_tools import get_or_create_tools
from app.ai.llm.models import gemini_3_pro, gemini_flash_latest, minimax_m2_5, gemini_3_flash_preview
from app.ai.prompts import CODING_PROMPT
from langchain.agents import create_agent

class State(MessagesState):
    sandbox_id: str | None



async def coding_step(state: State):
    messages = state.get("messages")
    sandboxId = state.get("sandbox_id")
    print("sandboxId", sandboxId)
    if not sandboxId:
        raise Exception("The sandbox id is required!")
    tools_result = await get_or_create_tools(sandboxId)
    agent = create_agent(
        model=gemini_3_flash_preview,
        system_prompt=CODING_PROMPT,
        tools=tools_result.tools,
    )
    result = await agent.ainvoke({"messages": messages})
    messages = result["messages"]

    return {"messages": messages, "sandbox_id": tools_result.sandbox_id}


# async def testing_step(state: State):
#     messages = state.get("messages")
#     agent = await build_testing_agent()
#     result = await agent.ainvoke({"messages": messages})
#     messages = result["messages"]
#     return {"messages": messages}


coding_workflow = StateGraph(State)
coding_workflow.add_node(coding_step)
# coding_workflow.add_node(testing_step)

coding_workflow.add_edge(START, "coding_step")
# coding_workflow.add_edge("coding_step", "testing_step")
# coding_workflow.add_edge("testing_step", END)
coding_workflow.add_edge("coding_step", END)
# workflow.add_edge("vision_process", END)

checkpointer = InMemorySaver()

config = {"configurable": {"thread_id": str(uuid.uuid4())}}
coding_graph = coding_workflow.compile(checkpointer=checkpointer)


# if __name__ == "__main__":
#     import asyncio
#     from app.utils.dev_utils import interactive_graph
# from app.utils.dev_utils import interactive_graph

#     # result = asyncio.run(sitemap_retriever("https://vercel.com", "vercel-blob|storage"))
#     # print(len(result))
#     asyncio.run(interactive_graph(graph))

#     # asyncio.run(interactive_graph(graph,is_use_google_models=True))
#     # user stories: .serena\architecture\file-upload-system\prd.json