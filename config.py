import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()

DEFAULT_ALLOWED_ROOTS = "/home/seradmin,/home/seradmin/jelec,/var/www"
DEFAULT_ALLOWED_SERVICES = "jarvis-bot,j-listoya-stt"
DEFAULT_MAX_FILE_CHARS = 12000
DEFAULT_MAX_SEARCH_RESULTS = 50


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return max(1, int(value))
    except ValueError:
        return default


def env_csv(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def get_allowed_roots() -> list[Path]:
    roots = []
    for raw_root in env_csv("ALLOWED_ROOTS", DEFAULT_ALLOWED_ROOTS):
        root = Path(raw_root).expanduser().resolve()
        if root.exists() and root.is_dir():
            roots.append(root)
    return roots


def get_allowed_services() -> list[str]:
    return env_csv("ALLOWED_SERVICES", DEFAULT_ALLOWED_SERVICES)


MAX_FILE_CHARS = env_int("MAX_FILE_CHARS", DEFAULT_MAX_FILE_CHARS)
MAX_SEARCH_RESULTS = env_int("MAX_SEARCH_RESULTS", DEFAULT_MAX_SEARCH_RESULTS)
AGENT_TOOLS_ENABLED = env_bool("AGENT_TOOLS_ENABLED", True)
