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
from typing import Any
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
import plugin_manager
import self_improvement
from action_schemas import extract_json_object, validate_create_static_site_action, validate_edit_workspace_site_action
from agent import answer_with_tools
from intent_router import detect_intent, extract_mentioned_project, format_workspace_inventory, handle_detected_intent
import memory
import semantic_router
from tools_fs import ToolError, allowed_roots_info, search_text, tree_summary
from tools_edit import apply_file_updates, read_workspace_project_files, verify_workspace_project
from tools_git import find_git_repos, git_diff, git_status, resolve_repo
from tools_project import inspect_project, project_structure
from tools_check import safe_code_check
from tools_system import get_allowed_services, read_journal
from tools_preview import (
    cleanup_stale_previews,
    curl_check,
    detect_lan_ip,
    list_previews,
    network_sockets_available,
    port_is_listening,
    preview_status,
    preview_url_for_port,
    scan_listening_ports,
    start_preview,
    stop_preview,
    stop_preview_by_port,
)
from tools_browser import (
    check_site_with_playwright,
    check_site_with_playwright_async,
    playwright_async_smoke_check,
    playwright_available,
)
from tools_media import (
    convert_to_webp,
    list_project_images,
    list_workspace_project_images,
    pillow_available,
    save_telegram_image_to_project,
    set_hero_background,
    resolve_existing_project_image,
    optimize_image_to_webp,
    save_image_to_project,
    set_fixed_background,
    verify_background_asset,
)
from tools_pending_media import (
    clear_old_pending_media,
    get_latest_available_media,
    get_latest_media_any_status,
    get_latest_pending_media,
    mark_media_failed,
    mark_media_used,
    save_pending_media,
)
from tools_errors import error_summary, latest_error, mask_error_text, save_last_error
from tools_site_state import (
    format_site_state_answer,
    get_site_requirements,
    infer_requirements_from_text,
    inspect_site_state,
    load_site_state,
    save_site_state,
)
from tools_snapshot import list_snapshots, rollback_project, snapshot_project
from tools_site_checks import detect_feature_regressions, run_acceptance_checks as run_persistent_acceptance_checks
from tools_site_operations import apply_operation_plan, op_set_background, validate_operation_plan
import project_state_manager
import learning_log
import task_orchestrator
import ui_component_model
from ui_component_model import build_component_model
from ui_component_verifier import format_verification_human, verify_component_static, verify_components_async
from site_technology_detector import detect_technology
from tools_write import (
    create_flask_site,
    create_project_dir,
    create_static_site,
    delete_workspace_dir,
    delete_workspace_file,
    list_workspace,
    list_workspace_project_files,
    read_workspace_file,
    resolve_write_path,
    tree_workspace_project,
    update_static_site_file,
    verify_project_files,
    verify_static_site,
    workspace_inventory,
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
        "Помогай с Linux, Django, Python, сервером, Telegram-ботами, API и администрированием. "
        "У тебя есть локальная память SQLite: используй историю диалога, memories и project_notes, когда они добавлены в контекст. "
        "Не говори, что у тебя нет доступа, если доступны read-only tools. "
        "Если tools доступны, никогда не говори 'у меня нет доступа к файловой системе'. "
        "Для серверных/проектных вопросов backend должен вызывать tools. Если ты видишь tool results — отвечай по ним. "
        "Если пользователь говорит 'это', 'он', 'там', 'проект' — используй последние сообщения для контекста. "
        "Не придумывай результаты команд. Если нужны логи или вывод команды — попроси пользователя выполнить команду или скажи, какую команду выполнить. "
        "Не говори 'выполнил команду', если backend не запускал такую команду. "
        "Если использовался встроенный tool, говори 'Проверил через встроенный tool' или 'По данным read-only анализа'. "
        "Не выдумывай команды вроде ls -la, если реально использовался tree_summary/project_structure/safe_code_check. "
        "Не говори 'создал', 'записал', 'удалил', 'остановил' или 'запустил', если write/preview tool не вернул успешный результат с подтвержденной проверкой (process/port/curl/exists). "
        "После write/preview действия всегда показывай tools_called, actual_path и созданные/измененные/удаленные файлы или preview_url. "
        "Для опасных действий, таких как удаление файлов, миграции, рестарт сервисов, deploy, git push или изменения nginx/systemd, требуй явное подтверждение."
    )


def is_allowed(update: Update) -> bool:
    # Read the env var at call time rather than the ALLOWED_USER_ID module
    # constant: tests import bot.py before some test modules get a chance to
    # set ALLOWED_USER_ID, which would otherwise bake in a stale value (0)
    # for the rest of the process.
    allowed_id = int(os.getenv("ALLOWED_USER_ID", "0"))
    return bool(update.effective_user) and update.effective_user.id == allowed_id


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
    from tools_claude import ask_claude_messages
    return ask_claude_messages(messages)


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

EDIT_SITE_PHRASES = (
    "поменяй стиль",
    "измени стиль",
    "поменяй дизайн",
    "сделай зеленый",
    "сделай зелёный",
    "добавь погоду",
    "добавь блок",
    "добавь кнопку",
    "редактируй сайт",
    "отредактируй сайт",
    "поменяй цвет",
    "edit the site",
    "change the design",
    "change the style",
    "update the site",
    "cambia el estilo",
    "cambia el diseño",
    "añade el tiempo",
    "modifica el sitio",
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
        "modified_files": action.get("modified_files") or [],
        "preview_url": action.get("preview_url") or "",
        "curl_check": action.get("curl_check"),
        "error": str(action.get("error") or ""),
        "tools_called": action.get("tools_called") or [],
        "verification": action.get("verification"),
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
        f"curl_check: {action.get('curl_check') or '-'}",
        f"tools_called: {', '.join(action.get('tools_called') or []) or '-'}",
        f"verification: {action.get('verification') or '-'}",
        f"error: {action.get('error') or '-'}",
        "created_files:",
    ]
    created = action.get("created_files") or []
    lines.extend(f"- {item}" for item in created) if created else lines.append("-")
    modified = action.get("modified_files") or []
    lines.append("modified_files:")
    lines.extend(f"- {item}" for item in modified) if modified else lines.append("-")
    return "\n".join(lines)


def _current_task_path() -> Path:
    path = Path(os.getenv("JARVIS_DB_PATH", config.JARVIS_DB_PATH)).resolve().parent / "current_task.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_current_tasks() -> dict:
    path = _current_task_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _save_current_tasks(data: dict) -> None:
    _current_task_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_current_task(chat_id: str | None, intent: str, project_name: str | None, step: str) -> dict:
    key = str(chat_id or "global")
    now = datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds").replace("+00:00", "Z")
    payload = {
        "intent": intent,
        "project_name": project_name,
        "status": "running",
        "step": step,
        "last_message": step,
        "started_at": now,
        "updated_at": now,
    }
    data = _load_current_tasks()
    data[key] = payload
    _save_current_tasks(data)
    return payload


def update_current_task_step(chat_id: str | None, step: str) -> None:
    key = str(chat_id or "global")
    data = _load_current_tasks()
    if key in data:
        data[key]["step"] = step
        data[key]["last_message"] = step
        data[key]["updated_at"] = datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds").replace("+00:00", "Z")
        _save_current_tasks(data)


def get_current_task(chat_id: str | None) -> dict | None:
    data = _load_current_tasks()
    return data.get(str(chat_id or "global"))


def clear_current_task(chat_id: str | None) -> None:
    key = str(chat_id or "global")
    data = _load_current_tasks()
    if key in data:
        data.pop(key, None)
        _save_current_tasks(data)


def _last_verification_path() -> Path:
    path = Path(os.getenv("JARVIS_DB_PATH", config.JARVIS_DB_PATH)).resolve().parent / "last_verification.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def save_last_verification(project_name: str, result: dict | None) -> None:
    if not result:
        return
    payload = {
        "project_name": project_name,
        "success": result.get("success"),
        "skipped": result.get("skipped"),
        "title": result.get("title"),
        "body_text_length": result.get("body_text_length"),
        "language_buttons_found": result.get("language_buttons_found"),
        "language_switch_ok": result.get("language_switch_ok"),
        "background_image_loaded": result.get("background_image_loaded"),
        "screenshot_path": result.get("screenshot_path"),
        "errors": result.get("errors"),
        "console_errors": result.get("console_errors"),
        "checked_at": datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    try:
        _last_verification_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logging.exception("failed_to_save_last_verification")


def get_last_verification() -> dict | None:
    path = _last_verification_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


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
    """Resolve a WORKSPACE project name, not a git repository.

    Important: this function must never return a random English token from the
    user's sentence unless that token is an actual workspace project. The old
    behavior returned the last Latin-looking word and then downstream routing
    sometimes treated it as a git repo, which produced confusing answers like
    "Репозиторий не найден: kuki" for a real workspace website.
    """
    lowered = (text or "").lower()
    try:
        projects = list_workspace()["projects"]
    except Exception:
        projects = []

    names = [str(item.get("name") or "") for item in projects if item.get("name")]

    # 1) Explicit existing workspace project mentioned in free text.
    for name in names:
        if re.search(rf"(?<![A-Za-z0-9_.-]){re.escape(name.lower())}(?![A-Za-z0-9_.-])", lowered):
            return name

    # 2) Explicit slug only if it really exists as a workspace project.
    explicit = _workspace_name_from_text(text)
    if explicit and explicit in names:
        return explicit

    # 3) Last/current project, but only if it still exists.
    action = get_last_action(chat_id)
    if action and action.get("project_name") and _workspace_project_exists(str(action["project_name"])):
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


def _must_preserve_lines(requirements: dict[str, Any] | None) -> list[str]:
    """Human-readable "do not remove this" list built from persisted
    requirements (tools_site_state) -- handed to Ollama so a task about one
    feature can't silently delete an unrelated one that an earlier task
    already established."""
    requirements = requirements or {}
    lines = []
    if requirements.get("background_required"):
        lines.append("фон (background-image) на hero/body -- должен остаться видимым")
    if requirements.get("language_switcher_required"):
        langs = ", ".join(c.upper() for c in (requirements.get("languages") or [])) or "RU/EN/ES"
        lines.append(f"переключатель языков ({langs}) с рабочими кнопками и обработчиком клика")
    if requirements.get("single_language_visible"):
        lines.append("одновременно виден текст только ОДНОГО языка, остальные скрыты (display:none/hidden)")
    if requirements.get("slider_required"):
        lines.append("слайдер/карусель")
    if requirements.get("weather_required"):
        lines.append("блок погоды (JS fetch к Open-Meteo) без console errors")
    if requirements.get("footer_required"):
        lines.append("<footer> блок")
    return lines


def ask_ollama_for_site_edit(
    user_text: str, project_name: str, current_files: list[dict], requirements: dict[str, Any] | None = None
) -> dict:
    files_block = "\n\n".join(f"--- {f['path']} ---\n{f['content']}" for f in current_files)
    must_preserve = _must_preserve_lines(requirements)
    must_preserve_block = (
        "\nОБЯЗАТЕЛЬНО СОХРАНИ в финальном коде (даже если задача про другое):\n"
        + "\n".join(f"- {line}" for line in must_preserve)
        + "\n"
        if must_preserve
        else ""
    )
    prompt = f"""
Верни только JSON. Не пиши markdown. Не пиши bash-команды. Не используй mkdir/cat/python/http.server.
Твоя задача: отредактировать существующий статический сайт workspace-проекта "{project_name}" по запросу пользователя.
Тебе дано полное текущее содержимое файлов проекта. Верни JSON с ПОЛНЫМ новым содержимым каждого
изменённого файла (не диффы, не фрагменты, целиком весь файл).

Строгий формат:
{{
  "action": "edit_workspace_site",
  "project_name": "{project_name}",
  "summary": "Short description of what changed",
  "files": [
    {{"path": "index.html", "content": "full new file content"}},
    {{"path": "assets/css/style.css", "content": "full new file content"}}
  ],
  "notes": ["short implementation notes"]
}}

Требования:
- project_name строго "{project_name}";
- меняй только те файлы, которые реально нужно изменить для выполнения запроса;
- все path только относительные внутри проекта, без "..", без абсолютных путей;
- без внешних CDN, удаленных шрифтов, внешних скриптов и npm-библиотек;
- сохраняй responsive design и существующую структуру секций, если явно не просили иное;
- если просят добавить погоду для Бильбао, используй клиентский JS fetch к Open-Meteo
  (https://api.open-meteo.com/v1/forecast?latitude=43.2630&longitude=-2.9350&current_weather=true),
  без API key, с try/catch и текстовым fallback на случай ошибки запроса.
{must_preserve_block}
Текущие файлы проекта:
{files_block}

Запрос пользователя:
{user_text}
""".strip()
    messages = [
        {
            "role": "system",
            "content": "Ты генератор JSON edit specs для Jarvis backend. Возвращай только валидный JSON с полным содержимым измененных файлов.",
        },
        {"role": "user", "content": prompt},
    ]
    last_error: Exception | None = None
    for attempt in range(2):
        raw = ask_ollama_messages(messages)
        try:
            if looks_like_fake_action_response(raw):
                raise ToolError("Ollama попыталась заменить JSON edit текстовыми shell-командами")
            data = extract_json_object(raw)
            return validate_edit_workspace_site_action(data, expected_project_name=project_name)
        except (ToolError, RuntimeError) as e:
            last_error = e
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {
                    "role": "user",
                    "content": "Ты вернул невалидный JSON. Исправь и верни только JSON без markdown, без пояснений, без ``` оберток.",
                }
            )
    raise last_error or ToolError("Не удалось получить валидный JSON edit spec от Ollama")


ALLOWED_OPERATIONS_TEXT = (
    "- \"add_feature\" / \"update_feature\" / \"repair_feature\" -- требуют поле \"feature\" из списка: "
    "background, slider, language_switcher, weather, footer\n"
    "- \"set_background\" (params: target=\"hero\"|\"whole_page\", fixed=true|false, image=\"filename.jpg\" опционально)\n"
    "- \"add_slider\"\n"
    "- \"fix_language_switcher\"\n"
    "- \"add_footer\"\n"
    "- \"add_weather\"\n"
    "- \"verify\" -- только проверить текущее состояние, без изменений\n"
    "- \"rollback\" (params: snapshot_id опционально, иначе откат к последнему успешному)"
)


def ask_ollama_for_operation_plan(user_text: str, project_name: str, project_state: dict[str, Any]) -> dict[str, Any]:
    """Ollama is only ever allowed to SELECT structured operations here --
    never author HTML/CSS/JS. validate_operation_plan() (tools_site_operations)
    is the actual trust boundary; this prompt is just guidance to make valid
    JSON likely on the first try."""
    requirements = project_state.get("requirements", {})
    features = project_state.get("features", {})
    must_preserve = _must_preserve_lines(requirements)
    must_preserve_block = (
        "\nОБЯЗАТЕЛЬНО СОХРАНИ (не предлагай операции, которые могут убрать эти функции):\n"
        + "\n".join(f"- {line}" for line in must_preserve)
        + "\n"
        if must_preserve
        else ""
    )
    feature_status_lines = "\n".join(f"- {name}: {entry.get('status', 'unknown')}" for name, entry in features.items())
    prompt = f"""
Верни только JSON. Не пиши markdown, не пиши HTML/CSS/JS код, не пиши shell-команды.
Ты выбираешь СТРУКТУРНЫЕ операции для редактирования сайта workspace-проекта "{project_name}".
Ты НЕ можешь писать содержимое файлов напрямую -- только выбрать одну или несколько операций
из строго фиксированного списка ниже; исполнитель сам сгенерирует безопасный HTML/CSS/JS.

Допустимые операции (поле "op"):
{ALLOWED_OPERATIONS_TEXT}

Строгий формат:
{{
  "operations": [{{"op": "...", "feature": "...", "params": {{}}}}],
  "summary": "short description"
}}

Текущее состояние фич проекта:
{feature_status_lines or '- нет данных'}
{must_preserve_block}
Запрос пользователя:
{user_text}
""".strip()
    messages = [
        {
            "role": "system",
            "content": (
                "Ты выбираешь структурные операции для Jarvis backend. Возвращай только валидный JSON "
                "со списком operations из допустимого списка, без HTML/CSS/JS контента."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    last_error: Exception | None = None
    for attempt in range(2):
        raw = ask_ollama_messages(messages)
        try:
            if looks_like_fake_action_response(raw):
                raise ToolError("Ollama попыталась вернуть текстовые shell-команды вместо JSON")
            data = extract_json_object(raw)
            operations = validate_operation_plan(data)
            return {"operations": operations, "summary": str(data.get("summary") or "")[:300]}
        except (ToolError, RuntimeError) as e:
            last_error = e
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Невалидный план. Верни только JSON со списком operations из допустимого списка, "
                        "без HTML/CSS/JS контента, без markdown."
                    ),
                }
            )
    raise last_error or ToolError("Не удалось получить валидный operation plan от Ollama")


def format_user_result(headline: str, info: dict | None = None) -> str:
    """Human-friendly summary for normal chat mode. No tools_called/WRITE_ROOT/raw JSON."""
    info = info or {}
    lines = [headline] if headline else []
    folder = info.get("resolved_path") or info.get("path") or info.get("actual_path")
    if folder:
        lines.append(f"Папка: {folder}")
    created = info.get("created_files")
    if created:
        lines.append("Создал файлы:")
        lines.extend(f"- {path}" for path in created)
    modified = info.get("modified_files")
    if modified:
        lines.append("Изменил файлы:")
        lines.extend(f"- {path}" for path in modified)
    deleted = info.get("deleted_files")
    if deleted:
        lines.append("Удалил файлы:")
        lines.extend(f"- {path}" for path in deleted)
    open_url = info.get("preview_url")
    if open_url:
        lines.append(f"Открыть сайт: {open_url}")
    elif info.get("preview_note"):
        lines.append(info["preview_note"])
    notes = info.get("notes")
    if notes:
        notes_text = "; ".join(str(n) for n in notes) if isinstance(notes, (list, tuple)) else str(notes)
        lines.append(f"Заметки: {notes_text}")
    if info.get("rollback"):
        lines.append(f"↩️ {info['rollback']}")
    if info.get("warning"):
        lines.append(f"⚠️ {info['warning']}")
    return "\n".join(lines)


def format_debug_result(info: dict | None = None) -> str:
    """Technical dump shown only when /debug_on (agent_debug) is active."""
    info = info or {}
    lines = ["--- debug ---", f"tools_called: {', '.join(info.get('tools_called') or []) or '-'}"]
    lines.append(f"рабочая папка Jarvis: {config.get_write_root()}")
    path_value = info.get("resolved_path") or info.get("path") or info.get("actual_path")
    if path_value:
        lines.append(f"actual_path: {path_value}")
    if info.get("created_files"):
        lines.append(f"created_files: {', '.join(info['created_files'])}")
    if info.get("modified_files"):
        lines.append(f"modified_files: {', '.join(info['modified_files'])}")
    if info.get("deleted_files"):
        lines.append(f"deleted_files: {', '.join(info['deleted_files'])}")
    if "preview_url" in info:
        lines.append(f"preview_url: {info.get('preview_url') or '-'}")
    if "curl_check" in info:
        lines.append(f"curl_check: {info.get('curl_check')}")
    if "browser_check" in info:
        lines.append(f"browser_check: {info.get('browser_check')}")
    if info.get("errors"):
        lines.append(f"errors: {'; '.join(str(e) for e in info['errors'])}")
    return "\n".join(lines)


