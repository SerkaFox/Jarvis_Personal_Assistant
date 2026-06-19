import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any


DEFAULT_ALLOWED_ROOTS = "/home/seradmin,/home/seradmin/jelec,/var/www"
DEFAULT_MAX_FILE_CHARS = 12000
DEFAULT_MAX_SEARCH_RESULTS = 50

EXCLUDED_DIR_NAMES = {
    ".cache",
    ".git",
    ".local",
    ".mypy_cache",
    ".npm",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "media",
    "node_modules",
    "staticfiles",
    "uploads",
    "venv",
    ".venv",
}

FORBIDDEN_FILE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.prod",
    "id_rsa",
    "id_ed25519",
}

FORBIDDEN_SUFFIXES = {
    ".db",
    ".key",
    ".pem",
    ".sqlite",
    ".sqlite3",
}

FORBIDDEN_NAME_FRAGMENTS = {
    "secret",
}

FORBIDDEN_PATH_PARTS = {
    "media",
    "uploads",
}


class ToolError(ValueError):
    pass


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return max(1, int(value))
    except ValueError:
        return default


def _coerce_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def max_file_chars() -> int:
    return _env_int("MAX_FILE_CHARS", DEFAULT_MAX_FILE_CHARS)


def max_search_results() -> int:
    return _env_int("MAX_SEARCH_RESULTS", DEFAULT_MAX_SEARCH_RESULTS)


def get_allowed_roots() -> list[Path]:
    raw_roots = os.getenv("ALLOWED_ROOTS", DEFAULT_ALLOWED_ROOTS)
    roots = []

    for raw_root in raw_roots.split(","):
        raw_root = raw_root.strip()
        if not raw_root:
            continue
        root = Path(raw_root).expanduser().resolve()
        if root.exists() and root.is_dir():
            roots.append(root)

    if not roots:
        raise ToolError("Нет доступных ALLOWED_ROOTS")

    return roots


def allowed_roots_info() -> dict[str, Any]:
    return {
        "allowed_roots": [str(root) for root in get_allowed_roots()],
        "max_file_chars": max_file_chars(),
        "max_search_results": max_search_results(),
    }


def _reject_path_traversal(path: str) -> None:
    if any(part == ".." for part in Path(path).parts):
        raise ToolError(f"Path traversal запрещен: {path}")


def resolve_allowed_path(path: str) -> Path:
    if not path:
        raise ToolError("Путь не задан")

    _reject_path_traversal(path)
    resolved = Path(path).expanduser().resolve()

    for root in get_allowed_roots():
        if resolved == root or root in resolved.parents:
            return resolved

    raise ToolError(f"Путь вне разрешенных корней: {resolved}")


def is_excluded_dir(path: Path) -> bool:
    return path.name in EXCLUDED_DIR_NAMES


def is_forbidden_file(path: Path) -> bool:
    lowered_name = path.name.lower()
    lowered_parts = {part.lower() for part in path.parts}

    if lowered_name in FORBIDDEN_FILE_NAMES:
        return True
    if path.suffix.lower() in FORBIDDEN_SUFFIXES:
        return True
    if any(fragment in lowered_name for fragment in FORBIDDEN_NAME_FRAGMENTS):
        return True
    if lowered_parts & FORBIDDEN_PATH_PARTS:
        return True
    return False


def ensure_readable_text_file(path: Path) -> None:
    if not path.exists():
        raise ToolError(f"Файл не найден: {path}")
    if not path.is_file():
        raise ToolError(f"Это не файл: {path}")
    if is_forbidden_file(path):
        raise ToolError(f"Чтение файла запрещено политикой безопасности: {path}")

    with open(path, "rb") as file:
        sample = file.read(4096)
    if b"\x00" in sample:
        raise ToolError(f"Бинарный файл запрещен: {path}")

    try:
        sample.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ToolError(f"Файл не похож на UTF-8 текст: {path}") from e


def log_tool_call(name: str, args: dict[str, Any]) -> None:
    logging.info("tool_call %s %s", name, json.dumps(args, ensure_ascii=False))


