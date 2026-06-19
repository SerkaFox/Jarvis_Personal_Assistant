import re
from typing import Any, Callable

import config
from tools_check import safe_code_check
from tools_fs import tree_summary
from tools_git import find_git_repos, git_diff, git_status, resolve_repo
from tools_preview import list_previews, scan_listening_ports
from tools_project import inspect_project, project_structure
from tools_write import workspace_inventory


WORKSPACE_INVENTORY_PHRASES = (
    "твоя папка",
    "твоей папке",
    "твоей папки",
    "рабочая папка",
    "рабочей папке",
    "рабочий каталог",
    "рабочем каталоге",
    "workspace",
    "папки с сайтами",
    "папка с сайтами",
    "сайты в твоей папке",
    "сайтами, какие у тебя",
    "на каких портах",
    "портах висят",
    "какие сайты",
    "проекты в твоей папке",
    "sitebota",
    "test-site",
)

GIT_WORD_RE = re.compile(r"\bgit\b", re.IGNORECASE)
GIT_STATUS_PATTERN = re.compile(r"git\s+status\s+([A-Za-zА-Яа-я0-9_.-]+)", re.IGNORECASE)


def _has_explicit_git_phrase(lowered: str) -> bool:
    if GIT_WORD_RE.search(lowered):
        return True
    return any(word in lowered for word in ("репозитор", "ветки", "ветк"))


LIST_PROJECT_PHRASES = (
    "какие проекты",
    "покажи проекты",
    "что у меня на сервере",
    "какие папки",
    "какие репозитории",
    "где git",
    "на сервере есть проекты",
)

INSPECT_PATTERNS = (
    r"посмотри\s+проект\s+([A-Za-zА-Яа-я0-9_.-]+)",
    r"изучи\s+проект\s+([A-Za-zА-Яа-я0-9_.-]+)",
    r"проверь\s+проект\s+([A-Za-zА-Яа-я0-9_.-]+)",
    r"на\s+ч[её]м\s+остановились(?:\s+в|\s+по)?\s+([A-Za-zА-Яа-я0-9_.-]+)",
    r"что\s+дальше\s+делать\s+по\s+([A-Za-zА-Яа-я0-9_.-]+)",
)

CONTINUATION_PHRASES = (
    "я прошу код изучить",
    "посмотри код",
    "изучи код",
    "на чем остановились",
    "на чём остановились",
)

STRUCTURE_PHRASES = (
    "посмотри структуру",
    "структуру",
    "сколько там файлов",
    "сколько файлов",
    "tree",
    "structure",
)

CHECK_PHRASES = (
    "проверь код",
    "есть ли ошибка",
    "ошибки в проекте",
    "проверь проект",
    "check project",
)

WORKSPACE_WHERE_PHRASES = (
    "где сайт",
    "где папка",
    "по какому адресу",
    "где ты создал",
    "я не вижу сайт",
    "не создал папку",
)

WORKSPACE_PREVIEW_PHRASES = (
    "на каком сервере",
    "ты запустил сервер",
    "запустил сервер",
    "где сервер",
)

CREATE_WORKSPACE_PHRASES = (
    "создай сайт",
    "создай проект",
    "создай страницу",
    "сделай лендинг",
)

PROJECT_ALIASES = {
    "anna": (
        "anna",
        "анна",
        "анны",
        "анне",
        "салон анны",
        "салон anna",
    ),
}


def _repo_names() -> set[str]:
    try:
        return {repo["path"].rstrip("/").split("/")[-1].lower() for repo in find_git_repos()["repositories"]}
    except Exception:
        return set()


def _extract_project(text: str) -> str | None:
    for pattern in INSPECT_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip(" .,;:!?")
    mentioned = extract_mentioned_project(text)
    if mentioned:
        return mentioned
    return None


def extract_mentioned_project(text: str) -> str | None:
    lowered = text.lower()
    names = _repo_names()
    for canonical, aliases in PROJECT_ALIASES.items():
        if canonical in names and any(alias in lowered for alias in aliases):
            return canonical
    for name in names:
        if re.search(rf"\b{re.escape(name)}\b", lowered):
            return name
    return None


