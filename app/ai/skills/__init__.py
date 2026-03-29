from app.utils.files_utils import read_file_from_init


CODE_EDITING_TOOLS_SKILL: str = read_file_from_init(
    "code_editing_tools/SKILL.md", "app.ai.skills"
)

__all__ = [
    "CODE_EDITING_TOOLS_SKILL",
]
