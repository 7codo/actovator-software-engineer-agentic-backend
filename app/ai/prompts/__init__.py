from app.utils.files_utils import read_file_from_init

CODE_EDITOR_PROMPT: str = read_file_from_init("code_editor_prompt.md", "app.ai.prompts")
CODE_EDITOR_VERIFICATION_PROMPT: str = read_file_from_init(
    "code_editor_verification_prompt.md", "app.ai.prompts"
)


__all__ = ["CODE_EDITOR_PROMPT", "CODE_EDITOR_VERIFICATION_PROMPT"]
