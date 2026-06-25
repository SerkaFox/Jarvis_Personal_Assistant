"""Claude tool-use agent loop.

Claude directly selects and executes tools — no JSON-plan intermediary.
The bot is only a transport layer: receives Telegram messages, passes them
here, sends back the final text Claude returns.

Flow per message:
  1. user_text + chat_id → run_claude_agent()
  2. Claude replies with tool_use blocks
  3. _dispatch() executes each tool on the server
  4. Results fed back to Claude
  5. Repeat until Claude returns a plain text answer
  6. That text is sent to the user via Telegram
"""
import json
import logging
import os
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

_ProgressCb = Callable[[str], Awaitable[None]]

AGENT_SYSTEM_PROMPT = """
Ты Jarvis — AI-агент на личном Linux-сервере Сергея в Испании.
Работаешь через Telegram-бота. Всегда отвечай по-русски.

Рабочая папка (workspace): /home/seradmin/jarvis_workspace/
— Создавай, редактируй, удаляй проекты ТОЛЬКО внутри неё.
— Любое действие с файлами — через инструменты. Никогда не говори "сделал",
  если инструмент не вернул ok/success.

Порты для preview-серверов: 8700–8799 (открыты в firewall).
После start_preview ВСЕГДА показывай полный URL (http://IP:port/).

При создании сайта:
— Пиши валидный, красивый HTML5/CSS/JS.
— Для слайдера — чистый CSS/JS без зависимостей.
— Для погоды — wttr.in API: https://wttr.in/{city}?format=j1 (JSON).
  Либо сделай нарисованные карточки, если API недоступен из браузера.
— Сначала напиши все файлы, потом запусти start_preview, потом дай ссылку.

Работа с фото от пользователя:
— Пользователь присылает фото → оно сохраняется в pending_media.
— Вызови get_pending_media чтобы увидеть актуальное фото.
— Вызови apply_photo_background чтобы поставить его фоном на сайт.

Никогда не выдумывай результаты инструментов.
Не говори "выполнил команду", если backend не запускал её.
""".strip()

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_workspace",
        "description": "Показать список всех проектов в рабочей папке workspace",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "read_project_files",
        "description": "Прочитать текущие файлы проекта из workspace (HTML/CSS/JS и др.)",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {"type": "string", "description": "Имя проекта"},
            },
            "required": ["project_name"],
        },
    },
    {
        "name": "write_project_file",
        "description": (
            "Записать или перезаписать файл в проект workspace. "
            "Автоматически создаёт проект если он не существует. "
            "filename — относительный путь, например index.html или assets/css/style.css"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {"type": "string"},
                "filename": {"type": "string", "description": "Относительный путь: index.html, assets/css/style.css"},
                "content": {"type": "string", "description": "Полное содержимое файла"},
            },
            "required": ["project_name", "filename", "content"],
        },
    },
    {
        "name": "delete_project",
        "description": "Удалить проект из workspace полностью",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {"type": "string"},
            },
            "required": ["project_name"],
        },
    },
    {
        "name": "start_preview",
        "description": "Запустить HTTP-сервер для проекта. Возвращает port и url.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {"type": "string"},
            },
            "required": ["project_name"],
        },
    },
    {
        "name": "stop_preview",
        "description": "Остановить запущенный HTTP-сервер проекта",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {"type": "string"},
            },
            "required": ["project_name"],
        },
    },
    {
        "name": "list_previews",
        "description": "Показать все запущенные preview-серверы с портами и URL",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "check_site_url",
        "description": "Проверить доступность сайта по HTTP (curl-проверка)",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {"type": "string", "description": "Имя проекта для проверки"},
            },
            "required": ["project_name"],
        },
    },
    {
        "name": "get_pending_media",
        "description": (
            "Получить последнее фото, которое пользователь прислал в Telegram и оно ещё не применено. "
            "Возвращает file_path и media_id."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "apply_photo_background",
        "description": (
            "Применить pending-фото как фоновое изображение сайта. "
            "target: 'whole_page_background' или 'hero_background'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {"type": "string"},
                "media_id": {"type": "integer", "description": "ID из get_pending_media"},
                "target": {
                    "type": "string",
                    "enum": ["whole_page_background", "hero_background"],
                    "description": "Куда применить фон",
                },
            },
            "required": ["project_name", "media_id", "target"],
        },
    },
    {
        "name": "find_git_repos",
        "description": "Найти git-репозитории на сервере (read-only)",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "git_status",
        "description": "Получить git status репозитория",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
            },
            "required": ["repo_path"],
        },
    },
    {
        "name": "git_diff",
        "description": "Получить git diff репозитория",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
            },
            "required": ["repo_path"],
        },
    },
    {
        "name": "service_status",
        "description": "Проверить статус systemd-сервиса (например jarvis-bot, nginx)",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "read_journal",
        "description": "Прочитать последние строки из journalctl для systemd-сервиса",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "lines": {"type": "integer", "default": 50},
            },
            "required": ["name"],
        },
    },
    {
        "name": "search_text",
        "description": "Поиск текста/кода по файлам на сервере (grep)",
        "input_schema": {
            "type": "object",
            "properties": {
                "root": {"type": "string", "description": "Директория для поиска"},
                "query": {"type": "string"},
                "glob": {"type": "string", "description": "Паттерн файлов, например *.py"},
            },
            "required": ["root", "query"],
        },
    },
]


