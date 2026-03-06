from pathlib import Path


def _read_prompt(filename: str) -> str:
    base_dir = Path(__file__).parent
    file_path = base_dir / filename
    return file_path.read_text(encoding="utf-8")


# ASSISTANT_PROMPT: str = _read_prompt("assistant_prompt.md")
EDITING_PROMPT: str = _read_prompt("editing_prompt.md")
TESTING_PROMPT: str = _read_prompt("testing_prompt.md")

__all__ = [
    "EDITING_PROMPT",
    "TESTING_PROMPT",
    # "ASSISTANT_PROMPT",
]
