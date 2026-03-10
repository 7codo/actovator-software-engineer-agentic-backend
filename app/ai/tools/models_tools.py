from langchain.tools import tool
from langchain_core.messages import SystemMessage, HumanMessage
from pydantic import BaseModel
from app.ai.utils import build_model
# from app.core.config import settings # for testing


class PackageVersion(BaseModel):
    version: str


@tool
async def get_coding_agent_known_package_version(package: str) -> str:
    """Query the agent's training knowledge for the last fully known version of a package.

    Args:
        package: The name of the package to query (e.g. "next", "react").

    Returns:
        The last known version string in x.x.x format.
    """
    model = build_model(provider="google_genai", model_id="gemini-3-pro-preview")
    response = await model.with_structured_output(PackageVersion).ainvoke(
        [
            SystemMessage(content="You are a senior software engineer."),
            HumanMessage(
                content=(
                    f"What is the last version of {package} you have full, reliable knowledge of? "
                    "Return only the version string in x.x.x format."
                )
            ),
        ]
    )
    return response.version


if __name__ == "__main__":
    print(get_coding_agent_known_package_version("react"))
