import os
import json
import logging
import re
import shutil
import subprocess
import tempfile
import traceback
import requests
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)
from dotenv import load_dotenv

import config
from action_schemas import extract_json_object, validate_create_static_site_action
from agent import answer_with_tools
from intent_router import detect_intent, extract_mentioned_project, handle_detected_intent
import memory
from tools_fs import allowed_roots_info, search_text, tree_summary
from tools_git import find_git_repos, git_diff, git_status, resolve_repo
from tools_project import inspect_project, project_structure
from tools_check import safe_code_check
from tools_system import get_allowed_services, read_journal
from tools_preview import list_previews, preview_status, start_preview, stop_preview
from tools_errors import error_summary, latest_error, mask_error_text, save_last_error
from tools_write import (
    create_flask_site,
    create_project_dir,
    create_static_site,
    delete_workspace_dir,
    delete_workspace_file,
    list_workspace,
    read_workspace_file,
    update_static_site_file,
    verify_project_files,
    verify_static_site,
    write_project_text_file,
    write_text_file,
    workspace_tree,
)

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


class SecretMaskingLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = BOT_TOKEN_RE.sub("bot[MASKED]", record.getMessage())
        record.args = ()
        return True


for handler in logging.getLogger().handlers:
    handler.addFilter(SecretMaskingLogFilter())

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.0.145:11434")
MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")

STT_URL = os.getenv("STT_URL", "http://127.0.0.1:8091")
STT_TOKEN = os.getenv("STT_TOKEN", "")

TTS_ENABLED = os.getenv("TTS_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
TTS_ENGINE = os.getenv("TTS_ENGINE", "piper").lower()
PIPER_BIN = os.getenv("PIPER_BIN", "/usr/local/bin/piper")
PIPER_MODEL = os.getenv(
    "PIPER_MODEL",
    "/home/seradmin/jarvis_bot/models/tts/ru_RU/model.onnx",
)
PIPER_CONFIG = os.getenv(
    "PIPER_CONFIG",
    "/home/seradmin/jarvis_bot/models/tts/ru_RU/model.onnx.json",
)
TTS_TMP_DIR = Path(os.getenv("TTS_TMP_DIR", "/tmp/jarvis_tts"))
TTS_MAX_CHARS = 900


class TTSError(RuntimeError):
    pass


def get_system_prompt() -> str:
    now = datetime.now(ZoneInfo("Europe/Madrid"))
    current_time = now.strftime("%d.%m.%Y %H:%M")

    return (
        "Ты локальный ИИ-ассистент Jarvis на личной AI-станции Сергея. "
        "Отвечай по-русски, естественно и по делу. "
        "Ты работаешь через Telegram-бота. "
        f"Текущая дата и время в Испании: {current_time}. "
        "Если пользователь спрашивает дату, день недели или время — используй эту дату, а не свои старые знания. "
        "Помогай с Linux, Django, Python, сервером, Telegram-ботами, Ollama, Codex и администрированием. "
        "У тебя есть локальная память SQLite: используй историю диалога, memories и project_notes, когда они добавлены в контекст. "
        "Не говори, что у тебя нет доступа, если доступны read-only tools. "
        "Если tools доступны, никогда не говори 'у меня нет доступа к файловой системе'. "
        "Для серверных/проектных вопросов backend должен вызывать tools. Если ты видишь tool results — отвечай по ним. "
        "Если пользователь говорит 'это', 'он', 'там', 'проект' — используй последние сообщения для контекста. "
        "Не придумывай результаты команд. Если нужны логи или вывод команды — попроси пользователя выполнить команду или скажи, какую команду выполнить. "
        "Не говори 'выполнил команду', если backend не запускал такую команду. "
        "Если использовался встроенный tool, говори 'Проверил через встроенный tool' или 'По данным read-only анализа'. "
        "Не выдумывай команды вроде ls -la, если реально использовался tree_summary/project_structure/safe_code_check. "
        "Не говори 'создал', 'записал', 'удалил' или 'запустил', если write/preview tool не вернул успешный результат. "
        "После write/preview действия всегда показывай tools_called, actual_path и созданные/измененные/удаленные файлы или preview_url. "
        "Для опасных действий, таких как удаление файлов, миграции, рестарт сервисов, deploy, git push или изменения nginx/systemd, требуй явное подтверждение. "
        "Не говори, что ты облачный сервис. Ты локальный Jarvis, подключенный к Ollama."
    )


def is_allowed(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ALLOWED_USER_ID


def ask_ollama(user_text: str, chat_id: str | None = None) -> str:
    memory_context = memory.build_memory_context(chat_id, user_text) if chat_id else ""
    content = user_text
    if memory_context:
        content = f"Контекст памяти и истории:\n{memory_context}\n\nЗапрос пользователя:\n{user_text}"
    return ask_ollama_messages(
        [
            {
                "role": "system",
                "content": get_system_prompt(),
            },
            {
                "role": "user",
                "content": content,
            },
        ]
    )


def ask_ollama_messages(messages: list[dict[str, str]]) -> str:
    payload = {
        "model": MODEL,
        "stream": False,
        "messages": messages,
    }

    r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=180)
    r.raise_for_status()
    return r.json()["message"]["content"]


def memory_status_answer(user_text: str) -> str | None:
    lowered = user_text.lower()
    if not any(phrase in lowered for phrase in ("ты сохраняешь историю", "сохраняешь историю", "есть история")):
        return None
    if not config.MEMORY_ENABLED:
        return "Memory сейчас выключена: MEMORY_ENABLED=false."
    try:
        memory.init_db()
        return "Да, сохраняю последние сообщения в SQLite. База: data/jarvis.db."
    except Exception as e:
        return f"Memory недоступна: {e}"


CREATE_PROJECT_PHRASES = (
    "создай сайт",
    "создай тестовый сайт",
    "создай проект",
    "создай страницу",
    "с нуля создай сайт",
    "с нуля сделай сайт",
    "сделай лендинг",
)

EDIT_PROJECT_PHRASES = (
    "удали файл",
    "отредактируй",
    "добавь секцию",
    "измени дизайн",
    "добавь анимацию",
)


def _slug_from_text(text: str, fallback: str = "test-site") -> str:
    folder_match = re.search(r"(?:в\s+папке|папку|проект(?:е)?\s+)([A-Za-z0-9_.-]+)", text, re.IGNORECASE)
    if folder_match:
        return folder_match.group(1).strip(" .,:;!?")
    words = re.findall(r"[A-Za-z0-9_-]+", text)
    stop = {"create", "project", "site", "landing", "new", "test"}
    candidates = [word for word in words if word.lower() not in stop]
    return candidates[-1] if candidates else fallback


def _wants_preview(text: str) -> bool:
    lowered = text.lower()
    return any(
        phrase in lowered
        for phrase in (
            "запусти временный сервер",
            "запусти сервер",
            "временный сервер",
            "запусти preview",
            "запусти превью",
            "preview",
            "превью",
            "запусти сайт",
        )
    )


def _last_action_path() -> Path:
    path = Path(os.getenv("JARVIS_DB_PATH", config.JARVIS_DB_PATH)).resolve().parent / "last_actions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_last_actions() -> dict:
    path = _last_action_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def save_last_action(chat_id: str | None, action: dict) -> dict:
    key = str(chat_id or "global")
    payload = {
        "timestamp": datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "intent": action.get("intent"),
        "project_name": action.get("project_name"),
        "success": bool(action.get("success")),
        "path": action.get("path") or "",
        "created_files": action.get("created_files") or [],
        "preview_url": action.get("preview_url") or "",
        "error": str(action.get("error") or ""),
        "tools_called": action.get("tools_called") or [],
    }
    data = _load_last_actions()
    data[key] = payload
    _last_action_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def get_last_action(chat_id: str | None) -> dict | None:
    data = _load_last_actions()
    return data.get(str(chat_id or "global"))


def _format_last_action(action: dict | None) -> str:
    if not action:
        return "last_action: нет сохраненных действий для этого чата"
    lines = [
        f"timestamp: {action.get('timestamp')}",
        f"intent: {action.get('intent')}",
        f"project_name: {action.get('project_name')}",
        f"success: {action.get('success')}",
        f"path: {action.get('path') or '-'}",
        f"preview_url: {action.get('preview_url') or '-'}",
        f"tools_called: {', '.join(action.get('tools_called') or []) or '-'}",
        f"error: {action.get('error') or '-'}",
        "created_files:",
    ]
    created = action.get("created_files") or []
    lines.extend(f"- {item}" for item in created) if created else lines.append("-")
    return "\n".join(lines)


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
        "workspace",
    }
    candidates = [word for word in words if word.lower() not in stop]
    return candidates[-1] if candidates else None


def _workspace_project_from_context(text: str, chat_id: str | None) -> str | None:
    explicit = _workspace_name_from_text(text)
    if explicit:
        return explicit
    try:
        projects = list_workspace()["projects"]
        lowered = text.lower()
        for item in projects:
            if item["name"].lower() in lowered:
                return item["name"]
    except Exception:
        pass
    action = get_last_action(chat_id)
    if action and action.get("project_name"):
        return str(action["project_name"])
    current = memory.get_current_project(chat_id) if chat_id else None
    if current and _workspace_project_exists(current):
        return current
    return None


