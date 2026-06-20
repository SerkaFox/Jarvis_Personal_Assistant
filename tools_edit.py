from pathlib import Path
from typing import Any

from tools_fs import ToolError
from tools_write import (
    FORBIDDEN_WRITE_FRAGMENTS,
    FORBIDDEN_WRITE_NAMES,
    FORBIDDEN_WRITE_SUFFIXES,
    RESERVED_WORKSPACE_NAMES,
    _validate_project_name,
    ensure_write_root,
    resolve_write_path,
    write_project_text_file,
)


MAX_TOTAL_READ_CHARS = 80_000
DEFAULT_INCLUDE_GLOBS = ("index.html", "assets/css/*.css", "assets/js/*.js", "README.md")
READABLE_SUFFIXES = {".html", ".css", ".js", ".md", ".txt", ".json"}


def _is_forbidden_for_read(path: Path) -> bool:
    lowered_name = path.name.lower()
    if lowered_name in FORBIDDEN_WRITE_NAMES:
        return True
    if path.suffix.lower() in FORBIDDEN_WRITE_SUFFIXES:
        return True
    return any(fragment in lowered_name for fragment in FORBIDDEN_WRITE_FRAGMENTS)


def read_workspace_project_files(project_name: str, max_chars_per_file: int = 20000) -> dict[str, Any]:
    ensure_write_root()
    project = _validate_project_name(project_name)
    root = resolve_write_path(project)
    if not root.is_dir():
        raise ToolError(f"Проект не найден в WRITE_ROOT: {root}")

    candidates: list[Path] = []
    seen: set[Path] = set()
    for pattern in DEFAULT_INCLUDE_GLOBS:
        for path in sorted(root.glob(pattern)):
            if path not in seen:
                seen.add(path)
                candidates.append(path)
    for path in sorted(root.rglob("*")):
        if (
            path.is_file()
            and path not in seen
            and ".git" not in path.parts
            and not any(part in RESERVED_WORKSPACE_NAMES for part in path.relative_to(root).parts[:-1])
            and path.suffix.lower() in READABLE_SUFFIXES
        ):
            seen.add(path)
            candidates.append(path)

    files: list[dict[str, Any]] = []
    missing: list[str] = []
    total_chars = 0
    for path in candidates:
        relative = str(path.relative_to(root))
        if not path.is_file():
            missing.append(relative)
            continue
        if _is_forbidden_for_read(path):
            continue
        raw = path.read_bytes()
        if b"\x00" in raw[:4096]:
            continue
        text = raw.decode("utf-8", errors="replace")
        truncated = len(text) > max_chars_per_file
        content = text[:max_chars_per_file]
        files.append({"path": relative, "content": content, "chars": len(content), "truncated": truncated})
        total_chars += len(content)
        if total_chars >= MAX_TOTAL_READ_CHARS:
            break

    for required in ("index.html",):
        if not (root / required).is_file():
            missing.append(required)

    return {
        "project_name": project,
        "path": str(root),
        "files": files,
        "missing": missing,
    }


def apply_file_updates(project_name: str, files: list[dict[str, str]]) -> dict[str, Any]:
    ensure_write_root()
    project = _validate_project_name(project_name)
    resolve_write_path(project)
    if not files:
        raise ToolError("Нет файлов для записи")

    modified_files: list[str] = []
    errors: list[str] = []
    for file_spec in files:
        relative_path = file_spec.get("path") if isinstance(file_spec, dict) else None
        content = file_spec.get("content") if isinstance(file_spec, dict) else None
        if not relative_path or content is None:
            errors.append(f"Некорректная запись файла: {file_spec}")
            continue
        try:
            result = write_project_text_file(project, relative_path, content, overwrite=True)
            modified_files.append(result["path"])
        except ToolError as e:
            errors.append(f"{relative_path}: {e}")

    return {
        "project_name": project,
        "modified_files": modified_files,
        "errors": errors,
        "success": bool(modified_files) and not errors,
    }


def verify_workspace_project(project_name: str) -> dict[str, Any]:
    ensure_write_root()
    project = _validate_project_name(project_name)
    root = resolve_write_path(project)
    exists = root.is_dir()
    index_html = exists and (root / "index.html").is_file()
    css_files = sorted(str(p) for p in (root / "assets" / "css").glob("*.css")) if exists else []
    js_files = sorted(str(p) for p in (root / "assets" / "js").glob("*.js")) if exists else []
    files_count = sum(1 for p in root.rglob("*") if p.is_file()) if exists else 0
    required_files = {
        "index.html": index_html,
        "assets/css": bool(css_files),
        "assets/js": bool(js_files),
    }
    return {
        "project_name": project,
        "path": str(root),
        "exists": exists,
        "index_html": index_html,
        "css_files": css_files,
        "js_files": js_files,
        "files_count": files_count,
        "required_files": required_files,
        "success": exists and index_html and bool(css_files) and bool(js_files),
    }
