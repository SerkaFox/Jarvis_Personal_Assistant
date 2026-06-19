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


def _coerce_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _ensure_git_repo(repo_path: str) -> Path:
    repo = resolve_allowed_path(repo_path)
    if not (repo / ".git").exists():
        raise ValueError(f"Не git-репозиторий: {repo}")
    return repo


def git_branch(repo_path: str) -> dict[str, Any]:
    log_tool_call("git_branch", {"repo_path": repo_path})
    repo = _ensure_git_repo(repo_path)
    return {
        "path": str(repo),
        "branch": _run_git(repo, ["branch", "--show-current"]),
    }


def git_remote(repo_path: str) -> dict[str, Any]:
    log_tool_call("git_remote", {"repo_path": repo_path})
    repo = _ensure_git_repo(repo_path)
    return {
        "path": str(repo),
        "remote": _run_git(repo, ["remote", "-v"]),
    }


def git_diff(repo_path: str, max_chars: int = 20000) -> dict[str, Any]:
    max_chars = _coerce_int(max_chars, 20000, 1, 50000)
    log_tool_call("git_diff", {"repo_path": repo_path, "max_chars": max_chars})
    repo = _ensure_git_repo(repo_path)
    diff = _run_git(repo, ["diff"], timeout=20)
    return {
        "path": str(repo),
        "diff": diff[:max_chars],
        "truncated": len(diff) > max_chars,
        "chars": min(len(diff), max_chars),
    }


def git_status(repo_path: str) -> dict[str, Any]:
    log_tool_call("git_status", {"repo_path": repo_path})
    repo = _ensure_git_repo(repo_path)

    return {
        "path": str(repo),
        "branch": _run_git(repo, ["branch", "--show-current"]),
        "remote": _run_git(repo, ["remote", "-v"]),
        "status_short": _run_git(repo, ["status", "--short"]),
    }


def find_git_repos(root: str | None = None, max_depth: int = 5) -> dict[str, Any]:
    log_tool_call("find_git_repos", {"root": root, "max_depth": max_depth})
    roots = [resolve_allowed_path(root)] if root else get_allowed_roots()
    repos: list[dict[str, Any]] = []
    max_depth = _coerce_int(max_depth, 5, 1, 8)

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


def resolve_repo(repo_name_or_path: str) -> Path:
    if not repo_name_or_path:
        raise ValueError("repo не задан")

    try:
        path = resolve_allowed_path(repo_name_or_path)
        if (path / ".git").exists():
            return path
    except Exception:
        pass

    needle = repo_name_or_path.strip().lower()
    matches = []
    for repo in find_git_repos()["repositories"]:
        repo_path = Path(repo["path"])
        if repo_path.name.lower() == needle or needle in str(repo_path).lower():
            matches.append(repo_path)

    if not matches:
        raise ValueError(f"Репозиторий не найден: {repo_name_or_path}")
    if len(matches) > 1:
        raise ValueError(
            "Найдено несколько репозиториев: "
            + ", ".join(str(match) for match in matches[:10])
        )
    return matches[0]