def _format_write_success(
    action: str,
    tools_called: list[str],
    actual_path: str,
    created: list[str] | None = None,
    modified: list[str] | None = None,
    deleted: list[str] | None = None,
    preview_url: str | None = None,
    debug: bool = False,
) -> str:
    info = {
        "tools_called": tools_called,
        "resolved_path": actual_path,
        "created_files": created,
        "modified_files": modified,
        "deleted_files": deleted,
        "preview_url": preview_url,
    }
    if not preview_url:
        info["preview_note"] = "Preview не запущен. Команда: /preview_start <project>"
    text = format_user_result(action, info)
    if debug:
        text += "\n\n" + format_debug_result(info)
    return text


def _format_stop_result(label: str, result: dict, debug: bool = False) -> str:
    checks = result.get("checks") or {}
    success = bool(result.get("success"))
    project = result.get("project")
    path_value = result.get("path") or result.get("cwd") or ""
    if success:
        headline = f"Остановил preview {project}." if project else "Остановил preview."
    else:
        headline = f"Не смог остановить preview {project}." if project else "Не смог остановить preview."
    info: dict = {"resolved_path": path_value}
    lines = [format_user_result(headline, info)]
    if result.get("port"):
        lines.append(f"Порт: {result.get('port')}")
    if not success and result.get("error"):
        lines.append(f"Причина: {result['error']}")
    if debug:
        debug_info = {
            "tools_called": [label.split(" ")[0]],
            "resolved_path": path_value,
        }
        lines.append("")
        lines.append(format_debug_result(debug_info))
        lines.append(f"success: {success} pid: {result.get('pid', '-')} checks: {checks}")
    return "\n".join(lines)


def _format_delete_result(result: dict, debug: bool = False) -> str:
    success = bool(result.get("success"))
    project_name = result.get("project_name")
    verification = result.get("verification") or {}
    headline = f"Удалил проект {project_name}." if success else f"Не смог удалить проект {project_name}."
    info = {"resolved_path": result.get("path")}
    lines = [format_user_result(headline, info)]
    preview_stop = result.get("preview_stop")
    if preview_stop is not None:
        lines.append("Preview тоже остановлен." if preview_stop.get("success") else "Preview не подтвержден как остановленный.")
    if not success and result.get("error"):
        lines.append(f"Причина: {result['error']}")
    if debug:
        lines.append("")
        lines.append(format_debug_result({"tools_called": ["delete_workspace_dir"], "resolved_path": result.get("path")}))
        lines.append(f"verification: {verification}")
        if preview_stop is not None:
            lines.append(f"preview_stop: {preview_stop}")
    return "\n".join(lines)


def _stop_all_previews() -> list[dict]:
    rows = []
    for item in list_previews()["previews"]:
        name = item["project"]
        try:
            result = stop_preview(name)
            rows.append(
                {
                    "project": name,
                    "port": item.get("port"),
                    "was_running": True,
                    "stopped": bool(result.get("success")),
                    "verification": result.get("checks") or {},
                    "error": result.get("error"),
                }
            )
        except Exception as e:
            rows.append(
                {
                    "project": name,
                    "port": item.get("port"),
                    "was_running": True,
                    "stopped": False,
                    "verification": {},
                    "error": str(e),
                }
            )
    return rows


def _format_stop_all_table(rows: list[dict]) -> str:
    if not rows:
        return "preview_stop_all result:\nнет зарегистрированных preview процессов"
    lines = ["preview_stop_all result:", "project | port | was_running | stopped | verification"]
    for row in rows:
        checks = row.get("verification") or {}
        verification = (
            "process_alive={} port_listening={} curl_responds={}".format(
                checks.get("process_alive"), checks.get("port_listening"), checks.get("curl_responds")
            )
            if checks
            else (row.get("error") or "-")
        )
        lines.append(f"{row['project']} | {row.get('port')} | {row['was_running']} | {row['stopped']} | {verification}")
    return "\n".join(lines)


