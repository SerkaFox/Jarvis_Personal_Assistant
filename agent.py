import json
import logging
from typing import Any, Callable

from tools_fs import (
    ToolError,
    list_dir,
    read_file,
    search_text,
    tree_summary,
)
from tools_git import find_git_repos, git_status
from tools_shell import read_journal, service_status


TOOL_REGISTRY: dict[str, Callable[..., dict[str, Any]]] = {
    "list_dir": list_dir,
    "read_file": read_file,
    "search_text": search_text,
    "find_git_repos": find_git_repos,
    "git_status": git_status,
    "tree_summary": tree_summary,
    "service_status": service_status,
    "read_journal": read_journal,
}

PLANNER_SYSTEM_PROMPT = """
Ты планировщик read-only инструментов Jarvis.
Верни только валидный JSON, без Markdown.
Если инструменты не нужны, верни {"use_tools": false, "reason": "...", "tools": []}.
Если нужны, верни {"use_tools": true, "reason": "...", "tools": [{"name": "...", "args": {...}}]}.
Доступные tools:
- list_dir(path)
- read_file(path, max_chars)
- search_text(root, query, glob)
- find_git_repos(root)
- git_status(repo_path)
- tree_summary(path, depth)
- service_status(name)
- read_journal(service_name, lines)
Правила: только чтение, не планируй записи, sudo, restart, deploy, rm/mv/cp, git pull/push/commit.
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


def build_planner_messages(user_text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]


def execute_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
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
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": FINAL_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Запрос пользователя:\n"
                f"{user_text}\n\n"
                "JSON-план:\n"
                f"{json.dumps(plan, ensure_ascii=False)}\n\n"
                "Результаты инструментов:\n"
                f"{json.dumps(tool_results, ensure_ascii=False, indent=2)}"
            ),
        },
    ]


def answer_with_tools(user_text: str, ask_model: Callable[[list[dict[str, str]]], str]) -> str | None:
    planner_response = ask_model(build_planner_messages(user_text))
    try:
        plan = _extract_json(planner_response)
    except (json.JSONDecodeError, ValueError):
        return None

    if not plan.get("use_tools"):
        return None

    try:
        tool_results = execute_plan(plan)
    except (ToolError, ValueError) as e:
        logging.warning("agent_plan_error %s", e)
        return None

    return ask_model(build_final_messages(user_text, plan, tool_results))