def _is_workspace_status_question(text: str) -> bool:
    lowered = text.lower()
    return any(
        phrase in lowered
        for phrase in (
            "где сайт",
            "где папка",
            "на каком сервере",
            "по какому адресу",
            "ты запустил сервер",
            "я не вижу сайт",
            "где ты создал",
            "не создал папку",
            "почему не создал",
        )
    )


def _wants_start_preview(text: str) -> bool:
    lowered = text.lower()
    return "запусти" in lowered and any(word in lowered for word in ("сервер", "preview", "превью"))


def looks_like_fake_action_response(text: str) -> bool:
    lowered = (text or "").lower()
    return any(
        marker in lowered
        for marker in (
            "mkdir ",
            "cat >",
            "cat <<",
            "python3 -m http.server",
            "python -m http.server",
            "создаю файл",
            "запускаю сервер",
            "файл создан",
        )
    )


def ask_ollama_for_site_spec(user_text: str, project_name: str) -> dict:
    prompt = f"""
Верни только JSON. Не пиши markdown. Не пиши bash-команды. Не используй mkdir/cat/python/http.server.
Твоя задача: сгенерировать спецификацию статического сайта, который backend запишет через tools.

Строгий формат:
{{
  "action": "create_static_site",
  "project_name": "{project_name}",
  "title": "Site title",
  "description": "Short description",
  "files": [
    {{"path": "index.html", "content": "..."}},
    {{"path": "assets/css/style.css", "content": "..."}},
    {{"path": "assets/js/main.js", "content": "..."}},
    {{"path": "README.md", "content": "..."}}
  ],
  "start_preview": true
}}

Требования:
- project_name строго "{project_name}";
- все path только относительные внутри проекта;
- минимум файлы index.html, assets/css/style.css, assets/js/main.js, README.md;
- без внешних CDN, удаленных шрифтов и внешних скриптов;
- современный responsive landing page;
- hero, features, workflow/use-cases, contact CTA;
- CSS animations и prefers-reduced-motion;
- JS только локальный и безопасный;
- HTML должен ссылаться на assets/css/style.css и assets/js/main.js.

Запрос пользователя:
{user_text}
""".strip()
    raw = ask_ollama_messages(
        [
            {"role": "system", "content": "Ты генератор JSON action specs для Jarvis backend. Возвращай только валидный JSON."},
            {"role": "user", "content": prompt},
        ]
    )
    if looks_like_fake_action_response(raw):
        raise RuntimeError("Ollama попыталась заменить JSON action текстовыми shell-командами")
    data = extract_json_object(raw)
    return validate_create_static_site_action(data, expected_project_name=project_name)


def _format_write_success(
    action: str,
    tools_called: list[str],
    actual_path: str,
    created: list[str] | None = None,
    modified: list[str] | None = None,
    deleted: list[str] | None = None,
    preview_url: str | None = None,
) -> str:
    lines = [
        action,
        f"tools_called: {', '.join(tools_called)}",
        f"actual_path: {actual_path}",
    ]
    if created is not None:
        lines.append("created files:")
        lines.extend(f"- {path}" for path in created)
    if modified is not None:
        lines.append("modified files:")
        lines.extend(f"- {path}" for path in modified)
    if deleted is not None:
        lines.append("deleted files:")
        lines.extend(f"- {path}" for path in deleted)
    if preview_url:
        lines.append(f"preview_url: {preview_url}")
    else:
        lines.append("preview command: /preview_start <project>")
    return "\n".join(lines)


def workspace_where_answer(project_name: str | None, chat_id: str | None = None) -> tuple[str, dict]:
    tools_called = ["verify_project_files", "preview_status"]
    if not project_name:
        action = get_last_action(chat_id)
        if not action or not action.get("success"):
            return (
                "Я не вижу подтверждения, что сайт был создан. Проверяю workspace...\n"
                f"WRITE_ROOT: {config.get_write_root()}\n"
                "Проект не определен. Укажи имя: /where <project>",
                {"detected": {"intent": "where_project"}, "tools_called": [], "errors": ["project not resolved"]},
            )
        project_name = str(action["project_name"])
    verify = verify_project_files(project_name)
    exists = Path(verify["path"]).is_dir()
    status = preview_status(project_name)
    if not exists:
        answer = "\n".join(
            [
                "Я не вижу подтверждения, что сайт был создан. Проверяю workspace...",
                f"project_name: {project_name}",
                "exists: false",
                f"path: {verify['path']}",
                "Проект не найден в WRITE_ROOT",
                f"preview running: {status.get('running', False)}",
            ]
        )
    else:
        required = {
            "index": str(Path(verify["path"]) / "index.html"),
            "css": str(Path(verify["path"]) / "assets" / "css" / "style.css"),
            "js": str(Path(verify["path"]) / "assets" / "js" / "main.js"),
            "readme": str(Path(verify["path"]) / "README.md"),
        }
        lines = [
            f"project_name: {project_name}",
            "exists: true",
            f"path: {verify['path']}",
            "files exist:",
        ]
        lines.extend(f"- {key}: {Path(path).is_file()} ({path})" for key, path in required.items())
        lines.append(f"preview running: {status.get('running', False)}")
        if status.get("url"):
            lines.append(f"url: {status['url']}")
        elif status.get("running") is False:
            lines.append("url: -")
        answer = "\n".join(lines)
    return (
        answer,
        {
            "detected": {"intent": "where_project", "project": project_name},
            "tools_called": tools_called,
            "errors": [] if exists else ["project not found"],
            "resolved_path": verify.get("path"),
            "project": project_name,
        },
    )


def create_site_workflow(
    user_text: str,
    project_name: str | None = None,
    chat_id: str | None = None,
    start_preview_requested: bool = True,
    site_spec_provider=None,
    with_preview: bool | None = None,
) -> tuple[str, dict]:
    if project_name is None:
        project_name = user_text
        user_text = f"создай сайт {project_name}"
    if with_preview is not None:
        start_preview_requested = with_preview
    detected = {"intent": "create_and_preview" if start_preview_requested else "create_site", "project": project_name}
    tools_called: list[str] = []
    action = {
        "intent": detected["intent"],
        "project_name": project_name,
        "success": False,
        "path": "",
        "created_files": [],
        "preview_url": "",
        "error": "",
        "tools_called": tools_called,
    }
    if not config.env_bool("WRITE_MODE_ENABLED", config.WRITE_MODE_ENABLED):
        action["error"] = "Write mode выключен. Включи WRITE_MODE_ENABLED=true в .env"
        saved = save_last_action(chat_id, action)
        return (
            saved["error"],
            {"detected": detected, "tools_called": tools_called, "errors": [saved["error"]], "resolved_path": str(config.get_write_root())},
        )
    try:
        provider = site_spec_provider or ask_ollama_for_site_spec
        spec = provider(user_text, project_name)
        tools_called.append("ask_ollama_for_site_spec")
        spec = validate_create_static_site_action(spec, expected_project_name=project_name)
        tools_called.append("validate_create_static_site_action")
        project = create_project_dir(project_name)
        tools_called.append("create_project_dir")
        created_files = []
        for file_spec in spec["files"]:
            result = write_project_text_file(project_name, file_spec["path"], file_spec["content"], overwrite=False)
            created_files.append(result["path"])
        tools_called.append("write_text_file")
        verify = verify_project_files(project_name)
        tools_called.append("verify_project_files")
        if not verify.get("success"):
            raise RuntimeError("Файлы проекта не созданы: " + ", ".join(verify.get("missing") or []))
        preview = None
        curl_check = None
        should_start_preview = bool(start_preview_requested)
        if should_start_preview:
            preview = start_preview(project_name)
            tools_called.append("start_preview")
            status = preview_status(project_name)
            tools_called.append("preview_status")
            if not preview.get("success") or not status.get("running"):
                raise RuntimeError("Preview tool не подтвердил запущенный процесс")
            response = requests.get(f"http://127.0.0.1:{preview['port']}/", timeout=5)
            response.raise_for_status()
            if "<html" not in response.text.lower():
                raise RuntimeError("Preview HTTP-check не увидел HTML проекта")
            curl_check = {"success": True, "url": f"http://127.0.0.1:{preview['port']}/", "status_code": response.status_code}
            tools_called.append("curl_localhost")
        if chat_id:
            memory.set_current_project(chat_id, project["project_name"])
        action.update(
            {
                "project_name": project["project_name"],
                "success": True,
                "path": project["path"],
                "created_files": created_files,
                "preview_url": preview["url"] if preview else "",
                "tools_called": tools_called,
            }
        )
        save_last_action(chat_id, action)
        answer = _format_write_success(
            "Создал проект в WRITE_ROOT." if not preview else "Создал проект в WRITE_ROOT и запустил preview.",
            tools_called,
            project["path"],
            created=created_files,
            preview_url=preview["url"] if preview else None,
        )
        if curl_check:
            answer += f"\ncurl_check: success={curl_check['success']} status={curl_check['status_code']} url={curl_check['url']}"
        return (
            answer,
            {
                "detected": detected,
                "tools_called": tools_called,
                "errors": [],
                "resolved_path": project["path"],
                "project": project["project_name"],
                "preview_url": preview["url"] if preview else "",
                "curl_check": curl_check or {},
            },
        )
    except Exception as e:
        action["error"] = str(e)
        save_last_action(chat_id, action)
        save_last_error(chat_id=chat_id or "", user_id="", handler="create_site_workflow", error=e, user_text=user_text)
        if "json" in str(e).lower() or "ollama" in str(e).lower():
            message = f"Генерация JSON не удалась: {e}. Файлы не создавались."
        else:
            message = f"Генерация/создание сайта не выполнены: {e}"
        return (
            message,
            {"detected": detected, "tools_called": tools_called, "errors": [str(e)], "project": project_name},
        )