def _last_project_from_history(recent_messages: list[dict[str, Any]]) -> str | None:
    names = _repo_names()
    for message in reversed(recent_messages or []):
        content = str(message.get("content", "")).lower()
        mentioned = extract_mentioned_project(content)
        if mentioned:
            return mentioned
        for name in names:
            if re.search(rf"\b{re.escape(name)}\b", content):
                return name
    return None


def _project_from_context(
    text: str,
    recent_messages: list[dict[str, Any]] | None = None,
    current_project: str | None = None,
) -> str | None:
    return extract_mentioned_project(text) or current_project or _last_project_from_history(recent_messages or [])


def _workspace_name_from_text(text: str) -> str | None:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", text)
    stop = {
        "create",
        "project",
        "site",
        "landing",
        "new",
        "test",
        "preview",
        "server",
    }
    candidates = [word for word in words if word.lower() not in stop]
    return candidates[-1] if candidates else None


def detect_intent(
    text: str,
    recent_messages: list[dict[str, Any]] | None = None,
    current_project: str | None = None,
) -> dict[str, Any]:
    lowered = text.lower()

    git_status_match = GIT_STATUS_PATTERN.search(text)
    if git_status_match:
        return {"intent": "git_status", "project": git_status_match.group(1).strip(" .,;:!?")}

    if any(phrase in lowered for phrase in CREATE_WORKSPACE_PHRASES):
        project = _workspace_name_from_text(text)
        if any(word in lowered for word in ("сервер", "preview", "превью")):
            return {"intent": "create_and_preview", "project": project}
        return {"intent": "create_site", "project": project}

    if any(phrase in lowered for phrase in WORKSPACE_PREVIEW_PHRASES):
        return {"intent": "preview_status", "project": _workspace_name_from_text(text)}

    if any(phrase in lowered for phrase in WORKSPACE_WHERE_PHRASES):
        return {"intent": "where_project", "project": _workspace_name_from_text(text)}

    if any(phrase in lowered for phrase in CHECK_PHRASES):
        project = _project_from_context(text, recent_messages, current_project)
        if project:
            return {"intent": "safe_code_check", "project": project}

    if any(phrase in lowered for phrase in STRUCTURE_PHRASES):
        project = _project_from_context(text, recent_messages, current_project)
        if project:
            return {"intent": "project_structure", "project": project}

    project = _extract_project(text)
    if project and any(word in lowered for word in ("проект", "код", "изучи", "посмотри", "проверь")):
        return {"intent": "inspect_project", "project": project}

    if any(phrase in lowered for phrase in CONTINUATION_PHRASES):
        project = _project_from_context(text, recent_messages, current_project)
        if project:
            return {"intent": "inspect_project", "project": project}

    if any(phrase in lowered for phrase in WORKSPACE_INVENTORY_PHRASES) and not _has_explicit_git_phrase(lowered):
        return {"intent": "workspace_inventory"}

    has_project_word = any(word in lowered for word in ("проект", "проекты", "репозитории", "git", "папки"))
    has_list_word = any(word in lowered for word in ("какие", "покажи", "есть", "сервер", "где"))
    if any(phrase in lowered for phrase in LIST_PROJECT_PHRASES) or (has_project_word and has_list_word):
        return {"intent": "list_projects"}

    return {"intent": "normal_chat"}


def _format_projects() -> tuple[str, list[str]]:
    result = find_git_repos()
    lines = ["Git-репозитории по разрешенным roots:", f"Найдено: {result['count']}"]
    for repo in result["repositories"]:
        status = repo.get("status_short") or "clean"
        lines.append(
            "\n".join(
                [
                    f"- {repo.get('path')}",
                    f"  branch: {repo.get('branch') or '-'}",
                    f"  status: {status[:500]}",
                ]
            )
        )
    return "\n".join(lines), ["find_git_repos"]


