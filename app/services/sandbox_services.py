from typing import List
from e2b import AsyncSandbox
from e2b.sandbox.filesystem.filesystem import WriteEntry
from app.core.config import settings


async def create_sandbox_with_auto_pause(github_token: str | None = None):
    """
    Creates a new sandbox with auto_pause enabled.

    Returns:
        Sandbox: The created Sandbox instance.
    """
    sandbox = await AsyncSandbox.beta_create(
        template="nextjs-latest",
        api_key=settings.e2b_api_key,
        auto_pause=True,
        envs={
            "NEXT_TELEMETRY_DISABLED": "1",
            "GITHUB_TOKEN": github_token or "",
        },
    )
    if github_token:
        print("[DEBUG] Setting global git user email...")
        result = await sandbox.commands.run(
            'git config --global user.email "contact@actovator.com"', user="root"
        )
        print("[DEBUG] Result:", result)

        print("[DEBUG] Setting global git user name...")
        result = await sandbox.commands.run(
            'git config --global user.name "actovator"', user="root"
        )
        print("[DEBUG] Result:", result)

        print("[DEBUG] Setting global git credential.helper to store...")
        result = await sandbox.commands.run(
            "git config --global credential.helper store"
        )
        print("[DEBUG] Result:", result)

        print("[DEBUG] Adding github credentials to ~/.git-credentials ...")
        result = await sandbox.commands.run(
            'echo "https://oauth2:$GITHUB_TOKEN@github.com" > ~/.git-credentials',
            user="root",
        )
        print("[DEBUG] Result:", result)

        print("[DEBUG] Setting git safe.directory for all directories...")
        result = await sandbox.commands.run(
            "git config --global --add safe.directory '*'", user="root"
        )
        print("[DEBUG] Result:", result)

    return sandbox.sandbox_id


async def get_sandbox_host_url(sandbox_id: str, port: int):
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
    host = sandbox.get_host(port)
    url = f"https://{host}"
    return url


async def read_file(sandbox_id: str, path: str):
    """
    Connect to the sandbox and read the contents of the file at the given path.

    Args:
        sandbox_id (str): The ID of the sandbox.
        path (str): The path to the file in the sandbox.

    Returns:
        str: The contents of the file.
    """
    sandbox = await AsyncSandbox.connect(
        sandbox_id=sandbox_id, api_key=settings.e2b_api_key
    )
    file_content = await sandbox.files.read(path)
    return file_content


async def execute_command_in_sandbox(
    sandbox_id: str, command: str, cwd: str = None, user: str = None
):
    """
    Execute a command in the sandbox with the specified working directory and user.

    Args:
        sandbox_id (str): The ID of the sandbox.
        command (str): The command to execute.
        cwd (str, optional): The working directory to run the command in. Defaults to None.
        user (str, optional): The user context in which to run the command (e.g., 'root'). Defaults to None.

    Returns:
        Any: The result of the command execution.
    """
    sandbox = await AsyncSandbox.connect(
        sandbox_id=sandbox_id, api_key=settings.e2b_api_key
    )
    result = await sandbox.commands.run(command, cwd=cwd, user=user)
    return result


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


async def kill_sandbox(sandbox_id: str):  ## kill for beta save sandbox meaning delete
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

    # sandbox_id = asyncio.run(create_sandbox_with_auto_pause())
    # print(f"sandbox_id: {sandbox_id}")

    # Example: kill the created sandbox (uncomment to run)
    asyncio.run(kill_sandbox(sandbox_id="igjgy8c0rxosyitmur8x0"))
