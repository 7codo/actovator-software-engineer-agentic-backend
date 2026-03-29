from langchain.tools import tool

from app.ai.skills.agent_browser import AGENT_BROWSER_COMMANDS_REF, AGENT_BROWSER_SKILL


@tool
def load_agent_browser_commands_ref() -> str:
    """
    Loads and returns documentation for agent-browser commands.
    Use this tool when you need comprehensive access to available commands and their usage.
    """
    return AGENT_BROWSER_COMMANDS_REF


@tool
def get_agent_browser_skill() -> str:
    """
    Use this tool before you start executing agent-browser commands.
    Returns:
        The agent-browser CLI guide needed to perform browser testing.
    """
    return AGENT_BROWSER_SKILL