def fixture_site_spec(user_text: str, project_name: str) -> dict:
    return {
        "action": "create_static_site",
        "project_name": project_name,
        "title": "Jarvis Selftest",
        "description": "Temporary workspace selftest site",
        "files": [
            {
                "path": "index.html",
                "content": (
                    "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
                    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
                    "<title>Jarvis Selftest</title><link rel=\"stylesheet\" href=\"assets/css/style.css\">"
                    "</head><body><main class=\"hero\"><h1>Jarvis Selftest</h1>"
                    "<p>Workspace write and preview fixture.</p></main>"
                    "<script src=\"assets/js/main.js\"></script></body></html>"
                ),
            },
            {
                "path": "assets/css/style.css",
                "content": (
                    "body{margin:0;font-family:Arial,sans-serif;background:#f8fafc;color:#111827}"
                    ".hero{min-height:100vh;display:grid;place-items:center;text-align:center;padding:32px}"
                    "h1{font-size:clamp(32px,8vw,72px)}"
                    "@media (prefers-reduced-motion:no-preference){.hero{animation:fade .4s ease-out}}"
                    "@keyframes fade{from{opacity:.2;transform:translateY(8px)}to{opacity:1;transform:none}}"
                ),
            },
            {"path": "assets/js/main.js", "content": "document.documentElement.dataset.jarvisSelftest='ok';\n"},
            {"path": "README.md", "content": "# Jarvis Selftest\n\nTemporary selftest project.\n"},
        ],
        "start_preview": True,
    }


def workspace_status_answer(user_text: str, chat_id: str | None = None) -> tuple[str, dict] | None:
    if not _is_workspace_status_question(user_text) and not _wants_start_preview(user_text):
        return None
    project = _workspace_project_from_context(user_text, chat_id)
    if _wants_start_preview(user_text) and project:
        tools_called = ["verify_project_files", "start_preview", "preview_status"]
        try:
            verify = verify_project_files(project)
            if not Path(verify["path"]).is_dir() or not verify.get("success"):
                raise RuntimeError("Проект не найден или файлы неполные")
            preview = start_preview(project)
            status = preview_status(project)
            if not preview.get("success") or not status.get("running"):
                raise RuntimeError("Preview tool не подтвердил запущенный процесс")
            save_last_action(
                chat_id,
                {
                    "intent": "start_preview",
                    "project_name": project,
                    "success": True,
                    "path": verify["path"],
                    "created_files": [],
                    "preview_url": preview["url"],
                    "tools_called": tools_called,
                },
            )
            return (
                _format_write_success(
                    "Запустил preview для workspace-проекта.",
                    tools_called,
                    verify["path"],
                    preview_url=preview["url"],
                ),
                {
                    "detected": {"intent": "preview_start", "project": project},
                    "tools_called": tools_called,
                    "errors": [],
                    "resolved_path": verify["path"],
                    "project": project,
                    "preview_url": preview["url"],
                },
            )
        except Exception as e:
            save_last_action(chat_id, {"intent": "start_preview", "project_name": project, "success": False, "error": str(e), "tools_called": tools_called})
            save_last_error(chat_id=chat_id or "", user_id="", handler="workspace_status_answer", error=e, user_text=user_text)
            return (
                f"Preview не запущен: {e}",
                {"detected": {"intent": "preview_start", "project": project}, "tools_called": tools_called, "errors": [str(e)]},
            )
    return workspace_where_answer(project, chat_id=chat_id)


def _workspace_project_exists(name: str) -> bool:
    try:
        return any(project["name"] == name for project in list_workspace()["projects"])
    except Exception:
        return False


def _workspace_edit_answer(user_text: str, chat_id: str | None) -> tuple[str, dict] | None:
    lowered = user_text.lower()
    if not any(phrase in lowered for phrase in EDIT_PROJECT_PHRASES):
        return None
    project = memory.get_current_project(chat_id) if chat_id else None
    if not project or not _workspace_project_exists(project):
        return None
    detected = {"intent": "edit_workspace_project", "project": project}
    if not config.env_bool("WRITE_MODE_ENABLED", config.WRITE_MODE_ENABLED):
        return (
            "Write mode выключен: WRITE_MODE_ENABLED=false. Файлы не изменены.",
            {"detected": detected, "tools_called": [], "errors": [], "resolved_path": str(config.get_write_root())},
        )
    try:
        index = read_workspace_file(f"{project}/index.html")["content"]
        marker = "<!-- jarvis-extra-section -->"
        section = """
    <!-- jarvis-extra-section -->
    <section class="section cta cta--secondary">
      <h2>Новая секция</h2>
      <p>Jarvis добавил этот блок через безопасный write tool внутри WRITE_ROOT.</p>
    </section>
"""
        if marker not in index:
            index = index.replace("  </main>", section + "  </main>")
            result = update_static_site_file(project, "index.html", index, overwrite=True)
            answer = _format_write_success(
                "Изменил файл в workspace.",
                ["read_workspace_file", "update_static_site_file"],
                str(config.get_write_root() / project),
                modified=[result["path"]],
            )
            return (
                answer,
                {
                    "detected": detected,
                    "tools_called": ["read_workspace_file", "update_static_site_file"],
                    "errors": [],
                    "resolved_path": str(config.get_write_root() / project),
                    "project": project,
                },
            )
        return (
            "Секция уже есть, файл не изменял.",
            {"detected": detected, "tools_called": ["read_workspace_file"], "errors": [], "project": project},
        )
    except Exception as e:
        return (
            f"Не смог изменить проект в WRITE_ROOT: {e}",
            {"detected": detected, "tools_called": ["read_workspace_file", "update_static_site_file"], "errors": [str(e)]},
        )


def write_mode_answer(user_text: str, chat_id: str | None = None) -> tuple[str, dict] | None:
    lowered = user_text.lower()
    edit_answer = _workspace_edit_answer(user_text, chat_id)
    if edit_answer:
        return edit_answer
    has_create_intent = any(phrase in lowered for phrase in CREATE_PROJECT_PHRASES) or (
        "в папке" in lowered and any(word in lowered for word in ("создай", "сделай"))
    )
    if not has_create_intent:
        return None
    name = _slug_from_text(user_text)
    if "flask" in lowered:
        detected = {"intent": "create_workspace_project", "project": name}
        if not config.env_bool("WRITE_MODE_ENABLED", config.WRITE_MODE_ENABLED):
            error = "Write mode выключен. Включи WRITE_MODE_ENABLED=true в .env"
            save_last_action(chat_id, {"intent": "create_flask_site", "project_name": name, "success": False, "error": error})
            return (error, {"detected": detected, "tools_called": [], "errors": [error], "resolved_path": str(config.get_write_root())})
        try:
            data = create_flask_site(name)
            if not data.get("success"):
                raise RuntimeError("create_flask_site не вернул success=True")
            if chat_id:
                memory.set_current_project(chat_id, data["project_name"])
            save_last_action(
                chat_id,
                {
                    "intent": "create_flask_site",
                    "project_name": data["project_name"],
                    "success": True,
                    "path": data["path"],
                    "created_files": data["created_files"],
                    "tools_called": ["create_flask_site"],
                },
            )
            return (
                _format_write_success("Создал Flask-проект в WRITE_ROOT.", ["create_flask_site"], data["path"], created=data["created_files"]),
                {"detected": detected, "tools_called": ["create_flask_site"], "errors": [], "resolved_path": data["path"], "project": data["project_name"]},
            )
        except Exception as e:
            save_last_action(chat_id, {"intent": "create_flask_site", "project_name": name, "success": False, "error": str(e), "tools_called": ["create_flask_site"]})
            save_last_error(chat_id=chat_id or "", user_id="", handler="write_mode_answer", error=e, user_text=user_text)
            return (f"Не смог создать Flask-проект в WRITE_ROOT: {e}", {"detected": detected, "tools_called": ["create_flask_site"], "errors": [str(e)]})
    return create_site_workflow(user_text, project_name=name, chat_id=chat_id, start_preview_requested=True)


def summarize_project_with_ollama(data: dict) -> str:
    summary = ask_ollama_messages(project_summary_prompt(data))
    memory.save_project_note(
        data["project_name"],
        data["path"],
        summary,
        data["git"].get("last_commit", ""),
    )
    return summary


def pending_task_for_text(user_text: str) -> str | None:
    lowered = user_text.lower()
    has_create_intent = any(phrase in lowered for phrase in CREATE_PROJECT_PHRASES) or (
        "в папке" in lowered and any(word in lowered for word in ("создай", "сделай"))
    )
    if has_create_intent:
        return "create_workspace_project"
    if any(phrase in lowered for phrase in EDIT_PROJECT_PHRASES):
        return "edit_workspace_project"
    if any(phrase in lowered for phrase in ("проверь код", "есть ли ошибка", "ошибки в проекте", "проверь проект", "check project")):
        return "safe_code_check"
    if any(phrase in lowered for phrase in ("посмотри проект", "изучи проект", "изучи код", "на чем остановились", "на чём остановились")):
        return "inspect_project"
    if any(phrase in lowered for phrase in ("preview", "превью", "запусти сайт", "запусти проект")):
        return "preview"
    return None


