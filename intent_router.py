import re
from typing import Any, Callable

import config
from tools_fs import tree_summary
from tools_git import find_git_repos
from tools_project import inspect_project


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
    return None


def _last_project_from_history(recent_messages: list[dict[str, Any]]) -> str | None:
    names = _repo_names()
    for message in reversed(recent_messages or []):
        content = str(message.get("content", "")).lower()
        for name in names:
            if re.search(rf"\b{re.escape(name)}\b", content):
                return name
    return None


def detect_intent(text: str, recent_messages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    lowered = text.lower()

    project = _extract_project(text)
    if project:
        return {"intent": "inspect_project", "project": project}

    if any(phrase in lowered for phrase in CONTINUATION_PHRASES):
        project = _last_project_from_history(recent_messages or [])
        if project:
            return {"intent": "inspect_project", "project": project}

    has_project_word = any(word in lowered for word in ("проект", "проекты", "репозитории", "git", "папки"))
    has_list_word = any(word in lowered for word in ("какие", "покажи", "есть", "сервер", "где"))
    if any(phrase in lowered for phrase in LIST_PROJECT_PHRASES) or (has_project_word and has_list_word):
        return {"intent": "list_projects"}

    return {"intent": "normal_chat"}


def _format_projects() -> tuple[str, list[str]]:
    result = find_git_repos()
    lines = ["Проекты и git-репозитории по разрешенным roots:"]
    for root in config.get_allowed_roots():
        try:
            tree = tree_summary(str(root), depth=1)["tree"]
            lines.append(f"\n{root}:\n{tree}")
        except Exception as e:
            lines.append(f"\n{root}: ошибка дерева: {e}")

    lines.append(f"\nGit-репозитории: {result['count']}")
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
    return "\n".join(lines), ["find_git_repos", "tree_summary"]


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
    except Exception as e:
        errors.append(str(e))
        return {"answer": f"Ошибка tool routing: {e}", "tools_called": tools_called, "errors": errors}

    return {"answer": "", "tools_called": tools_called, "errors": errors}
