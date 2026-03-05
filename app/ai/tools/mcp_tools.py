from langchain_mcp_adapters.client import MultiServerMCPClient
from e2b import AsyncSandbox, Sandbox
from app.core.config import settings
from langchain_core.tools import BaseTool
from pydantic import BaseModel
from sensai.util import logging
from app.constants import PROJECT_PATH

log = logging.getLogger(__name__)

# Constants
TOOL_PREFIX_MAP = {
    "github/7codo/serena-": len("github/7codo/serena-"),
}


class Result(BaseModel):
    tools: list[BaseTool]
    sandbox_id: str

    model_config = {"arbitrary_types_allowed": True}


_tool_cache: dict[str, Result] = {}


# --- Helper Functions ---
async def _create_mcp_client(mcp_url: str, mcp_token: str) -> MultiServerMCPClient:
    """Create an MCP client for the configured server."""
    return MultiServerMCPClient(
        {
            "main": {
                "url": mcp_url,
                "headers": {"Authorization": f"Bearer {mcp_token}"},
                "transport": "streamable_http",
            },
        }
    )


def _normalize_tool_name(tool: BaseTool, prefix_map: dict[str, int]) -> None:
    """Strip known prefixes from tool names in-place."""
    original_name = tool.name
    for prefix, prefix_len in prefix_map.items():
        if original_name.startswith(prefix):
            tool.name = original_name[prefix_len:]
            return
    tool.name = original_name


async def get_or_create_tools(sandbox_id: str) -> Result:
    """
    Retrieves tools from cache or creates a new sandbox connection.
    """
    if sandbox_id and sandbox_id in _tool_cache:
        return _tool_cache[sandbox_id]

    log.info(f"Initializing MCP connection for sandbox {sandbox_id}")
    log.debug(f"sandbox_id: {sandbox_id}")

    sandbox = await AsyncSandbox.connect(
        sandbox_id=sandbox_id, api_key=settings.e2b_api_key
    )

    mcp_url = sandbox.get_mcp_url()
    mcp_token = await sandbox.get_mcp_token()

    client = await _create_mcp_client(mcp_url=mcp_url, mcp_token=mcp_token)
    tools = await client.get_tools()

    for tool in tools:
        _normalize_tool_name(tool, TOOL_PREFIX_MAP)

    result = Result(tools=tools, sandbox_id=sandbox.sandbox_id)
    _tool_cache[sandbox.sandbox_id] = result
    return result


async def build_tools(
    sandbox_id: str,
    allowed_tools: list[str] | None = None,
    excluded_tools: list[str] | None = None,
) -> Result:
    """
    Retrieves tools from the sandbox and filters them based on
    allowed and excluded lists.
    """
    sandbox_result = await get_or_create_tools(sandbox_id)

    allowed = set(allowed_tools or [])
    excluded = set(excluded_tools or [])

    filtered_tools = [
        tool for tool in sandbox_result.tools
        if (not allowed or tool.name in allowed)
        and tool.name not in excluded
    ]

    return Result(tools=filtered_tools, sandbox_id=sandbox_result.sandbox_id)

async def execute_specific_tool(
    sandbox_id: str, tool_name: str, input: dict | None = None
) -> dict:
    """
    Finds and executes a specific tool by name within the sandbox.
    """
    # Use empty dict if input is None
    input = input or {}

    sandbox_result = await get_or_create_tools(sandbox_id)

    tool = next((t for t in sandbox_result["tools"] if t.name == tool_name), None)
    if tool is None:
        raise ValueError(f"Tool '{tool_name}' not found.")

    result = await tool.ainvoke(input)
    print(result)
    print("*" * 25)

    # Added safe access check for result parsing.
    # If the result is a list containing text, we extract it directly into the 'result' field.
    if isinstance(result, list) and result and "text" in result[0]:
        return {"result": result[0]["text"], "sandbox_id": sandbox_result["sandbox_id"]}

    return {"result": result, "sandbox_id": sandbox_result["sandbox_id"]}


def debug_mcp():
    sandbox_id = "i8l1hn2cvt7nstr0jn716"
    sandbox = Sandbox.connect(sandbox_id=sandbox_id, api_key=settings.e2b_api_key)
    dirname = PROJECT_PATH
    # handle = sandbox.files.watch_dir(dirname)
    # Trigger file write event
    sandbox.files.write(f"{dirname}/test2/test2", "hello")

    # Retrieve the latest new events since the last `get_new_events()` call
    # events = handle.get_new_events()
    # for event in events:
    #     print(event)
    # mcp_url = sandbox.get_mcp_url()
    # mcp_token = await sandbox.get_mcp_token()
    # print(mcp_url)
    # print(mcp_token)
    # client = await _create_mcp_client(mcp_url=mcp_url, mcp_token=mcp_token)

    # tools = await client.get_tools()
    # print(len(tools))


if __name__ == "__main__":
    import asyncio

    debug_mcp()
    # result = asyncio.run(debug_mcp())

    # bunx @modelcontextprotocol/inspector --transport http --url https://50005-iyehv27vkb9mb0fxcalqo.e2b.app/mcp --header "Authorization: Bearer cce8aad9-f416-438f-b0d5-e0df3d41f59f"