def workspace_where_answer(project_name: str | None, chat_id: str | None = None, debug: bool = False) -> tuple[str, dict]:
    tools_called = ["verify_project_files", "preview_status"]
    if not project_name:
        action = get_last_action(chat_id)
        if not action or not action.get("success"):
            text = "Не вижу подтвержденного созданного сайта. Укажи имя проекта: /where <project>"
            if debug:
                text += "\n\n" + format_debug_result({"tools_called": []})
            return (
                text,
                {"detected": {"intent": "where_project"}, "tools_called": [], "errors": ["project not resolved"]},
            )
        project_name = str(action["project_name"])
    verify = verify_project_files(project_name)
    exists = Path(verify["path"]).is_dir()
    status = preview_status(project_name)
    port = status.get("port")
    port_listening = bool(port) and port_is_listening(int(port))
    curl_result = status.get("curl_check")
    running = bool(status.get("running"))
    preview_url = preview_url_for_port(int(port)) if running and port else ""

    if not exists:
        headline = f"Не нашел проект {project_name} в рабочей папке Jarvis."
        answer = format_user_result(headline, {"resolved_path": verify["path"]})
    else:
        headline = f"Сайт {project_name} запущен." if running else f"Проект {project_name} на месте, но preview не запущен."
        info: dict = {"resolved_path": verify["path"], "preview_url": preview_url}
        if not running:
            info["preview_note"] = f"Команда для запуска: /preview_start {project_name}"
        answer = format_user_result(headline, info)

    if debug:
        debug_info = {
            "tools_called": tools_called,
            "resolved_path": verify.get("path"),
            "preview_url": preview_url,
            "curl_check": curl_result,
        }
        answer += "\n\n" + format_debug_result(debug_info)
        answer += f"\nport listening: {port_listening}; preview registered: {status.get('registered', False)}"

    return (
        answer,
        {
            "detected": {"intent": "where_project", "project": project_name},
            "tools_called": tools_called,
            "errors": [] if exists else ["project not found"],
            "resolved_path": verify.get("path"),
            "project": project_name,
            "preview_url": preview_url,
            "curl_check": curl_result,
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
        headline = f"Готово! Я создал сайт {project['project_name']}." if not preview else f"Готово! Я создал сайт {project['project_name']} и запустил предпросмотр."
        answer = _format_write_success(
            headline,
            tools_called,
            project["path"],
            created=created_files,
            preview_url=preview["url"] if preview else None,
        )
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


RESTART_PREVIEW_WORDS = (
    "запусти", "перезапусти", "проверь", "включи",
    "start", "restart", "check", "launch",
    "inicia", "reinicia", "comprueba", "verifica", "arranca",
)

LANGUAGE_TASK_WORDS = (
    "язык", "language", "idioma", "переключ", "switch lang", "ru/en/es", "en/es/ru", "мультиязыч",
)
ANIMATION_TASK_WORDS = (
    "360", "rotate", "вращ", "анимац", "animation", "крутит", "переворач", "spin",
)
BACKGROUND_TASK_WORDS = (
    "фон", "background", "hero", "обои", "wallpaper", "задний план",
)
MAX_REPAIR_ITERATIONS = 2


def _mentions_languages(text: str) -> bool:
    lowered = text.lower()
    if any(word in lowered for word in LANGUAGE_TASK_WORDS):
        return True
    hits = sum(1 for code in ("ru", "en", "es", "русск", "англ", "испан", "español", "ingl") if code in lowered)
    return hits >= 2


def _mentions_animation(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in ANIMATION_TASK_WORDS)


def _mentions_background(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in BACKGROUND_TASK_WORDS)


def _check_language_support(haystack: str) -> tuple[bool, str]:
    lowered = haystack.lower()

    def has_lang(code: str) -> bool:
        patterns = (f'"{code}"', f"'{code}'", f'data-lang="{code}"', f"lang-{code}", f">{code}<", f'id="{code}"')
        return any(p in lowered for p in patterns)

    missing = [code.upper() for code in ("ru", "es", "en") if not has_lang(code)]
    if missing:
        return False, f"не найдены языковые маркеры: {', '.join(missing)}"

    has_click_handler = ("addeventlistener" in lowered or "onclick" in lowered) and "lang" in lowered
    if not has_click_handler:
        return False, "не найден обработчик клика для переключения языка"

    main_idx = lowered.find("<main")
    if main_idx == -1:
        main_idx = lowered.find("<section")
    lang_marker_positions = [
        idx for idx in (lowered.find("data-lang"), lowered.find("lang-switch"), lowered.find("lang-btn")) if idx != -1
    ]
    if lang_marker_positions and main_idx != -1 and min(lang_marker_positions) > main_idx:
        return False, "языковые кнопки не похожи на расположенные в header/верхнем углу"
    return True, "ok"


def _check_animation(haystack: str) -> tuple[bool, str]:
    lowered = haystack.lower().replace(" ", "")
    has_rotate = "rotatey(360deg)" in lowered or "rotatex(360deg)" in lowered or "rotate(360deg)" in lowered
    if not has_rotate:
        return False, "не найден rotateY(360deg)/rotate(360deg) в CSS/JS"
    has_anim_marker = "@keyframes" in lowered or "animation:" in lowered or "animation-name" in lowered or "classlist" in lowered
    if not has_anim_marker:
        return False, "не найден класс/animation для карточек или букв"
    has_dom_hook = any(
        marker in lowered
        for marker in ('class="card', "class='card", 'class="letter', "class='letter", 'class="anim', "classlist.add")
    )
    if not has_dom_hook:
        return False, "не найден элемент (card/letter), к которому применяется анимация"
    return True, "ok"


def _html_only_haystack(files: list[dict]) -> str:
    return "\n".join(f["content"] for f in files if f["path"].lower().endswith(".html"))


def _check_sections_preserved(before_haystack: str, after_haystack: str) -> tuple[bool, str]:
    """Expects HTML-only content. CSS/JS files routinely mention "card" in class
    selectors and would otherwise swing the count even when no markup changed."""
    before_sections = before_haystack.lower().count("<section")
    after_sections = after_haystack.lower().count("<section")
    before_cards = before_haystack.lower().count("card")
    after_cards = after_haystack.lower().count("card")
    if before_sections and after_sections < before_sections:
        return False, f"количество <section> уменьшилось: {before_sections} -> {after_sections}"
    if before_cards and after_cards < max(1, before_cards // 2):
        return False, f"похоже пропали карточки (card): {before_cards} -> {after_cards}"
    return True, "ok"


def _check_background_reference(after_haystack: str, image_relative_path: str) -> tuple[bool, str]:
    image_name = Path(image_relative_path).name.lower()
    lowered = after_haystack.lower()
    if image_name not in lowered and image_relative_path.lower() not in lowered:
        return False, f"CSS/HTML не ссылается на {image_relative_path}"
    return True, "ok"


def run_acceptance_checks(
    user_text: str,
    before_files: list[dict],
    after_files: list[dict],
    browser_result: dict | None = None,
    expect_background_image: str | None = None,
) -> dict:
    before_haystack = "\n".join(f["content"] for f in before_files)
    after_haystack = "\n".join(f["content"] for f in after_files)
    failed: list[str] = []
    checks: dict = {}
    have_browser = bool(browser_result and not browser_result.get("skipped"))

    if _mentions_languages(user_text):
        ok, detail = _check_language_support(after_haystack)
        checks["languages"] = {"ok": ok, "detail": detail}
        if not ok:
            failed.append(f"языки: {detail}")
        elif have_browser:
            found = {code.lower() for code in (browser_result.get("language_buttons_found") or [])}
            missing_buttons = [code.upper() for code in ("ru", "es", "en") if code not in found]
            if missing_buttons:
                checks["languages"] = {"ok": False, "detail": f"браузер не нашел кнопки {', '.join(missing_buttons)}"}
                failed.append(f"языки: браузер не нашел кнопки {', '.join(missing_buttons)}")
            elif browser_result.get("language_switch_ok") is False:
                checks["languages"] = {"ok": False, "detail": "клик по кнопкам не меняет видимый текст"}
                failed.append("языки: переключение кнопок не меняет видимый текст (проверено в браузере)")

    if _mentions_animation(user_text):
        ok, detail = _check_animation(after_haystack)
        checks["animation"] = {"ok": ok, "detail": detail}
        if not ok:
            failed.append(f"анимация 360°: {detail}")

    if expect_background_image:
        ok, detail = _check_background_reference(after_haystack, expect_background_image)
        checks["background_image"] = {"ok": ok, "detail": detail}
        if not ok:
            failed.append(f"фон: {detail}")
        elif have_browser and browser_result.get("background_image_loaded") is False:
            checks["background_image"] = {"ok": False, "detail": "браузер не подтвердил отображение фона"}
            failed.append("фон: браузер не подтвердил, что изображение реально отображается как фон")

    before_html = _html_only_haystack(before_files)
    after_html = _html_only_haystack(after_files)
    if after_html:
        ok, detail = _check_sections_preserved(before_html, after_html)
        checks["sections_preserved"] = {"ok": ok, "detail": detail}
        if not ok:
            failed.append(f"секции/карточки: {detail}")

    if have_browser and browser_result.get("success") is False:
        reason = "; ".join(browser_result.get("errors") or browser_result.get("console_errors") or ["сайт не загрузился корректно"])
        checks["browser"] = {"ok": False, "detail": reason}
        failed.append(f"браузерная проверка: {reason}")

    return {"success": not failed, "failed": failed, "checks": checks}


def build_verification_report(failed_checks: list[str], browser_result: dict | None = None) -> str:
    if not failed_checks:
        return ""
    lines = [
        "Отчет автоматической проверки. Исправь ТОЛЬКО перечисленные ниже проблемы,",
        "остальной код и функционал сайта не трогай:",
    ]
    lines.extend(f"- {item}" for item in failed_checks)
    if browser_result and not browser_result.get("skipped"):
        if browser_result.get("console_errors"):
            lines.append("- JS console errors: " + "; ".join(browser_result["console_errors"][:5]))
        if browser_result.get("errors"):
            lines.append("- Page errors: " + "; ".join(browser_result["errors"][:5]))
    return "\n".join(lines)


KEEP_DESPITE_FAILURE_PHRASES = (
    "оставь всё равно", "оставь все равно", "оставь как есть", "не откатывай",
    "не откатывать", "keep anyway", "leave it anyway", "don't rollback", "no rollback",
)


def _wants_keep_despite_failure(text: str) -> bool:
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in KEEP_DESPITE_FAILURE_PHRASES)


def edit_workspace_site_workflow(
    user_text: str,
    project_name: str,
    chat_id: str | None = None,
    expect_background_image: str | None = None,
) -> tuple[str, dict]:
    tools_called: list[str] = []
    save_current_task(chat_id, "edit_workspace_site", project_name, "starting")
    action = {
        "intent": "edit_workspace_site",
        "project_name": project_name,
        "success": False,
        "path": "",
        "modified_files": [],
        "preview_url": "",
        "curl_check": None,
        "error": "",
        "tools_called": tools_called,
        "rolled_back": False,
    }
    if not config.env_bool("WRITE_MODE_ENABLED", config.WRITE_MODE_ENABLED):
        action["error"] = "Write mode выключен. Включи WRITE_MODE_ENABLED=true в .env"
        save_last_action(chat_id, action)
        clear_current_task(chat_id)
        return (
            action["error"],
            {"detected": {"intent": "edit_workspace_site", "project": project_name}, "tools_called": tools_called, "errors": [action["error"]]},
        )
    try:
        update_current_task_step(chat_id, "reading_files")
        read_result = read_workspace_project_files(project_name)
        tools_called.append("read_workspace_project_files")
        if not read_result["files"]:
            raise RuntimeError("Не нашел текстовых файлов проекта для редактирования")
        before_files = read_result["files"]

        update_current_task_step(chat_id, "snapshotting")
        snapshot = snapshot_project(project_name, reason=user_text[:200])
        tools_called.append("snapshot_project")

        update_current_task_step(chat_id, "merging_requirements")
        inferred_requirements = infer_requirements_from_text(user_text)
        site_state = save_site_state(project_name, inferred_requirements) if inferred_requirements else load_site_state(project_name)
        requirements = site_state["requirements"]
        tools_called.append("save_site_state")

        before_inspected = inspect_site_state(project_name)
        tools_called.append("inspect_site_state")
        project_state = project_state_manager.sync_features_from_inspection(project_name)
        tools_called.append("sync_features_from_inspection")

        update_current_task_step(chat_id, "asking_ollama")
        try:
            plan = ask_ollama_for_operation_plan(user_text, project_name, project_state)
        except Exception as e:
            action["error"] = str(e)
            action["tools_called"] = tools_called
            save_last_action(chat_id, action)
            save_last_error(
                chat_id=chat_id or "", user_id="", handler="edit_workspace_site_workflow.ask_ollama", error=e, user_text=user_text
            )
            clear_current_task(chat_id)
            friendly = "Не смог получить корректный план операций от модели. Я не стал трогать файлы, чтобы не сломать сайт."
            return (
                friendly,
                {
                    "detected": {"intent": "edit_workspace_site", "project": project_name},
                    "tools_called": tools_called,
                    "errors": [str(e)],
                },
            )
        tools_called.append("ask_ollama_for_operation_plan")
        tools_called.append("validate_operation_plan")

        update_current_task_step(chat_id, "applying_operations")
        apply_result = apply_operation_plan(project_name, plan["operations"])
        tools_called.append("apply_operation_plan")
        operations_applied: list[dict] = list(apply_result["applied"])
        files_changed: list[str] = list(apply_result["files_changed"])
        plan_summary = plan.get("summary") or ""

        update_current_task_step(chat_id, "verifying_structure")
        verify = verify_workspace_project(project_name)
        tools_called.append("verify_workspace_project")
        if not verify["exists"]:
            raise RuntimeError("Проект не найден после редактирования")

        update_current_task_step(chat_id, "checking_preview")
        status = preview_status(project_name)
        tools_called.append("preview_status")
        if not status.get("running") and any(word in user_text.lower() for word in RESTART_PREVIEW_WORDS):
            start_preview(project_name)
            tools_called.append("start_preview")
            status = preview_status(project_name)
            tools_called.append("preview_status")

        # Feedback loop: apply operations -> verify (static + persistent requirement
        # checks + real browser check + feature-regression check) -> if it fails, ask
        # Ollama for another (still structured-operations-only) plan addressing ONLY
        # the failed checks. Capped at MAX_REPAIR_ITERATIONS so a stubborn failure
        # can't loop forever or run away with Ollama calls.
        acceptance = {"success": True, "failed": [], "critical_failed": [], "checks": {}}
        browser_result: dict | None = None
        curl_result = None
        preview_url = ""
        report_text = ""
        iteration = 0
        while True:
            iteration += 1
            update_current_task_step(chat_id, f"checking_acceptance_{iteration}")
            after_read = read_workspace_project_files(project_name)
            tools_called.append("read_workspace_project_files")

            browser_result = None
            if status.get("running") and status.get("port"):
                curl_result = curl_check(int(status["port"]))
                preview_url = preview_url_for_port(int(status["port"]))
                tools_called.append("curl_check")
                if playwright_available():
                    update_current_task_step(chat_id, "browser_check")
                    browser_result = check_site_with_playwright(
                        project_name,
                        f"http://127.0.0.1:{status['port']}/",
                        expect_background_image=expect_background_image,
                    )
                    tools_called.append("check_site_with_playwright")
                    save_last_verification(project_name, browser_result)

            text_acceptance = run_acceptance_checks(
                user_text,
                before_files,
                after_read["files"],
                browser_result=browser_result,
                expect_background_image=expect_background_image,
            )
            tools_called.append("run_acceptance_checks")
            persistent_acceptance = run_persistent_acceptance_checks(
                project_name, requirements, files=after_read["files"], browser_result=browser_result
            )
            tools_called.append("run_persistent_acceptance_checks")
            after_inspected = inspect_site_state(project_name)
            tools_called.append("inspect_site_state")
            regressions = detect_feature_regressions(before_inspected, after_inspected)
            tools_called.append("detect_feature_regressions")

            # Every failure from the legacy per-message heuristic check and every
            # feature regression is treated as critical; the persistent,
            # requirement-driven check additionally flags which of ITS failures are
            # critical (see tools_site_checks.run_acceptance_checks).
            combined_failed = list(dict.fromkeys(text_acceptance["failed"] + persistent_acceptance["failed"] + regressions))
            combined_critical = list(
                dict.fromkeys(text_acceptance["failed"] + persistent_acceptance["critical_failed"] + regressions)
            )
            acceptance = {
                "success": not combined_critical,
                "failed": combined_failed,
                "critical_failed": combined_critical,
                "checks": {**text_acceptance["checks"], **persistent_acceptance["checks"]},
            }

            if acceptance["success"] or iteration > MAX_REPAIR_ITERATIONS:
                break

            report_text = build_verification_report(acceptance["failed"], browser_result)
            update_current_task_step(chat_id, f"repairing_{iteration}")
            try:
                plan_repair = ask_ollama_for_operation_plan(user_text + "\n\n" + report_text, project_name, project_state)
                tools_called.append("ask_ollama_for_operation_plan_repair")
                apply_result_repair = apply_operation_plan(project_name, plan_repair["operations"])
                tools_called.append("apply_operation_plan")
                operations_applied.extend(apply_result_repair["applied"])
                for path in apply_result_repair["files_changed"]:
                    if path not in files_changed:
                        files_changed.append(path)
                plan_summary = plan_repair.get("summary") or plan_summary
            except Exception:
                # Can't repair further (e.g. another invalid JSON from Ollama); stop
                # looping and report the last known (failing) acceptance state below.
                break

        if chat_id:
            memory.set_current_project(chat_id, project_name)

        kept_despite_failure = _wants_keep_despite_failure(user_text)
        rollback_result: dict | None = None
        if not acceptance["success"] and not kept_despite_failure:
            update_current_task_step(chat_id, "rolling_back")
            rollback_result = rollback_project(project_name, snapshot["snapshot_id"])
            tools_called.append("rollback_project")

        success_snapshot_id = snapshot["snapshot_id"]
        if acceptance["success"]:
            success_snapshot = snapshot_project(project_name, reason=f"after: {plan_summary or user_text[:160]}")
            success_snapshot_id = success_snapshot["snapshot_id"]
            tools_called.append("snapshot_project")

        project_state_manager.record_applied_operation(
            project_name,
            user_text=user_text,
            operations=operations_applied,
            files_changed=files_changed,
            checks=acceptance,
            success=bool(acceptance["success"]),
            snapshot_id=success_snapshot_id if acceptance["success"] else snapshot["snapshot_id"],
        )
        tools_called.append("record_applied_operation")
        learning_log.record(
            project_name=project_name,
            chat_id=chat_id,
            user_text=user_text,
            detected_intent="edit_workspace_site",
            before_state=project_state,
            operation_plan=operations_applied,
            files_changed=files_changed,
            checks=acceptance,
            success=bool(acceptance["success"]),
            rollback_used=bool(rollback_result),
        )
        tools_called.append("learning_log.record")

        action.update(
            {
                "success": bool(acceptance["success"]),
                "path": verify["path"],
                "modified_files": files_changed,
                "preview_url": preview_url,
                "curl_check": curl_result,
                "tools_called": tools_called,
                "rolled_back": bool(rollback_result),
                "snapshot_id": snapshot["snapshot_id"],
            }
        )
        save_last_action(chat_id, action)
        clear_current_task(chat_id)

        info = {
            "resolved_path": verify["path"],
            "modified_files": files_changed,
            "preview_url": preview_url,
            "notes": [op.get("detail", "") for op in operations_applied if op.get("detail")],
        }
        if not preview_url:
            info["preview_note"] = "Preview не запущен (я его не запускал и не останавливал)."

        if acceptance["success"]:
            headline = f"Готово! Я изменил сайт {project_name}: {plan_summary or 'внес изменения по запросу'}."
            answer = format_user_result(headline, info)
        elif rollback_result is not None:
            headline = "Я попробовал, проверка не прошла, изменения откатил: " + "; ".join(acceptance["failed"])
            info["modified_files"] = []
            info["rollback"] = f"Файлы восстановлены из snapshot {snapshot['snapshot_id']} (см. /site_snapshots {project_name})."
            answer = format_user_result(headline, info)
        else:
            headline = "Проверка не прошла, но ты попросил оставить изменения как есть -- не откатываю: " + "; ".join(
                acceptance["failed"]
            )
            info["warning"] = f"Можно вернуть прежнюю версию: /site_rollback {project_name} {snapshot['snapshot_id']}"
            answer = format_user_result(headline, info)

        return (
            answer,
            {
                "detected": {"intent": "edit_workspace_site", "project": project_name},
                "tools_called": tools_called,
                "errors": [] if acceptance["success"] else acceptance["failed"],
                "resolved_path": verify["path"],
                "project": project_name,
                "modified_files": files_changed,
                "operations": operations_applied,
                "preview_url": preview_url,
                "curl_check": curl_result,
                "browser_check": browser_result,
                "acceptance": acceptance,
                "snapshot_id": snapshot["snapshot_id"],
                "rolled_back": bool(rollback_result),
                "requirements": requirements,
                "verification_report": report_text,
            },
        )
    except Exception as e:
        action["error"] = str(e)
        action["tools_called"] = tools_called
        rolled_back_on_error = False
        snapshot_local = locals().get("snapshot")
        if snapshot_local:
            try:
                rollback_project(project_name, snapshot_local["snapshot_id"])
                tools_called.append("rollback_project")
                rolled_back_on_error = True
                action["rolled_back"] = True
                action["snapshot_id"] = snapshot_local["snapshot_id"]
            except ToolError:
                pass
        save_last_action(chat_id, action)
        save_last_error(chat_id=chat_id or "", user_id="", handler="edit_workspace_site_workflow", error=e, user_text=user_text)
        clear_current_task(chat_id)
        suffix = " Файлы откатил до состояния до правки." if rolled_back_on_error else ""
        return (
            f"Не смог отредактировать сайт {project_name}: {e}.{suffix}",
            {"detected": {"intent": "edit_workspace_site", "project": project_name}, "tools_called": tools_called, "errors": [str(e)]},
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


def _preview_start_workflow(project: str, chat_id: str | None, user_text: str = "") -> tuple[str, dict]:
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
        save_last_error(chat_id=chat_id or "", user_id="", handler="preview_start_workflow", error=e, user_text=user_text)
        return (
            f"Preview не запущен: {e}",
            {"detected": {"intent": "preview_start", "project": project}, "tools_called": tools_called, "errors": [str(e)]},
        )


def _preview_stop_workflow(project: str, chat_id: str | None, user_text: str = "") -> tuple[str, dict]:
    tools_called = ["stop_preview"]
    try:
        result = stop_preview(project)
        save_last_action(
            chat_id,
            {
                "intent": "stop_preview",
                "project_name": project,
                "success": bool(result.get("success")),
                "path": result.get("path", ""),
                "error": result.get("error", ""),
                "tools_called": tools_called,
                "verification": result.get("checks"),
            },
        )
        return (
            _format_stop_result("stop_preview result:", result),
            {
                "detected": {"intent": "stop_preview", "project": project},
                "tools_called": tools_called,
                "errors": [] if result.get("success") else [result.get("error", "")],
            },
        )
    except Exception as e:
        save_last_error(chat_id=chat_id or "", user_id="", handler="preview_stop_workflow", error=e, user_text=user_text)
        return (
            f"Не смог остановить preview: {e}",
            {"detected": {"intent": "stop_preview", "project": project}, "tools_called": tools_called, "errors": [str(e)]},
        )


def _preview_stop_by_port_workflow(port: int, chat_id: str | None, user_text: str = "") -> tuple[str, dict]:
    tools_called = ["stop_preview_by_port"]
    try:
        result = stop_preview_by_port(port)
        save_last_action(
            chat_id,
            {
                "intent": "stop_preview_by_port",
                "project_name": f"port:{port}",
                "success": bool(result.get("success")),
                "path": result.get("cwd", ""),
                "error": result.get("error", ""),
                "tools_called": tools_called,
                "verification": result.get("checks"),
            },
        )
        return (
            _format_stop_result("stop_preview_by_port result:", result),
            {
                "detected": {"intent": "stop_preview_by_port", "port": port},
                "tools_called": tools_called,
                "errors": [] if result.get("success") else [result.get("error", "")],
            },
        )
    except Exception as e:
        save_last_error(chat_id=chat_id or "", user_id="", handler="preview_stop_by_port_workflow", error=e, user_text=user_text)
        return (
            f"Не смог остановить preview на порту {port}: {e}",
            {"detected": {"intent": "stop_preview_by_port", "port": port}, "tools_called": tools_called, "errors": [str(e)]},
        )


def _preview_stop_all_workflow(chat_id: str | None) -> tuple[str, dict]:
    rows = _stop_all_previews()
    save_last_action(
        chat_id,
        {
            "intent": "preview_stop_all",
            "project_name": ", ".join(row["project"] for row in rows) or "-",
            "success": all(row["stopped"] for row in rows) if rows else True,
            "tools_called": ["stop_preview"] * len(rows),
            "verification": rows,
        },
    )
    return (
        _format_stop_all_table(rows),
        {"detected": {"intent": "preview_stop_all"}, "tools_called": ["stop_preview"] * len(rows), "errors": []},
    )


def _workspace_delete_workflow(project: str | None, chat_id: str | None, user_text: str = "") -> tuple[str, dict]:
    if not project:
        try:
            projects = [item["name"] for item in list_workspace()["projects"]]
        except Exception:
            projects = []
        listing = ", ".join(projects) or "-"
        return (
            "Нужно явно указать проект для удаления (опасное действие).\n"
            f"Доступные workspace-проекты: {listing}\n"
            "Используй: /workspace_delete <project>",
            {"detected": {"intent": "delete_workspace_dir"}, "tools_called": [], "errors": ["project not specified"]},
        )
    tools_called = ["delete_workspace_dir"]
    try:
        result = delete_workspace_dir(project, confirm_token=f"DELETE:{project}")
        save_last_action(
            chat_id,
            {
                "intent": "delete_workspace_dir",
                "project_name": result.get("project_name", project),
                "success": bool(result.get("success")),
                "path": result.get("path", ""),
                "error": result.get("error", ""),
                "tools_called": tools_called,
                "verification": result.get("verification"),
            },
        )
        return (
            _format_delete_result(result),
            {
                "detected": {"intent": "delete_workspace_dir", "project": project},
                "tools_called": tools_called,
                "errors": [] if result.get("success") else [result.get("error", "")],
            },
        )
    except Exception as e:
        save_last_error(chat_id=chat_id or "", user_id="", handler="workspace_delete_workflow", error=e, user_text=user_text)
        return (
            f"Не смог удалить проект {project}: {e}",
            {"detected": {"intent": "delete_workspace_dir", "project": project}, "tools_called": tools_called, "errors": [str(e)]},
        )


def workspace_status_answer(user_text: str, chat_id: str | None = None) -> tuple[str, dict] | None:
    if not _is_workspace_status_question(user_text) and not _wants_start_preview(user_text):
        return None
    project = _workspace_project_from_context(user_text, chat_id)
    if _wants_start_preview(user_text) and project:
        return _preview_start_workflow(project, chat_id, user_text)
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
            f"Не смог изменить проект: {e}",
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
                _format_write_success("Готово! Создал Flask-проект.", ["create_flask_site"], data["path"], created=data["created_files"]),
                {"detected": detected, "tools_called": ["create_flask_site"], "errors": [], "resolved_path": data["path"], "project": data["project_name"]},
            )
        except Exception as e:
            save_last_action(chat_id, {"intent": "create_flask_site", "project_name": name, "success": False, "error": str(e), "tools_called": ["create_flask_site"]})
            save_last_error(chat_id=chat_id or "", user_id="", handler="write_mode_answer", error=e, user_text=user_text)
            return (f"Не смог создать Flask-проект: {e}", {"detected": detected, "tools_called": ["create_flask_site"], "errors": [str(e)]})
    return create_site_workflow(user_text, project_name=name, chat_id=chat_id, start_preview_requested=True)


STOP_WORDS = ("останови", "остановить", "отключи", "отключить", "выключи", "выключить", "стопни", "stop")
DELETE_WORDS = ("удали", "удалить", "снеси", "снести", "delete")
STOP_ALL_PHRASES = (
    "оба сервера",
    "оба preview",
    "обе превью",
    "все сервера",
    "все серверы",
    "все preview",
    "все превью",
)
DELETE_VAGUE_PHRASES = (
    "папки сайтов",
    "папку сайтов",
    "все сайты",
    "все папки",
    "все проекты workspace",
)
PORT_RE = re.compile(r"порт[ауе]?\s*:?\s*(\d{2,5})")


def _extract_port(text: str) -> int | None:
    match = PORT_RE.search(text.lower())
    return int(match.group(1)) if match else None


def stop_delete_answer(user_text: str, chat_id: str | None = None) -> tuple[str, dict] | None:
    lowered = user_text.lower()
    wants_stop = any(word in lowered for word in STOP_WORDS)
    wants_delete = any(word in lowered for word in DELETE_WORDS)
    if not wants_stop and not wants_delete:
        return None

    if wants_stop:
        port = _extract_port(lowered)
        if port is not None:
            return _preview_stop_by_port_workflow(port, chat_id, user_text)

        if any(phrase in lowered for phrase in STOP_ALL_PHRASES):
            return _preview_stop_all_workflow(chat_id)

        if any(word in lowered for word in ("сервер", "preview", "превью")):
            project = _workspace_project_from_context(user_text, chat_id)
            if not project:
                return (
                    "Не понял, какой проект остановить. Укажи имя: /preview_stop <project> или назови порт.",
                    {"detected": {"intent": "stop_preview"}, "tools_called": [], "errors": ["project not resolved"]},
                )
            return _preview_stop_workflow(project, chat_id, user_text)

    if wants_delete and any(word in lowered for word in ("папк", "сайт", "проект", "workspace")):
        explicit_name = _workspace_name_from_text(user_text)
        is_vague = any(phrase in lowered for phrase in DELETE_VAGUE_PHRASES)
        project = explicit_name if explicit_name and _workspace_project_exists(explicit_name) else None
        if not project and not is_vague:
            project = _workspace_project_from_context(user_text, chat_id)
            if project and not _workspace_project_exists(project):
                project = None
        return _workspace_delete_workflow(project, chat_id, user_text)

    return None


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
    if any(phrase in lowered for phrase in EDIT_SITE_PHRASES) or any(verb in lowered for verb in semantic_router.ACTION_VERBS) and any(
        word in lowered for word in ("стиль", "дизайн", "погод", "style", "design", "weather", "estilo", "diseño", "tiempo")
    ):
        return "edit_workspace_site"
    if any(phrase in lowered for phrase in EDIT_PROJECT_PHRASES):
        return "edit_workspace_project"
    if any(word in lowered for word in STOP_WORDS) or any(word in lowered for word in DELETE_WORDS):
        return "stop_or_delete_workspace"
    if any(phrase in lowered for phrase in ("проверь код", "есть ли ошибка", "ошибки в проекте", "проверь проект", "check project")):
        return "safe_code_check"
    if any(phrase in lowered for phrase in ("посмотри проект", "изучи проект", "изучи код", "на чем остановились", "на чём остановились")):
        return "inspect_project"
    if any(phrase in lowered for phrase in ("preview", "превью", "запусти сайт", "запусти проект")):
        return "preview"
    return None


def semantic_router_answer(
    user_text: str,
    chat_id: str | None = None,
    recent_messages: list[dict] | None = None,
    current_project: str | None = None,
) -> tuple[str, dict] | None:
    last_action = get_last_action(chat_id)
    classification = semantic_router.classify_intent(
        user_text,
        recent_messages=recent_messages,
        current_project=current_project,
        last_action=last_action,
        ask_model=ask_ollama_messages,
    )
    intent = classification.get("intent", "unknown")
    confidence = float(classification.get("confidence") or 0.0)
    action_like = semantic_router.is_action_like(user_text)

    if semantic_router.is_router_failure(classification):
        if action_like:
            return (
                semantic_router.CLARIFY_MESSAGE,
                {"detected": {"intent": "unknown"}, "tools_called": [], "errors": ["router_failed"], "router": classification},
            )
        return None

    if intent == "unknown":
        if action_like:
            return (
                semantic_router.CLARIFY_MESSAGE,
                {"detected": {"intent": "unknown"}, "tools_called": [], "errors": ["router_uncertain"], "router": classification},
            )
        intent = "normal_chat"

    if intent != "normal_chat" and confidence < semantic_router.LOW_CONFIDENCE_THRESHOLD and action_like:
        return (
            semantic_router.CLARIFY_MESSAGE,
            {"detected": {"intent": intent}, "tools_called": [], "errors": ["low_confidence"], "router": classification},
        )

    project = classification.get("project_name") or current_project
    start_preview_flag = intent == "create_and_preview" or bool(classification.get("start_preview"))

    if intent == "normal_chat":
        answer = ask_ollama(user_text, chat_id=chat_id)
        return answer, {"detected": {"intent": "normal_chat"}, "tools_called": [], "errors": [], "router": classification}

    if intent == "workspace_inventory":
        routed = handle_detected_intent({"intent": "workspace_inventory"})
        return routed["answer"], {**routed, "detected": {"intent": "workspace_inventory"}, "router": classification}

    if intent in ("create_static_site", "create_and_preview"):
        name = project or _slug_from_text(user_text)
        answer, debug_info = create_site_workflow(
            user_text, project_name=name, chat_id=chat_id, start_preview_requested=start_preview_flag
        )
        debug_info["router"] = classification
        return answer, debug_info

    if intent == "edit_workspace_site":
        proj = project or (last_action or {}).get("project_name") or _workspace_project_from_context(user_text, chat_id)
        if not proj or not _workspace_project_exists(proj):
            return (
                "Не понял, какой проект редактировать. Укажи: /edit_site <project> <task>, "
                "или сначала упомяни проект.",
                {"detected": {"intent": "edit_workspace_site"}, "tools_called": [], "errors": ["project not resolved"], "router": classification},
            )
        answer, debug_info = edit_workspace_site_workflow(user_text, proj, chat_id)
        debug_info["router"] = classification
        return answer, debug_info

    if intent == "where_project":
        proj = project or _workspace_project_from_context(user_text, chat_id)
        answer, debug_info = workspace_where_answer(proj, chat_id=chat_id)
        debug_info["router"] = classification
        return answer, debug_info

    if intent == "preview_start":
        proj = project or _workspace_project_from_context(user_text, chat_id)
        if not proj:
            return (
                semantic_router.CLARIFY_MESSAGE,
                {"detected": {"intent": "preview_start"}, "tools_called": [], "errors": ["project not resolved"], "router": classification},
            )
        answer, debug_info = _preview_start_workflow(proj, chat_id, user_text)
        debug_info["router"] = classification
        return answer, debug_info

    if intent == "preview_stop":
        proj = project or _workspace_project_from_context(user_text, chat_id)
        if not proj:
            return (
                semantic_router.CLARIFY_MESSAGE,
                {"detected": {"intent": "preview_stop"}, "tools_called": [], "errors": ["project not resolved"], "router": classification},
            )
        answer, debug_info = _preview_stop_workflow(proj, chat_id, user_text)
        debug_info["router"] = classification
        return answer, debug_info

    if intent == "preview_stop_all":
        answer, debug_info = _preview_stop_all_workflow(chat_id)
        debug_info["router"] = classification
        return answer, debug_info

    if intent == "workspace_delete":
        answer, debug_info = _workspace_delete_workflow(project, chat_id, user_text)
        debug_info["router"] = classification
        return answer, debug_info

    if intent == "project_inspect":
        proj = project or extract_mentioned_project(user_text) or current_project
        if not proj:
            return None
        routed = handle_detected_intent({"intent": "inspect_project", "project": proj}, summarize_project=summarize_project_with_ollama)
        return routed["answer"], {**routed, "detected": {"intent": "inspect_project", "project": proj}, "router": classification}

    if intent == "safe_code_check":
        proj = project or current_project or extract_mentioned_project(user_text)
        if not proj:
            return None
        routed = handle_detected_intent({"intent": "safe_code_check", "project": proj})
        return routed["answer"], {**routed, "detected": {"intent": "safe_code_check", "project": proj}, "router": classification}

    if intent == "git_repos":
        routed = handle_detected_intent({"intent": "list_projects"})
        return routed["answer"], {**routed, "detected": {"intent": "list_projects"}, "router": classification}

    if intent == "git_status":
        proj = project or current_project or extract_mentioned_project(user_text)
        if not proj:
            return None
        routed = handle_detected_intent({"intent": "git_status", "project": proj})
        return routed["answer"], {**routed, "detected": {"intent": "git_status", "project": proj}, "router": classification}

    if intent == "git_diff":
        proj = project or current_project or extract_mentioned_project(user_text)
        if not proj:
            return None
        routed = handle_detected_intent({"intent": "git_diff", "project": proj})
        return routed["answer"], {**routed, "detected": {"intent": "git_diff", "project": proj}, "router": classification}

    if intent == "memory_save":
        candidates = memory.extract_memory_candidates(user_text)
        if not candidates:
            candidates = [{"kind": "note", "key": "_".join(user_text.lower().split()[:5])[:80], "value": user_text, "confidence": 0.8}]
        for candidate in candidates:
            memory.upsert_memory(**candidate)
        answer = "Запомнил: " + ", ".join(candidate["key"] for candidate in candidates)
        return answer, {
            "detected": {"intent": "memory_save"},
            "tools_called": ["extract_memory_candidates", "upsert_memory"],
            "errors": [],
            "router": classification,
        }

    if intent == "memory_query":
        rows = memory.list_memories(20)
        answer = "Память пуста." if not rows else "\n".join(
            f"{row['key']}: {row['value']} ({row['kind']}, {row['confidence']})" for row in rows
        )
        return answer, {
            "detected": {"intent": "memory_query"},
            "tools_called": ["list_memories"],
            "errors": [],
            "router": classification,
        }

    if intent == "last_action":
        answer = _format_last_action(get_last_action(chat_id))
        return answer, {
            "detected": {"intent": "last_action"},
            "tools_called": ["get_last_action"],
            "errors": [],
            "router": classification,
        }

    if intent == "last_error":
        item = latest_error(chat_id)
        if not item:
            answer = "Ошибок нет."
        else:
            answer = "\n".join(
                [
                    f"timestamp: {item.get('timestamp')}",
                    f"handler: {item.get('handler')}",
                    f"error_type: {item.get('error_type')}",
                    f"error_message: {item.get('error_message')}",
                ]
            )
        return answer, {
            "detected": {"intent": "last_error"},
            "tools_called": ["latest_error"],
            "errors": [],
            "router": classification,
        }

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

    current_project = memory.get_current_project(chat_id) if chat_id else None

    router_answer = semantic_router_answer(user_text, chat_id=chat_id, recent_messages=recent_messages, current_project=current_project)
    if router_answer:
        answer, debug_info = router_answer
        router_project = (debug_info.get("router") or {}).get("project_name") or debug_info.get("detected", {}).get("project")
        if router_project and chat_id:
            memory.set_current_project(chat_id, router_project)
        return answer, debug_info

    # Fallback: the semantic router failed outright on a non-action message, or
    # explicitly deferred (e.g. could not resolve a project for a read-only intent).
    # The old deterministic phrase matching below only runs in that case.
    status_answer = workspace_status_answer(user_text, chat_id=chat_id)
    if status_answer:
        return status_answer
    stop_delete = stop_delete_answer(user_text, chat_id=chat_id)
    if stop_delete:
        return stop_delete
    write_answer = write_mode_answer(user_text, chat_id=chat_id)
    if write_answer:
        return write_answer

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


class ProgressTracker:
    """Edits a single Telegram message in place across workflow steps when possible,
    falling back to a new message if editing fails (e.g. message too old)."""

    def __init__(self, message):
        self._message = message
        self._sent = None

    async def step(self, text: str) -> None:
        safe_text = mask_error_text(text)[:900]
        logging.info("progress %s", safe_text)
        if not self._message:
            return
        if self._sent is None:
            try:
                self._sent = await self._message.reply_text(safe_text)
            except Exception:
                self._sent = None
            return
        try:
            await self._sent.edit_text(safe_text)
        except Exception:
            try:
                self._sent = await self._message.reply_text(safe_text)
            except Exception:
                self._sent = None


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


def _workspace_status_text() -> str:
    data = workspace_inventory()
    ports = scan_listening_ports()
    return format_workspace_inventory(data, ports)


async def workspace_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id, user_id = chat_user_ids(update)
    try:
        await reply_long(update.message, _workspace_status_text())
    except Exception as e:
        save_last_error(chat_id=chat_id, user_id=user_id, handler="workspace_status_command", error=e, user_text=update.message.text or "")
        await update.message.reply_text(f"Ошибка /workspace_status: {e}")


async def workspace_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await workspace_status_command(update, context)


async def ports_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id, user_id = chat_user_ids(update)
    try:
        ports = scan_listening_ports()
        registered = ports.get("registered_previews") or []
        listening = ports.get("listening") or []
        port_range = ports.get("range") or [None, None]
        suspicious = [item for item in listening if item.get("suspicious")]
        lines = [
            f"Preview port range: {port_range[0]}-{port_range[1]}",
            "Registered previews (previews.json):",
        ]
        if registered:
            lines.extend(f"- {item['project']}: port {item['port']}" for item in registered)
        else:
            lines.append("- нет зарегистрированных preview")
        lines.append("Реально слушающие порты:")
        if listening:
            for item in listening:
                tag = item.get("registered_project") or ("suspicious" if item.get("suspicious") else "unknown")
                lines.append(f"- {item['port']} pid={item.get('pid')} owner={tag}")
        else:
            lines.append("- нет активных preview")
        if suspicious:
            lines.append("Suspicious (слушают http.server, но не зарегистрированы):")
            lines.extend(f"- {item['port']} pid={item.get('pid')} cwd={item.get('cwd') or '-'}" for item in suspicious)
        await reply_long(update.message, "\n".join(lines))
    except Exception as e:
        save_last_error(chat_id=chat_id, user_id=user_id, handler="ports_command", error=e, user_text=update.message.text or "")
        await update.message.reply_text(f"Ошибка /ports: {e}")


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
                "Создал статический сайт в рабочей папке Jarvis.",
                ["create_static_site", "verify_static_site"],
                data["path"],
                created=data["created_files"],
                debug=bool(context.user_data.get("agent_debug")),
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
                "Создал Flask-проект в рабочей папке Jarvis.",
                ["create_flask_site"],
                data["path"],
                created=data["created_files"],
                debug=bool(context.user_data.get("agent_debug")),
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
                "Записал файл в рабочей папке Jarvis.",
                ["write_text_file"],
                result["path"],
                modified=[result["path"]],
                debug=bool(context.user_data.get("agent_debug")),
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
                "Удалил файл из рабочей папки Jarvis.",
                ["delete_workspace_file"],
                result["path"],
                deleted=[result["path"]],
                debug=bool(context.user_data.get("agent_debug")),
            ),
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка /delete_file: {e}")


async def preview_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id, _ = chat_user_ids(update)
    project = context.args[0] if context.args else (memory.get_current_project(chat_id) or (get_last_action(chat_id) or {}).get("project_name"))
    if not project or not _workspace_project_exists(str(project)):
        projects = ", ".join(item["name"] for item in list_workspace().get("projects", [])) or "-"
        await update.message.reply_text(
            "Использование: /preview_start <project>\n"
            f"Текущий проект не выбран. Доступные workspace-проекты: {projects}"
        )
        return
    project = str(project)
    try:
        result = start_preview(project)
        status = preview_status(project)
        if not result.get("success") or not status.get("running"):
            raise RuntimeError("Preview tool не подтвердил запущенный процесс")
        memory.set_current_project(chat_id, project)
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
                "Запустил preview для проекта.",
                ["start_preview", "preview_status"],
                result["path"],
                preview_url=preview_url_for_port(int(result["port"])),
                debug=bool(context.user_data.get("agent_debug")),
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
    chat_id, user_id = chat_user_ids(update)
    project = context.args[0]
    try:
        result = stop_preview(project)
        save_last_action(
            chat_id,
            {
                "intent": "stop_preview",
                "project_name": project,
                "success": bool(result.get("success")),
                "path": result.get("path", ""),
                "error": result.get("error", ""),
                "tools_called": ["stop_preview"],
                "verification": result.get("checks"),
            },
        )
        await reply_long(
            update.message,
            _format_stop_result("stop_preview result:", result, debug=bool(context.user_data.get("agent_debug"))),
        )
    except Exception as e:
        save_last_error(chat_id=chat_id, user_id=user_id, handler="preview_stop_command", error=e, user_text=update.message.text or "")
        await update.message.reply_text(f"Ошибка /preview_stop: {e}")


async def preview_stop_port_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /preview_stop_port <port>")
        return
    chat_id, user_id = chat_user_ids(update)
    try:
        port = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Порт должен быть числом")
        return
    try:
        result = stop_preview_by_port(port)
        save_last_action(
            chat_id,
            {
                "intent": "stop_preview_by_port",
                "project_name": f"port:{port}",
                "success": bool(result.get("success")),
                "path": result.get("cwd", ""),
                "error": result.get("error", ""),
                "tools_called": ["stop_preview_by_port"],
                "verification": result.get("checks"),
            },
        )
        await reply_long(
            update.message,
            _format_stop_result("stop_preview_by_port result:", result, debug=bool(context.user_data.get("agent_debug"))),
        )
    except Exception as e:
        save_last_error(chat_id=chat_id, user_id=user_id, handler="preview_stop_port_command", error=e, user_text=update.message.text or "")
        await update.message.reply_text(f"Ошибка /preview_stop_port: {e}")


async def preview_stop_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id, user_id = chat_user_ids(update)
    try:
        rows = _stop_all_previews()
        save_last_action(
            chat_id,
            {
                "intent": "preview_stop_all",
                "project_name": ", ".join(row["project"] for row in rows) or "-",
                "success": all(row["stopped"] for row in rows) if rows else True,
                "tools_called": ["stop_preview"] * len(rows),
                "verification": rows,
            },
        )
        await reply_long(update.message, _format_stop_all_table(rows))
    except Exception as e:
        save_last_error(chat_id=chat_id, user_id=user_id, handler="preview_stop_all_command", error=e, user_text=update.message.text or "")
        await update.message.reply_text(f"Ошибка /preview_stop_all: {e}")


async def workspace_delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /workspace_delete <project>")
        return
    chat_id, user_id = chat_user_ids(update)
    project = context.args[0]
    try:
        result = delete_workspace_dir(project, confirm_token=f"DELETE:{project}")
        save_last_action(
            chat_id,
            {
                "intent": "delete_workspace_dir",
                "project_name": result.get("project_name", project),
                "success": bool(result.get("success")),
                "path": result.get("path", ""),
                "error": result.get("error", ""),
                "tools_called": ["delete_workspace_dir"],
                "verification": result.get("verification"),
            },
        )
        await reply_long(
            update.message,
            _format_delete_result(result, debug=bool(context.user_data.get("agent_debug"))),
        )
    except Exception as e:
        save_last_error(chat_id=chat_id, user_id=user_id, handler="workspace_delete_command", error=e, user_text=update.message.text or "")
        await update.message.reply_text(f"Ошибка /workspace_delete: {e}")


async def workspace_clean_stopped_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    try:
        result = cleanup_stale_previews()
        lines = [
            "workspace_clean_stopped result:",
            f"removed: {', '.join(result['removed']) or '-'}",
            f"remaining: {', '.join(result['remaining']) or '-'}",
        ]
        await reply_long(update.message, "\n".join(lines))
    except Exception as e:
        chat_id, user_id = chat_user_ids(update)
        save_last_error(chat_id=chat_id, user_id=user_id, handler="workspace_clean_stopped_command", error=e, user_text=update.message.text or "")
        await update.message.reply_text(f"Ошибка /workspace_clean_stopped: {e}")


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


async def edit_site_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /edit_site <project> <задача>")
        return
    chat_id, _ = chat_user_ids(update)
    project = context.args[0]
    task_text = " ".join(context.args[1:])
    tracker = ProgressTracker(update.message)
    await tracker.step("⏳ Принял задачу...")
    await tracker.step("🧠 Генерирую изменения...")
    await tracker.step("💾 Записываю файлы...")
    await tracker.step("🧪 Проверяю сайт...")
    try:
        answer, debug = edit_workspace_site_workflow(task_text, project, chat_id)
        await tracker.step("✅ Готово.")
        await reply_long(update.message, answer)
        await maybe_send_intent_debug(update.message, context, debug)
    except Exception as e:
        chat_id, user_id = chat_user_ids(update)
        save_last_error(chat_id=chat_id, user_id=user_id, handler="edit_site_command", error=e, user_text=update.message.text or "")
        await update.message.reply_text(f"Ошибка /edit_site: {e}")


async def site_check_workflow(project_name: str, chat_id: str | None = None) -> tuple[str, dict]:
    """Read-only acceptance check against the project's persisted requirements
    (tools_site_state) and -- if a preview can be reached -- a real browser
    check. Never edits files, never calls Ollama. This is what "А где фон?"
    and similar status questions should run instead of just preview_start."""
    tools_called: list[str] = []
    if not config.env_bool("WRITE_MODE_ENABLED", config.WRITE_MODE_ENABLED):
        msg = "Write mode выключен. Включи WRITE_MODE_ENABLED=true в .env"
        return msg, {"detected": {"intent": "site_check", "project": project_name}, "tools_called": tools_called, "errors": [msg]}
    try:
        requirements = get_site_requirements(project_name)
        tools_called.append("get_site_requirements")
        files = read_workspace_project_files(project_name)["files"]
        tools_called.append("read_workspace_project_files")

        status = preview_status(project_name)
        tools_called.append("preview_status")
        if not status.get("running"):
            start_preview(project_name)
            tools_called.append("start_preview")
            status = preview_status(project_name)
            tools_called.append("preview_status")

        browser_result = None
        preview_url = ""
        if status.get("running") and status.get("port"):
            preview_url = preview_url_for_port(int(status["port"]))
            if playwright_available():
                browser_result = await check_site_with_playwright_async(project_name, f"http://127.0.0.1:{status['port']}/")
                tools_called.append("check_site_with_playwright_async")
                save_last_verification(project_name, browser_result)

        acceptance = run_persistent_acceptance_checks(project_name, requirements, files=files, browser_result=browser_result)
        tools_called.append("run_persistent_acceptance_checks")
        inspected = inspect_site_state(project_name)
        tools_called.append("inspect_site_state")

        lines = [f"Проверка сайта {project_name}:"]
        if acceptance["success"]:
            lines.append("Все проверки по сохранённым требованиям пройдены.")
        else:
            lines.append("Найдены проблемы:")
            lines.extend(f"- {item}" for item in acceptance["failed"])
        if requirements.get("background_required"):
            lines.append(
                f"Фон: {'есть, ' + inspected['background_image_path'] if inspected['has_background'] else 'не найден в CSS'}."
            )
        if preview_url:
            lines.append(f"Preview: {preview_url}")
        answer = "\n".join(lines)
        return answer, {
            "detected": {"intent": "site_check", "project": project_name},
            "tools_called": tools_called,
            "errors": [] if acceptance["success"] else acceptance["failed"],
            "acceptance": acceptance,
            "inspected": inspected,
        }
    except ToolError as e:
        return (
            f"Не смог проверить сайт {project_name}: {e}",
            {"detected": {"intent": "site_check", "project": project_name}, "tools_called": tools_called, "errors": [str(e)]},
        )


async def site_state_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /site_state <project>")
        return
    project = context.args[0]
    try:
        await reply_long(update.message, format_site_state_answer(project))
    except Exception as e:
        await update.message.reply_text(f"Ошибка /site_state: {e}")


async def site_snapshots_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /site_snapshots <project>")
        return
    project = context.args[0]
    try:
        snapshots = list_snapshots(project)
        if not snapshots:
            await update.message.reply_text(f"Снапшотов для {project} ещё нет.")
            return
        lines = [f"Снапшоты {project} (новые сверху):"]
        for snap in snapshots[:20]:
            reason = (snap.get("reason") or "")[:60]
            lines.append(f"- {snap['snapshot_id']} ({snap.get('created_at', '-')}) {reason}")
        await reply_long(update.message, "\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Ошибка /site_snapshots: {e}")


async def site_rollback_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /site_rollback <project> <snapshot_id>")
        return
    project, snapshot_id = context.args[0], context.args[1]
    try:
        result = rollback_project(project, snapshot_id)
        if result["success"]:
            await update.message.reply_text(
                f"Откатил {project} к snapshot {snapshot_id}. Восстановлено файлов: {len(result['restored_files'])}."
            )
        else:
            await update.message.reply_text(f"Откат не полностью удался: {'; '.join(result['errors']) or 'неизвестная ошибка'}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка /site_rollback: {e}")


async def site_check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id, _ = chat_user_ids(update)
    project = context.args[0] if context.args else memory.get_current_project(chat_id)
    if not project or not _workspace_project_exists(str(project)):
        projects = ", ".join(item["name"] for item in list_workspace().get("projects", [])) or "-"
        await update.message.reply_text(
            "Использование: /site_check <project>\n"
            f"Текущий проект не выбран. Доступные workspace-проекты: {projects}"
        )
        return
    project = str(project)
    try:
        answer, debug = await site_check_workflow(project, chat_id=chat_id)
        await reply_long(update.message, answer)
        await maybe_send_intent_debug(update.message, context, debug)
    except Exception as e:
        await update.message.reply_text(f"Ошибка /site_check: {e}")


async def repair_site_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /repair_site <project>")
        return
    project = context.args[0]
    chat_id, _ = chat_user_ids(update)
    try:
        check_answer, check_debug = await site_check_workflow(project, chat_id=chat_id)
        acceptance = check_debug.get("acceptance") or {}
        if acceptance.get("success"):
            await reply_long(update.message, f"Проверка сайта {project} уже проходит, чинить нечего.\n\n{check_answer}")
            return
        failed = acceptance.get("failed") or []
        task_text = "Почини сайт: исправь проблемы из автоматической проверки, не убирая существующий функционал.\n" + "\n".join(
            f"- {item}" for item in failed
        )
        tracker = ProgressTracker(update.message)
        await tracker.step("🧪 Нашел проблемы, пробую починить...")
        answer, debug = edit_workspace_site_workflow(task_text, project, chat_id)
        await tracker.step("✅ Готово.")
        await reply_long(update.message, answer)
        await maybe_send_intent_debug(update.message, context, debug)
    except Exception as e:
        chat_id, user_id = chat_user_ids(update)
        save_last_error(chat_id=chat_id, user_id=user_id, handler="repair_site_command", error=e, user_text=update.message.text or "")
        await update.message.reply_text(f"Ошибка /repair_site: {e}")


async def site_history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /site_history <project>")
        return
    project = context.args[0]
    try:
        await reply_long(update.message, project_state_manager.format_history_answer(project))
    except Exception as e:
        await update.message.reply_text(f"Ошибка /site_history: {e}")


async def site_last_success_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /site_last_success <project>")
        return
    project = context.args[0]
    try:
        await update.message.reply_text(project_state_manager.format_last_success_answer(project))
    except Exception as e:
        await update.message.reply_text(f"Ошибка /site_last_success: {e}")


async def site_diff_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /site_diff <project>")
        return
    project = context.args[0]
    try:
        await reply_long(update.message, project_state_manager.format_diff_answer(project))
    except Exception as e:
        await update.message.reply_text(f"Ошибка /site_diff: {e}")


async def site_requirements_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /site_requirements <project>")
        return
    project = context.args[0]
    try:
        await reply_long(update.message, project_state_manager.format_requirements_answer(project))
    except Exception as e:
        await update.message.reply_text(f"Ошибка /site_requirements: {e}")


CURRENT_ACTIVITY_QUESTION_PHRASES = (
    "что делаешь сейчас", "что ты сейчас делаешь", "чем занят", "что сейчас происходит",
    "what are you doing now", "what are you doing right now", "what's happening now",
    "qué estás haciendo ahora", "que estas haciendo ahora",
)


def _wants_current_activity(text: str) -> bool:
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in CURRENT_ACTIVITY_QUESTION_PHRASES)


