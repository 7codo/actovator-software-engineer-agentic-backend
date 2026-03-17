from app.utils.files_utils import read_file_from_init


AGENT_BROWSER_SKILL: str = read_file_from_init("agent_browser/SKILL.md", "app.ai.skills")
SERENA_TOOLS_USAGE_SKILL: str = read_file_from_init("serena_tools_usage/SKILL.md", "app.ai.skills")
AGENT_BROWSER_COMMANDS_REF: str = read_file_from_init("agent_browser/references/commands.md", "app.ai.skills")

__all__ = [
    "AGENT_BROWSER_SKILL",
    "AGENT_BROWSER_COMMANDS_REF",
]