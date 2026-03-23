from app.utils.files_utils import read_file_from_init

SANDBOX_TOOLS_DEFINITIONS: str = read_file_from_init(
    "serena_tools_definitions.json", "app.ai.resources", is_json_file=True
)

__all__ = ["SANDBOX_TOOLS_DEFINITIONS"]