FEEDBACK_APPROVED_PHRASES = (
    "да, правильно", "да правильно", "всё верно", "все верно", "хорошо сделал", "так и было нужно",
    "yes, correct", "that's correct", "looks good", "correcto", "está bien", "esta bien",
)
FEEDBACK_REJECTED_PHRASES = (
    "нет, сломал", "нет сломал", "это сломало", "ты сломал", "стало хуже", "это неправильно",
    "no, broke", "that broke", "this is wrong", "no, eso rompió", "eso rompio",
)


def _detect_feedback(text: str) -> str | None:
    lowered = (text or "").lower()
    if any(phrase in lowered for phrase in FEEDBACK_REJECTED_PHRASES):
        return "rejected"
    if any(phrase in lowered for phrase in FEEDBACK_APPROVED_PHRASES):
        return "approved"
    return None


async def current_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id, _ = chat_user_ids(update)
    task = get_current_task(chat_id)
    if not task:
        await update.message.reply_text("Сейчас нет активной задачи.")
        return
    lines = [
        f"intent: {task.get('intent')}",
        f"project: {task.get('project_name')}",
        f"step: {task.get('step')}",
        f"started_at: {task.get('started_at')}",
        f"updated_at: {task.get('updated_at')}",
    ]
    await update.message.reply_text("\n".join(lines))


