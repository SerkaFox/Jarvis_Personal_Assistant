import subprocess
from pathlib import Path
from typing import Any

from tools_fs import get_allowed_roots, is_excluded_dir, log_tool_call, resolve_allowed_path


EXCLUDED_GIT_SEARCH_NAMES = {
    ".codex",
    ".config",
    ".git",
    ".ssh",
    "__pycache__",
    "backups",
    "media",
    "node_modules",
    "staticfiles",
    "uploads",
    "venv",
}


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


def _run_git_checked(repo_path: Path, args: list[str], timeout: int = 10) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        logging_text = (result.stderr or result.stdout or "").strip()
        if "fatal:" in logging_text.lower():
            return None
        return None
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
    if _run_git_checked(repo, ["rev-parse", "--show-toplevel"]) is None:
        raise ValueError(f"Git-команда не подтвердила репозиторий: {repo}")
    return repo


def _is_excluded_search_dir(path: Path) -> bool:
    name = path.name
    lowered = name.lower()
    return (
        is_excluded_dir(path)
        or name in EXCLUDED_GIT_SEARCH_NAMES
        or lowered in EXCLUDED_GIT_SEARCH_NAMES
        or "_backup" in lowered
        or lowered.endswith("_backup")
        or lowered.startswith("backup_")
    )


def _repo_from_git_entry(git_entry: Path) -> Path | None:
    if not git_entry.exists():
        return None
    if git_entry.name != ".git":
        return None
    repo = git_entry.parent
    if not (repo / ".git").exists():
        return None
    return repo


def _valid_repo_status(repo: Path) -> dict[str, Any] | None:
    branch = _run_git_checked(repo, ["branch", "--show-current"])
    remote = _run_git_checked(repo, ["remote", "-v"])
    status = _run_git_checked(repo, ["status", "--short"])
    if branch is None or remote is None or status is None:
        return None
    return {
        "path": str(repo),
        "branch": branch,
        "remote": remote,
        "status_short": status,
    }


def git_branch(repo_path: str) -> dict[str, Any]:
    log_tool_call("git_branch", {"repo_path": repo_path})
    repo = _ensure_git_repo(repo_path)
    branch = _run_git_checked(repo, ["branch", "--show-current"])
    if branch is None:
        raise ValueError(f"git branch failed: {repo}")
    return {
        "path": str(repo),
        "branch": branch,
    }


def git_remote(repo_path: str) -> dict[str, Any]:
    log_tool_call("git_remote", {"repo_path": repo_path})
    repo = _ensure_git_repo(repo_path)
    remote = _run_git_checked(repo, ["remote", "-v"])
    if remote is None:
        raise ValueError(f"git remote failed: {repo}")
    return {
        "path": str(repo),
        "remote": remote,
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
    status = _valid_repo_status(repo)
    if status is None:
        raise ValueError(f"git status failed: {repo}")

    return status


def find_git_repos(root: str | None = None, max_depth: int = 5) -> dict[str, Any]:
    log_tool_call("find_git_repos", {"root": root, "max_depth": max_depth})
    roots = [resolve_allowed_path(root)] if root else get_allowed_roots()
    repos: list[dict[str, Any]] = []
    seen_realpaths: set[Path] = set()
    max_depth = _coerce_int(max_depth, 5, 1, 8)

    def add_repo(repo: Path) -> None:
        real_repo = repo.resolve()
        if real_repo in seen_realpaths:
            return
        status = _valid_repo_status(repo)
        if status is None:
            return
        seen_realpaths.add(real_repo)
        repos.append(status)

    def walk(path: Path, depth: int) -> None:
        if depth > max_depth or _is_excluded_search_dir(path):
            return
        git_entry = path / ".git"
        repo = _repo_from_git_entry(git_entry)
        if repo is not None:
            add_repo(repo)
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
            if not _is_excluded_search_dir(child):
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
