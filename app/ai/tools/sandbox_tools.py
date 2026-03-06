from e2b import AsyncSandbox
from langchain.tools import tool

from app.core.config import settings

def create_get_server_logs_tool(sdbx_id: str):
    @tool
    async def get_server_logs(lines_count: int = 25) -> str:
        """Fetch and return Next.js development server logs from the sandbox.

        Args:
            lines_count (int): Number of log lines to retrieve. Defaults to 25.

        Returns:
            str: Server logs output.
        """
        sandbox = await AsyncSandbox.connect(
            sandbox_id=sdbx_id, api_key=settings.e2b_api_key
        )
        result = await sandbox.commands.run(
            f"pm2 logs project --raw --time --lines {lines_count} --nostream"
        )
        return result.stdout
    return fetch_server_logs