async def progress_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id, _ = chat_user_ids(update)
    task = get_current_task(chat_id)
    if task:
        lines = [
            "Выполняется задача:",
            f"intent: {task.get('intent')}",
            f"project: {task.get('project_name')}",
            f"step: {task.get('step')}",
        ]
        await update.message.reply_text("\n".join(lines))
        return
    await reply_long(update.message, _format_last_action(get_last_action(chat_id)))


async def browser_check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /browser_check <project>")
        return
    project = context.args[0]
    try:
        status = preview_status(project)
        if not status.get("running") or not status.get("port"):
            await update.message.reply_text(f"Preview для {project} не запущен. Сначала: /preview_start {project}")
            return
        if not playwright_available():
            await update.message.reply_text("Playwright не установлен, браузерная проверка недоступна.")
            return
        result = await check_site_with_playwright_async(project, f"http://127.0.0.1:{status['port']}/")
        save_last_verification(project, result)
        lines = [
            f"Браузерная проверка {project}:",
            f"success: {result.get('success')}",
            f"title: {result.get('title') or '-'}",
            f"body_present: {result.get('body_present')} (length {result.get('body_text_length')})",
            f"sections_count: {result.get('sections_count')}",
            f"language_buttons_found: {', '.join(result.get('language_buttons_found') or []) or '-'}",
            f"language_switch_ok: {result.get('language_switch_ok')}",
        ]
        if result.get("console_errors"):
            lines.append(f"console_errors: {'; '.join(result['console_errors'][:5])}")
        if result.get("errors"):
            lines.append(f"errors: {'; '.join(result['errors'][:5])}")
        if result.get("screenshot_path"):
            lines.append(f"screenshot: {result['screenshot_path']}")
        await reply_long(update.message, "\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Ошибка /browser_check: {e}")


async def images_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /images <project>")
        return
    project = context.args[0]
    try:
        result = list_workspace_project_images(project)
        if not result["images"]:
            searched = ", ".join(result["searched_dirs"]) or "assets/img, assets/images, static/img, public/img"
            await update.message.reply_text(f"В проекте {project} изображений не найдено (искал в: {searched}).")
            return
        lines = [f"В проекте {project} нашёл изображения:"]
        lines.extend(f"- {img['path']}" for img in result["images"])
        await reply_long(update.message, "\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Ошибка /images: {e}")


async def files_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /files <project>")
        return
    project = context.args[0]
    try:
        result = list_workspace_project_files(project, depth=3)
        if not result["files"]:
            await update.message.reply_text(f"В проекте {project} файлов не найдено.")
            return
        lines = [f"В проекте {project} нашёл файлы:"]
        lines.extend(f"- {f['path']}" for f in result["files"])
        await reply_long(update.message, "\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Ошибка /files: {e}")


async def tree_project_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /tree_project <project>")
        return
    project = context.args[0]
    try:
        result = tree_workspace_project(project, depth=3)
        await reply_long(update.message, result["tree"])
    except Exception as e:
        await update.message.reply_text(f"Ошибка /tree_project: {e}")


async def set_background_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /set_background <project> <image_name>")
        return
    project, image_name = context.args[0], context.args[1]
    chat_id, user_id = chat_user_ids(update)
    try:
        info = resolve_existing_project_image(project, f"assets/img/{image_name}")
        task_text = (
            f"Используй изображение {info['relative_path']} как фон hero-секции "
            "(background-image, cover, по центру, без потери читаемости текста). Сохрани остальной функционал."
        )
        tracker = ProgressTracker(update.message)
        await tracker.step("🧠 Обновляю CSS/HTML под новое фото...")
        answer, debug = edit_workspace_site_workflow(
            task_text, project, chat_id, expect_background_image=info["relative_path"]
        )
        await tracker.step("✅ Готово.")
        await reply_long(update.message, answer)
        await maybe_send_intent_debug(update.message, context, debug)
    except Exception as e:
        save_last_error(chat_id=chat_id, user_id=user_id, handler="set_background_command", error=e, user_text=update.message.text or "")
        await update.message.reply_text(f"Ошибка /set_background: {e}")


async def last_media_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id, _ = chat_user_ids(update)
    media = get_latest_media_any_status(chat_id)
    if not media:
        await update.message.reply_text("Фото ещё не присылали.")
        return
    lines = [
        f"Статус: {media.get('status') or '-'}",
        f"Проект: {media.get('used_project') or '-'}",
        f"Файл: {media.get('saved_path') or '-'}",
    ]
    if media.get("status") == "failed":
        lines.append(f"Причина: {media.get('failed_reason') or '-'}")
    await update.message.reply_text("\n".join(lines))


async def media_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Same data as /last_media -- explicit name for task_orchestrator's
    media_source decision (pending_media vs existing_project_image) so it's
    easy to check what apply_media_to_site will actually use right now."""
    if not is_allowed(update):
        return
    chat_id, _ = chat_user_ids(update)
    media = get_latest_media_any_status(chat_id)
    available = get_latest_available_media(chat_id)
    lines = [f"pending_media доступна для apply_media_to_site: {'да' if available else 'нет'}"]
    if media:
        lines.append(f"Последнее фото -- статус: {media.get('status') or '-'}")
        lines.append(f"Проект: {media.get('used_project') or '-'}")
        lines.append(f"Файл: {media.get('saved_path') or '-'}")
        if media.get("status") == "failed":
            lines.append(f"Причина: {media.get('failed_reason') or '-'}")
    else:
        lines.append("Фото ещё не присылали.")
    await update.message.reply_text("\n".join(lines))


async def task_debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id, _ = chat_user_ids(update)
    await update.message.reply_text(task_orchestrator.format_last_decision(chat_id))


async def check_component_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if len(context.args) < 2:
        kinds = ", ".join(ui_component_model.COMPONENT_KINDS)
        await update.message.reply_text(f"Использование: /check_component <project> <kind>\nДоступные kind: {kinds}")
        return
    project, kind_arg = context.args[0], context.args[1]
    chat_id, _ = chat_user_ids(update)
    kind = ui_component_model.normalize_kind(kind_arg) or kind_arg
    try:
        answer, debug = await verify_ui_component_workflow(project, kind, chat_id=chat_id)
        await reply_long(update.message, answer)
        await maybe_send_intent_debug(update.message, context, debug)
    except Exception as e:
        await update.message.reply_text(f"Ошибка /check_component: {e}")


async def project_state_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fuller technical dump than /site_state: per-feature status table,
    last_successful_snapshot, last_failed_action, operation history count."""
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /project_state <project>")
        return
    project = context.args[0]
    try:
        state = project_state_manager.load_project_state(project)
        lines = [
            f"project_state {project}:",
            f"last_successful_snapshot: {state.get('last_successful_snapshot') or '-'}",
            f"last_failed_action: {'есть, см. /site_history' if state.get('last_failed_action') else '-'}",
            f"applied_operations_history: {len(state.get('applied_operations_history') or [])} записей",
            "features:",
        ]
        for name, entry in state.get("features", {}).items():
            lines.append(f"- {name}: {entry.get('status')}")
        await reply_long(update.message, "\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Ошибка /project_state: {e}")


async def retry_media_background_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /retry_media_background <project> [hero]")
        return
    project = context.args[0]
    target = "hero_background" if len(context.args) > 1 and "hero" in context.args[1].lower() else "whole_page_background"
    chat_id, _ = chat_user_ids(update)
    media = get_latest_available_media(chat_id)
    if not media:
        await update.message.reply_text("Нет фото для повтора. Пришли фото ещё раз.")
        return
    await add_background_image_workflow(update.message, context, project_name=project, media=media, target=target)


IMAGE_CAPTION_PHRASES = (
    "добавь на фон", "фон сайта", "добавь эту фотку", "добавь это фото", "добавь эту фото",
    "используй как hero", "hero background", "добавь фото на сайт", "поставь как фон",
    "добавь картинку", "вставь фото", "поставь фото",
    "add to background", "use as hero", "as background", "set as background", "add this photo",
    "fondo del sitio", "usa como fondo", "como hero",
)

BACKGROUND_AFTER_PHOTO_PHRASES = (
    "добавь на фон",
    "фото на фон",
    "это фото на фон",
    "помести на фон",
    "используй как background",
    "сделай фоном сайта",
    "поставь на задний фон",
    "поставь как фон",
    "поставь его фоном",
    "поставь это фоном",
    "поставь фото фоном",
    "поставь её фоном",
    "сделай это фото фоном",
    "сделай его фоном",
    "сделай фото фоном",
    "добавь это фото в hero",
    "добавь фото в hero",
    "добавь это фото как фон",
    "поставь его фоном на блок херо",
    "поставь фото фоном на hero",
    "фоном на блок херо",
    "фоном на hero",
    "add it as background",
    "use it as background",
    "as background",
    "set as background",
    "use this photo as hero background",
    "use this photo as background",
    "use it as hero background",
    "usalo como fondo",
    "úsalo como fondo",
    "como fondo",
    "pon esta foto como fondo del hero",
    "pon esta foto como fondo",
)

# Broader fallback: if pending_media exists and the text combines *any* photo
# reference word with *any* background/hero reference word, treat it as a
# background-from-photo request even if the exact phrase isn't in the list
# above. This must win over the generic edit_workspace_site routing.
PHOTO_REFERENCE_WORDS = (
    "фото", "фотк", "фотограф", "снимок", "картинк", "изображен",
    "это", "него", "ее", "её", "его",
    "photo", "picture", "image", "this photo", "this picture", "this image",
    "esta foto", "esa foto", "la foto",
)
BACKGROUND_REFERENCE_WORDS = (
    "фон", "background", "hero", "херо", "fondo",
)


def _wants_background_from_latest_photo(text: str, *, has_pending_media: bool = False) -> bool:
    lowered = (text or "").lower()
    if any(phrase in lowered for phrase in BACKGROUND_AFTER_PHOTO_PHRASES):
        return True
    if not has_pending_media:
        # The broad word-combination heuristic below is only safe to apply
        # when we already know there's a photo waiting -- otherwise generic
        # phrases like "поменяй фон, это плохо смотрится" would misfire.
        return False
    has_photo_ref = any(word in lowered for word in PHOTO_REFERENCE_WORDS)
    has_background_ref = any(word in lowered for word in BACKGROUND_REFERENCE_WORDS)
    return has_photo_ref and has_background_ref


HERO_TARGET_WORDS = (
    "hero", "херо", "блок херо", "hero block", "hero section", "hero-секц", "героическ",
)


def _wants_hero_target(text: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in HERO_TARGET_WORDS)


FIXED_ATTACHMENT_WORDS = (
    "закрепи", "зафиксир", "закреплён", "закреплен", "fixed", "fija", "fijo",
)


def _wants_fixed_attachment(text: str) -> bool:
    lowered = (text or "").lower()
    return any(word in lowered for word in FIXED_ATTACHMENT_WORDS)


SITE_STATUS_QUESTION_PHRASES = (
    "где фон", "а где фон", "почему нет фона", "пропал фон", "фон пропал", "фон не виден", "фон не отображается",
    "что не так с сайтом", "почему сайт сломан", "сайт сломан", "проверь сайт", "проверь сайт на ошибки",
    "работают ли языки", "переключение языков работает", "языки работают", "языки переключаются",
    "where is the background", "background is missing", "is the background working", "check the site",
    "what's wrong with the site", "site is broken", "is the language switcher working",
    "dónde está el fondo", "donde esta el fondo", "revisa el sitio",
)


def _wants_site_status_check(text: str) -> bool:
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in SITE_STATUS_QUESTION_PHRASES)


SITE_FEATURE_CHECK_WORDS = (
    "слайдер", "slider", "slide", "карусел", "carousel",
    "рецепт", "recipe", "печень", "cookie",
    "кнопк", "button", "язык", "language", "idioma",
    "фон", "background", "hero", "херо",
    "футер", "footer", "шапк", "header", "погод", "weather",
)

SITE_CHECK_ACTION_WORDS = (
    "проверь", "проверить", "есть ли", "найди", "посмотри", "покажи",
    "работает", "работают", "check", "verify", "find", "show", "revisa", "comprueba",
)


def _wants_workspace_site_check(text: str) -> bool:
    """Free-text request to inspect a workspace website.

    This deliberately catches requests such as:
    "проверь, есть ли слайдер с рецептами на сайте kuki"
    before semantic/git routing. It is not a git/repo question.
    """
    lowered = (text or "").lower()
    if _wants_site_status_check(lowered):
        return True
    if "код" in lowered and not any(w in lowered for w in ("сайт", "страниц", "preview", "превью")):
        return False
    has_action = any(word in lowered for word in SITE_CHECK_ACTION_WORDS)
    has_site_hint = any(word in lowered for word in ("сайт", "страниц", "preview", "превью", "website", "site", "sitio"))
    has_feature = any(word in lowered for word in SITE_FEATURE_CHECK_WORDS)
    return has_action and (has_site_hint or has_feature)


