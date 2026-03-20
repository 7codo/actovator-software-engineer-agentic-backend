# import app.utils.make_json_safe_patch
from ag_ui_langgraph import add_langgraph_fastapi_endpoint
from copilotkit import LangGraphAGUIAgent
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.ai.workflows import coding_graph

from app.api.v1.routers import sandbox_router


app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*.cloudworkstations.dev"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


add_langgraph_fastapi_endpoint(
    app=app,
    agent=LangGraphAGUIAgent(
        name="coding_agent",
        description="",
        graph=coding_graph,
        config={
            "recursion_limit": 100,
        },
    ),
    path="/coding",
)

app.include_router(sandbox_router)


def main():
    """Run the uvicorn server."""
    import uvicorn

    uvicorn.run(
        "main:app",
        host="localhost",
        port=8000,
        reload=True,
    )


if __name__ == "__main__":
    main()