def list_dir(path: str, limit: int = 200) -> dict[str, Any]:
    limit = _coerce_int(limit, 200, 1, 500)
    log_tool_call("list_dir", {"path": path, "limit": limit})
    directory = resolve_allowed_path(path)
    if not directory.is_dir():
        raise ToolError(f"Это не директория: {directory}")

    items = []
    for child in sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if child.is_dir() and is_excluded_dir(child):
            continue
        if child.is_file() and is_forbidden_file(child):
            continue
        items.append(
            {
                "name": child.name,
                "path": str(child),
                "type": "dir" if child.is_dir() else "file",
                "is_symlink": child.is_symlink(),
                "is_git_repo": child.is_dir() and (child / ".git").exists(),
            }
        )
        if len(items) >= limit:
            break

    return {
        "path": str(directory),
        "items": items,
        "truncated": len(items) >= limit,
    }


def read_file(path: str, max_chars: int | None = None) -> dict[str, Any]:
    effective_max = _coerce_int(max_chars, max_file_chars(), 1, max_file_chars())
    log_tool_call("read_file", {"path": path, "max_chars": effective_max})

    file_path = resolve_allowed_path(path)
    ensure_readable_text_file(file_path)

    text = file_path.read_text(encoding="utf-8")
    return {
        "path": str(file_path),
        "content": text[:effective_max],
        "truncated": len(text) > effective_max,
        "chars": min(len(text), effective_max),
    }


def search_text(root: str, query: str, glob: str | None = None) -> dict[str, Any]:
    if not query:
        raise ToolError("query не задан")

    limit = max_search_results()
    log_tool_call("search_text", {"root": root, "query": query, "glob": glob, "limit": limit})

    root_path = resolve_allowed_path(root)
    if not root_path.is_dir():
        raise ToolError(f"root не директория: {root_path}")

    command = [
        "rg",
        "--line-number",
        "--no-heading",
        "--color",
        "never",
        "--hidden",
        "--glob",
        "!.git/**",
        "--glob",
        "!venv/**",
        "--glob",
        "!.venv/**",
        "--glob",
        "!node_modules/**",
        "--glob",
        "!__pycache__/**",
        "--glob",
        "!media/**",
        "--glob",
        "!uploads/**",
        "--glob",
        "!staticfiles/**",
        "--glob",
        "!.env",
        "--glob",
        "!*.pem",
        "--glob",
        "!*.key",
        "--glob",
        "!*.db",
        "--glob",
        "!*.sqlite",
        "--glob",
        "!*.sqlite3",
        "--glob",
        "!*secret*",
    ]
    if glob:
        command.extend(["--glob", glob])
    command.extend([query, str(root_path)])

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise ToolError((result.stderr or result.stdout or "rg failed").strip())

    lines = result.stdout.splitlines()[:limit]
    return {
        "root": str(root_path),
        "query": query,
        "glob": glob,
        "results": lines,
        "count": len(lines),
        "truncated": len(result.stdout.splitlines()) > limit,
    }


def tree_summary(path: str, depth: int = 2) -> dict[str, Any]:
    depth = max(0, min(int(depth), 5))
    log_tool_call("tree_summary", {"path": path, "depth": depth})

    root = resolve_allowed_path(path)
    if not root.is_dir():
        raise ToolError(f"Это не директория: {root}")

    lines: list[str] = [root.name or str(root)]

    def walk(directory: Path, current_depth: int, prefix: str) -> None:
        if current_depth >= depth:
            return

        try:
            children = [
                child
                for child in sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
                if not (child.is_dir() and is_excluded_dir(child))
                and not (child.is_file() and is_forbidden_file(child))
            ][:80]
        except PermissionError:
            lines.append(f"{prefix}[permission denied]")
            return

        for index, child in enumerate(children):
            connector = "`-- " if index == len(children) - 1 else "|-- "
            lines.append(f"{prefix}{connector}{child.name}{'/' if child.is_dir() else ''}")
            if child.is_dir():
                next_prefix = prefix + ("    " if index == len(children) - 1 else "|   ")
                walk(child, current_depth + 1, next_prefix)

    walk(root, 0, "")
    return {
        "path": str(root),
        "depth": depth,
        "tree": "\n".join(lines),
    }
