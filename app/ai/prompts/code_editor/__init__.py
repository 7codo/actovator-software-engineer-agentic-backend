from app.utils.files_utils import read_file_from_init

CODE_EDITOR_PROMPT: str = read_file_from_init("PROMPT.md", "app.ai.prompts.code_editor")

__all__ = ["CODE_EDITOR_PROMPT"]
