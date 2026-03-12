from fastapi import APIRouter, HTTPException, UploadFile, File, Query
from fastapi import Body
from typing import List
import e2b.exceptions
from app.constants import PROJECT_PATH
from app.services.sandbox_services import (
    create_sandbox_with_auto_pause,
    get_sandbox_host_url,
    kill_sandbox,
    upload_files_to_sandbox,
    read_file,execute_command_in_sandbox
)
from e2b.sandbox.filesystem.filesystem import WriteEntry

router = APIRouter(prefix="/sandbox", tags=["sandbox"])


@router.post("/create", summary="Create a new sandbox with auto_pause enabled")
async def create_sandbox(github_token: str):
    """
    Creates a new sandbox environment with auto_pause enabled.
    Returns the sandbox ID.
    """
    try:
        sandbox_id = await create_sandbox_with_auto_pause(github_token=github_token)
        return {"id": sandbox_id}
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"An unexpected error occurred: {str(e)}"
        )

@router.get("/host/{sandbox_id}", summary="Get the host URL for a sandbox")
async def get_host(sandbox_id: str):
    """
    Retrieves the host URL for a given sandbox ID.
    """
    try:
        url = await get_sandbox_host_url(sandbox_id)
        return {"url": url}
    except e2b.exceptions.NotFoundException:
        raise HTTPException(
            status_code=404, detail=f"Paused sandbox {sandbox_id} not found"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"An unexpected error occurred: {str(e)}"
        )


@router.delete(
    "/kill/{sandbox_id}", summary="Kill (delete) a sandbox by its ID"
)
async def kill_sdbx(sandbox_id: str):
    """
    Kills (terminates/deletes) a sandbox given its ID.
    """
    try:
        await kill_sandbox(sandbox_id)
        return {"detail": f"Sandbox {sandbox_id} killed successfully."}
    except e2b.exceptions.NotFoundException:
        raise HTTPException(
            status_code=404, detail=f"Sandbox {sandbox_id} not found"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"An unexpected error occurred: {str(e)}"
        )


@router.post(
    "/upload/{sandbox_id}",
    summary="Upload files to a sandbox",
    description="Upload one or more files to a given sandbox by its ID. Each file is uploaded with its destination path.",
)
async def upload_files(
    sandbox_id: str,
    files: List[UploadFile] = File(..., description="Files to upload"),
    file_path: str = f"{PROJECT_PATH}/public"
):
    """
    Upload one or more files to the sandbox at a given path.

    The request expects multipart/form-data for files. All files will be placed inside the specified file_path.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files provided for upload.")


    write_entries = []
    for upload_file in files:
        dest_path = f"{file_path}/{upload_file.filename}"
        file_data = await upload_file.read()
        write_entries.append(WriteEntry(path=dest_path, data=file_data))

    try:
        await upload_files_to_sandbox(sandbox_id, write_entries)
        return {"detail": f"{len(write_entries)} file(s) uploaded to sandbox {sandbox_id}."}
    except e2b.exceptions.NotFoundException:
        raise HTTPException(
            status_code=404, detail=f"Sandbox {sandbox_id} not found"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"An unexpected error occurred: {str(e)}"
        )


@router.get(
    "/file/{sandbox_id}",
    summary="Read a file from the sandbox",
    description="Read a file given a sandbox ID and file path."
)
async def read_sandbox_file(
    sandbox_id: str,
    file_path: str = Query(..., description="Path to the file inside the sandbox")
):
    """
    Read a file's contents from a sandbox.
    """
    try:
        content = await read_file(sandbox_id, file_path)
        return {"file_path": file_path, "content": content}
    except e2b.exceptions.NotFoundException:
        raise HTTPException(
            status_code=404,
            detail=f"Sandbox {sandbox_id} not found or file {file_path} does not exist"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"An unexpected error occurred while reading file: {str(e)}"
        )

@router.post(
    "/command/{sandbox_id}",
    summary="Execute a command in the sandbox",
    description="Execute a shell command in the specified sandbox with optional working directory and user context.",
)
async def execute_command_in_sandbox_endpoint(
    sandbox_id: str,
    command: str = Query(..., description="The shell command to execute"),
    cwd: str = Query(None, description="Working directory inside the sandbox"),
    user: str = Query(None, description="User context for command execution"),
):
    """
    Execute a command inside the sandbox environment.
    """

    try:
        result = await execute_command_in_sandbox(sandbox_id, command, cwd=cwd, user=user)
        return {"sandbox_id": sandbox_id, "command": command, "result": result}
    except Exception as e:
        # Customize error handling for specific exceptions if needed
        raise HTTPException(
            status_code=500,
            detail=f"An error occurred while executing the command: {str(e)}"
        )