import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()

DEFAULT_ALLOWED_ROOTS = "/home/seradmin,/home/seradmin/jelec,/var/www"
DEFAULT_ALLOWED_SERVICES = "jarvis-bot,j-listoya-stt"
DEFAULT_MAX_FILE_CHARS = 12000
DEFAULT_MAX_SEARCH_RESULTS = 50
DEFAULT_JARVIS_DB_PATH = "/home/seradmin/jarvis_bot/data/jarvis.db"
DEFAULT_HISTORY_LIMIT = 12
DEFAULT_WRITE_ROOT = "/home/seradmin/jarvis_workspace"
DEFAULT_PREVIEW_PORT_MIN = 8700
DEFAULT_PREVIEW_PORT_MAX = 8799
DEFAULT_SERVER_HOST = "http://127.0.0.1"


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


def get_write_root() -> Path:
    return Path(os.getenv("WRITE_ROOT", DEFAULT_WRITE_ROOT)).expanduser().resolve()


MAX_FILE_CHARS = env_int("MAX_FILE_CHARS", DEFAULT_MAX_FILE_CHARS)
MAX_SEARCH_RESULTS = env_int("MAX_SEARCH_RESULTS", DEFAULT_MAX_SEARCH_RESULTS)
AGENT_TOOLS_ENABLED = env_bool("AGENT_TOOLS_ENABLED", True)
JARVIS_DB_PATH = os.getenv("JARVIS_DB_PATH", DEFAULT_JARVIS_DB_PATH)
MEMORY_ENABLED = env_bool("MEMORY_ENABLED", True)
HISTORY_LIMIT = env_int("HISTORY_LIMIT", DEFAULT_HISTORY_LIMIT)
WRITE_MODE_ENABLED = env_bool("WRITE_MODE_ENABLED", False)
WRITE_ROOT = os.getenv("WRITE_ROOT", DEFAULT_WRITE_ROOT)
PREVIEW_PORT_MIN = env_int("PREVIEW_PORT_MIN", DEFAULT_PREVIEW_PORT_MIN)
PREVIEW_PORT_MAX = env_int("PREVIEW_PORT_MAX", DEFAULT_PREVIEW_PORT_MAX)
SERVER_HOST = os.getenv("SERVER_HOST", DEFAULT_SERVER_HOST).rstrip("/")