def answer_user_text(
    user_text: str,
    use_agent: bool,
    chat_id: str | None = None,
    recent_messages: list[dict] | None = None,
    debug: bool = False,
) -> tuple[str, dict]:
    age = memory.age_answer(user_text)
    if age:
        detected = {"intent": "age"}
        return age, {"detected": detected, "tools_called": [], "errors": []}
    memory_status = memory_status_answer(user_text)
    if memory_status:
        detected = {"intent": "memory_status"}
        return memory_status, {"detected": detected, "tools_called": [], "errors": []}
    status_answer = workspace_status_answer(user_text, chat_id=chat_id)
    if status_answer:
        return status_answer
    write_answer = write_mode_answer(user_text, chat_id=chat_id)
    if write_answer:
        return write_answer

    current_project = memory.get_current_project(chat_id) if chat_id else None
    mentioned_project = extract_mentioned_project(user_text)
    if mentioned_project and chat_id:
        memory.set_current_project(chat_id, mentioned_project)
        current_project = mentioned_project

    detected = detect_intent(user_text, recent_messages or [], current_project=current_project)
    if detected.get("project") and chat_id:
        memory.set_current_project(chat_id, detected["project"])
        current_project = detected["project"]

    if detected.get("intent") != "normal_chat":
        routed = handle_detected_intent(detected, summarize_project=summarize_project_with_ollama)
        return routed["answer"], {"detected": detected, "current_project": current_project, **routed}

    memory_context = memory.build_memory_context(chat_id, user_text) if chat_id else ""
    if use_agent:
        agent_answer = answer_with_tools(user_text, ask_ollama_messages, memory_context=memory_context)
        if agent_answer:
            if looks_like_fake_action_response(agent_answer):
                name = _slug_from_text(user_text)
                return create_site_workflow(
                    "Модель попыталась заменить действие текстом. Запускаю tool workflow.\n" + user_text,
                    project_name=name,
                    chat_id=chat_id,
                    start_preview_requested=True,
                )
            return agent_answer, {"detected": detected, "current_project": current_project, "tools_called": ["agent"], "errors": []}
    ollama_answer = ask_ollama(user_text, chat_id=chat_id)
    if looks_like_fake_action_response(ollama_answer):
        name = _slug_from_text(user_text)
        return create_site_workflow(
            "Модель попыталась заменить действие текстом. Запускаю tool workflow.\n" + user_text,
            project_name=name,
            chat_id=chat_id,
            start_preview_requested=True,
        )
    return ollama_answer, {"detected": detected, "current_project": current_project, "tools_called": [], "errors": []}


def transcribe_audio_file(path: str) -> dict:
    headers = {
        "X-JARVIS-TOKEN": STT_TOKEN,
    }

    with open(path, "rb") as f:
        files = {
            "file": (Path(path).name, f, "application/octet-stream"),
        }
        r = requests.post(
            f"{STT_URL}/api/stt/transcribe",
            headers=headers,
            files=files,
            timeout=180,
        )

    r.raise_for_status()
    return r.json()


def _resolve_executable(path_or_name: str, label: str) -> str:
    if os.path.isabs(path_or_name):
        if Path(path_or_name).is_file() and os.access(path_or_name, os.X_OK):
            return path_or_name
        raise TTSError(f"{label} не найден или не исполняемый: {path_or_name}")

    resolved = shutil.which(path_or_name)
    if resolved:
        return resolved
    raise TTSError(f"{label} не найден в PATH: {path_or_name}")