def _workspace_check_feature_report(user_text: str, files: list[dict], browser_result: dict | None = None) -> list[str]:
    lowered = (user_text or "").lower()
    by_path = {str(f.get("path") or ""): str(f.get("content") or "") for f in files}
    html = "\n".join(content for path, content in by_path.items() if path.lower().endswith(".html"))
    css = "\n".join(content for path, content in by_path.items() if path.lower().endswith(".css"))
    js = "\n".join(content for path, content in by_path.items() if path.lower().endswith(".js"))
    all_text = (html + "\n" + css + "\n" + js).lower()
    html_lower = html.lower()
    css_lower = css.lower()
    js_lower = js.lower()
    lines: list[str] = []

    if any(word in lowered for word in ("слайдер", "slider", "карусел", "carousel", "slide")):
        has_slider_markup = any(marker in html_lower for marker in ("slider", "slide", "carousel", "data-slide"))
        has_slider_js = any(marker in js_lower for marker in ("nextslide", "prevslide", "currentslide", "slideindex", "queryselectorall('.slide", "queryselectorall(\".slide"))
        recipe_required = any(word in lowered for word in ("рецепт", "recipe", "печень", "cookie"))
        has_recipe_text = any(word in all_text for word in ("рецепт", "recipe", "печень", "cookie", "ингреди", "ingredients", "мука", "сахар"))
        ok = has_slider_markup and (has_slider_js or "button" in html_lower) and (has_recipe_text if recipe_required else True)
        if ok:
            detail = "слайдер найден" + ("; тексты рецептов тоже найдены" if recipe_required else "")
        else:
            missing = []
            if not has_slider_markup:
                missing.append("нет slider/slide-разметки")
            if not (has_slider_js or "button" in html_lower):
                missing.append("нет JS/кнопок навигации")
            if recipe_required and not has_recipe_text:
                missing.append("нет текста рецептов")
            detail = "слайдер не подтверждён: " + ", ".join(missing)
        lines.append(("✅ " if ok else "❌ ") + detail)

    if any(word in lowered for word in ("язык", "language", "idioma", "кнопк", "button")):
        found_langs = [code.upper() for code in ("ru", "en", "es") if f'data-lang="{code}"' in all_text or f"data-lang='{code}'" in all_text or f">{code}<" in all_text]
        has_handler = "addEventListener".lower() in js_lower or "onclick" in html_lower
        ok = set(found_langs) >= {"RU", "EN", "ES"} and has_handler
        lines.append(("✅ " if ok else "❌ ") + f"языки: найдены кнопки {', '.join(found_langs) or '-'}; обработчик: {'есть' if has_handler else 'не найден'}")
        if browser_result and browser_result.get("language_switch_ok") is not None:
            lines.append(f"Браузерная проверка переключения языков: {browser_result.get('language_switch_ok')}")

    if any(word in lowered for word in ("фон", "background", "hero", "херо")):
        has_bg = "background-image" in css_lower or "url(" in css_lower
        bg_target = "hero" if "hero" in css_lower or "херо" in lowered else "body/общий"
        lines.append(("✅ " if has_bg else "❌ ") + f"фон: {'найден' if has_bg else 'не найден'}; цель: {bg_target}")
        if browser_result and browser_result.get("background_image_loaded") is not None:
            lines.append(f"Браузер подтвердил фон: {browser_result.get('background_image_loaded')}")

    if any(word in lowered for word in ("футер", "footer")):
        ok = "<footer" in html_lower or "footer" in html_lower
        lines.append(("✅ " if ok else "❌ ") + f"footer: {'найден' if ok else 'не найден'}")

    if any(word in lowered for word in ("шапк", "header")):
        ok = "<header" in html_lower or "header" in html_lower
        lines.append(("✅ " if ok else "❌ ") + f"header: {'найден' if ok else 'не найден'}")

    if any(word in lowered for word in ("погод", "weather")):
        ok = "open-meteo" in all_text or "weather" in all_text or "погод" in all_text
        lines.append(("✅ " if ok else "❌ ") + f"погода: {'найдена' if ok else 'не найдена'}")

    if not lines:
        lines.append("Проверил HTML/CSS/JS сайта. Уточни, какую функцию искать: слайдер, языки, фон, footer, weather.")
    return lines


async def verify_ui_component_workflow(project_name: str, kind: str, chat_id: str | None = None) -> tuple[str, dict]:
    """Universal, read-only UI component check (ui_component_model.py /
    ui_component_verifier.py): works the same way for slider/carousel/
    accordion/tabs/hamburger_menu/gallery/language_switcher/form/
    weather_block/footer/hero/background, regardless of the project's
    server-side stack -- site_technology_detector is purely informational
    here, the verifier only ever inspects the rendered page. Never writes
    files, never says "Готово" (it's a check, not an edit)."""
    tools_called: list[str] = []
    if not config.env_bool("WRITE_MODE_ENABLED", config.WRITE_MODE_ENABLED):
        msg = "Write mode выключен. Включи WRITE_MODE_ENABLED=true в .env"
        return msg, {"detected": {"intent": "verify_ui_component", "project": project_name}, "tools_called": tools_called, "errors": [msg]}
    try:
        model = build_component_model(kind)
        tech = detect_technology(project_name)
        tools_called.append("detect_technology")

        status = preview_status(project_name)
        tools_called.append("preview_status")
        if not status.get("running"):
            start_preview(project_name)
            tools_called.append("start_preview")
            status = preview_status(project_name)
            tools_called.append("preview_status")

        preview_url = ""
        if status.get("running") and status.get("port") and playwright_available():
            preview_url = preview_url_for_port(int(status["port"]))
            url = f"http://127.0.0.1:{status['port']}/"
            results = await verify_components_async(project_name, url, [model])
            tools_called.append("verify_components_async")
            result = results[model.kind]
        else:
            files = read_workspace_project_files(project_name)["files"]
            tools_called.append("read_workspace_project_files")
            result = verify_component_static(files, model)
            tools_called.append("verify_component_static")

        project_state_manager.update_feature_from_verification(
            project_name, model.kind, result, selectors=model.selectors, related_files=model.related_files
        )
        tools_called.append("update_feature_from_verification")

        if chat_id:
            memory.set_current_project(chat_id, project_name)

        lines = [f"Проверка компонента «{model.kind}» на сайте {project_name}:", format_verification_human(result)]
        lines.append(f"Технология: {tech['technology']}")
        if result.get("items_expected"):
            lines.append(f"Элементов: {result['items_found']} (ожидалось минимум {result['items_expected']})")
        else:
            lines.append(f"Элементов: {result['items_found']}")
        if preview_url:
            lines.append(f"Открыть сайт: {preview_url}")
        answer = "\n".join(lines)

        return answer, {
            "detected": {"intent": "verify_ui_component", "project": project_name, "kind": model.kind},
            "tools_called": tools_called,
            "errors": [] if result["status"] != "missing" else [result["detail"]],
            "result": result,
        }
    except Exception as e:
        save_last_error(chat_id=chat_id or "", user_id="", handler="verify_ui_component_workflow", error=e, user_text=kind)
        return (
            f"Не смог проверить компонент {kind} на сайте {project_name}: {e}",
            {"detected": {"intent": "verify_ui_component", "project": project_name}, "tools_called": tools_called, "errors": [str(e)]},
        )


async def workspace_site_feature_check_workflow(project_name: str, user_text: str, chat_id: str | None = None) -> tuple[str, dict]:
    """Read-only site/feature check for workspace projects.

    Does not touch git/repo tools. Starts preview if needed, reads real files,
    optionally runs browser check, and returns a human answer.
    """
    tools_called: list[str] = []
    try:
        if not _workspace_project_exists(project_name):
            projects = ", ".join(item["name"] for item in list_workspace().get("projects", [])) or "-"
            msg = f"Workspace-проект {project_name} не найден. Доступные проекты: {projects}"
            return msg, {"detected": {"intent": "workspace_site_check", "project": project_name}, "tools_called": tools_called, "errors": [msg]}

        read_result = read_workspace_project_files(project_name)
        tools_called.append("read_workspace_project_files")
        files = read_result.get("files") or []

        status = preview_status(project_name)
        tools_called.append("preview_status")
        if not status.get("running"):
            start_preview(project_name)
            tools_called.append("start_preview")
            status = preview_status(project_name)
            tools_called.append("preview_status")

        preview_url = ""
        curl_result = None
        browser_result = None
        if status.get("running") and status.get("port"):
            port = int(status["port"])
            preview_url = preview_url_for_port(port)
            curl_result = curl_check(port)
            tools_called.append("curl_check")
            if playwright_available():
                browser_result = await check_site_with_playwright_async(project_name, f"http://127.0.0.1:{port}/")
                tools_called.append("check_site_with_playwright_async")
                save_last_verification(project_name, browser_result)

        feature_lines = _workspace_check_feature_report(user_text, files, browser_result=browser_result)
        lines = [f"Проверил сайт {project_name}."]
        lines.extend(feature_lines)
        if curl_result:
            lines.append(f"HTTP-проверка: {curl_result.get('status') or curl_result.get('status_code') or ('OK' if curl_result.get('success') else 'ошибка')}")
        if preview_url:
            lines.append(f"Открыть сайт: {preview_url}")
        if chat_id:
            memory.set_current_project(chat_id, project_name)
        return "\n".join(lines), {
            "detected": {"intent": "workspace_site_check", "project": project_name},
            "tools_called": tools_called,
            "errors": [],
            "project": project_name,
            "preview_url": preview_url,
            "curl_check": curl_result,
            "browser_check": browser_result,
        }
    except Exception as e:
        save_last_error(chat_id=chat_id or "", user_id="", handler="workspace_site_feature_check_workflow", error=e, user_text=user_text)
        return (
            f"Не смог проверить сайт {project_name}: {e}",
            {"detected": {"intent": "workspace_site_check", "project": project_name}, "tools_called": tools_called, "errors": [str(e)]},
        )


async def _check_background_live(project_name: str, base_url: str, relative_image: str) -> dict[str, Any]:
    """Read-only liveness check: confirms image/CSS are reachable over HTTP and,
    if Playwright is available, that the background-image is actually applied
    on the rendered page. Never writes anything."""
    image_url = f"{base_url}/{relative_image}"
    css_url = f"{base_url}/assets/css/style.css"
    img_resp = requests.get(image_url, timeout=5)
    css_resp = requests.get(css_url, timeout=5)
    result = {
        "image_status": img_resp.status_code,
        "css_status": css_resp.status_code,
        "background_image_loaded": None,
    }
    if img_resp.status_code != 200 or css_resp.status_code != 200:
        result["ok"] = False
        return result
    if playwright_available():
        browser = await check_site_with_playwright_async(
            project_name, f"{base_url}/", expect_background_image=relative_image
        )
        result["background_image_loaded"] = browser.get("background_image_loaded")
        result["ok"] = browser.get("background_image_loaded") is not False
    else:
        result["ok"] = True
    return result


def _record_media_operation(
    project_name: str,
    *,
    chat_id: str | None,
    user_text: str,
    operations: list[dict],
    files_changed: list[str],
    success: bool,
    snapshot_id: str | None = None,
    rollback_used: bool = False,
    error: str = "",
) -> None:
    """Every apply_media_to_site outcome writes project_state + learning_log,
    same as edit_workspace_site_workflow -- task/source/target/changed_files/
    checks/success all need to be real and queryable via /task_debug,
    /project_state, /media_status, not just a chat reply."""
    checks = {"success": success, "failed": [] if success else [error or "не подтверждено"]}
    try:
        project_state_manager.record_applied_operation(
            project_name,
            user_text=user_text,
            operations=operations,
            files_changed=files_changed,
            checks=checks,
            success=success,
            snapshot_id=snapshot_id,
        )
    except ToolError:
        pass
    try:
        learning_log.record(
            project_name=project_name,
            chat_id=chat_id,
            user_text=user_text,
            detected_intent="apply_media_to_site",
            before_state=None,
            operation_plan=operations,
            files_changed=files_changed,
            checks=checks,
            success=success,
            rollback_used=rollback_used,
        )
    except ToolError:
        pass


async def add_background_image_workflow(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    project_name: str,
    media: dict[str, Any],
    target: str = "whole_page_background",
    fixed: bool = False,
) -> None:
    """Downloads/converts the pending photo and applies it as a CSS background.
    target == "hero_background" scopes the CSS to the page's hero element
    (tools_media.set_hero_background); otherwise it's a whole-page body
    background (tools_media.set_fixed_background). Never calls Ollama/
    edit_workspace_site -- this is a deterministic, targeted CSS-only patch."""
    chat_id = str(message.chat.id) if getattr(message, "chat", None) else ""
    user_id = str(message.from_user.id) if getattr(message, "from_user", None) else ""
    tracker = ProgressTracker(message)
    intent = "add_background_image"
    save_current_task(chat_id, intent, project_name, "starting")
    step = 0
    total = 6
    hero = target == "hero_background"
    try:
        intro = (
            f"🖼 Пробую применить последнее фото как фон hero-блока сайта {project_name}..."
            if hero
            else f"🖼 Пробую применить последнее фото как фон сайта {project_name}..."
        )
        await tracker.step(intro)

        step = 1
        update_current_task_step(message.chat_id if hasattr(message, "chat_id") else "", "download_telegram")
        await tracker.step(f"Шаг {step}/{total}: скачиваю фото из Telegram...")
        original_name = (media.get("file_unique_id") or "photo") + ".jpg"
        saved = await save_image_to_project(
            context.bot,
            project_name,
            media["telegram_file_id"],
            original_name=original_name,
            mime_type=media.get("mime_type") or "",
        )

        step = 2
        update_current_task_step(message.chat_id if hasattr(message, "chat_id") else "", "save_project")
        await tracker.step(f"Шаг {step}/{total}: сохраняю в папку сайта...")

        step = 3
        update_current_task_step(chat_id, "convert_webp")
        await tracker.step(f"Шаг {step}/{total}: сжимаю и конвертирую в WebP...")
        if not pillow_available():
            raise ToolError("Pillow не установлен, конвертация в WebP недоступна")
        src_path = Path(saved["path"])
        out_name = f"background-{int(datetime.now().timestamp())}.webp"
        out_abs = resolve_write_path(str(Path(project_name) / "assets" / "img" / out_name)).resolve()
        optimize_image_to_webp(str(src_path), str(out_abs), max_width=1920, quality=82)
        if not out_abs.is_file() or out_abs.stat().st_size <= 0:
            raise ToolError("WebP файл не создался или пустой")
        try:
            if src_path.is_file():
                src_path.unlink()
        except Exception:
            pass
        relative_image = f"assets/img/{out_name}"

        step = 4
        update_current_task_step(message.chat_id if hasattr(message, "chat_id") else "", "update_css")
        await tracker.step(f"Шаг {step}/{total}: обновляю CSS{' для hero-блока' if hero else ''}...")
        if hero:
            set_hero_background(project_name, relative_image, fixed=fixed)
        else:
            set_fixed_background(project_name, relative_image)

        step = 5
        update_current_task_step(message.chat_id if hasattr(message, "chat_id") else "", "verify_assets")
        await tracker.step(f"Шаг {step}/{total}: проверяю, что файл доступен с сайта...")
        verify = verify_background_asset(project_name, relative_image)
        if not verify.get("success"):
            raise ToolError(
                "Файл или ссылка в CSS не подтверждены "
                f"(файл найден: {verify.get('image_exists')}, "
                f"CSS найден: {verify.get('css_exists')}, "
                f"CSS ссылается на файл: {verify.get('css_references_image')})"
            )
        status = preview_status(project_name)
        if not status.get("running"):
            status = start_preview(project_name)
        port = int(status.get("port") or 0)
        if not port:
            raise ToolError("Не удалось определить порт preview")
        base_url = f"http://127.0.0.1:{port}"

        step = 6
        update_current_task_step(message.chat_id if hasattr(message, "chat_id") else "", "browser_check")
        await tracker.step(f"Шаг {step}/{total}: проверяю страницу в браузере...")
        live = await _check_background_live(project_name, base_url, relative_image)
        repaired = False
        if not live["ok"]:
            await tracker.step("⚠️ Фон не подтвердился, пробую исправить CSS-путь...")
            if hero:
                set_hero_background(project_name, relative_image, fixed=fixed)
            else:
                set_fixed_background(project_name, relative_image)
            live = await _check_background_live(project_name, base_url, relative_image)
            repaired = True
        if not live["ok"]:
            raise ToolError(
                "Не удалось подтвердить фон даже после автоисправления CSS-пути "
                f"(картинка HTTP {live['image_status']}, CSS HTTP {live['css_status']}, "
                f"фон виден в браузере: {live['background_image_loaded']})"
            )

        mark_media_used(int(media["id"]), project_name, str(out_abs))
        _record_media_operation(
            project_name,
            chat_id=chat_id,
            user_text=str(getattr(message, "text", "") or getattr(message, "caption", "") or ""),
            operations=[{"op": "set_background", "source": "pending_media", "target": target, "image": relative_image}],
            files_changed=["assets/css/style.css", relative_image],
            success=True,
        )

        if live["background_image_loaded"]:
            check_note = "Проверка: изображение отдаётся сайтом HTTP 200. Браузер подтвердил фон."
        else:
            check_note = (
                "Проверка: изображение отдаётся сайтом HTTP 200. "
                "Браузерная проверка недоступна, но файл изображения и CSS проверены."
            )
        if repaired:
            check_note += " Потребовалось автоисправление CSS-пути."

        await tracker.step("✅ Готово.")
        headline = (
            "Готово. Фото применено как фон hero-блока сайта."
            if hero
            else "Готово. Фото применено как фон сайта."
        )
        await message.reply_text(
            "\n".join(
                [
                    headline,
                    f"Проект: {project_name}",
                    f"Фото сохранено: {relative_image}",
                    "CSS обновлён: assets/css/style.css",
                    check_note,
                    f"Открыть сайт: {preview_url_for_port(port)}",
                ]
            )
        )
    except Exception as e:
        downloaded_ok = "out_abs" in locals()
        try:
            saved_hint = str(locals().get("out_abs", "") or "")
            mark_media_failed(int(media["id"]), project_name, saved_hint, reason=str(e))
        except Exception:
            pass
        save_last_error(chat_id=chat_id, user_id=user_id, handler="add_background_image_workflow", error=e, user_text=str(getattr(message, "text", "") or getattr(message, "caption", "") or ""))
        _record_media_operation(
            project_name,
            chat_id=chat_id,
            user_text=str(getattr(message, "text", "") or getattr(message, "caption", "") or ""),
            operations=[{"op": "set_background", "source": "pending_media", "target": target}],
            files_changed=[],
            success=False,
            error=str(e),
        )
        if downloaded_ok:
            await message.reply_text(f"Фото скачано, но не удалось применить фон: {e}")
        else:
            await message.reply_text(f"Фото не было скачано, поэтому CSS не трогал. ({e})")
    finally:
        clear_current_task(chat_id)


async def apply_existing_image_background_workflow(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    project_name: str,
    target: str = "whole_page_background",
) -> None:
    """Applies an image ALREADY saved in the project's assets/img as the
    background -- never downloads from Telegram, never calls Ollama. This is
    task_orchestrator's media_source="existing_project_image" branch: used
    when the user explicitly asks for "any image from the folder", or there's
    no pending photo to fall back on (see tools_site_operations.op_set_background,
    which always picks a real, already-existing file -- never invents one)."""
    chat_id = str(message.chat.id) if getattr(message, "chat", None) else ""
    user_id = str(message.from_user.id) if getattr(message, "from_user", None) else ""
    tracker = ProgressTracker(message)
    user_text = str(getattr(message, "text", "") or getattr(message, "caption", "") or "")
    hero = target == "hero_background"
    save_current_task(chat_id, "apply_existing_image_background", project_name, "starting")
    snapshot: dict | None = None
    try:
        await tracker.step(f"🖼 Ищу подходящее изображение в assets/img проекта {project_name}...")
        update_current_task_step(chat_id, "snapshotting")
        snapshot = snapshot_project(project_name, reason="apply_existing_image_background")

        update_current_task_step(chat_id, "applying_operation")
        result = op_set_background(project_name, {"target": "hero" if hero else "whole_page"})
        relative_image = result["image_path"]

        update_current_task_step(chat_id, "verify_assets")
        status = preview_status(project_name)
        if not status.get("running"):
            status = start_preview(project_name)
        port = int(status.get("port") or 0)
        if not port:
            raise ToolError("Не удалось определить порт preview")
        base_url = f"http://127.0.0.1:{port}"
        live = await _check_background_live(project_name, base_url, relative_image)
        if not live["ok"]:
            raise ToolError(
                "Не удалось подтвердить фон "
                f"(картинка HTTP {live['image_status']}, CSS HTTP {live['css_status']}, "
                f"фон виден в браузере: {live['background_image_loaded']})"
            )

        save_site_state(project_name, {"background_required": True})
        _record_media_operation(
            project_name,
            chat_id=chat_id,
            user_text=user_text,
            operations=[{"op": "set_background", "source": "existing_project_image", "target": target, "image": relative_image}],
            files_changed=result["files_changed"],
            success=True,
            snapshot_id=snapshot["snapshot_id"],
        )

        await tracker.step("✅ Готово.")
        headline = "Готово. Изображение из папки проекта применено как фон" + (
            " hero-блока." if hero else " сайта."
        )
        await message.reply_text(
            "\n".join(
                [
                    headline,
                    f"Проект: {project_name}",
                    f"Изображение: {relative_image}",
                    f"Открыть сайт: {preview_url_for_port(port)}",
                ]
            )
        )
    except Exception as e:
        save_last_error(chat_id=chat_id, user_id=user_id, handler="apply_existing_image_background_workflow", error=e, user_text=user_text)
        rolled_back = False
        if snapshot:
            try:
                rollback_project(project_name, snapshot["snapshot_id"])
                rolled_back = True
            except ToolError:
                pass
        _record_media_operation(
            project_name,
            chat_id=chat_id,
            user_text=user_text,
            operations=[{"op": "set_background", "source": "existing_project_image", "target": target}],
            files_changed=[],
            success=False,
            snapshot_id=snapshot["snapshot_id"] if snapshot else None,
            rollback_used=rolled_back,
            error=str(e),
        )
        suffix = " Откатил изменения." if rolled_back else ""
        await message.reply_text(f"Не завершено: не удалось применить изображение из папки как фон сайта {project_name}: {e}.{suffix}")
    finally:
        clear_current_task(chat_id)


