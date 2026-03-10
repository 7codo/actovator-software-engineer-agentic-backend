import app.utils.make_json_safe_patch
import sys
from ag_ui_langgraph import add_langgraph_fastapi_endpoint
from copilotkit import LangGraphAGUIAgent
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver # Commented out if not used directly here
from app.ai.workflows import coding_graph, testing_graph
from app.api.v1.routers import sandbox_router
from logging import Logger
from sensai.util import logging
from app.constants import LOG_FORMAT

# Logger.root.setLevel(logging.INFO)
# formatter = logging.Formatter(LOG_FORMAT)

# stderr_handler = logging.StreamHandler(stream=sys.stderr)
# stderr_handler.formatter = formatter
# Logger.root.addHandler(stderr_handler)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://verbose-lamp-7r699jpwq692r7qj-3000.app.github.dev"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# add_langgraph_fastapi_endpoint(
#     app=app,
#     agent=LangGraphAGUIAgent(
#         name="assistant_agent",
#         description="",
#         graph=assistant_graph,
#     ),
#     path="/assistant",
# )

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

# add_langgraph_fastapi_endpoint(
#     app=app,
#     agent=LangGraphAGUIAgent(
#         name="architecture_agent",
#         description="",
#         graph=architecture_graph,
#     ),
#     path="/architecture",
# )

# app.include_router(codebase_router)
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
