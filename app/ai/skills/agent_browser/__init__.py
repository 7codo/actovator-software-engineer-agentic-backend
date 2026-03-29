from app.utils.files_utils import read_file_from_init


AGENT_BROWSER_SKILL: str = read_file_from_init(
    "SKILL.md", "app.ai.skills.agent_browser"
)

AGENT_BROWSER_COMMANDS_REF: str = read_file_from_init(
    "agent_browser/references/commands.md", "app.ai.skills.agent_browser"
)

__all__ = [
    "AGENT_BROWSER_SKILL",
    "AGENT_BROWSER_COMMANDS_REF",
]
