import json
import re
import traceback as traceback_module
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import config
import memory


MAX_ERRORS = 20
TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def mask_error_text(text: str | None) -> str:
    masked = memory.mask_secrets(text or "")
    return TOKEN_RE.sub("bot[MASKED]", masked)


def _errors_path() -> Path:
    path = Path(config.JARVIS_DB_PATH).resolve().parent / "last_errors.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_errors() -> list[dict[str, Any]]:
    path = _errors_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def save_last_error(
    *,
    chat_id: str = "",
    user_id: str = "",
    handler: str,
    error: BaseException,
    user_text: str = "",
    tb_text: str = "",
) -> dict[str, Any]:
    if not tb_text:
        tb_text = "".join(traceback_module.format_exception(type(error), error, error.__traceback__))
    item = {
        "timestamp": datetime.now(ZoneInfo("Europe/Madrid")).isoformat(timespec="seconds"),
        "chat_id": str(chat_id or ""),
        "user_id": str(user_id or ""),
        "handler": handler,
        "error_type": type(error).__name__,
        "error_message": mask_error_text(str(error)),
        "traceback": mask_error_text(tb_text)[-12000:],
        "user_text": mask_error_text(user_text)[:3000],
    }
    errors = _load_errors()
    errors.append(item)
    _errors_path().write_text(json.dumps(errors[-MAX_ERRORS:], ensure_ascii=False, indent=2), encoding="utf-8")
    return item


def latest_error(chat_id: str | None = None) -> dict[str, Any] | None:
    errors = _load_errors()
    if chat_id:
        for item in reversed(errors):
            if str(item.get("chat_id") or "") == str(chat_id):
                return item
        return None
    return errors[-1] if errors else None


def error_summary(chat_id: str | None = None) -> dict[str, Any]:
    errors = _load_errors()
    relevant = [item for item in errors if not chat_id or str(item.get("chat_id") or "") == str(chat_id)]
    last = relevant[-1] if relevant else None
    return {
        "count": len(relevant),
        "last_timestamp": last.get("timestamp") if last else "",
        "last_type": last.get("error_type") if last else "",
    }