def format_workspace_inventory(data: dict[str, Any], ports: dict[str, Any]) -> str:
    lines = [
        f"WRITE_ROOT: {data.get('write_root')}",
        f"exists: {data.get('exists')}",
        f"writable: {data.get('writable')}",
    ]
    projects = data.get("projects") or []
    if not projects:
        lines.append("Projects: нет проектов в WRITE_ROOT")
    else:
        lines.append("Projects:")
        for project in projects:
            preview_state = "running" if project.get("running") else "stopped"
            lines.append(f"- {project.get('project_name')}")
            lines.append(f"  path: {project.get('path')}")
            lines.append(f"  files: {project.get('files_count')}")
            lines.append(f"  index.html: {'yes' if project.get('has_index_html') else 'no'}")
            lines.append(f"  preview: {preview_state}")
            port = project.get("preview_port")
            if port:
                lines.append(f"  port: {port}")
                lines.append(f"  url: {project.get('url') or '-'}")
                curl_status = project.get("curl_status")
                lines.append(f"  curl: {curl_status if curl_status is not None else 'failed'}")
            else:
                lines.append("  port: -")

    listening = ports.get("listening") or []
    port_range = ports.get("range") or [None, None]
    lines.append(
        f"Listening preview ports ({port_range[0]}-{port_range[1]}): "
        + (", ".join(str(item["port"]) for item in listening) or "нет")
    )
    suspicious = [item for item in listening if item.get("suspicious")]
    if suspicious:
        lines.append(
            "Suspicious (не зарегистрированы в previews.json, но слушают http.server): "
            + ", ".join(str(item["port"]) for item in suspicious)
        )
    return "\n".join(lines)


def _format_structure(data: dict[str, Any]) -> str:
    counts = data.get("counts", {})
    return "\n".join(
        [
            f"Проект: {data.get('project_name')}",
            f"Путь: {data.get('path')}",
            "Файлы:",
            f"- всего файлов: {counts.get('total_files', 0)}",
            f"- всего директорий: {counts.get('total_dirs', 0)}",
            f"- Python: {counts.get('python_files', 0)}",
            f"- templates: {counts.get('template_files', 0)}",
            f"- JS/TS: {counts.get('js_files', 0)}",
            f"- CSS: {counts.get('css_files', 0)}",
            "",
            "Структура:",
            data.get("tree_summary") or "-",
        ]
    )


def _format_check(data: dict[str, Any]) -> str:
    checks = data.get("checks", {})
    project_type = checks.get("project_type", {}).get("project_type", "unknown")
    py_compile = checks.get("py_compile")
    django_check = checks.get("django_check")
    todos = checks.get("todos", {})
    lines = [
        f"Проверил через встроенный read-only tool: {data.get('project_name')}",
        f"Путь: {data.get('path')}",
        f"Тип проекта: {project_type}",
        f"Git status: {checks.get('git_status', {}).get('status_short') or 'clean'}",
    ]
    if py_compile:
        lines.append(
            f"Python syntax: {'ok' if py_compile.get('ok') else 'errors'} "
            f"({py_compile.get('checked_files', 0)} файлов)"
        )
        for error in py_compile.get("errors", [])[:5]:
            lines.append(f"- {error.get('path')}: {error.get('error')}")
    if django_check:
        if django_check.get("skipped"):
            lines.append(f"Django check: skipped ({django_check.get('reason')})")
        else:
            lines.append(f"Django check returncode: {django_check.get('returncode')}")
            if django_check.get("stdout"):
                lines.append(f"stdout:\n{django_check.get('stdout')}")
            if django_check.get("stderr"):
                lines.append(f"stderr:\n{django_check.get('stderr')}")
    if "node" in checks:
        scripts = checks["node"].get("scripts") or {}
        lines.append("Node scripts: " + (", ".join(scripts.keys()) if scripts else "-"))
    lines.append(f"TODO/FIXME/BUG/HACK: {todos.get('count', 0) if isinstance(todos, dict) else 0}")
    lines.append(f"Git status unchanged: {data.get('git_status_unchanged')}")
    return "\n".join(lines)