def synthesize_tts(text: str) -> Path:
    if not TTS_ENABLED:
        raise TTSError("TTS выключен: TTS_ENABLED=false")
    if TTS_ENGINE != "piper":
        raise TTSError(f"Неподдерживаемый TTS_ENGINE: {TTS_ENGINE}")

    piper_bin = _resolve_executable(PIPER_BIN, "Piper")
    ffmpeg_bin = _resolve_executable("ffmpeg", "ffmpeg")

    model_path = Path(PIPER_MODEL)
    config_path = Path(PIPER_CONFIG)
    if not model_path.is_file():
        raise TTSError(f"Модель Piper не найдена: {model_path}")
    if not config_path.is_file():
        raise TTSError(f"Конфиг Piper не найден: {config_path}")

    tts_text = " ".join(text.split()).strip()
    if not tts_text:
        raise TTSError("Нет текста для озвучивания")
    if len(tts_text) > TTS_MAX_CHARS:
        tts_text = (
            tts_text[:TTS_MAX_CHARS].rsplit(" ", 1)[0].strip()
            or tts_text[:TTS_MAX_CHARS]
        )

    TTS_TMP_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        dir=TTS_TMP_DIR,
        suffix=".wav",
        delete=False,
    ) as wav_tmp:
        wav_path = Path(wav_tmp.name)

    ogg_path = TTS_TMP_DIR / f"{wav_path.stem}.ogg"

    try:
        piper_result = subprocess.run(
            [
                piper_bin,
                "--model",
                str(model_path),
                "--config",
                str(config_path),
                "--output_file",
                str(wav_path),
            ],
            input=tts_text,
            text=True,
            capture_output=True,
            timeout=120,
        )
        if piper_result.returncode != 0:
            error_text = (piper_result.stderr or piper_result.stdout or "").strip()
            raise TTSError(
                f"Piper завершился с ошибкой: {error_text or piper_result.returncode}"
            )

        ffmpeg_result = subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-i",
                str(wav_path),
                "-c:a",
                "libopus",
                "-b:a",
                "32k",
                "-vbr",
                "on",
                str(ogg_path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if ffmpeg_result.returncode != 0:
            error_text = (ffmpeg_result.stderr or ffmpeg_result.stdout or "").strip()
            raise TTSError(
                f"ffmpeg завершился с ошибкой: {error_text or ffmpeg_result.returncode}"
            )

        return ogg_path
    except subprocess.TimeoutExpired as e:
        raise TTSError(f"TTS команда не ответила вовремя: {e.cmd}") from e
    finally:
        wav_path.unlink(missing_ok=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    await update.message.reply_text(
        "Jarvis online. Локальный Ollama подключен. Голосовые тоже слушаю."
    )


async def reply_long(message, text: str, limit: int = 4000):
    text = memory.mask_secrets(text)
    if not text:
        await message.reply_text("Пустой ответ.")
        return

    for start in range(0, len(text), limit):
        await message.reply_text(text[start : start + limit])


async def progress(message, text: str):
    safe_text = mask_error_text(text)[:900]
    logging.info("progress %s", safe_text)
    if message:
        await message.reply_text(safe_text)


def _error_context(update: Update | None) -> tuple[str, str, str]:
    if not update:
        return "", "", ""
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    user_id = str(update.effective_user.id) if update.effective_user else ""
    user_text = ""
    if update.effective_message and getattr(update.effective_message, "text", None):
        user_text = update.effective_message.text or ""
    return chat_id, user_id, user_text


async def _report_handler_error(
    *,
    update: Update | None,
    handler: str,
    error: BaseException,
    user_text: str = "",
) -> dict:
    chat_id, user_id, detected_text = _error_context(update)
    if not user_text:
        user_text = detected_text
    tb_text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    saved = save_last_error(
        chat_id=chat_id,
        user_id=user_id,
        handler=handler,
        error=error,
        user_text=user_text,
        tb_text=tb_text,
    )
    logging.error("jarvis_handler_error %s\n%s", saved["error_type"], saved["traceback"])
    message = update.effective_message if update and update.effective_message else None
    if message:
        try:
            await message.reply_text(f"Ошибка Jarvis: {saved['error_type']}. Детали сохранены в /last_error")
        except Exception:
            logging.exception("failed_to_send_error_message")
    return saved


async def roots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    try:
        info = allowed_roots_info()
        text = (
            "Разрешенные корни:\n"
            + "\n".join(f"- {root}" for root in info["allowed_roots"])
            + f"\n\nMAX_FILE_CHARS={info['max_file_chars']}"
            + f"\nMAX_SEARCH_RESULTS={info['max_search_results']}"
        )
    except Exception as e:
        text = f"Ошибка /roots: {e}"

    await update.message.reply_text(text)


async def repos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    try:
        result = find_git_repos()
        lines = [f"Найдено репозиториев: {result['count']}"]
        for repo in result["repositories"]:
            status = repo.get("status_short") or "clean"
            lines.append(
                "\n".join(
                    [
                        f"- {repo.get('path')}",
                        f"  branch: {repo.get('branch') or '-'}",
                        f"  remote: {repo.get('remote') or '-'}",
                        f"  status: {status[:500]}",
                    ]
                )
            )
        text = "\n".join(lines)
    except Exception as e:
        text = f"Ошибка /repos: {e}"

    await reply_long(update.message, text)


async def projects_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await repos(update, context)


def chat_user_ids(update: Update) -> tuple[str, str]:
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    user_id = str(update.effective_user.id) if update.effective_user else ""
    return chat_id, user_id


def _format_git_status(repo: dict) -> str:
    return "\n".join(
        [
            f"repo: {repo.get('path')}",
            f"branch: {repo.get('branch') or '-'}",
            f"remote:\n{repo.get('remote') or '-'}",
            f"status:\n{repo.get('status_short') or 'clean'}",
        ]
    )


def _format_structure_result(data: dict) -> str:
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


def _format_check_result(data: dict) -> str:
    checks = data.get("checks", {})
    project_type = checks.get("project_type", {}).get("project_type", "unknown")
    lines = [
        f"Проверил через встроенный read-only tool: {data.get('project_name')}",
        f"Путь: {data.get('path')}",
        f"Тип проекта: {project_type}",
        f"Git status: {checks.get('git_status', {}).get('status_short') or 'clean'}",
    ]

    py_compile = checks.get("py_compile")
    if py_compile:
        lines.append(
            f"Python syntax: {'ok' if py_compile.get('ok') else 'errors'} "
            f"({py_compile.get('checked_files', 0)} файлов)"
        )
        for error in py_compile.get("errors", [])[:5]:
            lines.append(f"- {error.get('path')}: {error.get('error')}")

    django_check = checks.get("django_check")
    if django_check:
        if django_check.get("skipped"):
            lines.append(f"Django check: skipped ({django_check.get('reason')})")
        else:
            lines.append(f"Django check returncode: {django_check.get('returncode')}")
            if django_check.get("stdout"):
                lines.append(f"stdout:\n{django_check.get('stdout')}")
            if django_check.get("stderr"):
                lines.append(f"stderr:\n{django_check.get('stderr')}")

    node = checks.get("node")
    if node:
        scripts = node.get("scripts") or {}
        lines.append("Node scripts: " + (", ".join(scripts.keys()) if scripts else "-"))

    todos = checks.get("todos", {})
    todo_count = todos.get("count", 0) if isinstance(todos, dict) else 0
    lines.append(f"TODO/FIXME/BUG/HACK: {todo_count}")
    lines.append(f"Git status unchanged: {data.get('git_status_unchanged')}")
    return "\n".join(lines)


def _format_project_report(structure: dict, inspection: dict, check: dict) -> str:
    git = inspection.get("git", {})
    status = git.get("status", {})
    todos = inspection.get("todos", {})
    return "\n\n".join(
        [
            _format_structure_result(structure),
            "\n".join(
                [
                    "Git:",
                    f"branch: {status.get('branch') or '-'}",
                    f"remote: {status.get('remote') or '-'}",
                    f"status: {status.get('status_short') or 'clean'}",
                    f"last commits:\n{git.get('log_oneline') or '-'}",
                    f"diff stat:\n{git.get('diff_stat') or 'empty'}",
                    f"TODO/FIXME/HACK/BUG: {todos.get('count', 0) if isinstance(todos, dict) else 0}",
                ]
            ),
            _format_check_result(check),
        ]
    )


async def git_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    if not context.args:
        await update.message.reply_text("Использование: /git <repo_name_or_path>")
        return

    try:
        repo_path = resolve_repo(" ".join(context.args))
        await reply_long(update.message, _format_git_status(git_status(str(repo_path))))
    except Exception as e:
        await update.message.reply_text(f"Ошибка /git: {e}")


async def diff_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    if not context.args:
        await update.message.reply_text("Использование: /diff <repo_name_or_path>")
        return

    try:
        repo_path = resolve_repo(" ".join(context.args))
        result = git_diff(str(repo_path))
        diff_text = result["diff"] or "diff пустой"
        suffix = "\n\n[truncated]" if result.get("truncated") else ""
        await reply_long(update.message, f"repo: {result['path']}\n\n{diff_text}{suffix}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка /diff: {e}")


async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Использование: /find <query>")
        return

    try:
        lines = [f"query: {query}"]
        for root in config.get_allowed_roots():
            result = search_text(str(root), query)
            lines.append(f"\n{root} ({result['count']}):")
            lines.extend(result["results"] or ["ничего не найдено"])
        await reply_long(update.message, "\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Ошибка /find: {e}")


async def tree_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    path = " ".join(context.args).strip() if context.args else str(config.get_allowed_roots()[0])
    try:
        if context.args:
            try:
                result = project_structure(path)
                chat_id, _ = chat_user_ids(update)
                memory.set_current_project(chat_id, result["project_name"])
                await reply_long(update.message, _format_structure_result(result))
                return
            except Exception:
                pass
        result = tree_summary(path)
        await reply_long(update.message, result["tree"])
    except Exception as e:
        await update.message.reply_text(f"Ошибка /tree: {e}")


async def structure_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /structure <repo>")
        return
    try:
        data = project_structure(" ".join(context.args))
        chat_id, _ = chat_user_ids(update)
        memory.set_current_project(chat_id, data["project_name"])
        await reply_long(update.message, _format_structure_result(data))
    except Exception as e:
        await update.message.reply_text(f"Ошибка /structure: {e}")


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /check <repo>")
        return
    try:
        data = safe_code_check(" ".join(context.args))
        chat_id, _ = chat_user_ids(update)
        memory.set_current_project(chat_id, data["project_name"])
        await reply_long(update.message, _format_check_result(data))
    except Exception as e:
        await update.message.reply_text(f"Ошибка /check: {e}")


def _write_mode_status_text() -> str:
    return "\n".join(
        [
            f"WRITE_MODE_ENABLED={config.env_bool('WRITE_MODE_ENABLED', config.WRITE_MODE_ENABLED)}",
            f"WRITE_ROOT={config.get_write_root()}",
            "Это sandbox для новых тестовых проектов, не deploy.",
        ]
    )


async def write_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(_write_mode_status_text())


async def workspace_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    try:
        data = list_workspace()
        lines = [
            f"WRITE_ROOT: {data['write_root']}",
            f"WRITE_MODE_ENABLED: {data['enabled']}",
            f"Проектов: {data['count']}",
        ]
        lines.extend(f"- {item['name']} ({item['path']})" for item in data["projects"])
        await reply_long(update.message, "\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Ошибка /workspace: {e}")


async def new_static_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /new_static <name>")
        return
    try:
        data = create_static_site(context.args[0])
        verify = verify_static_site(data["project_name"])
        if not data.get("success") or not verify.get("success"):
            raise RuntimeError("create_static_site не подтвердил создание файлов")
        chat_id, _ = chat_user_ids(update)
        memory.set_current_project(chat_id, data["project_name"])
        save_last_action(
            chat_id,
            {
                "intent": "create_site",
                "project_name": data["project_name"],
                "success": True,
                "path": data["path"],
                "created_files": data["created_files"],
                "tools_called": ["create_static_site", "verify_static_site"],
            },
        )
        await reply_long(
            update.message,
            _format_write_success(
                "Создал статический сайт в WRITE_ROOT.",
                ["create_static_site", "verify_static_site"],
                data["path"],
                created=data["created_files"],
            ),
        )
    except Exception as e:
        chat_id, user_id = chat_user_ids(update)
        save_last_error(chat_id=chat_id, user_id=user_id, handler="new_static_command", error=e, user_text=update.message.text or "")
        await update.message.reply_text(f"Ошибка /new_static: {e}")


async def new_flask_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /new_flask <name>")
        return
    try:
        data = create_flask_site(context.args[0])
        chat_id, _ = chat_user_ids(update)
        memory.set_current_project(chat_id, data["project_name"])
        await reply_long(
            update.message,
            _format_write_success(
                "Создал Flask-проект в WRITE_ROOT.",
                ["create_flask_site"],
                data["path"],
                created=data["created_files"],
            ),
        )
    except Exception as e:
        chat_id, user_id = chat_user_ids(update)
        save_last_error(chat_id=chat_id, user_id=user_id, handler="new_flask_command", error=e, user_text=update.message.text or "")
        await update.message.reply_text(f"Ошибка /new_flask: {e}")


async def preview_info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /preview_info <name>")
        return
    try:
        data = workspace_tree(context.args[0], depth=2)
        project_path = data["path"]
        lines = [
            f"Проект: {project_path}",
            "Static preview:",
            f"cd {project_path}",
            "python3 -m http.server 8000",
            "",
            "Flask preview:",
            f"cd {project_path}",
            "python3 -m venv venv",
            "venv/bin/pip install -r requirements.txt",
            "venv/bin/python app.py",
        ]
        await reply_long(update.message, "\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Ошибка /preview_info: {e}")


async def workspace_tree_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    try:
        data = workspace_tree(context.args[0] if context.args else None)
        await reply_long(update.message, f"{data['path']}\n\n{data['tree']}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка /workspace_tree: {e}")


async def write_file_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    raw = update.message.text or ""
    payload = raw.split(maxsplit=2)
    if len(payload) < 3:
        await update.message.reply_text("Использование: /write_file <project>/<file> <text content>")
        return
    path, content = payload[1], payload[2]
    try:
        result = write_text_file(path, content, overwrite=True)
        await reply_long(
            update.message,
            _format_write_success(
                "Записал файл в WRITE_ROOT.",
                ["write_text_file"],
                result["path"],
                modified=[result["path"]],
            ),
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка /write_file: {e}")


async def delete_file_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /delete_file <project>/<file>")
        return
    try:
        result = delete_workspace_file(context.args[0])
        await reply_long(
            update.message,
            _format_write_success(
                "Удалил файл из WRITE_ROOT.",
                ["delete_workspace_file"],
                result["path"],
                deleted=[result["path"]],
            ),
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка /delete_file: {e}")


async def preview_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /preview_start <project>")
        return
    try:
        result = start_preview(context.args[0])
        status = preview_status(context.args[0])
        if not result.get("success") or not status.get("running"):
            raise RuntimeError("Preview tool не подтвердил запущенный процесс")
        chat_id, _ = chat_user_ids(update)
        save_last_action(
            chat_id,
            {
                "intent": "start_preview",
                "project_name": result["project"],
                "success": True,
                "path": result["path"],
                "preview_url": result["url"],
                "tools_called": ["start_preview", "preview_status"],
            },
        )
        await reply_long(
            update.message,
            _format_write_success(
                "Запустил preview для workspace-проекта.",
                ["start_preview", "preview_status"],
                result["path"],
                preview_url=result["url"],
            ),
        )
    except Exception as e:
        chat_id, user_id = chat_user_ids(update)
        save_last_error(chat_id=chat_id, user_id=user_id, handler="preview_start_command", error=e, user_text=update.message.text or "")
        await update.message.reply_text(f"Ошибка /preview_start: {e}")


async def preview_stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /preview_stop <project>")
        return
    try:
        result = stop_preview(context.args[0])
        await reply_long(
            update.message,
            "\n".join(
                [
                    "Остановил preview.",
                    "tools_called: stop_preview",
                    f"project: {result.get('project')}",
                    f"stopped: {result.get('stopped')}",
                ]
            ),
        )
    except Exception as e:
        chat_id, user_id = chat_user_ids(update)
        save_last_error(chat_id=chat_id, user_id=user_id, handler="preview_stop_command", error=e, user_text=update.message.text or "")
        await update.message.reply_text(f"Ошибка /preview_stop: {e}")


async def preview_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    try:
        result = list_previews()
        lines = [f"Preview процессов: {result['count']}"]
        for item in result["previews"]:
            lines.append(f"- {item['project']} pid={item['pid']} port={item['port']} url={item['url']}")
        await reply_long(update.message, "\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Ошибка /preview_list: {e}")


async def preview_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /preview_status <project>")
        return
    try:
        result = preview_status(context.args[0])
        await reply_long(update.message, "\n".join(f"{key}: {value}" for key, value in result.items()))
    except Exception as e:
        await update.message.reply_text(f"Ошибка /preview_status: {e}")


async def where_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id, _ = chat_user_ids(update)
    project = context.args[0] if context.args else None
    try:
        answer, debug = workspace_where_answer(project, chat_id=chat_id)
        await reply_long(update.message, answer)
        await maybe_send_intent_debug(update.message, context, debug)
    except Exception as e:
        chat_id, user_id = chat_user_ids(update)
        save_last_error(chat_id=chat_id, user_id=user_id, handler="where_command", error=e, user_text=update.message.text or "")
        await update.message.reply_text(f"Ошибка /where: {e}")


async def create_site_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /create_site <project>")
        return
    chat_id, _ = chat_user_ids(update)
    await progress(update.message, f"Принял: создаю проект {context.args[0]} через Ollama + tools.")
    await progress(update.message, "Шаг 1/5: прошу Ollama сгенерировать структуру сайта...")
    await progress(update.message, "Шаг 2/5: проверяю JSON...")
    await progress(update.message, "Шаг 3/5: записываю файлы...")
    try:
        answer, debug = create_site_workflow(update.message.text or "", project_name=context.args[0], chat_id=chat_id, start_preview_requested=False)
        await progress(update.message, "Шаг 5/5: проверяю результат...")
        await reply_long(update.message, answer)
        await maybe_send_intent_debug(update.message, context, debug)
    except Exception as e:
        chat_id, user_id = chat_user_ids(update)
        save_last_error(chat_id=chat_id, user_id=user_id, handler="create_site_command", error=e, user_text=update.message.text or "")
        await update.message.reply_text(f"Ошибка /create_site: {e}")


async def create_and_preview_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /create_and_preview <project>")
        return
    chat_id, _ = chat_user_ids(update)
    project = context.args[0]
    await progress(update.message, f"Принял: создаю проект {project} через Ollama + tools.")
    await progress(update.message, "Шаг 1/5: прошу Ollama сгенерировать структуру сайта...")
    await progress(update.message, "Шаг 2/5: проверяю JSON...")
    await progress(update.message, "Шаг 3/5: записываю файлы...")
    await progress(update.message, "Шаг 4/5: запускаю preview...")
    await progress(update.message, "Шаг 5/5: проверяю curl...")
    try:
        answer, debug = create_site_workflow(update.message.text or "", project_name=project, chat_id=chat_id, start_preview_requested=True)
        await reply_long(update.message, answer)
        await maybe_send_intent_debug(update.message, context, debug)
    except Exception as e:
        chat_id, user_id = chat_user_ids(update)
        save_last_error(chat_id=chat_id, user_id=user_id, handler="create_and_preview_command", error=e, user_text=update.message.text or "")
        await update.message.reply_text(f"Ошибка /create_and_preview: {e}")


async def last_action_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id, _ = chat_user_ids(update)
    await reply_long(update.message, _format_last_action(get_last_action(chat_id)))


def selftest_workspace_result() -> dict:
    if not config.env_bool("WRITE_MODE_ENABLED", config.WRITE_MODE_ENABLED):
        return {
            "success": False,
            "error": "Write mode выключен. Включи WRITE_MODE_ENABLED=true в .env",
            "write_root": str(config.get_write_root()),
        }
    project = "__jarvis_selftest_static__"
    cleanup = None
    try:
        if Path(config.get_write_root() / project).exists():
            delete_workspace_dir(project, confirm_token=f"DELETE:{project}")
        answer, debug = create_site_workflow(
            "selftest workspace static site",
            project_name=project,
            chat_id="selftest",
            start_preview_requested=True,
            site_spec_provider=fixture_site_spec,
        )
        if debug.get("errors"):
            raise RuntimeError("; ".join(debug["errors"]))
        status = preview_status(project)
        if not status.get("running"):
            raise RuntimeError("selftest preview not running")
        response = requests.get(f"http://127.0.0.1:{status['port']}/", timeout=5)
        response.raise_for_status()
        if "Jarvis Selftest" not in response.text:
            raise RuntimeError("preview response does not contain expected HTML")
        stopped = stop_preview(project)
        cleanup = delete_workspace_dir(project, confirm_token=f"DELETE:{project}")
        return {
            "success": True,
            "write_root": str(config.get_write_root()),
            "project": project,
            "created_files": get_last_action("selftest").get("created_files", []),
            "preview_url": status["url"],
            "stopped": stopped.get("stopped"),
            "cleanup": cleanup.get("deleted"),
            "tools_called": debug.get("tools_called", []),
        }
    except Exception:
        try:
            stop_preview(project)
        except Exception:
            pass
        try:
            cleanup = delete_workspace_dir(project, confirm_token=f"DELETE:{project}")
        except Exception:
            cleanup = None
        raise


async def selftest_workspace_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await progress(update.message, "Принял: запускаю selftest workspace")
    try:
        result = selftest_workspace_result()
        await reply_long(update.message, "\n".join(f"{key}: {value}" for key, value in result.items()))
    except Exception as e:
        chat_id, user_id = chat_user_ids(update)
        save_last_error(chat_id=chat_id, user_id=user_id, handler="selftest_workspace_command", error=e, user_text=update.message.text or "")
        await update.message.reply_text(f"Ошибка /selftest_workspace: {e}. Детали сохранены в /last_error")


async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    if not context.args:
        await update.message.reply_text("Использование: /logs <service>")
        return

    try:
        result = read_journal(context.args[0], lines=80)
        await reply_long(update.message, result["output"] or "journal пустой")
    except Exception as e:
        await update.message.reply_text(f"Ошибка /logs: {e}")


async def bot_logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    try:
        result = read_journal("jarvis-bot", lines=80)
        await reply_long(update.message, mask_error_text(result["output"] or "journal пустой"))
    except Exception as e:
        await update.message.reply_text(f"Ошибка /bot_logs: {e}")


async def last_error_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id, _ = chat_user_ids(update)
    item = latest_error(chat_id)
    if not item:
        await update.message.reply_text("Ошибок нет.")
        return
    lines = [
        f"timestamp: {item.get('timestamp')}",
        f"handler: {item.get('handler')}",
        f"error_type: {item.get('error_type')}",
        f"error_message: {item.get('error_message')}",
        f"user_text: {item.get('user_text') or '-'}",
        "traceback:",
        item.get("traceback") or "-",
    ]
    await reply_long(update.message, "\n".join(lines))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    await update.message.reply_text(
        "\n".join(
            [
                "Jarvis commands:",
                "/roots - разрешенные roots",
                "/repos - найти git-репозитории",
                "/projects - алиас /repos",
                "/git <repo> - status, branch, remote",
                "/diff <repo> - git diff",
                "/find <query> - поиск по ALLOWED_ROOTS",
                "/tree <path> - краткое дерево",
                "/structure <repo> - структура и счетчики проекта",
                "/check <repo> - безопасная read-only проверка кода",
                "/workspace - WRITE_ROOT и тестовые проекты",
                "/write_mode - состояние write sandbox",
                "/new_static <name> - создать статический сайт в WRITE_ROOT",
                "/create_site <project> - создать статический сайт и проверить файлы",
                "/create_and_preview <project> - создать сайт, проверить файлы и запустить preview",
                "/where <project> - показать путь, файлы и preview status проекта",
                "/new_flask <name> - создать Flask-проект в WRITE_ROOT",
                "/write_file <project>/<file> <content> - записать текстовый файл",
                "/delete_file <project>/<file> - удалить файл из WRITE_ROOT",
                "/preview_start <project> - запустить preview",
                "/preview_stop <project> - остановить preview",
                "/preview_list - список preview",
                "/preview_status <project> - статус preview",
                "/selftest_workspace - проверить write/preview workspace",
                "/preview_info <name> - как открыть/запустить проект",
                "/workspace_tree [name] - дерево workspace или проекта",
                "/logs <service> - последние 80 строк journal",
                "/bot_logs - последние 80 строк journal jarvis-bot",
                "/last_error - последняя ошибка Jarvis для этого чата",
                "/last_action - последнее write/preview действие для этого чата",
                "/memory - сохраненная память",
                "/remember <text> - сохранить факт",
                "/forget <key> - удалить memory",
                "/history - последние 10 сообщений",
                "/clear_history - очистить историю чата",
                "/project <repo> - structure + git + TODO/FIXME + safe check",
                "/status - Ollama/STT/TTS/agent status",
                "/agent_on, /agent_off - read-only agent mode",
                "/agent_debug_on, /agent_debug_off - debug intent routing",
                "/debug_on, /debug_off - aliases for debug mode",
                "/debug_last_intent - последний intent",
                "/tts_test - проверить TTS",
                "/patch, /apply_patch, /test, /deploy - отключены на read-only этапе",
            ]
        )
    )


async def memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    rows = memory.list_memories(20)
    if not rows:
        await update.message.reply_text("Память пуста.")
        return
    await reply_long(
        update.message,
        "\n".join(f"{row['key']}: {row['value']} ({row['kind']}, {row['confidence']})" for row in rows),
    )


async def remember_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Использование: /remember <text>")
        return
    candidates = memory.extract_memory_candidates("запомни " + text)
    if not candidates:
        candidates = [{"kind": "note", "key": "_".join(text.lower().split()[:5])[:80], "value": text, "confidence": 0.8}]
    for candidate in candidates:
        memory.upsert_memory(**candidate)
    await update.message.reply_text("Запомнил: " + ", ".join(candidate["key"] for candidate in candidates))


async def forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /forget <key>")
        return
    deleted = memory.delete_memory(context.args[0])
    await update.message.reply_text(f"Удалено записей: {deleted}")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id, _ = chat_user_ids(update)
    rows = memory.recent_messages(chat_id, 10)
    if not rows:
        await update.message.reply_text("История пуста.")
        return
    await reply_long(update.message, "\n".join(f"{row['role']}: {row['content']}" for row in rows))


async def clear_history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id, _ = chat_user_ids(update)
    deleted = memory.clear_history(chat_id)
    await update.message.reply_text(f"История очищена. Удалено сообщений: {deleted}")


def project_summary_prompt(data: dict) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "Ты Jarvis. Составь короткое человеческое резюме проекта по read-only inspection data."},
        {
            "role": "user",
            "content": (
                "Составь резюме: что это за проект, на чем остановились, "
                "что не закоммичено, что делать дальше.\n\n"
                f"{data}"
            ),
        },
    ]


async def project_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /project <repo>")
        return
    try:
        repo = " ".join(context.args)
        structure = project_structure(repo)
        data = inspect_project(repo)
        check = safe_code_check(repo)
        summary = _format_project_report(structure, data, check)
        chat_id, _ = chat_user_ids(update)
        memory.set_current_project(chat_id, data["project_name"])
        memory.save_project_note(
            data["project_name"],
            data["path"],
            summary,
            data["git"].get("last_commit", ""),
        )
        await reply_long(update.message, summary)
    except Exception as e:
        await update.message.reply_text(f"Ошибка /project: {e}")


async def disabled_write_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    await update.message.reply_text(
        "Этот режим пока отключен: текущий этап только read-only. "
        "Никаких patch/apply/test/deploy без отдельного подтвержденного режима записи."
    )


async def agent_debug_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    context.user_data["agent_debug"] = True
    await update.message.reply_text("Agent debug: on")


async def agent_debug_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    context.user_data["agent_debug"] = False
    await update.message.reply_text("Agent debug: off")


async def debug_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await agent_debug_on(update, context)


async def debug_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await agent_debug_off(update, context)


async def debug_last_intent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await reply_long(update.message, str(context.user_data.get("last_intent") or "intent еще не распознавался"))


async def maybe_send_intent_debug(message, context: ContextTypes.DEFAULT_TYPE, debug_info: dict | None):
    if debug_info is None or not isinstance(debug_info, dict):
        debug_info = {}
    context.user_data["last_intent"] = debug_info
    if not context.user_data.get("agent_debug"):
        return
    detected = debug_info.get("detected", {})
    lines = [
        "intent debug:",
        f"detected_intent: {detected.get('intent')}",
        f"current_project: {debug_info.get('current_project') or debug_info.get('project') or detected.get('project') or '-'}",
        f"pending_task: {debug_info.get('pending_task') or '-'}",
        f"resolved_path: {debug_info.get('resolved_path') or debug_info.get('project_data', {}).get('path') or '-'}",
        f"tools_called: {', '.join(debug_info.get('tools_called') or []) or '-'}",
        f"errors: {'; '.join(debug_info.get('errors') or []) or '-'}",
    ]
    if message:
        await message.reply_text("\n".join(lines))


def _check_ollama() -> str:
    try:
        response = requests.get(f"{OLLAMA_URL}/api/version", timeout=5)
        response.raise_for_status()
        version = response.json().get("version", "unknown")
        return f"ok ({version})"
    except Exception as e:
        return f"error ({e})"


def _check_stt() -> str:
    try:
        response = requests.get(f"{STT_URL.rstrip('/')}/docs", timeout=5)
        response.raise_for_status()
        return "ok (/docs)"
    except Exception as e:
        return f"error ({e})"


def _check_tts() -> str:
    if not TTS_ENABLED:
        return "disabled"

    checks = []
    checks.append("piper ok" if Path(PIPER_BIN).is_file() else f"piper missing: {PIPER_BIN}")
    checks.append("model ok" if Path(PIPER_MODEL).is_file() else f"model missing: {PIPER_MODEL}")
    checks.append(
        "config ok" if Path(PIPER_CONFIG).is_file() else f"config missing: {PIPER_CONFIG}"
    )
    checks.append("ffmpeg ok" if shutil.which("ffmpeg") else "ffmpeg missing")
    return "; ".join(checks)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    try:
        roots_info = allowed_roots_info()
        errors = error_summary()
        try:
            previews = list_previews()
            previews_count = previews["count"]
        except Exception:
            previews_count = "unknown"
        text = "\n".join(
            [
                "Jarvis status:",
                "Polling ok: yes",
                f"Ollama: {_check_ollama()}",
                f"Ollama URL: {OLLAMA_URL}",
                f"Ollama model: {MODEL}",
                f"STT: {_check_stt()}",
                f"STT URL: {STT_URL}",
                f"TTS: {_check_tts()}",
                f"TTS enabled: {TTS_ENABLED}",
                f"TTS engine: {TTS_ENGINE}",
                f"Piper model: {PIPER_MODEL}",
                f"Agent tools enabled: {config.AGENT_TOOLS_ENABLED}",
                f"Memory enabled: {config.MEMORY_ENABLED}",
                f"DB path: {config.JARVIS_DB_PATH}",
                f"Write mode enabled: {config.env_bool('WRITE_MODE_ENABLED', config.WRITE_MODE_ENABLED)}",
                f"Write root: {config.get_write_root()}",
                f"Preview ports: {config.PREVIEW_PORT_MIN}..{config.PREVIEW_PORT_MAX}",
                f"Previews count: {previews_count}",
                f"Server host: {config.SERVER_HOST}",
                f"Last errors count: {errors['count']}",
                f"Last error: {errors['last_timestamp'] or '-'} {errors['last_type'] or ''}".strip(),
                f"Allowed services: {', '.join(get_allowed_services())}",
                "Allowed roots:",
                *[f"- {root}" for root in roots_info["allowed_roots"]],
            ]
        )
    except Exception as e:
        text = f"Ошибка /status: {e}"

    await reply_long(update.message, text)


async def agent_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    context.user_data["agent_enabled"] = True
    await update.message.reply_text("Read-only agent mode: on")


async def agent_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    context.user_data["agent_enabled"] = False
    await update.message.reply_text("Read-only agent mode: off")


async def tts_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    voice_path = None

    try:
        voice_path = synthesize_tts("Jarvis online. Голосовой ответ работает.")
        with open(voice_path, "rb") as voice_file:
            await update.message.reply_voice(voice=voice_file)
    except TTSError as e:
        await update.message.reply_text(f"Ошибка TTS: {e}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка отправки TTS-теста: {e}")
    finally:
        if voice_path:
            voice_path.unlink(missing_ok=True)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    user_text = ""
    debug_info = {"detected": {"intent": "unknown"}, "tools_called": [], "errors": []}
    try:
        user_text = update.message.text or ""
        chat_id, user_id = chat_user_ids(update)
        recent = memory.recent_messages(chat_id, config.HISTORY_LIMIT)
        message_id = memory.save_message(chat_id, user_id, "user", user_text, "text")
        use_agent = bool(context.user_data.get("agent_enabled"))
        pending_task = pending_task_for_text(user_text)
        debug_info["pending_task"] = pending_task

        if pending_task:
            await progress(update.message, f"Принял задачу: {pending_task}")
            if pending_task == "create_workspace_project":
                project_name = _slug_from_text(user_text)
                await progress(update.message, f"Принял: создаю проект {project_name} через Ollama + tools.")
                await progress(update.message, "Шаг 1/5: прошу Ollama сгенерировать структуру сайта...")
                await progress(update.message, "Шаг 2/5: проверяю JSON...")
                await progress(update.message, "Шаг 3/5: записываю файлы...")
                if _wants_preview(user_text):
                    await progress(update.message, "Шаг 4/5: запускаю preview...")
                    await progress(update.message, "Шаг 5/5: проверяю curl...")
            else:
                await progress(update.message, "Шаг 1/4: определяю проект и контекст...")
                await progress(update.message, "Шаг 2/4: вызываю безопасные tools...")

        try:
            answer, debug_info = answer_user_text(
                user_text,
                use_agent,
                chat_id=chat_id,
                recent_messages=recent,
                debug=bool(context.user_data.get("agent_debug")),
            )
            if not isinstance(debug_info, dict):
                debug_info = {}
            debug_info.setdefault("pending_task", pending_task)
        except requests.exceptions.ConnectionError as e:
            save_last_error(chat_id=chat_id, user_id=user_id, handler="handle_text", error=e, user_text=user_text)
            answer = (
                "Не могу подключиться к Ollama на AI-ПК.\n\n"
                f"Проверь с сервера:\n"
                f"curl {OLLAMA_URL}/api/version\n\n"
                "Возможные причины: Windows-ПК выключен/уснул, сменился IP, "
                "Ollama не запущена или firewall блокирует порт 11434."
            )
            debug_info = {"detected": {"intent": "connection_error"}, "tools_called": [], "errors": [str(e)], "pending_task": pending_task}
        except requests.exceptions.Timeout as e:
            save_last_error(chat_id=chat_id, user_id=user_id, handler="handle_text", error=e, user_text=user_text)
            answer = "Ollama не ответила вовремя. Возможно, модель грузится или AI-ПК занят."
            debug_info = {"detected": {"intent": "timeout"}, "tools_called": [], "errors": [str(e)], "pending_task": pending_task}

        if pending_task and pending_task != "create_workspace_project":
            await progress(update.message, "Шаг 4/4: готовлю ответ...")

        await maybe_send_intent_debug(update.message, context, debug_info)
        memory.save_message(chat_id, user_id, "assistant", answer, "text")
        memory.save_memory_candidates(user_text, answer, message_id)
        await reply_long(update.message, answer)
    except Exception as e:
        await _report_handler_error(update=update, handler="handle_text", error=e, user_text=user_text)


async def handle_voice_or_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    message = update.message
    recognized_text = ""
    chat_id, user_id = chat_user_ids(update)

    try:
        if message.voice:
            tg_file = await context.bot.get_file(message.voice.file_id)
            suffix = ".ogg"
        elif message.audio:
            tg_file = await context.bot.get_file(message.audio.file_id)
            suffix = Path(message.audio.file_name or "audio.ogg").suffix or ".ogg"
        else:
            return

        tmp_dir = Path("/tmp/jarvis_voice")
        tmp_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            dir=tmp_dir,
            suffix=suffix,
            delete=False,
        ) as tmp:
            tmp_path = tmp.name

        await tg_file.download_to_drive(tmp_path)

        stt_result = transcribe_audio_file(tmp_path)
        recognized_text = (stt_result.get("text") or "").strip()

        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass

        if not recognized_text:
            await message.reply_text("🎙 Не смог распознать голосовое сообщение.")
            return

        message_id = memory.save_message(chat_id, user_id, "user", recognized_text, "voice")
        await message.reply_text(f"🎙 Распознал:\n{memory.mask_secrets(recognized_text)[:1000]}")

        use_agent = bool(context.user_data.get("agent_enabled"))
        recent = memory.recent_messages(chat_id, config.HISTORY_LIMIT)
        pending_task = pending_task_for_text(recognized_text)
        if pending_task:
            await progress(message, f"Принял задачу: {pending_task}")
            await progress(message, "Шаг 1/4: определяю проект и контекст...")
            await progress(message, "Шаг 2/4: вызываю безопасные tools...")
        answer, debug_info = answer_user_text(
            recognized_text,
            use_agent,
            chat_id=chat_id,
            recent_messages=recent,
            debug=bool(context.user_data.get("agent_debug")),
        )
        if pending_task:
            debug_info["pending_task"] = pending_task
            await progress(message, "Шаг 3/4: проверяю результат...")
            await progress(message, "Шаг 4/4: готовлю ответ...")
        await maybe_send_intent_debug(message, context, debug_info)
        memory.save_message(chat_id, user_id, "assistant", answer, "voice")
        memory.save_memory_candidates(recognized_text, answer, message_id)
        await reply_long(message, answer)

        voice_path = None
        try:
            voice_path = synthesize_tts(answer)
            with open(voice_path, "rb") as voice_file:
                await message.reply_voice(voice=voice_file)
        except TTSError as e:
            await message.reply_text(f"Ошибка TTS: {e}")
        finally:
            if voice_path:
                voice_path.unlink(missing_ok=True)

    except requests.exceptions.ConnectionError as e:
        save_last_error(chat_id=chat_id, user_id=user_id, handler="handle_voice_or_audio", error=e, user_text=recognized_text)
        await message.reply_text(
            "Ошибка STT/Ollama: не могу подключиться к локальному сервису.\n\n"
            f"STT: {STT_URL}\n"
            f"Ollama: {OLLAMA_URL}\n\n"
            f"Детали: {e}"
        )
    except requests.exceptions.Timeout as e:
        save_last_error(chat_id=chat_id, user_id=user_id, handler="handle_voice_or_audio", error=e, user_text=recognized_text)
        await message.reply_text("STT/Ollama не ответили вовремя.")
    except Exception as e:
        await _report_handler_error(update=update, handler="handle_voice_or_audio", error=e, user_text=recognized_text)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    error = context.error or RuntimeError("Unknown Telegram application error")
    tg_update = update if isinstance(update, Update) else None
    await _report_handler_error(update=tg_update, handler="telegram_error_handler", error=error)


def main():
    memory.init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("roots", roots))
    app.add_handler(CommandHandler("repos", repos))
    app.add_handler(CommandHandler("projects", projects_command))
    app.add_handler(CommandHandler("git", git_command))
    app.add_handler(CommandHandler("diff", diff_command))
    app.add_handler(CommandHandler("find", find_command))
    app.add_handler(CommandHandler("tree", tree_command))
    app.add_handler(CommandHandler("structure", structure_command))
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(CommandHandler("workspace", workspace_command))
    app.add_handler(CommandHandler("write_mode", write_mode_command))
    app.add_handler(CommandHandler("new_static", new_static_command))
    app.add_handler(CommandHandler("create_site", create_site_command))
    app.add_handler(CommandHandler("create_and_preview", create_and_preview_command))
    app.add_handler(CommandHandler("where", where_command))
    app.add_handler(CommandHandler("new_flask", new_flask_command))
    app.add_handler(CommandHandler("write_file", write_file_command))
    app.add_handler(CommandHandler("delete_file", delete_file_command))
    app.add_handler(CommandHandler("preview_start", preview_start_command))
    app.add_handler(CommandHandler("preview_stop", preview_stop_command))
    app.add_handler(CommandHandler("preview_list", preview_list_command))
    app.add_handler(CommandHandler("preview_status", preview_status_command))
    app.add_handler(CommandHandler("selftest_workspace", selftest_workspace_command))
    app.add_handler(CommandHandler("preview_info", preview_info_command))
    app.add_handler(CommandHandler("workspace_tree", workspace_tree_command))
    app.add_handler(CommandHandler("logs", logs_command))
    app.add_handler(CommandHandler("bot_logs", bot_logs_command))
    app.add_handler(CommandHandler("last_error", last_error_command))
    app.add_handler(CommandHandler("last_action", last_action_command))
    app.add_handler(CommandHandler("memory", memory_command))
    app.add_handler(CommandHandler("remember", remember_command))
    app.add_handler(CommandHandler("forget", forget_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("clear_history", clear_history_command))
    app.add_handler(CommandHandler("project", project_command))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("agent_on", agent_on))
    app.add_handler(CommandHandler("agent_off", agent_off))
    app.add_handler(CommandHandler("agent_debug_on", agent_debug_on))
    app.add_handler(CommandHandler("agent_debug_off", agent_debug_off))
    app.add_handler(CommandHandler("debug_on", debug_on))
    app.add_handler(CommandHandler("debug_off", debug_off))
    app.add_handler(CommandHandler("debug_last_intent", debug_last_intent))
    app.add_handler(CommandHandler("tts_test", tts_test))
    app.add_handler(CommandHandler("patch", disabled_write_command))
    app.add_handler(CommandHandler("apply_patch", disabled_write_command))
    app.add_handler(CommandHandler("test", disabled_write_command))
    app.add_handler(CommandHandler("deploy", disabled_write_command))

    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice_or_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling()


if __name__ == "__main__":
    main()
