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


PROJECT_ROOT = Path(__file__).resolve().parent

SELFDEV_MODES = {"off", "suggest", "auto_plugins"}
DEFAULT_SELFDEV_MODE = "suggest"

# Self-improvement is only ever allowed to write inside these prefixes
# (relative to PROJECT_ROOT). This is an allowlist, not a denylist, so it is
# correct by construction: anything not explicitly listed here (bot.py,
# config.py, .env, systemd/nginx config, venv/, models/, data/*.db,
# previews.json, last_errors.json, anything outside the project root, ...)
# can never be a valid install target regardless of what a generated plugin
# spec claims.
SELFDEV_ALLOWED_WRITE_PREFIXES = ("plugins/", "tests/", "skills/", "docs/selfdev/")


def get_selfdev_mode() -> str:
    value = (os.getenv("SELFDEV_MODE", DEFAULT_SELFDEV_MODE) or DEFAULT_SELFDEV_MODE).strip().lower()
    return value if value in SELFDEV_MODES else DEFAULT_SELFDEV_MODE


def _data_dir() -> Path:
    path = Path(os.getenv("JARVIS_DB_PATH", JARVIS_DB_PATH)).expanduser().resolve().parent
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_selfdev_proposed_dir() -> Path:
    path = _data_dir() / "proposed_plugins"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_selfdev_jobs_dir() -> Path:
    path = _data_dir() / "selfdev_jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_plugins_dir() -> Path:
    path = Path(os.getenv("JARVIS_PLUGINS_DIR", str(PROJECT_ROOT / "plugins"))).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_plugin_tests_dir() -> Path:
    path = Path(os.getenv("JARVIS_PLUGIN_TESTS_DIR", str(PROJECT_ROOT / "tests"))).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_skills_dir() -> Path:
    path = Path(os.getenv("JARVIS_SKILLS_DIR", str(PROJECT_ROOT / "skills"))).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_workspace_state_dir() -> Path:
    path = Path(os.getenv("JARVIS_WORKSPACE_STATE_DIR", str(_data_dir() / "workspace_state"))).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_workspace_snapshots_dir() -> Path:
    path = Path(os.getenv("JARVIS_WORKSPACE_SNAPSHOTS_DIR", str(_data_dir() / "workspace_snapshots"))).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


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
