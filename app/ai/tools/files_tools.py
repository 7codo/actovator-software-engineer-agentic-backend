from langchain.tools import tool

from app.ai.skills import AGENT_BROWSER_COMMANDS_REF, SERENA_TOOLS_USAGE_SKILL
from app.utils.files_utils import build_skills_index


@tool
def load_agent_browser_commands_ref() -> str:
    """
    Loads and returns documentation for agent-browser commands.
    Use this tool when you need comprehensive access to available commands and their usage.
    """
    return AGENT_BROWSER_COMMANDS_REF


skills_files = [SERENA_TOOLS_USAGE_SKILL]
skills_by_name = build_skills_index(skills_files)


@tool
def load_skill(name: str) -> str:
    """
    Loads and returns specific skill
    Use this tool when you want to read a skill
    """
    body = skills_by_name.get(name)
    return body or "No skills found with this name"
