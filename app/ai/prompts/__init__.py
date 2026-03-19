from app.utils.files_utils import read_file_from_init

EDITING_PROMPT: str = read_file_from_init("editing_prompt.md", "app.ai.prompts")
DEPENDENCY_AUDITOR_PROMPT: str = read_file_from_init("dependency_auditor_prompt.md", "app.ai.prompts")
COIL_FRAMEWORK_PROMPT: str = read_file_from_init("coil_framework_prompt.md", "app.ai.prompts")
COIL_VERIFICATION_PROMPT: str = read_file_from_init("coil_verification_prompt.md", "app.ai.prompts")
TESTING_PROMPT: str = read_file_from_init("testing_prompt.md", "app.ai.prompts")
E2E_TESTING_PROMPT: str = read_file_from_init("e2e_testing_prompt.md", "app.ai.prompts")
RESEARCH_PROMPT: str = read_file_from_init("research_prompt.md", "app.ai.prompts")
FIX_PLANNER_PROMPT: str = read_file_from_init("fix_planner_prompt.md", "app.ai.prompts")
ARCHITECTURE_PROMPT: str = read_file_from_init("architecture_prompt.md", "app.ai.prompts")
TECH_STACK_PROMPT: str = read_file_from_init("tech_stack_prompt.md", "app.ai.prompts")
PRD_GENERATOR_PROMPT: str = read_file_from_init("prd_generator_prompt.md", "app.ai.prompts")
USER_STORIES_GENERATOR_PROMPT: str = read_file_from_init("user_stories_generator_prompt.md", "app.ai.prompts")
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
    "ARCHITECTURE_PROMPT",
    "PRD_GENERATOR_PROMPT",
    "TECH_STACK_PROMPT",
    "USER_STORIES_GENERATOR_PROMPT",
    "COIL_VERIFICATION_PROMPT"
    "COIL_FRAMEWORK_PROMPT"
]

if __name__ == "__main__":
    print(E2E_TESTING_PROMPT)
