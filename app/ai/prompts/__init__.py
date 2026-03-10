from app.utils.files_utils import read_file_from_init

EDITING_PROMPT: str = read_file_from_init("editing_prompt.md", "app.ai.prompts")
TESTING_PROMPT: str = read_file_from_init("testing_prompt.md", "app.ai.prompts")
E2E_TESTING_PROMPT: str = read_file_from_init("e2e_testing_prompt.md", "app.ai.prompts")
RESEARCH_PROMPT: str = read_file_from_init("research_prompt.md", "app.ai.prompts")
FIX_PLANNER_PROMPT: str = read_file_from_init("fix_planner_prompt.md", "app.ai.prompts")
MINI_DOCS_CREATION_PROMPT: str = read_file_from_init(
    "mini_docs_creation_prompt.md", "app.ai.prompts"
)
PlANNER_PROMPT: str = read_file_from_init("planner_prompt.md", "app.ai.prompts")

__all__ = [
    "EDITING_PROMPT",
    "TESTING_PROMPT",
    "E2E_TESTING_PROMPT",
    "RESEARCH_PROMPT",
    "MINI_DOCS_CREATION_PROMPT",
    "PlANNER_PROMPT",
    "FIX_PLANNER_PROMPT",
]

if __name__ == "__main__":
    print(E2E_TESTING_PROMPT)
