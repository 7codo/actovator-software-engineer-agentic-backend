from __future__ import annotations

from typing import TypedDict

from e2b import AsyncSandbox
from langchain.tools import tool
from langchain_core.tools import BaseTool

from app.constants import PROJECT_PATH
from app.core.config import settings


class GitRemoteResult(TypedDict):
    status: str
    remote_url: str | None
    created_repo: str | None
    error: str


def build_git_tools(sandbox_id: str) -> dict[str, BaseTool]:
    async def _get_sandbox() -> AsyncSandbox:
        return await AsyncSandbox.connect(
            sandbox_id=sandbox_id,
            api_key=settings.e2b_api_key,
        )

    async def _run(
        command: str,
        *,
        user: str = "root",
        cwd: str | None = None,
        background: bool = False,
    ):
        try:
            sandbox = await _get_sandbox()
            return await sandbox.commands.run(
                command, user=user, cwd=cwd, background=background
            )
        except Exception as e:
            raise RuntimeError(
                f"Shell command failed.\nCommand: {command}\n{type(e).__name__}: {e}"
            ) from e

    async def _get_origin_url() -> str | None:
        """Return the current origin URL, or ``None`` if none is configured."""
        res = await _run("git remote -v", cwd=PROJECT_PATH)
        if res.exit_code != 0 or not res.stdout.strip():
            return None
        for line in res.stdout.splitlines():
            if line.startswith("origin"):
                parts = line.split()
                return parts[1] if len(parts) > 1 else None
        return None

    async def _ensure_git_remote_origin(
        remote_url: str, repo_name: str
    ) -> GitRemoteResult:
        """
        Runs the full check-and-configure sequence for a git remote origin inside
        the sandbox. Exceptions bubble up rather than being caught (the public
        wrapper handles that). Executes the following steps in order:
          1. Checks for a git repo; runs ``git init`` if one is absent.
          2. Returns early if an origin remote is already set.
          3. Adds a caller-supplied ``remote_url`` directly as origin.
          4. Creates a new private GitHub repo via the CLI and sets it as origin.

        Args:
            remote_url: Remote URL to set as origin. When provided, step 4 is skipped.
            repo_name:  Name for the new GitHub repo (step 4 only). Falls back to
                        the sandbox directory basename when empty or absent.

        Returns:
            GitRemoteResult with the following possible ``status`` values:
              - ``already-exists``          origin was already configured.
              - ``set-remote-url-supplied`` origin set from the provided remote_url.
              - ``created-remote-url``      new GitHub repo created and set as origin.
              - ``error-initialising-repo`` git init failed.
              - ``error-adding-remote-url`` git remote add failed.
              - ``error-creating-remote-url`` gh repo create failed or name could not
                                              be determined.
            ``remote_url`` holds the resolved origin URL on success, ``None`` on failure.
            ``created_repo`` holds the repo name when one was created, ``None`` otherwise.
            ``error`` is non-empty on any failure status.
        """
        # 1. Verify this is a git repository; initialise one if not.
        res = await _run("git rev-parse --is-inside-work-tree", cwd=PROJECT_PATH)
        if res.exit_code != 0 or res.stdout.strip() != "true":
            init_res = await _run("git init", cwd=PROJECT_PATH)
            if init_res.exit_code != 0:
                return GitRemoteResult(
                    status="error-initialising-repo",
                    remote_url=None,
                    created_repo=None,
                    error=init_res.stderr
                    or "Unknown error initialising git repository.",
                )

        # 2. Return early if an origin remote already exists.
        existing = await _get_origin_url()
        if existing is not None:
            return GitRemoteResult(
                status="already-exists",
                remote_url=existing,
                created_repo=None,
                error="",
            )

        # 3. Add a caller-supplied remote URL.
        if remote_url:
            res = await _run(f"git remote add origin {remote_url}", cwd=PROJECT_PATH)
            if res.exit_code != 0:
                return GitRemoteResult(
                    status="error-adding-remote-url",
                    remote_url=None,
                    created_repo=None,
                    error=res.stderr or "Unknown error adding remote URL.",
                )
            return GitRemoteResult(
                status="set-remote-url-supplied",
                remote_url=remote_url,
                created_repo=None,
                error="",
            )

        # 4. Create a new GitHub repo with the CLI.
        # Resolve repo_name: if absent or blank, derive it from the sandbox directory.
        name = repo_name.strip() if isinstance(repo_name, str) else ""
        if not name:
            basename_res = await _run("basename $(pwd)", cwd=PROJECT_PATH)
            name = basename_res.stdout.strip() if basename_res.exit_code == 0 else ""
        if not name:
            return GitRemoteResult(
                status="error-creating-remote-url",
                remote_url=None,
                created_repo=None,
                error="Could not determine a repository name: provide repo_name or ensure PROJECT_PATH is a named directory.",
            )
        res = await _run(
            f"gh repo create {name} --private --source=. --remote=origin",
            cwd=PROJECT_PATH,
        )
        origin_url = await _get_origin_url()
        if res.exit_code == 0 and origin_url:
            return GitRemoteResult(
                status="created-remote-url",
                remote_url=origin_url,
                created_repo=name,
                error="",
            )
        return GitRemoteResult(
            status="error-creating-remote-url",
            remote_url=None,
            created_repo=name,
            error=res.stderr or "Unknown error creating remote URL via gh CLI.",
        )

    @tool
    async def ensure_git_remote_origin(
        remote_url: str = "",
        repo_name: str = "",
    ) -> GitRemoteResult:
        """
        Public wrapper around _ensure_git_remote_origin. Ensures the sandbox repo
        at PROJECT_PATH has a remote origin, catching any unhandled exceptions and
        surfacing them as an ``exception`` status rather than propagating.

        Args:
            remote_url: Remote URL to set as origin. When provided, skips repo creation.
            repo_name:  Name for the new GitHub repo if one needs to be created.
                        Falls back to the sandbox directory name when empty or absent.

        Returns:
            GitRemoteResult describing the outcome. See _ensure_git_remote_origin
            for the full set of possible status values.
        """
        try:
            return await _ensure_git_remote_origin(remote_url, repo_name)
        except Exception as e:
            return GitRemoteResult(
                status="exception",
                remote_url=None,
                created_repo=None,
                error=f"{type(e).__name__}: {e}",
            )

    @tool
    async def get_branches(
        include_merged: bool = True,
        include_not_merged: bool = True,
        delete_merged: bool = True,
    ) -> dict:
        """
        Lists merged and/or non-merged branch names in the sandbox git repository,
        with an option to delete merged branches automatically.

        Args:
            include_merged:     Whether to include branches already merged into HEAD.
                                Defaults to True.
            include_not_merged: Whether to include branches not yet merged into HEAD.
                                Defaults to True.
            delete_merged:      Whether to delete merged branches after listing them.
                                Defaults to True. Only applies when include_merged is True.

        Returns:
            A dict with:
              - ``merged``     (list[str]): merged branch names, empty if not requested.
              - ``not_merged`` (list[str]): non-merged branch names, empty if not requested.
              - ``deleted``    (list[str]): branches deleted, empty if delete_merged is False.
              - ``error``      (str): non-empty on failure.
        """
        result: dict = {"merged": [], "not_merged": [], "deleted": [], "error": ""}

        try:
            if include_merged:
                res = await _run("git branch --merged", cwd=PROJECT_PATH)
                if res.exit_code != 0:
                    result["error"] = res.stderr or "Failed to list merged branches."
                    return result
                merged = [
                    b.strip().lstrip("* ")
                    for b in res.stdout.splitlines()
                    if b.strip() and b.strip().lstrip("* ") not in ("main", "master")
                ]
                result["merged"] = merged

                if delete_merged and merged:
                    for branch in merged:
                        del_res = await _run(
                            f"git branch -d {branch}", cwd=PROJECT_PATH
                        )
                        if del_res.exit_code == 0:
                            result["deleted"].append(branch)

            if include_not_merged:
                res = await _run("git branch --no-merged", cwd=PROJECT_PATH)
                if res.exit_code != 0:
                    result["error"] = (
                        res.stderr or "Failed to list non-merged branches."
                    )
                    return result
                result["not_merged"] = [
                    b.strip().lstrip("* ") for b in res.stdout.splitlines() if b.strip()
                ]

        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"

        return result

    @tool
    async def create_and_switch_branch(branch_name: str) -> dict:
        """
        Creates a new git branch and switches to it in the sandbox repository.
        Equivalent to running ``git checkout -b <branch-name>``.

        Args:
            branch_name: Name of the branch to create and check out. Must be a
                         non-empty, valid git branch name.

        Returns:
            A dict with:
              - ``branch`` (str):  the name of the created branch on success, empty on failure.
              - ``error``  (str):  non-empty on failure.
        """
        branch_name_val = branch_name.strip() if isinstance(branch_name, str) else ""
        if not branch_name_val:
            return {"branch": "", "error": "branch_name must be a non-empty string."}

        try:
            res = await _run(f"git checkout -b {branch_name_val}", cwd=PROJECT_PATH)
            if res.exit_code != 0:
                return {
                    "branch": "",
                    "error": res.stderr
                    or f"Failed to create and switch to branch '{branch_name_val}'.",
                }
            return {"branch": branch_name_val, "error": ""}
        except Exception as e:
            return {"branch": "", "error": f"{type(e).__name__}: {e}"}

    @tool
    async def create_branch(branch_name: str) -> dict:
        """
        Creates a new git branch.
        Equivalent to running ``git branch <branch-name>``.

        Args:
            branch_name: Name of the branch to create and check out. Must be a
                         non-empty, valid git branch name.

        Returns:
            A dict with:
              - ``branch`` (str):  the name of the created branch on success, empty on failure.
              - ``error``  (str):  non-empty on failure.
        """
        branch_name_val = branch_name.strip() if isinstance(branch_name, str) else ""
        if not branch_name_val:
            return {"branch": "", "error": "branch_name must be a non-empty string."}

        try:
            res = await _run(f"git branch {branch_name_val}", cwd=PROJECT_PATH)
            if res.exit_code != 0:
                return {
                    "branch": "",
                    "error": res.stderr
                    or f"Failed to create to branch '{branch_name_val}'.",
                }
            return {"branch": branch_name_val, "error": ""}
        except Exception as e:
            return {"branch": "", "error": f"{type(e).__name__}: {e}"}

    return {
        "ensure_git_remote_origin": ensure_git_remote_origin,
        "get_branches": get_branches,
        "create_and_switch_branch": create_and_switch_branch,
        "create_branch": create_branch,
    }


async def _main() -> None:
    tools = build_git_tools("i9na59ivfvvyq3ha8qsct")
    result = await tools["ensure_git_remote_origin"]()
    print("result", result)


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
