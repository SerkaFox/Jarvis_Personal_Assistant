import json
import logging
import re
import subprocess
from typing import Any

import config


SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")


class SystemToolError(ValueError):
    pass


def get_allowed_services() -> list[str]:
    return config.get_allowed_services()


def _validate_service_name(name: str) -> str:
    if not name:
        raise SystemToolError("service name не задан")

    normalized = name[:-8] if name.endswith(".service") else name
    if not SERVICE_NAME_RE.fullmatch(normalized):
        raise SystemToolError(f"Недопустимое имя сервиса: {name}")
    if normalized not in get_allowed_services():
        raise SystemToolError(f"Сервис не в ALLOWED_SERVICES: {normalized}")
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


def read_journal(
    name: str | None = None,
    lines: int = 100,
    service_name: str | None = None,
) -> dict[str, Any]:
    name = name or service_name
    service = _validate_service_name(name)
    try:
        lines = int(lines)
    except (TypeError, ValueError):
        lines = 100
    lines = max(1, min(lines, 500))
    _log_tool_call("read_journal", {"name": service, "lines": lines})

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
