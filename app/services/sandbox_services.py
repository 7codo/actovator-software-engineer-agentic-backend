from typing import List
from e2b import AsyncSandbox
from e2b.sandbox.filesystem.filesystem import WriteEntry
from app.constants import PROJECT_PATH
from app.core.config import settings


async def create_sandbox_with_auto_pause(github_token:str):
    """
    Creates a new sandbox with auto_pause enabled.

    Returns:
        Sandbox: The created Sandbox instance.
    """
    sandbox = await AsyncSandbox.beta_create(
        template="nextjs-latest",
        api_key=settings.e2b_api_key,
        auto_pause=True,
        mcp={
            "github/7codo/serena": {
                "installCmd": "uv pip install -r pyproject.toml --system",
                "runCmd": f"uv run serena-mcp-server --project {PROJECT_PATH}",
            },
        },
        envs={
            "NEXT_TELEMETRY_DISABLED": "1",
            "GITHUB_TOKEN": github_token,
        },
    )
    await sandbox.commands.run('git config --global user.email "contact@actovator.com"')
    await sandbox.commands.run('git config --global user.name "actovator"')
    await sandbox.commands.run('git config --global credential.helper store')
    await sandbox.commands.run(
        'echo "https://oauth2:$GITHUB_TOKEN@github.com" > ~/.git-credentials'
    )
    return sandbox.sandbox_id


async def get_sandbox_host_url(sandbox_id: str):
    """
    Connects to a sandbox by its ID and retrieves the host URL for port 3000.

    Args:
        sandbox_id (str): The ID of the sandbox.

    Returns:
        str: The full HTTPS host URL for port 3000.
    """
    sandbox = await AsyncSandbox.connect(
        sandbox_id=sandbox_id, api_key=settings.e2b_api_key
    )
    host = sandbox.get_host(3000)
    url = f"https://{host}"
    return url


async def upload_files_to_sandbox(sandbox_id: str, files: List[WriteEntry]):
    """
    Connects to a sandbox by its ID and uploads files.

    Args:
        sandbox_id (str): The ID of the sandbox.
        files (dict): A dictionary of file paths to their content.

    Returns:
        None
    """
    sandbox = await AsyncSandbox.connect(
        sandbox_id=sandbox_id, api_key=settings.e2b_api_key
    )
    await sandbox.files.write_files(files)

async def kill_sandbox(sandbox_id: str): ## kill for beta save sandbox meaning delete
    """
    Kills (terminates) a sandbox given its ID.

    Args:
        sandbox_id (str): The ID of the sandbox to terminate.

    Returns:
        None
    """
    sandbox = await AsyncSandbox.connect(
        sandbox_id=sandbox_id, api_key=settings.e2b_api_key
    )
    await sandbox.kill()


if __name__ == "__main__":
    import asyncio

    sandbox_id = asyncio.run(create_sandbox_with_auto_pause())
    print(f"sandbox_id: {sandbox_id}")

    # Example: kill the created sandbox (uncomment to run)
    # asyncio.run(kill_sandbox(sandbox_id))