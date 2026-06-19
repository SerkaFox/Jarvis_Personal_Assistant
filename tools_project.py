from pathlib import Path
from typing import Any

from tools_fs import read_file, search_text, tree_summary
from tools_git import git_branch, git_diff, git_remote, git_status, resolve_repo, _run_git

PROJECT_EXCLUDED_DIRS = {
    ".codex",
    ".config",
    ".git",
    ".venv",
    "__pycache__",
    "media",
    "node_modules",
    "staticfiles",
    "uploads",
    "venv",
}


def _safe_read(path: Path, max_chars: int = 3000) -> dict[str, Any] | None:
    try:
        return read_file(str(path), max_chars=max_chars)
    except Exception:
        return None


def _find_files(root: Path, names: tuple[str, ...], max_items: int = 20) -> list[str]:
    matches = []
    for path in root.rglob("*"):
        if any(part in PROJECT_EXCLUDED_DIRS for part in path.parts):
            continue
        if path.is_file() and any(path.name.lower().startswith(name.lower()) for name in names):
            matches.append(str(path))
            if len(matches) >= max_items:
                break
    return matches


def _find_django(root: Path) -> dict[str, Any]:
    data: dict[str, Any] = {
        "manage_py": (root / "manage.py").exists(),
        "settings": [],
        "urls": [],
        "apps": [],
        "models": [],
        "views": [],
        "templates": [],
    }
    for path in root.rglob("*.py"):
        if any(part in PROJECT_EXCLUDED_DIRS for part in path.parts):
            continue
        if path.name == "settings.py":
            data["settings"].append(str(path))
        elif path.name == "urls.py":
            data["urls"].append(str(path))
        elif path.name == "apps.py":
            data["apps"].append(str(path))
        elif path.name == "models.py":
            data["models"].append(str(path))
        elif path.name == "views.py":
            data["views"].append(str(path))
    templates = root / "templates"
    if templates.exists():
        data["templates"].append(str(templates))
    return data


def project_structure(repo_name_or_path: str, depth: int = 2) -> dict[str, Any]:
    repo = resolve_repo(repo_name_or_path)
    counts = {
        "total_files": 0,
        "total_dirs": 0,
        "python_files": 0,
        "template_files": 0,
        "js_files": 0,
        "css_files": 0,
    }

    for path in repo.rglob("*"):
        if any(part in PROJECT_EXCLUDED_DIRS for part in path.relative_to(repo).parts):
            continue
        if path.is_dir():
            counts["total_dirs"] += 1
            continue
        if not path.is_file():
            continue

        counts["total_files"] += 1
        suffix = path.suffix.lower()
        if suffix == ".py":
            counts["python_files"] += 1
        elif suffix in {".html", ".htm", ".jinja", ".j2"}:
            counts["template_files"] += 1
        elif suffix in {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"}:
            counts["js_files"] += 1
        elif suffix == ".css":
            counts["css_files"] += 1

    tree = tree_summary(str(repo), depth=depth)
    return {
        "project_name": repo.name,
        "path": str(repo),
        "counts": counts,
        "tree_summary": tree["tree"],
        "depth": tree["depth"],
    }


def inspect_project(repo_name_or_path: str) -> dict[str, Any]:
    repo = resolve_repo(repo_name_or_path)
    status = git_status(str(repo))
    diff = git_diff(str(repo), max_chars=12000)
    diff_stat = _run_git(repo, ["diff", "--stat"], timeout=20)
    git_log = _run_git(repo, ["log", "--oneline", "-n", "10"], timeout=20)
    last_commit = _run_git(repo, ["rev-parse", "HEAD"], timeout=10)

    important_files = _find_files(repo, ("README", "TODO", "CHANGELOG", "docs"), max_items=30)
    important_content = []
    for file_path in important_files[:8]:
        content = _safe_read(Path(file_path), max_chars=2500)
        if content:
            important_content.append(content)

    todo_search = search_text(str(repo), "TODO|FIXME|HACK|BUG", glob="*.py")
    return {
        "project_name": repo.name,
        "path": str(repo),
        "git": {
            "status": status,
            "branch": git_branch(str(repo)),
            "remote": git_remote(str(repo)),
            "diff_stat": diff_stat,
            "diff": diff,
            "log_oneline": git_log,
            "last_commit": last_commit,
        },
        "tree": tree_summary(str(repo), depth=2),
        "important_files": important_files,
        "important_content": important_content,
        "todos": todo_search,
        "django": _find_django(repo),
    }
