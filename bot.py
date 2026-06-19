import os
import logging
import shutil
import subprocess
import tempfile
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
from agent import answer_with_tools
from intent_router import detect_intent, handle_detected_intent
import memory
from tools_fs import allowed_roots_info, search_text, tree_summary
from tools_git import find_git_repos, git_diff, git_status, resolve_repo
from tools_project import inspect_project
from tools_system import get_allowed_services, read_journal

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID"))

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


def summarize_project_with_ollama(data: dict) -> str:
    summary = ask_ollama_messages(project_summary_prompt(data))
    memory.save_project_note(
        data["project_name"],
        data["path"],
        summary,
        data["git"].get("last_commit", ""),
    )
    return summary


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

    detected = detect_intent(user_text, recent_messages or [])
    if detected.get("intent") != "normal_chat":
        routed = handle_detected_intent(detected, summarize_project=summarize_project_with_ollama)
        return routed["answer"], {"detected": detected, **routed}

    memory_context = memory.build_memory_context(chat_id, user_text) if chat_id else ""
    if use_agent:
        agent_answer = answer_with_tools(user_text, ask_ollama_messages, memory_context=memory_context)
        if agent_answer:
            return agent_answer, {"detected": detected, "tools_called": ["agent"], "errors": []}
    return ask_ollama(user_text, chat_id=chat_id), {"detected": detected, "tools_called": [], "errors": []}


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
        result = tree_summary(path)
        await reply_long(update.message, result["tree"])
    except Exception as e:
        await update.message.reply_text(f"Ошибка /tree: {e}")


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
                "/logs <service> - последние 80 строк journal",
                "/memory - сохраненная память",
                "/remember <text> - сохранить факт",
                "/forget <key> - удалить memory",
                "/history - последние 10 сообщений",
                "/clear_history - очистить историю чата",
                "/project <repo> - inspection проекта",
                "/status - Ollama/STT/TTS/agent status",
                "/agent_on, /agent_off - read-only agent mode",
                "/agent_debug_on, /agent_debug_off - debug intent routing",
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
        data = inspect_project(" ".join(context.args))
        summary = ask_ollama_messages(project_summary_prompt(data))
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


async def debug_last_intent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await reply_long(update.message, str(context.user_data.get("last_intent") or "intent еще не распознавался"))


async def maybe_send_intent_debug(message, context: ContextTypes.DEFAULT_TYPE, debug_info: dict):
    context.user_data["last_intent"] = debug_info
    if not context.user_data.get("agent_debug"):
        return
    detected = debug_info.get("detected", {})
    lines = [
        "intent debug:",
        f"detected intent: {detected.get('intent')}",
        f"selected project: {debug_info.get('project') or detected.get('project') or '-'}",
        f"tools called: {', '.join(debug_info.get('tools_called') or []) or '-'}",
        f"errors: {'; '.join(debug_info.get('errors') or []) or '-'}",
    ]
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
        text = "\n".join(
            [
                "Jarvis status:",
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

    user_text = update.message.text
    chat_id, user_id = chat_user_ids(update)
    recent = memory.recent_messages(chat_id, config.HISTORY_LIMIT)
    message_id = memory.save_message(chat_id, user_id, "user", user_text, "text")
    use_agent = bool(context.user_data.get("agent_enabled"))

    try:
        answer, debug_info = answer_user_text(
            user_text,
            use_agent,
            chat_id=chat_id,
            recent_messages=recent,
            debug=bool(context.user_data.get("agent_debug")),
        )
    except requests.exceptions.ConnectionError:
        answer = (
            "Не могу подключиться к Ollama на AI-ПК.\n\n"
            f"Проверь с сервера:\n"
            f"curl {OLLAMA_URL}/api/version\n\n"
            "Возможные причины: Windows-ПК выключен/уснул, сменился IP, "
            "Ollama не запущена или firewall блокирует порт 11434."
        )
    except requests.exceptions.Timeout:
        answer = "Ollama не ответила вовремя. Возможно, модель грузится или AI-ПК занят."
    except Exception as e:
        answer = f"Ошибка обращения к Ollama: {e}"
        debug_info = {"detected": {"intent": "error"}, "tools_called": [], "errors": [str(e)]}

    await maybe_send_intent_debug(update.message, context, debug_info)
    memory.save_message(chat_id, user_id, "assistant", answer, "text")
    memory.save_memory_candidates(user_text, answer, message_id)
    await reply_long(update.message, answer)


async def handle_voice_or_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    message = update.message

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

        chat_id, user_id = chat_user_ids(update)
        message_id = memory.save_message(chat_id, user_id, "user", recognized_text, "voice")
        await message.reply_text(f"🎙 Распознал:\n{memory.mask_secrets(recognized_text)[:1000]}")

        use_agent = bool(context.user_data.get("agent_enabled"))
        recent = memory.recent_messages(chat_id, config.HISTORY_LIMIT)
        answer, debug_info = answer_user_text(
            recognized_text,
            use_agent,
            chat_id=chat_id,
            recent_messages=recent,
            debug=bool(context.user_data.get("agent_debug")),
        )
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
        await message.reply_text(
            "Ошибка STT/Ollama: не могу подключиться к локальному сервису.\n\n"
            f"STT: {STT_URL}\n"
            f"Ollama: {OLLAMA_URL}\n\n"
            f"Детали: {e}"
        )
    except requests.exceptions.Timeout:
        await message.reply_text("STT/Ollama не ответили вовремя.")
    except Exception as e:
        await message.reply_text(f"Ошибка обработки голосового сообщения: {e}")


def main():
    memory.init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("roots", roots))
    app.add_handler(CommandHandler("repos", repos))
    app.add_handler(CommandHandler("projects", projects_command))
    app.add_handler(CommandHandler("git", git_command))
    app.add_handler(CommandHandler("diff", diff_command))
    app.add_handler(CommandHandler("find", find_command))
    app.add_handler(CommandHandler("tree", tree_command))
    app.add_handler(CommandHandler("logs", logs_command))
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