def _wants_image_on_site(caption: str) -> bool:
    if not caption:
        return False
    lowered = caption.lower()
    return any(phrase in lowered for phrase in IMAGE_CAPTION_PHRASES)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    message = update.message
    caption = message.caption or ""
    chat_id, user_id = chat_user_ids(update)
    try:
        clear_old_pending_media()
        if message.photo:
            tg_photo = message.photo[-1]
            original_name = f"{tg_photo.file_unique_id}.jpg"
            mime_type = "image/jpeg"
            file_id = tg_photo.file_id
            unique_id = tg_photo.file_unique_id
            size_bytes = tg_photo.file_size or 0
        elif message.document and (message.document.mime_type or "").startswith("image/"):
            original_name = message.document.file_name
            mime_type = message.document.mime_type
            file_id = message.document.file_id
            unique_id = message.document.file_unique_id
            size_bytes = message.document.file_size or 0
        else:
            await message.reply_text("Это не похоже на изображение.")
            return

        media = save_pending_media(
            chat_id,
            user_id,
            file_id,
            file_unique_id=unique_id,
            mime_type=mime_type,
            size_bytes=size_bytes,
            caption=caption,
        )

        await message.reply_text(
            "Фото получил. Могу добавить его на сайт.\n"
            'Напиши, например: "добавь это фото на фон сайта hola".'
        )

        if _wants_image_on_site(caption) or _wants_background_from_latest_photo(caption, has_pending_media=True):
            project = _workspace_project_from_context(caption, chat_id) or memory.get_current_project(chat_id)
            if not project:
                await message.reply_text("На какой сайт добавить фото? Напиши имя проекта, например: hola")
                return
            target = "hero_background" if _wants_hero_target(caption) else "whole_page_background"
            fixed = _wants_fixed_attachment(caption)
            await add_background_image_workflow(
                message, context, project_name=project, media=media, target=target, fixed=fixed
            )
    except Exception as e:
        save_last_error(chat_id=chat_id, user_id=user_id, handler="handle_photo", error=e, user_text=caption)
        await message.reply_text(f"Ошибка обработки фото: {e}")


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
        want_preview = bool(network_sockets_available())
        answer, debug = create_site_workflow(
            "selftest workspace static site",
            project_name=project,
            chat_id="selftest",
            start_preview_requested=want_preview,
            site_spec_provider=fixture_site_spec,
        )
        if debug.get("errors"):
            raise RuntimeError("; ".join(debug["errors"]))
        status = preview_status(project) if want_preview else {"running": False}
        stopped = stop_preview(project) if want_preview else {"stopped": True}
        cleanup = delete_workspace_dir(project, confirm_token=f"DELETE:{project}")
        return {
            "success": True,
            "write_root": str(config.get_write_root()),
            "project": project,
            "created_files": get_last_action("selftest").get("created_files", []),
            "preview_url": status.get("url", ""),
            "stopped": stopped.get("stopped"),
            "cleanup": cleanup.get("deleted"),
            "tools_called": debug.get("tools_called", []),
            "preview_skipped": not want_preview,
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


def selftest_stop_delete_result() -> dict:
    if not config.env_bool("WRITE_MODE_ENABLED", config.WRITE_MODE_ENABLED):
        return {
            "success": False,
            "error": "Write mode выключен. Включи WRITE_MODE_ENABLED=true в .env",
            "write_root": str(config.get_write_root()),
        }
    project = "__jarvis_stop_delete_test__"
    checks: dict[str, Any] = {}
    try:
        if Path(config.get_write_root() / project).exists():
            try:
                delete_workspace_dir(project, confirm_token=f"DELETE:{project}")
            except ToolError:
                pass

        create_result = create_static_site(project, title="Jarvis Stop Delete Selftest")
        if not create_result.get("success"):
            raise RuntimeError("create_static_site не вернул success=True")
        checks["created"] = True

        preview = start_preview(project)
        if not preview.get("success"):
            raise RuntimeError("start_preview не вернул success=True")
        status_after_start = preview_status(project)
        checks["preview_running_after_start"] = bool(status_after_start.get("running"))
        response = requests.get(f"http://127.0.0.1:{preview['port']}/", timeout=5)
        checks["curl_200_after_start"] = response.status_code == 200

        stop_result = stop_preview(project)
        checks["stop_success"] = bool(stop_result.get("success"))
        checks["stop_checks"] = stop_result.get("checks")
        port_still_listening = port_is_listening(int(preview["port"]))
        checks["port_listening_after_stop"] = port_still_listening
        try:
            requests.get(f"http://127.0.0.1:{preview['port']}/", timeout=2)
            checks["curl_fails_after_stop"] = False
        except requests.exceptions.RequestException:
            checks["curl_fails_after_stop"] = True

        delete_result = delete_workspace_dir(project, confirm_token=f"DELETE:{project}")
        checks["delete_success"] = bool(delete_result.get("success"))
        checks["folder_gone"] = not Path(config.get_write_root() / project).exists()

        overall = (
            checks.get("preview_running_after_start")
            and checks.get("curl_200_after_start")
            and checks.get("stop_success")
            and not checks.get("port_listening_after_stop")
            and checks.get("curl_fails_after_stop")
            and checks.get("delete_success")
            and checks.get("folder_gone")
        )
        return {"success": bool(overall), "project": project, "checks": checks}
    except Exception as e:
        try:
            stop_preview(project)
        except Exception:
            pass
        try:
            delete_workspace_dir(project, confirm_token=f"DELETE:{project}")
        except Exception:
            pass
        return {"success": False, "project": project, "checks": checks, "error": str(e)}


async def selftest_stop_delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await progress(update.message, "Принял: запускаю selftest_stop_delete (временный проект, реальные sitebota/test-site не трогаю)")
    try:
        result = selftest_stop_delete_result()
        lines = [f"success: {result.get('success')}", f"project: {result.get('project')}"]
        for key, value in (result.get("checks") or {}).items():
            lines.append(f"- {key}: {value}")
        if result.get("error"):
            lines.append(f"error: {result['error']}")
        await reply_long(update.message, "\n".join(lines))
    except Exception as e:
        chat_id, user_id = chat_user_ids(update)
        save_last_error(chat_id=chat_id, user_id=user_id, handler="selftest_stop_delete_command", error=e, user_text=update.message.text or "")
        await update.message.reply_text(f"Ошибка /selftest_stop_delete: {e}. Детали сохранены в /last_error")


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
                "/workspace - алиас /workspace_status",
                "/workspace_status - полная инвентаризация WRITE_ROOT: файлы, preview, порты, curl",
                "/ports - registered/listening/suspicious preview-порты",
                "/write_mode - состояние write sandbox",
                "/new_static <name> - создать статический сайт в WRITE_ROOT",
                "/create_site <project> - создать статический сайт и проверить файлы",
                "/create_and_preview <project> - создать сайт, проверить файлы и запустить preview",
                "/where <project> - показать путь, файлы и preview status проекта",
                "/edit_site <project> <задача> - отредактировать существующий сайт через Ollama (snapshot + rollback при провале проверки)",
                "/site_state <project> - сохранённые требования к сайту и что реально есть в файлах",
                "/site_snapshots <project> - список снапшотов до правок (для отката)",
                "/site_rollback <project> <snapshot_id> - откатить файлы проекта к снапшоту",
                "/site_check <project> - read-only проверка сайта по сохранённым требованиям (без правок)",
                "/repair_site <project> - найти проблемы через /site_check и попробовать их исправить",
                "/site_history <project> - история применённых structured operations (success/rollback)",
                "/site_last_success <project> - последний успешно принятый snapshot",
                "/site_diff <project> - diff текущих файлов относительно последнего успешного snapshot",
                "/site_requirements <project> - алиас /site_state (сохранённые требования + что реально есть)",
                "/current_task - текущая выполняемая задача",
                "/progress - прогресс текущей задачи или последнее действие",
                "/browser_check <project> - проверить запущенный preview через Playwright",
                "/images <project> - список изображений проекта (assets/img, assets/images, static/img, public/img)",
                "/files <project> - список файлов проекта (alias: /ls)",
                "/tree_project <project> - дерево файлов проекта",
                "/set_background <project> <image_name> - поставить изображение фоном hero-секции",
                "/last_media - статус последнего присланного фото",
                "/media_status - доступна ли pending_media для apply_media_to_site прямо сейчас",
                "/task_debug - последнее решение task_orchestrator для этого чата (task_type/entity/reason)",
                "/check_component <project> <kind> - универсальная проверка UI-компонента (slider/accordion/tabs/...)",
                "/project_state <project> - технический dump project_state: features, snapshots, history",
                "/retry_media_background <project> [hero] - повторить применение последнего фото без повторной отправки",
                "/new_flask <name> - создать Flask-проект в WRITE_ROOT",
                "/write_file <project>/<file> <content> - записать текстовый файл",
                "/delete_file <project>/<file> - удалить файл из WRITE_ROOT",
                "/preview_start <project> - запустить preview",
                "/preview_stop <project> - остановить preview с проверкой (pid/port/curl)",
                "/preview_stop_port <port> - остановить preview процесс по порту",
                "/preview_stop_all - остановить все зарегистрированные preview с таблицей результатов",
                "/workspace_delete <project> - удалить проект из WRITE_ROOT с проверкой",
                "/workspace_clean_stopped - убрать из реестра записи о мертвых preview",
                "/selftest_stop_delete - selftest на временном проекте, не трогает реальные сайты",
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
                "/router_test <text> - semantic router classify_intent без выполнения tools",
                "/workflow_debug_on, /workflow_debug_off - debug деталей planner/workflow",
                "/selfdev_on, /selfdev_off - режим self-improvement (suggest/off)",
                "/selfdev_propose <задача> - предложить новый plugin для незнакомой задачи",
                "/selfdev_run <job_id> <запрос> - dry-run: can_handle score без изменений",
                "/selfdev_test <job_id> - прогнать safety-проверки предложенного plugin",
                "/selfdev_install <job_id> - установить plugin после успешных проверок",
                "/selfdev_rollback <job_id> - откатить установленный plugin",
                "/selfdev_status [job_id] - статус self-improvement задачи",
                "/plugins - список установленных plugins",
                "/plugin_show <name> - детали plugin",
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




async def current_project_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id, _ = chat_user_ids(update)
    current = memory.get_current_project(chat_id)
    if current and _workspace_project_exists(current):
        await update.message.reply_text(f"Текущий workspace-проект: {current}")
        return
    projects = ", ".join(item["name"] for item in list_workspace().get("projects", [])) or "-"
    await update.message.reply_text(f"Текущий workspace-проект не выбран. Доступные: {projects}")


async def use_project_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /use_project <workspace_project>")
        return
    project = context.args[0]
    if not _workspace_project_exists(project):
        projects = ", ".join(item["name"] for item in list_workspace().get("projects", [])) or "-"
        await update.message.reply_text(f"Workspace-проект {project} не найден. Доступные: {projects}")
        return
    chat_id, _ = chat_user_ids(update)
    memory.set_current_project(chat_id, project)
    await update.message.reply_text(f"Текущий workspace-проект: {project}")


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


def _effective_selfdev_mode(context: ContextTypes.DEFAULT_TYPE) -> str:
    override = context.user_data.get("selfdev_mode_override")
    return override if override in config.SELFDEV_MODES else config.get_selfdev_mode()


async def workflow_debug_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    context.user_data["workflow_debug"] = True
    await update.message.reply_text("Workflow debug: on")


async def workflow_debug_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    context.user_data["workflow_debug"] = False
    await update.message.reply_text("Workflow debug: off")


async def selfdev_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    context.user_data["selfdev_mode_override"] = "suggest"
    await update.message.reply_text(
        "Self-improvement: suggest включён. Если я не найду подходящий workflow/plugin для задачи, "
        "предложу план нового навыка вместо обычного ответа. Установка только через /selfdev_install <job_id>."
    )


async def selfdev_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    context.user_data["selfdev_mode_override"] = "off"
    await update.message.reply_text("Self-improvement выключен.")


async def selfdev_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    mode = _effective_selfdev_mode(context)
    job_id = context.args[0] if context.args else context.user_data.get("selfdev_last_job_id")
    if not job_id:
        await update.message.reply_text(f"SELFDEV_MODE: {mode}\nНет текущей selfdev задачи.")
        return
    report = self_improvement.get_job_report(job_id)
    if not report:
        await update.message.reply_text(f"SELFDEV_MODE: {mode}\nJob {job_id} не найден.")
        return
    lines = [
        f"SELFDEV_MODE: {mode}",
        f"job_id: {job_id}",
        f"plugin: {report.get('plugin_name')}",
        f"status: {report.get('status')}",
        f"step: {report.get('step')}",
        "files: " + (", ".join(report.get("files") or []) or "-"),
    ]
    if report.get("checks"):
        lines.append("checks: " + ", ".join(f"{k}={'OK' if v else 'FAIL'}" for k, v in report["checks"].items()))
    if report.get("errors"):
        lines.append("errors: " + "; ".join(str(e) for e in report["errors"]))
    await reply_long(update.message, "\n".join(lines))


async def selfdev_propose_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /selfdev_propose <текст задачи>")
        return
    mode = _effective_selfdev_mode(context)
    if mode == "off":
        await update.message.reply_text("Self-improvement выключен. Включи: /selfdev_on")
        return
    user_text = " ".join(context.args)
    chat_id, user_id = chat_user_ids(update)
    await update.message.reply_text("🧠 Думаю над планом нового навыка...")
    try:
        plugin_context = {"project_name": memory.get_current_project(chat_id), "chat_id": chat_id}
        spec = self_improvement.propose_plugin(user_text, plugin_context)
        job_id = self_improvement.new_job_id()
        self_improvement.write_plugin_to_sandbox(job_id, spec)
        context.user_data["selfdev_last_job_id"] = job_id
        await reply_long(update.message, _format_selfdev_proposal(spec, job_id))
    except Exception as e:
        save_last_error(chat_id=chat_id, user_id=user_id, handler="selfdev_propose_command", error=e, user_text=user_text)
        await update.message.reply_text(f"Не смог подготовить план: {e}")


def _format_selfdev_proposal(spec: dict, job_id: str) -> str:
    lines = [
        "Я пока не умею это делать сам. Подготовил план нового навыка:",
        f"Название: {spec['plugin_name']}",
        f"Что будет уметь: {spec['description']}",
        "Файлы:",
    ]
    lines.extend(f"- {f['path']}" for f in spec["files"])
    if spec.get("risks"):
        lines.append("Риски: " + "; ".join(spec["risks"]))
    lines.append(f"job_id: {job_id}")
    lines.append(f"Сначала проверь: /selfdev_test {job_id}")
    lines.append(f"Напиши /selfdev_install {job_id}, если разрешаешь подключить.")
    return "\n".join(lines)


async def selfdev_test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    job_id = context.args[0] if context.args else context.user_data.get("selfdev_last_job_id")
    if not job_id:
        await update.message.reply_text("Использование: /selfdev_test <job_id>")
        return
    await update.message.reply_text(f"🧪 Прогоняю проверки для {job_id}...")
    try:
        report = self_improvement.run_selfdev_checks(job_id)
        checks = report.get("checks", {})
        lines = [f"Проверки job {job_id}: {'все пройдены' if report.get('success') else 'есть проблемы'}"]
        lines.extend(f"- {name}: {'OK' if ok else 'FAIL'}" for name, ok in checks.items())
        if not report.get("success"):
            lines.append("Ошибки: " + "; ".join(str(e) for e in report.get("errors") or []))
        else:
            lines.append(f"Можно ставить: /selfdev_install {job_id}")
        await reply_long(update.message, "\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Ошибка проверки: {e}")


async def selfdev_install_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    job_id = context.args[0] if context.args else context.user_data.get("selfdev_last_job_id")
    if not job_id:
        await update.message.reply_text("Использование: /selfdev_install <job_id>")
        return
    chat_id, user_id = chat_user_ids(update)
    await update.message.reply_text(f"📦 Устанавливаю plugin из job {job_id}...")
    try:
        report = self_improvement.install_plugin(job_id)
        await reply_long(
            update.message,
            "\n".join(
                [
                    f"Plugin {report.get('plugin_name')} установлен и сервис перезапущен.",
                    "Файлы: " + ", ".join(report.get("installed_paths") or []),
                ]
            ),
        )
    except Exception as e:
        save_last_error(chat_id=chat_id, user_id=user_id, handler="selfdev_install_command", error=e, user_text=update.message.text or "")
        await update.message.reply_text(f"Установка не удалась: {e}")


async def selfdev_rollback_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /selfdev_rollback <job_id>")
        return
    job_id = context.args[0]
    try:
        result = self_improvement.rollback_selfdev(job_id)
        await update.message.reply_text(f"Откатил до {result.get('rolled_back_to')}, сервис перезапущен.")
    except Exception as e:
        await update.message.reply_text(f"Rollback не удался: {e}")


async def plugins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    items = plugin_manager.list_plugins()
    if not items:
        await update.message.reply_text("Установленных plugins пока нет.")
        return
    lines = ["Установленные plugins:"]
    lines.extend(f"- {p['name']} v{p['version']}: {p['description']}" for p in items)
    await reply_long(update.message, "\n".join(lines))


async def plugin_show_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Использование: /plugin_show <name>")
        return
    name = context.args[0]
    module = plugin_manager.get_plugin_by_name(name)
    if not module:
        await update.message.reply_text(f"Plugin {name} не найден.")
        return
    await update.message.reply_text(
        f"{module.PLUGIN_NAME} v{module.PLUGIN_VERSION}\n{module.PLUGIN_DESCRIPTION}\nфайл: {Path(module.__file__).name}"
    )


async def selfdev_run_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /selfdev_run <job_id> <тестовый запрос>")
        return
    job_id = context.args[0]
    test_prompt = " ".join(context.args[1:])
    chat_id, _ = chat_user_ids(update)
    try:
        result = self_improvement.dry_run_plugin(
            job_id, test_prompt, {"project_name": memory.get_current_project(chat_id), "chat_id": chat_id}
        )
        await reply_long(
            update.message,
            "\n".join(
                [
                    f"Dry-run job {job_id} ({result['plugin_name']}):",
                    f"can_handle score: {result['can_handle_score']:.2f}",
                    f"parsed_task: {result['parsed_task']}",
                    result["planned"],
                ]
            ),
        )
    except Exception as e:
        await update.message.reply_text(f"Dry-run не удался: {e}")


async def _try_installed_plugin(
    user_text: str, update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: str
) -> tuple[str, dict] | None:
    """Universal extension point: scores user_text against every installed
    plugin's can_handle() and dispatches to the best match above threshold.
    This is how new capabilities should be added going forward (see
    plugins/workspace_inspector.py) instead of one-off phrase branches in
    bot.py. Returns None if no plugin is confident enough -- caller falls
    through to the normal router."""
    plugin_context = {"project_name": _workspace_project_from_context(user_text, chat_id) or memory.get_current_project(chat_id), "chat_id": chat_id}
    match = plugin_manager.select_plugin(user_text, plugin_context)
    if not match:
        return None
    module, score = match
    result = await plugin_manager.safe_dispatch(module, update, context, {"user_text": user_text, **plugin_context})
    answer = str(result.get("answer") or result.get("message") or ("Готово." if result.get("success") else "Не получилось."))
    return answer, {
        "detected": {"intent": "plugin", "plugin": module.PLUGIN_NAME, "score": score},
        "tools_called": [f"plugin:{module.PLUGIN_NAME}"],
        "errors": [] if result.get("success") else [str(result.get("error") or "plugin failed")],
    }


async def _maybe_handle_via_plugin_or_selfdev(
    user_text: str, update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: str
) -> tuple[str, dict] | None:
    """Called only when the normal router/intent system found nothing usable
    for an action-like message (an installed plugin already had its shot
    earlier in handle_text via _try_installed_plugin). If SELFDEV_MODE !=
    off, proposes a brand-new plugin instead of falling back to generic
    chat. Returns None (caller keeps the original answer) if selfdev is off
    or propose_plugin itself fails (e.g. Ollama unreachable)."""
    if _effective_selfdev_mode(context) == "off":
        return None
    plugin_context = {"project_name": memory.get_current_project(chat_id), "chat_id": chat_id}
    try:
        spec = self_improvement.propose_plugin(user_text, plugin_context)
        job_id = self_improvement.new_job_id()
        self_improvement.write_plugin_to_sandbox(job_id, spec)
        context.user_data["selfdev_last_job_id"] = job_id
        return _format_selfdev_proposal(spec, job_id), {
            "detected": {"intent": "selfdev_propose", "job_id": job_id},
            "tools_called": ["propose_plugin"],
            "errors": [],
        }
    except Exception:
        logging.exception("selfdev propose_plugin failed during routing fallback")
        return None


async def debug_last_intent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await reply_long(update.message, str(context.user_data.get("last_intent") or "intent еще не распознавался"))


async def router_test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Использование: /router_test <text>")
        return
    chat_id, _ = chat_user_ids(update)
    current_project = memory.get_current_project(chat_id) if chat_id else None
    last_action = get_last_action(chat_id)
    recent = memory.recent_messages(chat_id, config.HISTORY_LIMIT) if chat_id else None
    try:
        classification = semantic_router.classify_intent(
            text,
            recent_messages=recent,
            current_project=current_project,
            last_action=last_action,
            ask_model=ask_ollama_messages,
        )
        lines = [
            f"intent: {classification.get('intent')}",
            f"confidence: {classification.get('confidence')}",
            f"project_name: {classification.get('project_name') or '-'}",
            f"needs_tool: {classification.get('needs_tool')}",
            f"start_preview: {classification.get('start_preview')}",
            f"language: {classification.get('language')}",
            f"reason: {classification.get('reason') or '-'}",
        ]
        await reply_long(update.message, "\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Ошибка /router_test: {e}")


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
    router = debug_info.get("router")
    if router:
        lines.append(
            "router_intent: {} confidence={} project={} needs_tool={} language={} reason={}".format(
                router.get("intent"),
                router.get("confidence"),
                router.get("project_name") or "-",
                router.get("needs_tool"),
                router.get("language"),
                router.get("reason") or "-",
            )
        )
    if debug_info.get("modified_files"):
        lines.append(f"modified_files: {', '.join(debug_info['modified_files'])}")
    if debug_info.get("created_files"):
        lines.append(f"created_files: {', '.join(debug_info['created_files'])}")
    if "preview_url" in debug_info:
        lines.append(f"preview_url: {debug_info.get('preview_url') or '-'}")
    if "curl_check" in debug_info:
        lines.append(f"curl_check: {debug_info.get('curl_check')}")
    if "browser_check" in debug_info:
        browser_check = debug_info.get("browser_check")
        lines.append(f"browser_check: {browser_check}")
        if isinstance(browser_check, dict) and browser_check.get("screenshot_path"):
            lines.append(f"screenshot_path: {browser_check['screenshot_path']}")
    if "acceptance" in debug_info:
        lines.append(f"acceptance: {debug_info.get('acceptance')}")
    if debug_info.get("verification_report"):
        lines.append(f"verification_report:\n{debug_info['verification_report']}")
    lines.append(f"рабочая папка Jarvis: {config.get_write_root()}")
    if message:
        await message.reply_text("\n".join(lines))


def _check_ollama() -> str:
    try:
        import anthropic
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            return "error (ANTHROPIC_API_KEY не задан)"
        client = anthropic.Anthropic(api_key=api_key)
        client.models.list(limit=1)
        return f"ok (Claude, model={os.getenv('CLAUDE_MODEL', 'claude-sonnet-4-6')})"
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
            preview_lines = [
                f"- {item['project']}: {preview_url_for_port(int(item['port']))}"
                for item in previews["previews"]
                if item.get("port")
            ]
        except Exception:
            previews_count = "unknown"
            preview_lines = []
        server_host_env = os.getenv("SERVER_HOST", "").strip()
        lan_ip = detect_lan_ip()
        if playwright_available():
            pw_smoke = await playwright_async_smoke_check()
            playwright_line = "ok" if pw_smoke.get("ok") else f"fail ({pw_smoke.get('reason')})"
        else:
            playwright_line = "not installed"
        last_verification = get_last_verification()
        if last_verification:
            verification_line = (
                f"{last_verification.get('project_name')}: success={last_verification.get('success')} "
                f"at {last_verification.get('checked_at')}"
            )
            screenshot_line = last_verification.get("screenshot_path") or "-"
        else:
            verification_line = "-"
            screenshot_line = "-"
        text = "\n".join(
            [
                "Jarvis status:",
                "Polling ok: yes",
                f"Claude API: {_check_ollama()}",
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
                "Active previews:",
                *(preview_lines or ["- нет запущенных preview"]),
                f"SERVER_HOST (env): {server_host_env or '-'}",
                f"LAN IP (hostname -I): {lan_ip or '-'}",
                f"Playwright async check: {playwright_line}",
                f"Pillow: {'installed' if pillow_available() else 'not installed'}",
                f"Last verification: {verification_line}",
                f"Last screenshot: {screenshot_line}",
                f"Debug mode (agent_debug): {'on' if context.user_data.get('agent_debug') else 'off'}",
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
        running = get_current_task(chat_id)
        if running and any(p in user_text.lower() for p in ("ты получил", "получил сообщение", "получил?", "ты здесь")):
            await update.message.reply_text(
                f"Да, получил. Сейчас выполняю задачу: {running.get('step') or '-'}.\nПрогресс: /progress"
            )
            return

        # "что делаешь сейчас?" must read real current_task/project_state, never
        # improvise an activity Jarvis isn't actually doing.
        if _wants_current_activity(user_text):
            project = _workspace_project_from_context(user_text, chat_id) or memory.get_current_project(chat_id)
            await update.message.reply_text(project_state_manager.format_current_activity_answer(project, running))
            return

        # A quiet "да, правильно"/"нет, сломал" after a site edit retroactively
        # tags the most recent learning_log entry for this chat -- raw material
        # for future self-improvement, doesn't change behavior live.
        feedback_status = _detect_feedback(user_text)
        if feedback_status:
            project = memory.get_current_project(chat_id)
            if project and learning_log.mark_last_feedback(project, chat_id=chat_id, status=feedback_status):
                ack = "Принял, отметил как approved." if feedback_status == "approved" else "Принял, отметил как rejected."
                await update.message.reply_text(ack)
                return

        # task_orchestrator is the single, state-driven decision point for
        # apply_media_to_site vs edit_site vs check_site -- it resolves real
        # entities (entity_resolver: does a workspace project by this name
        # actually exist? is there a pending photo right now?) and picks a
        # task_type from that state, not from several independent
        # phrase-heuristics racing each other. This is what fixes "на сайт
        # kiki как фон" (no photo-reference word, only target words) falling
        # through to edit_workspace_site, and "проверь слайдер в kiki" landing
        # in git project inspection instead of a site check. See
        # task_orchestrator.py.
        decision = task_orchestrator.resolve_task(user_text, chat_id)

        if decision.task_type == "apply_media_to_site":
            project = decision.workspace_project
            if not project:
                await update.message.reply_text("На какой сайт применить фон? Напиши имя проекта, например: hola")
                return
            target = "hero_background" if _wants_hero_target(user_text) else "whole_page_background"
            if decision.media_source == "pending_media":
                available_media = get_latest_available_media(chat_id)
                if not available_media:
                    await update.message.reply_text("Я не вижу последнего фото. Пришли фото еще раз.")
                    return
                fixed = _wants_fixed_attachment(user_text)
                await add_background_image_workflow(
                    update.message, context, project_name=project, media=available_media, target=target, fixed=fixed
                )
            else:
                await apply_existing_image_background_workflow(
                    update.message, context, project_name=project, target=target
                )
            return

        # Status/feature questions about workspace websites must be intercepted
        # before semantic/git routing. Example: "проверь, есть ли слайдер ...
        # на сайте kuki" is a workspace-site check, not a git repo check.
        if decision.task_type == "check_site":
            project = decision.workspace_project
            if not project:
                await update.message.reply_text("Какой сайт проверить? Напиши имя проекта, например: kuki")
                return
            if decision.component_kind:
                # "проверь слайдер/карусель/меню/гармонь/форму/языки/фон" names
                # a specific UI component -- verify_ui_component_workflow runs
                # the universal, model-driven DOM check (ui_component_model.py/
                # ui_component_verifier.py), never git tools, never normal chat.
                answer, debug = await verify_ui_component_workflow(project, decision.component_kind, chat_id=chat_id)
            else:
                answer, debug = await workspace_site_feature_check_workflow(project, user_text, chat_id=chat_id)
            await reply_long(update.message, answer)
            await maybe_send_intent_debug(update.message, context, debug)
            return

        # Installed plugins (plugin_manager) get first refusal on free text via
        # their own can_handle() score -- this is the general extension point
        # for "Jarvis doesn't know how to do X yet" cases (see
        # plugins/workspace_inspector.py for the reference pattern: semantic
        # routing decides applicability, a safe deterministic tool does the
        # work, never an LLM guessing). Only dispatches when some plugin is
        # confident; otherwise falls through to the normal router below.
        plugin_answer = await _try_installed_plugin(user_text, update, context, chat_id)
        if plugin_answer:
            answer, _plugin_debug_info = plugin_answer
            await reply_long(update.message, answer)
            return

        recent = memory.recent_messages(chat_id, config.HISTORY_LIMIT)
        message_id = memory.save_message(chat_id, user_id, "user", user_text, "text")
        use_agent = bool(context.user_data.get("agent_enabled"))
        pending_task = pending_task_for_text(user_text)
        debug_info["pending_task"] = pending_task

        tracker = ProgressTracker(update.message) if pending_task else None
        if pending_task and tracker:
            await tracker.step("⏳ Принял задачу...")
            if pending_task == "create_workspace_project":
                project_name = _slug_from_text(user_text)
                await tracker.step(f"🧠 Генерирую сайт {project_name}...")
                await tracker.step("💾 Записываю файлы...")
                if _wants_preview(user_text):
                    await tracker.step("🧪 Проверяю сайт...")
            elif pending_task == "edit_workspace_site":
                await tracker.step("🧠 Генерирую изменения...")
                await tracker.step("💾 Записываю файлы...")
                await tracker.step("🧪 Проверяю сайт...")
            else:
                await tracker.step("🧠 Определяю проект и контекст...")

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
            if (debug_info.get("detected") or {}).get("intent") in ("normal_chat", "unknown") and semantic_router.is_action_like(
                user_text
            ):
                intercepted = await _maybe_handle_via_plugin_or_selfdev(user_text, update, context, chat_id)
                if intercepted:
                    answer, intercepted_debug = intercepted
                    intercepted_debug.setdefault("pending_task", pending_task)
                    debug_info = intercepted_debug
        except requests.exceptions.ConnectionError as e:
            save_last_error(chat_id=chat_id, user_id=user_id, handler="handle_text", error=e, user_text=user_text)
            answer = "Ошибка подключения к внешнему сервису. Детали: " + str(e)
            debug_info = {"detected": {"intent": "connection_error"}, "tools_called": [], "errors": [str(e)], "pending_task": pending_task}
        except requests.exceptions.Timeout as e:
            save_last_error(chat_id=chat_id, user_id=user_id, handler="handle_text", error=e, user_text=user_text)
            answer = "Сервис не ответил вовремя. Попробуй ещё раз."
            debug_info = {"detected": {"intent": "timeout"}, "tools_called": [], "errors": [str(e)], "pending_task": pending_task}

        if tracker:
            await tracker.step("✅ Готово.")

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
            "Ошибка STT: не могу подключиться к локальному сервису.\n\n"
            f"STT: {STT_URL}\n\n"
            f"Детали: {e}"
        )
    except requests.exceptions.Timeout as e:
        save_last_error(chat_id=chat_id, user_id=user_id, handler="handle_voice_or_audio", error=e, user_text=recognized_text)
        await message.reply_text("STT не ответил вовремя.")
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
    app.add_handler(CommandHandler("workspace_status", workspace_status_command))
    app.add_handler(CommandHandler("ports", ports_command))
    app.add_handler(CommandHandler("write_mode", write_mode_command))
    app.add_handler(CommandHandler("new_static", new_static_command))
    app.add_handler(CommandHandler("create_site", create_site_command))
    app.add_handler(CommandHandler("create_and_preview", create_and_preview_command))
    app.add_handler(CommandHandler("where", where_command))
    app.add_handler(CommandHandler("new_flask", new_flask_command))
    app.add_handler(CommandHandler("write_file", write_file_command))
    app.add_handler(CommandHandler("delete_file", delete_file_command))
    app.add_handler(CommandHandler("preview_start", preview_start_command))
    app.add_handler(CommandHandler("check_site", site_check_command))
    app.add_handler(CommandHandler("current_project", current_project_command))
    app.add_handler(CommandHandler("use_project", use_project_command))
    app.add_handler(CommandHandler("preview_stop", preview_stop_command))
    app.add_handler(CommandHandler("preview_stop_port", preview_stop_port_command))
    app.add_handler(CommandHandler("preview_stop_all", preview_stop_all_command))
    app.add_handler(CommandHandler("workspace_delete", workspace_delete_command))
    app.add_handler(CommandHandler("workspace_clean_stopped", workspace_clean_stopped_command))
    app.add_handler(CommandHandler("selftest_stop_delete", selftest_stop_delete_command))
    app.add_handler(CommandHandler("preview_list", preview_list_command))
    app.add_handler(CommandHandler("preview_status", preview_status_command))
    app.add_handler(CommandHandler("selftest_workspace", selftest_workspace_command))
    app.add_handler(CommandHandler("preview_info", preview_info_command))
    app.add_handler(CommandHandler("workspace_tree", workspace_tree_command))
    app.add_handler(CommandHandler("logs", logs_command))
    app.add_handler(CommandHandler("bot_logs", bot_logs_command))
    app.add_handler(CommandHandler("last_error", last_error_command))
    app.add_handler(CommandHandler("last_action", last_action_command))
    app.add_handler(CommandHandler("edit_site", edit_site_command))
    app.add_handler(CommandHandler("current_task", current_task_command))
    app.add_handler(CommandHandler("progress", progress_command))
    app.add_handler(CommandHandler("browser_check", browser_check_command))
    app.add_handler(CommandHandler("images", images_command))
    app.add_handler(CommandHandler("files", files_command))
    app.add_handler(CommandHandler("ls", files_command))
    app.add_handler(CommandHandler("tree_project", tree_project_command))
    app.add_handler(CommandHandler("set_background", set_background_command))
    app.add_handler(CommandHandler("site_state", site_state_command))
    app.add_handler(CommandHandler("site_snapshots", site_snapshots_command))
    app.add_handler(CommandHandler("site_rollback", site_rollback_command))
    app.add_handler(CommandHandler("site_check", site_check_command))
    app.add_handler(CommandHandler("repair_site", repair_site_command))
    app.add_handler(CommandHandler("site_history", site_history_command))
    app.add_handler(CommandHandler("site_last_success", site_last_success_command))
    app.add_handler(CommandHandler("site_diff", site_diff_command))
    app.add_handler(CommandHandler("site_requirements", site_requirements_command))
    app.add_handler(CommandHandler("last_media", last_media_command))
    app.add_handler(CommandHandler("media_status", media_status_command))
    app.add_handler(CommandHandler("task_debug", task_debug_command))
    app.add_handler(CommandHandler("check_component", check_component_command))
    app.add_handler(CommandHandler("project_state", project_state_command))
    app.add_handler(CommandHandler("retry_media_background", retry_media_background_command))
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
    app.add_handler(CommandHandler("workflow_debug_on", workflow_debug_on))
    app.add_handler(CommandHandler("workflow_debug_off", workflow_debug_off))
    app.add_handler(CommandHandler("selfdev_on", selfdev_on))
    app.add_handler(CommandHandler("selfdev_off", selfdev_off))
    app.add_handler(CommandHandler("selfdev_status", selfdev_status_command))
    app.add_handler(CommandHandler("selfdev_propose", selfdev_propose_command))
    app.add_handler(CommandHandler("selfdev_test", selfdev_test_command))
    app.add_handler(CommandHandler("selfdev_install", selfdev_install_command))
    app.add_handler(CommandHandler("selfdev_rollback", selfdev_rollback_command))
    app.add_handler(CommandHandler("selfdev_run", selfdev_run_command))
    app.add_handler(CommandHandler("plugins", plugins_command))
    app.add_handler(CommandHandler("plugin_show", plugin_show_command))
    app.add_handler(CommandHandler("debug_last_intent", debug_last_intent))
    app.add_handler(CommandHandler("router_test", router_test_command))
    app.add_handler(CommandHandler("tts_test", tts_test))
    app.add_handler(CommandHandler("patch", disabled_write_command))
    app.add_handler(CommandHandler("apply_patch", disabled_write_command))
    app.add_handler(CommandHandler("test", disabled_write_command))
    app.add_handler(CommandHandler("deploy", disabled_write_command))

    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice_or_audio))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling()


if __name__ == "__main__":
    main()
