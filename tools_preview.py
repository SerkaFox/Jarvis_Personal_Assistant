import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import config
from tools_fs import ToolError
from tools_write import ensure_write_root, resolve_write_path


PREVIEWS_PATH = Path("data/previews.json")
_PROCESS_HANDLES: dict[str, subprocess.Popen] = {}


def _registry_path() -> Path:
    path = Path(os.getenv("JARVIS_DB_PATH", config.JARVIS_DB_PATH)).resolve().parent / "previews.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_registry() -> dict[str, Any]:
    path = _registry_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _save_registry(data: dict[str, Any]) -> None:
    path = _registry_path()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _proc_cwd(pid: int) -> Path | None:
    proc_cwd = Path(f"/proc/{pid}/cwd")
    try:
        return proc_cwd.resolve()
    except Exception:
        return None


def _proc_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_text(encoding="utf-8").replace("\x00", " ")
    except Exception:
        return ""


def _is_own_preview_process(record: dict[str, Any]) -> bool:
    pid = int(record.get("pid") or 0)
    project_path = Path(str(record.get("path") or "")).resolve()
    if not pid or not _is_pid_alive(pid):
        return False
    cwd = _proc_cwd(pid)
    if cwd != project_path:
        return False
    cmdline = _proc_cmdline(pid)
    return "http.server" in cmdline or "app.py" in cmdline


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def _select_port() -> int:
    start = config.env_int("PREVIEW_PORT_MIN", config.PREVIEW_PORT_MIN)
    end = config.env_int("PREVIEW_PORT_MAX", config.PREVIEW_PORT_MAX)
    if start > end:
        raise ToolError("PREVIEW_PORT_MIN больше PREVIEW_PORT_MAX")
    for port in range(start, end + 1):
        if _port_free(port):
            return port
    raise ToolError(f"Нет свободных preview ports в диапазоне {start}..{end}")


def _project_path(project_name: str) -> Path:
    ensure_write_root()
    path = resolve_write_path(project_name)
    if not path.is_dir():
        raise ToolError(f"Проект не найден в WRITE_ROOT: {path}")
    return path


def _preview_url(port: int) -> str:
    host = os.getenv("SERVER_HOST", config.SERVER_HOST).rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    return f"{host}:{port}"


def _cleanup_stale(registry: dict[str, Any]) -> dict[str, Any]:
    clean = {}
    for name, record in registry.items():
        if _is_own_preview_process(record):
            clean[name] = record
    if clean != registry:
        _save_registry(clean)
    return clean


def start_preview(project_name: str) -> dict[str, Any]:
    project = _project_path(project_name)
    name = project.name
    registry = _cleanup_stale(_load_registry())
    existing = registry.get(name)
    if existing and _is_own_preview_process(existing):
        return {**existing, "already_running": True}

    port = _select_port()
    env = {**os.environ, "PORT": str(port)}
    if (project / "index.html").is_file():
        command = ["python3", "-m", "http.server", str(port), "--bind", "0.0.0.0"]
    elif (project / "app.py").is_file() and (project / "requirements.txt").is_file():
        venv_python = project / "venv" / "bin" / "python"
        if not venv_python.is_file():
            raise ToolError("Flask preview требует существующий venv; зависимости автоматически не ставятся")
        command = [str(venv_python), "app.py"]
    else:
        raise ToolError("Не найден index.html или app.py+requirements.txt для preview")

    process = subprocess.Popen(
        command,
        cwd=str(project),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(0.25)
    if process.poll() is not None:
        raise ToolError(f"Preview процесс завершился сразу: {process.returncode}")

    record = {
        "project": name,
        "path": str(project),
        "pid": process.pid,
        "port": port,
        "url": _preview_url(port),
        "command": " ".join(command),
        "started_at": int(time.time()),
    }
    _PROCESS_HANDLES[name] = process
    registry[name] = record
    _save_registry(registry)
    return record


def stop_preview(project_name: str) -> dict[str, Any]:
    registry = _load_registry()
    name = Path(project_name).name
    record = registry.get(name)
    if not record:
        raise ToolError(f"Preview не найден: {name}")
    if not _is_own_preview_process(record):
        registry.pop(name, None)
        _save_registry(registry)
        return {"project": name, "stopped": False, "reason": "process not running or not owned preview"}

    pid = int(record["pid"])
    os.killpg(pid, signal.SIGTERM)
    handle = _PROCESS_HANDLES.pop(name, None)
    for _ in range(20):
        if handle is not None and handle.poll() is not None:
            break
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                break
        except ChildProcessError:
            pass
        if not _is_pid_alive(pid):
            break
        time.sleep(0.1)
    if _is_pid_alive(pid):
        os.killpg(pid, signal.SIGKILL)
    if handle is not None:
        try:
            handle.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    else:
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
    registry.pop(name, None)
    _save_registry(registry)
    return {"project": name, "pid": pid, "stopped": True}


def list_previews() -> dict[str, Any]:
    registry = _cleanup_stale(_load_registry())
    return {"previews": list(registry.values()), "count": len(registry)}


def preview_status(project_name: str) -> dict[str, Any]:
    registry = _cleanup_stale(_load_registry())
    name = Path(project_name).name
    record = registry.get(name)
    if not record:
        return {"project": name, "running": False}
    return {**record, "running": _is_own_preview_process(record)}