def _make_dispatcher(chat_id: str | None) -> Callable[[str, dict], Any]:
    def dispatch(name: str, args: dict) -> Any:
        if name == "list_workspace":
            from tools_write import list_workspace
            return list_workspace()

        elif name == "read_project_files":
            from tools_edit import read_workspace_project_files
            return read_workspace_project_files(args["project_name"])

        elif name == "write_project_file":
            from tools_write import (
                create_static_site,
                list_workspace,
                write_project_text_file,
                resolve_write_path,
            )
            project = args["project_name"]
            existing = [p["name"] for p in list_workspace().get("projects", [])]
            if project not in existing:
                create_static_site(project)
            result = write_project_text_file(project, args["filename"], args["content"], overwrite=True)
            return {**result, "ok": True}

        elif name == "delete_project":
            from tools_write import delete_workspace_dir
            return delete_workspace_dir(args["project_name"], confirm_token="CONFIRMED")

        elif name == "start_preview":
            from tools_preview import start_preview, detect_lan_ip
            result = start_preview(args["project_name"])
            port = result.get("port")
            if port:
                lan_ip = detect_lan_ip() or "localhost"
                result["url"] = f"http://{lan_ip}:{port}/"
            return result

        elif name == "stop_preview":
            from tools_preview import stop_preview
            return stop_preview(args["project_name"])

        elif name == "list_previews":
            from tools_preview import list_previews, detect_lan_ip
            result = list_previews()
            lan_ip = detect_lan_ip() or "localhost"
            for p in result.get("previews", []):
                if p.get("port"):
                    p["url"] = f"http://{lan_ip}:{p['port']}/"
            return result

        elif name == "check_site_url":
            from tools_preview import list_previews, curl_check
            project = args["project_name"]
            previews = list_previews().get("previews", [])
            match = next((p for p in previews if p.get("project") == project), None)
            if not match:
                return {"ok": False, "error": f"Нет запущенного preview для {project}"}
            return curl_check(match["port"])

        elif name == "get_pending_media":
            from tools_pending_media import get_latest_available_media
            media = get_latest_available_media(chat_id)
            if not media:
                return {"available": False, "message": "Нет ожидающих фото"}
            return {
                "available": True,
                "media_id": media.get("id"),
                "file_path": media.get("file_path"),
                "created_at": media.get("created_at"),
                "status": media.get("status"),
            }

        elif name == "apply_photo_background":
            from tools_pending_media import get_latest_available_media, mark_media_used, mark_media_failed
            from tools_site_operations import op_set_background
            project = args["project_name"]
            media_id = args["media_id"]
            target = args.get("target", "whole_page_background")
            media = get_latest_available_media(chat_id)
            if not media or media.get("id") != media_id:
                return {"ok": False, "error": "Фото не найдено или уже использовано"}
            try:
                result = op_set_background(project, {
                    "type": "set_background",
                    "image_source": "pending_media",
                    "target": target,
                    "image_path": media["file_path"],
                })
                mark_media_used(media_id)
                return {**result, "ok": True, "media_id": media_id}
            except Exception as e:
                mark_media_failed(media_id)
                return {"ok": False, "error": str(e)}

        elif name == "find_git_repos":
            from tools_git import find_git_repos
            return find_git_repos()

        elif name == "git_status":
            from tools_git import git_status
            return git_status(args["repo_path"])

        elif name == "git_diff":
            from tools_git import git_diff
            return git_diff(args["repo_path"], max_chars=5000)

        elif name == "service_status":
            from tools_system import service_status
            return service_status(args["name"])

        elif name == "read_journal":
            from tools_system import read_journal
            return read_journal(args["name"], args.get("lines", 50))

        elif name == "search_text":
            from tools_fs import search_text
            return search_text(args["root"], args["query"], args.get("glob", "*"))

        else:
            return {"error": f"Неизвестный инструмент: {name}"}

    return dispatch


def _extract_text(response) -> str:
    for block in response.content:
        if hasattr(block, "text") and block.text:
            return block.text
    return ""


async def run_claude_agent(
    user_text: str,
    chat_id: str | None = None,
    *,
    progress_callback: _ProgressCb | None = None,
    max_iters: int = 20,
) -> str:
    """Run the Claude tool-use agent loop and return the final text answer."""
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "ANTHROPIC_API_KEY не задан — добавь в .env."

    client = anthropic.AsyncAnthropic(api_key=api_key)
    dispatch = _make_dispatcher(chat_id)

    memory_context = ""
    if chat_id:
        try:
            import memory
            memory_context = memory.build_memory_context(chat_id, user_text) or ""
        except Exception:
            pass

    content = user_text
    if memory_context:
        content = f"Контекст:\n{memory_context}\n\nСообщение:\n{user_text}"

    messages: list[dict[str, Any]] = [{"role": "user", "content": content}]

    model = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
    max_tokens = int(os.getenv("CLAUDE_MAX_TOKENS", "16384"))

    for iteration in range(max_iters):
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=AGENT_SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            return _extract_text(response) or "Готово."

        # Append assistant turn (may contain both text and tool_use blocks)
        messages.append({"role": "assistant", "content": response.content})

        tool_results: list[dict[str, Any]] = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            if progress_callback:
                try:
                    await progress_callback(f"🔧 {block.name}…")
                except Exception:
                    pass

            try:
                result = dispatch(block.name, block.input)
            except Exception as exc:
                logger.exception("Tool %s failed", block.name)
                result = {"error": str(exc)}

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })

        messages.append({"role": "user", "content": tool_results})

    return "Превышен лимит итераций агента."
