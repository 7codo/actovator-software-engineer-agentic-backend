from langchain.tools import tool

from app.ai.skills import AGENT_BROWSER_COMMANDS_REF


@tool
def load_agent_browser_commands_ref() -> str:
    """
    Loads and returns documentation for agent-browser commands.
    Use this tool when you need comprehensive access to available commands and their usage.
    """
    return AGENT_BROWSER_COMMANDS_REF