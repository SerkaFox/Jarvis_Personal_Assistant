import os
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

load_dotenv()

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
        "Не придумывай результаты команд. Если нужны логи или вывод команды — попроси пользователя выполнить команду или скажи, какую команду выполнить. "
        "Для опасных действий, таких как удаление файлов, миграции, рестарт сервисов, deploy, git push или изменения nginx/systemd, требуй явное подтверждение. "
        "Не говори, что ты облачный сервис. Ты локальный Jarvis, подключенный к Ollama."
    )


def is_allowed(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ALLOWED_USER_ID


def ask_ollama(user_text: str) -> str:
    payload = {
        "model": MODEL,
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": get_system_prompt(),
            },
            {
                "role": "user",
                "content": user_text,
            },
        ],
    }

    r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=180)
    r.raise_for_status()
    return r.json()["message"]["content"]


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

    try:
        answer = ask_ollama(user_text)
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

    await update.message.reply_text(answer[:4000])


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

        await message.reply_text(f"🎙 Распознал:\n{recognized_text[:1000]}")

        answer = ask_ollama(recognized_text)
        await message.reply_text(answer[:4000])

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
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("tts_test", tts_test))

    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice_or_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling()


if __name__ == "__main__":
    main()
