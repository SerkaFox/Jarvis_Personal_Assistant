import json
import os
import re
import signal
import socket
import subprocess
import time
import urllib.request
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


def _port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _find_listening_pid(port: int) -> int | None:
    try:
        result = subprocess.run(
            ["ss", "-H", "-ltnp", f"sport = :{port}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    match = re.search(r"pid=(\d+)", result.stdout)
    return int(match.group(1)) if match else None


def _path_in_write_root(path: Path) -> bool:
    root = config.get_write_root().resolve()
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


def _reap(pid: int) -> None:
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass


def _stop_pid(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    for _ in range(20):
        _reap(pid)
        if not _is_pid_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    for _ in range(10):
        _reap(pid)
        if not _is_pid_alive(pid):
            return
        time.sleep(0.1)
    _reap(pid)


def _select_port() -> int:
    start = config.env_int("PREVIEW_PORT_MIN", config.PREVIEW_PORT_MIN)
    end = config.env_int("PREVIEW_PORT_MAX", config.PREVIEW_PORT_MAX)
    if start > end:
        raise ToolError("PREVIEW_PORT_MIN больше PREVIEW_PORT_MAX")
    for port in range(start, end + 1):
        if _port_free(port):
            return port
    raise ToolError(f"Нет свободных preview ports в диапазоне {start}..{end}")


def find_free_port() -> int:
    return _select_port()


def port_is_listening(port: int) -> bool:
    return _port_listening(int(port))


def get_registry_snapshot() -> dict[str, Any]:
    return _load_registry()


def curl_check(port: int) -> dict[str, Any]:
    return _curl_localhost(int(port))


def preview_url_for_port(port: int) -> str:
    return _preview_url(int(port))


def is_own_preview_process(record: dict[str, Any]) -> bool:
    return _is_own_preview_process(record)


def scan_listening_ports() -> dict[str, Any]:
    min_port = config.env_int("PREVIEW_PORT_MIN", config.PREVIEW_PORT_MIN)
    max_port = config.env_int("PREVIEW_PORT_MAX", config.PREVIEW_PORT_MAX)
    registry = _load_registry()
    registered_by_port: dict[int, str] = {}
    for name, record in registry.items():
        port = record.get("port")
        if port:
            registered_by_port[int(port)] = name

    listening = []
    for port in range(min_port, max_port + 1):
        if not _port_listening(port):
            continue
        pid = _find_listening_pid(port)
        cwd = _proc_cwd(pid) if pid else None
        cmdline = _proc_cmdline(pid) if pid else ""
        registered_project = registered_by_port.get(port)
        listening.append(
            {
                "port": port,
                "pid": pid,
                "cwd": str(cwd) if cwd else "",
                "cmdline": cmdline.strip(),
                "registered_project": registered_project,
                "registered": registered_project is not None,
                "in_write_root": bool(cwd) and _path_in_write_root(cwd),
                "suspicious": registered_project is None and "http.server" in cmdline,
            }
        )

    return {
        "range": [min_port, max_port],
        "listening": listening,
        "registered_previews": [
            {"project": name, "port": int(record.get("port") or 0)} for name, record in registry.items()
        ],
    }


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


def _curl_localhost(port: int) -> dict[str, Any]:
    url = f"http://127.0.0.1:{port}/"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            body = response.read(4096).decode("utf-8", errors="replace")
            ok = 200 <= int(response.status) < 400 and "<html" in body.lower()
            return {"success": ok, "url": url, "status": int(response.status), "contains_html": "<html" in body.lower()}
    except Exception as exc:
        return {"success": False, "url": url, "error": str(exc)}


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
        curl_check = _curl_localhost(int(existing["port"]))
        return {**existing, "success": bool(curl_check.get("success")), "already_running": True, "curl_check": curl_check}

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
    curl_check = _curl_localhost(port)
    if not curl_check.get("success"):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except Exception:
            pass
        raise ToolError(f"Preview HTTP-check failed: {curl_check}")

    record = {
        "success": True,
        "project": name,
        "path": str(project),
        "pid": process.pid,
        "port": port,
        "url": _preview_url(port),
        "command": " ".join(command),
        "started_at": int(time.time()),
        "curl_check": curl_check,
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

    path = Path(str(record.get("path") or ""))
    if not _path_in_write_root(path):
        raise ToolError(f"Preview path вне WRITE_ROOT, отказ останова: {path}")

    pid = int(record.get("pid") or 0)
    port = int(record.get("port") or 0)

    if pid and _is_pid_alive(pid) and _is_own_preview_process(record):
        _stop_pid(pid)
    _PROCESS_HANDLES.pop(name, None)

    process_alive = bool(pid) and _is_pid_alive(pid)
    port_listening = bool(port) and _port_listening(port)
    curl_check = _curl_localhost(port) if port else {"success": False}
    curl_responds = bool(curl_check.get("success"))

    checks = {
        "process_alive": process_alive,
        "port_listening": port_listening,
        "curl_responds": curl_responds,
    }
    stopped = not process_alive and not port_listening and not curl_responds

    if stopped:
        registry.pop(name, None)
        _save_registry(registry)

    result = {
        "success": stopped,
        "stopped": stopped,
        "project": name,
        "pid": pid,
        "port": port,
        "path": str(path),
        "checks": checks,
    }
    if not stopped:
        result["error"] = (
            f"Preview не подтвержден как остановленный: process_alive={process_alive} "
            f"port_listening={port_listening} curl_responds={curl_responds}"
        )
    return result


def stop_preview_by_port(port: int) -> dict[str, Any]:
    port = int(port)
    min_port = config.env_int("PREVIEW_PORT_MIN", config.PREVIEW_PORT_MIN)
    max_port = config.env_int("PREVIEW_PORT_MAX", config.PREVIEW_PORT_MAX)
    registry = _load_registry()
    extra_allowed_ports = {int(record["port"]) for record in registry.values() if record.get("port")}
    if not (min_port <= port <= max_port) and port not in extra_allowed_ports:
        raise ToolError(
            f"Порт {port} вне разрешенного диапазона preview {min_port}-{max_port} "
            "и не зарегистрирован как preview"
        )

    if not _port_listening(port):
        return {
            "success": True,
            "stopped": False,
            "port": port,
            "reason": "port not listening",
            "checks": {"process_alive": False, "port_listening": False, "curl_responds": False},
        }

    pid = _find_listening_pid(port)
    if not pid:
        raise ToolError(f"Не удалось определить процесс, слушающий порт {port}")

    cwd = _proc_cwd(pid)
    cmdline = _proc_cmdline(pid)
    if "http.server" not in cmdline:
        raise ToolError(f"Процесс на порту {port} не похож на preview (нет http.server в cmdline): {cmdline.strip()}")
    if not cwd or not _path_in_write_root(cwd):
        raise ToolError(f"Процесс на порту {port} вне WRITE_ROOT, отказ останова: {cwd}")

    _stop_pid(pid)

    process_alive = _is_pid_alive(pid)
    port_listening = _port_listening(port)
    curl_check = _curl_localhost(port)
    curl_responds = bool(curl_check.get("success"))
    checks = {"process_alive": process_alive, "port_listening": port_listening, "curl_responds": curl_responds}
    stopped = not process_alive and not port_listening and not curl_responds

    if stopped:
        changed = False
        for proj_name, rec in list(registry.items()):
            if int(rec.get("port") or -1) == port:
                registry.pop(proj_name, None)
                changed = True
        if changed:
            _save_registry(registry)

    result = {
        "success": stopped,
        "stopped": stopped,
        "port": port,
        "pid": pid,
        "cwd": str(cwd) if cwd else "",
        "checks": checks,
    }
    if not stopped:
        result["error"] = f"Порт {port} все еще активен после попытки остановки"
    return result


def cleanup_stale_previews() -> dict[str, Any]:
    registry = _load_registry()
    removed = [name for name, record in registry.items() if not _is_own_preview_process(record)]
    clean = _cleanup_stale(registry)
    return {"removed": removed, "remaining": list(clean.keys())}


def list_previews() -> dict[str, Any]:
    registry = _cleanup_stale(_load_registry())
    return {"previews": list(registry.values()), "count": len(registry)}


def preview_status(project_name: str) -> dict[str, Any]:
    registry = _cleanup_stale(_load_registry())
    name = Path(project_name).name
    record = registry.get(name)
    if not record:
        return {"project": name, "running": False, "registered": False}
    running = _is_own_preview_process(record)
    result = {**record, "running": running, "registered": True}
    if running:
        result["curl_check"] = _curl_localhost(int(record["port"]))
    return result
