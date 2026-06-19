import json
import logging
import os
import re
import subprocess
from typing import Any


DEFAULT_ALLOWED_SERVICES = "jarvis-bot,j-listoya-stt"
SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")


class ShellToolError(ValueError):
    pass


def get_allowed_services() -> list[str]:
    raw_services = os.getenv("ALLOWED_SERVICES", DEFAULT_ALLOWED_SERVICES)
    return [service.strip() for service in raw_services.split(",") if service.strip()]


def _validate_service_name(name: str) -> str:
    if not name:
        raise ShellToolError("service name не задан")

    normalized = name[:-8] if name.endswith(".service") else name
    if not SERVICE_NAME_RE.fullmatch(normalized):
        raise ShellToolError(f"Недопустимое имя сервиса: {name}")
    if normalized not in get_allowed_services():
        raise ShellToolError(f"Сервис не в ALLOWED_SERVICES: {normalized}")
    return f"{normalized}.service"


def _log_tool_call(name: str, args: dict[str, Any]) -> None:
    logging.info("tool_call %s %s", name, json.dumps(args, ensure_ascii=False))


def service_status(name: str) -> dict[str, Any]:
    service = _validate_service_name(name)
    _log_tool_call("service_status", {"name": service})

    result = subprocess.run(
        ["systemctl", "status", service, "--no-pager"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    return {
        "service": service,
        "returncode": result.returncode,
        "output": (result.stdout or result.stderr).strip()[:12000],
    }


def read_journal(service_name: str, lines: int = 100) -> dict[str, Any]:
    service = _validate_service_name(service_name)
    try:
        lines = int(lines)
    except (TypeError, ValueError):
        lines = 100
    lines = max(1, min(lines, 500))
    _log_tool_call("read_journal", {"service_name": service, "lines": lines})

    result = subprocess.run(
        ["journalctl", "-u", service, "-n", str(lines), "--no-pager"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    return {
        "service": service,
        "lines": lines,
        "returncode": result.returncode,
        "output": (result.stdout or result.stderr).strip()[:20000],
    }
