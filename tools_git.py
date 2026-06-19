import subprocess
from pathlib import Path
from typing import Any

from tools_fs import get_allowed_roots, is_excluded_dir, log_tool_call, resolve_allowed_path


def _run_git(repo_path: Path, args: list[str], timeout: int = 10) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        return (result.stderr or result.stdout or "").strip()
    return result.stdout.strip()


def git_status(repo_path: str) -> dict[str, Any]:
    log_tool_call("git_status", {"repo_path": repo_path})
    repo = resolve_allowed_path(repo_path)
    if not (repo / ".git").exists():
        raise ValueError(f"Не git-репозиторий: {repo}")

    return {
        "path": str(repo),
        "branch": _run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"]),
        "remote_origin": _run_git(repo, ["config", "--get", "remote.origin.url"]),
        "status_short": _run_git(repo, ["status", "--short"]),
    }


def find_git_repos(root: str | None = None, max_depth: int = 5) -> dict[str, Any]:
    log_tool_call("find_git_repos", {"root": root, "max_depth": max_depth})
    roots = [resolve_allowed_path(root)] if root else get_allowed_roots()
    repos: list[dict[str, Any]] = []
    max_depth = max(1, min(int(max_depth), 8))

    def walk(path: Path, depth: int) -> None:
        if depth > max_depth or is_excluded_dir(path):
            return
        if (path / ".git").exists():
            repos.append(git_status(str(path)))
            return

        try:
            children = [child for child in path.iterdir() if child.is_dir()]
        except (PermissionError, FileNotFoundError):
            return

        for child in children:
            if child.is_symlink():
                try:
                    child.resolve().relative_to(path.resolve())
                except ValueError:
                    continue
            if not is_excluded_dir(child):
                walk(child, depth + 1)

    for allowed_root in roots:
        walk(allowed_root, 0)

    return {
        "roots": [str(root_path) for root_path in roots],
        "repositories": repos,
        "count": len(repos),
    }