def _compact_project_data(data: dict[str, Any]) -> str:
    git = data.get("git", {})
    status = git.get("status", {})
    return "\n".join(
        [
            f"Проект: {data.get('project_name')}",
            f"Путь: {data.get('path')}",
            f"Branch: {status.get('branch') or '-'}",
            f"Remote: {status.get('remote') or '-'}",
            f"Status: {status.get('status_short') or 'clean'}",
            f"Diff stat:\n{git.get('diff_stat') or 'empty'}",
            f"Git log:\n{git.get('log_oneline') or '-'}",
            f"Important files: {', '.join(data.get('important_files') or []) or '-'}",
            f"TODO count: {data.get('todos', {}).get('count', 0)}",
            f"Django: {data.get('django')}",
        ]
    )


def handle_detected_intent(
    detected: dict[str, Any],
    summarize_project: Callable[[dict[str, Any]], str] | None = None,
) -> dict[str, Any]:
    intent = detected.get("intent")
    tools_called: list[str] = []
    errors: list[str] = []

    try:
        if intent == "list_projects":
            answer, tools_called = _format_projects()
            return {"answer": answer, "tools_called": tools_called, "errors": errors}

        if intent == "workspace_inventory":
            data = workspace_inventory()
            previews = list_previews()
            ports = scan_listening_ports()
            tools_called = ["workspace_inventory", "list_previews", "scan_listening_ports"]
            return {
                "answer": format_workspace_inventory(data, ports),
                "tools_called": tools_called,
                "errors": errors,
                "inventory": data,
                "previews": previews,
                "ports": ports,
            }

        if intent == "git_status":
            project = detected.get("project")
            repo = resolve_repo(project)
            status = git_status(str(repo))
            tools_called = ["resolve_repo", "git_status"]
            answer = "\n".join(
                [
                    f"repo: {status.get('path')}",
                    f"branch: {status.get('branch') or '-'}",
                    f"remote:\n{status.get('remote') or '-'}",
                    f"status:\n{status.get('status_short') or 'clean'}",
                ]
            )
            return {
                "answer": answer,
                "tools_called": tools_called,
                "errors": errors,
                "project": repo.name,
                "resolved_path": str(repo),
            }

        if intent == "git_diff":
            project = detected.get("project")
            repo = resolve_repo(project)
            diff = git_diff(str(repo))
            tools_called = ["resolve_repo", "git_diff"]
            answer = "\n".join(
                [
                    f"repo: {diff.get('path')}",
                    f"diff (truncated={diff.get('truncated')}):",
                    diff.get("diff") or "(пусто)",
                ]
            )
            return {
                "answer": answer,
                "tools_called": tools_called,
                "errors": errors,
                "project": repo.name,
                "resolved_path": str(repo),
            }

        if intent == "inspect_project":
            project = detected.get("project")
            data = inspect_project(project)
            tools_called = ["inspect_project"]
            answer = summarize_project(data) if summarize_project else _compact_project_data(data)
            return {
                "answer": answer,
                "tools_called": tools_called,
                "errors": errors,
                "project": data.get("project_name"),
                "project_data": data,
            }

        if intent == "project_structure":
            project = detected.get("project")
            data = project_structure(project)
            tools_called = ["project_structure"]
            return {
                "answer": _format_structure(data),
                "tools_called": tools_called,
                "errors": errors,
                "project": data.get("project_name"),
                "resolved_path": data.get("path"),
                "project_data": data,
            }

        if intent == "safe_code_check":
            project = detected.get("project")
            data = safe_code_check(project)
            tools_called = ["safe_code_check"]
            return {
                "answer": _format_check(data),
                "tools_called": tools_called,
                "errors": errors,
                "project": data.get("project_name"),
                "resolved_path": data.get("path"),
                "project_data": data,
            }
    except Exception as e:
        errors.append(str(e))
        return {"answer": f"Ошибка tool routing: {e}", "tools_called": tools_called, "errors": errors}

    return {"answer": "", "tools_called": tools_called, "errors": errors}
