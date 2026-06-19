import json
import logging
from typing import Any, Callable

import config
from tools_fs import (
    ToolError,
    list_dir,
    read_file,
    search_text,
    tree_summary,
)
from tools_git import find_git_repos, git_diff, git_status
from tools_project import inspect_project
from tools_system import read_journal, service_status


TOOL_REGISTRY: dict[str, Callable[..., dict[str, Any]]] = {
    "list_dir": list_dir,
    "read_file": read_file,
    "search_text": search_text,
    "find_git_repos": find_git_repos,
    "git_status": git_status,
    "git_diff": git_diff,
    "inspect_project": inspect_project,
    "tree_summary": tree_summary,
    "service_status": service_status,
    "read_journal": read_journal,
}

PLANNER_SYSTEM_PROMPT = """
Ты планировщик read-only инструментов Jarvis.
Верни только валидный JSON, без Markdown.
Ты работаешь на реальном сервере пользователя, а не в абстрактном /home/user.
Для вопросов о проектах, папках, сервере, git-репозиториях, статусе, diff, логах или поиске по коду ОБЯЗАТЕЛЬНО выбирай tools.
Если инструменты не нужны, верни {"use_tools": false, "reason": "...", "tools": []}.
Если нужны, верни {"use_tools": true, "reason": "...", "tools": [{"name": "...", "args": {...}}]}.
Также можно вернуть один action: {"action": "search_text", "args": {...}}.
Доступные tools:
- list_dir(path)
- read_file(path, max_chars)
- search_text(root, query, glob)
- find_git_repos(root)
- git_status(repo_path)
- git_diff(repo_path, max_chars)
- inspect_project(repo_name_or_path)
- tree_summary(path, depth)
- service_status(name)
- read_journal(name, lines)
- final_answer(answer)
Правила: только чтение, не планируй записи, sudo, restart, deploy, rm/mv/cp, git pull/push/commit.
Примеры:
Вопрос "Какие проекты у меня есть на сервере и какие из них git-репозитории?" -> {"use_tools": true, "tools": [{"name": "find_git_repos", "args": {}}]}.
Вопрос "где используется booking_calendar_day" -> {"use_tools": true, "tools": [{"name": "search_text", "args": {"root": "/home/seradmin", "query": "booking_calendar_day"}}]}.
Вопрос "посмотри проект anna, на чем остановились" -> {"use_tools": true, "tools": [{"name": "inspect_project", "args": {"repo_name_or_path": "anna"}}]}.
""".strip()

FINAL_SYSTEM_PROMPT = """
Ты Jarvis, локальный read-only server/code agent.
Отвечай по-русски, кратко и по делу.
Используй только результаты инструментов как факты о сервере.
Если инструмент вернул ошибку или данных недостаточно, скажи это прямо.
Не предлагай опасные команды без явного запроса пользователя.
""".strip()


def _extract_json(text: str) -> dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.removeprefix("json").strip()
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("JSON plan must be object")
    return parsed


def build_planner_messages(user_text: str, memory_context: str = "") -> list[dict[str, str]]:
    content = user_text
    if memory_context:
        content = f"Контекст памяти и истории:\n{memory_context}\n\nЗапрос пользователя:\n{user_text}"
    return [
        {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def heuristic_plan(user_text: str) -> dict[str, Any] | None:
    text = user_text.lower()
    project_inspect_phrases = (
        "посмотри проект",
        "на чем остановились",
        "на чём остановились",
        "изучи код",
        "что дальше делать по",
    )
    if any(phrase in text for phrase in project_inspect_phrases):
        words = user_text.replace(",", " ").split()
        for word in reversed(words):
            cleaned = word.strip(" .:;!?").strip()
            if cleaned and cleaned.lower() not in {"проект", "код", "по", "в", "на", "чем", "чём", "остановились"}:
                return {
                    "use_tools": True,
                    "tools": [{"name": "inspect_project", "args": {"repo_name_or_path": cleaned}}],
                }
    project_words = ("проект", "проекты", "projects", "репозитор", "git", "repository", "repositories")
    server_words = ("сервер", "server", "папк", "директор")
    if any(word in text for word in project_words) and any(word in text for word in server_words + project_words):
        return {"use_tools": True, "tools": [{"name": "find_git_repos", "args": {}}]}
    return None


def execute_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    if plan.get("action") == "final_answer":
        return []
    if plan.get("action") and "tools" not in plan:
        plan = {
            "use_tools": True,
            "tools": [{"name": plan.get("action"), "args": plan.get("args") or {}}],
        }

    if not plan.get("use_tools"):
        return []

    tool_calls = plan.get("tools")
    if not isinstance(tool_calls, list):
        raise ValueError("tools must be list")
    if len(tool_calls) > 8:
        raise ValueError("too many tool calls")

    results = []
    for index, tool_call in enumerate(tool_calls, start=1):
        if not isinstance(tool_call, dict):
            raise ValueError("tool call must be object")

        name = tool_call.get("name")
        args = tool_call.get("args") or {}
        if name not in TOOL_REGISTRY:
            raise ValueError(f"unknown tool: {name}")
        if not isinstance(args, dict):
            raise ValueError(f"args must be object for {name}")

        logging.info("agent_execute_tool %s %s", name, json.dumps(args, ensure_ascii=False))
        try:
            output = TOOL_REGISTRY[name](**args)
            results.append(
                {
                    "index": index,
                    "tool": name,
                    "args": args,
                    "ok": True,
                    "output": output,
                }
            )
        except Exception as e:
            results.append(
                {
                    "index": index,
                    "tool": name,
                    "args": args,
                    "ok": False,
                    "error": str(e),
                }
            )

    return results


def build_final_messages(
    user_text: str,
    plan: dict[str, Any],
    tool_results: list[dict[str, Any]],
    memory_context: str = "",
) -> list[dict[str, str]]:
    context_block = f"Контекст памяти и истории:\n{memory_context}\n\n" if memory_context else ""
    return [
        {"role": "system", "content": FINAL_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                context_block
                + "Запрос пользователя:\n"
                f"{user_text}\n\n"
                "JSON-план:\n"
                f"{json.dumps(plan, ensure_ascii=False)}\n\n"
                "Результаты инструментов:\n"
                f"{json.dumps(tool_results, ensure_ascii=False, indent=2)}"
            ),
        },
    ]


def answer_with_tools(
    user_text: str,
    ask_model: Callable[[list[dict[str, str]]], str],
    memory_context: str = "",
) -> str | None:
    if not config.AGENT_TOOLS_ENABLED:
        return None

    plan = heuristic_plan(user_text)
    if plan is None:
        planner_response = ask_model(build_planner_messages(user_text, memory_context))
        try:
            plan = _extract_json(planner_response)
        except (json.JSONDecodeError, ValueError):
            return None

    if plan.get("action") == "final_answer":
        answer = plan.get("answer")
        return answer if isinstance(answer, str) and answer.strip() else None

    if not plan.get("use_tools") and not plan.get("action"):
        return None

    try:
        tool_results = execute_plan(plan)
    except (ToolError, ValueError) as e:
        logging.warning("agent_plan_error %s", e)
        return None

    return ask_model(build_final_messages(user_text, plan, tool_results, memory_context))
