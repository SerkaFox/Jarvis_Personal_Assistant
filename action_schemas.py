import json
from pathlib import Path
from typing import Any

from tools_fs import ToolError


REQUIRED_STATIC_FILES = (
    "index.html",
    "assets/css/style.css",
    "assets/js/main.js",
    "README.md",
)


def extract_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise ToolError("Ollama вернула пустой ответ вместо JSON")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ToolError("Ollama вернула не JSON")
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ToolError(f"Ollama JSON невалидный: {exc}") from exc
    if not isinstance(data, dict):
        raise ToolError("Ollama JSON должен быть объектом")
    return data


def _validate_relative_file_path(path: str) -> str:
    if not isinstance(path, str) or not path.strip():
        raise ToolError("file.path должен быть непустой строкой")
    cleaned = path.strip().replace("\\", "/")
    candidate = Path(cleaned)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise ToolError(f"file.path должен быть относительным внутри проекта: {path}")
    if cleaned.startswith(".") or "/." in cleaned:
        raise ToolError(f"Скрытые файлы в site spec запрещены: {path}")
    lowered = cleaned.lower()
    forbidden = (".env", "secret", "token", "key", ".pem", ".sqlite", ".db")
    if any(fragment in lowered for fragment in forbidden):
        raise ToolError(f"Запрещенный файл в site spec: {path}")
    return cleaned


def validate_create_static_site_action(data: dict[str, Any], expected_project_name: str | None = None) -> dict[str, Any]:
    if data.get("action") != "create_static_site":
        raise ToolError("action должен быть create_static_site")
    project_name = str(data.get("project_name") or expected_project_name or "").strip()
    if not project_name:
        raise ToolError("project_name обязателен")
    if expected_project_name and project_name != expected_project_name:
        raise ToolError(f"project_name не совпадает: {project_name} != {expected_project_name}")
    files = data.get("files")
    if not isinstance(files, list) or not files:
        raise ToolError("files должен быть непустым массивом")
    normalized_files = []
    seen = set()
    for item in files:
        if not isinstance(item, dict):
            raise ToolError("Каждый files item должен быть объектом")
        relative_path = _validate_relative_file_path(item.get("path", ""))
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ToolError(f"content обязателен для {relative_path}")
        if "\x00" in content:
            raise ToolError(f"Бинарное содержимое запрещено: {relative_path}")
        if relative_path in seen:
            raise ToolError(f"Дубликат файла в site spec: {relative_path}")
        seen.add(relative_path)
        normalized_files.append({"path": relative_path, "content": content})
    missing = [path for path in REQUIRED_STATIC_FILES if path not in seen]
    if missing:
        raise ToolError("Ollama site spec не содержит обязательные файлы: " + ", ".join(missing))
    return {
        "action": "create_static_site",
        "project_name": project_name,
        "title": str(data.get("title") or project_name),
        "description": str(data.get("description") or ""),
        "files": normalized_files,
        "start_preview": bool(data.get("start_preview", True)),
    }
