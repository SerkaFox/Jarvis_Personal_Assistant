"""Controlled self-improvement: when no existing workflow/plugin can handle a
request, Jarvis can propose a new plugin, sandbox it, run safety checks, and
-- only after those checks pass and (depending on SELFDEV_MODE) the user
explicitly confirms -- install it into plugins/ and tests/.

Pipeline: propose_plugin -> write_plugin_to_sandbox -> run_selfdev_checks ->
install_plugin (or rollback_selfdev if something goes wrong after install).

Everything before install_plugin only ever touches
data/proposed_plugins/<job_id>/ (see config.get_selfdev_proposed_dir()).
install_plugin only ever writes inside config.SELFDEV_ALLOWED_WRITE_PREFIXES
(plugins/, tests/, skills/, docs/selfdev/) under the project root -- never
.env, bot.py, config.py, venv/, models/, data/*.db, or anything outside the
project. There is no code path here that runs arbitrary shell commands;
git/systemctl are the only subprocess calls, with fixed argument lists (no
shell=True), and both accept an injectable dry-run / mock path for testing.
"""

import ast
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import requests

import config
import plugin_manager
from action_schemas import extract_json_object
from tools_fs import ToolError


OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.0.145:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")

FORBIDDEN_IMPORT_MODULES = {"subprocess", "socket", "ctypes", "pty", "telnetlib", "ftplib"}
FORBIDDEN_CALL_PATTERNS = {
    ("os", "system"),
    ("os", "popen"),
    ("os", "execv"),
    ("os", "execve"),
    ("os", "execl"),
    ("shutil", "rmtree"),
}
NETWORK_MODULES = {"requests", "urllib", "http", "httpx", "aiohttp", "socket"}


def _now_iso() -> str:
    return datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ollama_chat(messages: list[dict[str, str]]) -> str:
    payload = {"model": OLLAMA_MODEL, "stream": False, "messages": messages}
    r = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=180)
    r.raise_for_status()
    return r.json()["message"]["content"]


def new_job_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]


# --------------------------------------------------------------------------
# spec validation
# --------------------------------------------------------------------------

def _validate_relative_plugin_path(path: str) -> str:
    if not isinstance(path, str) or not path.strip():
        raise ToolError("file.path должен быть непустой строкой")
    cleaned = path.strip().replace("\\", "/").lstrip("/")
    candidate = Path(cleaned)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise ToolError(f"Недопустимый путь сгенерированного файла: {path}")
    if not any(cleaned.startswith(prefix) for prefix in config.SELFDEV_ALLOWED_WRITE_PREFIXES):
        raise ToolError(
            f"Путь {path} вне разрешённых директорий selfdev "
            f"({', '.join(config.SELFDEV_ALLOWED_WRITE_PREFIXES)})"
        )
    return cleaned


def validate_plugin_spec(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ToolError("plugin spec должен быть JSON объектом")
    plugin_name = str(data.get("plugin_name") or "").strip()
    if not plugin_name or not all(c.isalnum() or c in "_-" for c in plugin_name):
        raise ToolError("plugin_name обязателен и должен быть простым идентификатором (буквы/цифры/_/-)")
    description = str(data.get("description") or "").strip()
    if not description:
        raise ToolError("description обязателен")

    files = data.get("files")
    if not isinstance(files, list) or not files:
        raise ToolError("files должен быть непустым массивом")
    normalized_files: list[dict[str, str]] = []
    seen: set[str] = set()
    has_plugin_file = False
    has_test_file = False
    for item in files:
        if not isinstance(item, dict):
            raise ToolError("Каждый files item должен быть объектом")
        relative_path = _validate_relative_plugin_path(item.get("path", ""))
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ToolError(f"content обязателен для {relative_path}")
        if "\x00" in content:
            raise ToolError(f"Бинарное содержимое запрещено: {relative_path}")
        if relative_path in seen:
            raise ToolError(f"Дубликат файла в plugin spec: {relative_path}")
        seen.add(relative_path)
        if relative_path.startswith("plugins/") and relative_path.endswith(".py"):
            has_plugin_file = True
        if relative_path.startswith("tests/") and relative_path.endswith(".py"):
            has_test_file = True
        normalized_files.append({"path": relative_path, "content": content})
    if not has_plugin_file:
        raise ToolError("spec не содержит ни одного файла plugins/<name>.py")
    if not has_test_file:
        raise ToolError("spec не содержит ни одного файла tests/test_<name>.py")

    capabilities = data.get("capabilities")
    capabilities = [str(c) for c in capabilities][:20] if isinstance(capabilities, list) else []
    risks = data.get("risks")
    risks = [str(r) for r in risks][:20] if isinstance(risks, list) else []
    tests = data.get("tests")
    tests = [str(t) for t in tests][:20] if isinstance(tests, list) else []

    return {
        "plugin_name": plugin_name,
        "description": description[:500],
        "files": normalized_files,
        "capabilities": capabilities,
        "risks": risks,
        "tests": tests,
    }


def _looks_like_fake_codegen_response(text: str) -> bool:
    lowered = (text or "").lower()
    return any(
        marker in lowered
        for marker in ("```", "mkdir ", "cat >", "cat <<", "sudo ", "git clone", "pip install")
    )


SAFE_CODEGEN_SYSTEM_PROMPT = (
    "Ты генератор безопасных JSON plugin specs для Jarvis self-improvement. "
    "Возвращай только валидный JSON, без markdown, без ``` оберток, без пояснений вне JSON."
)


def propose_plugin(user_text: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Asks Ollama for a strict-JSON plugin spec for a task no existing
    workflow/plugin can handle. Returns the validated spec. Does not write
    anything to disk -- see write_plugin_to_sandbox for that."""
    context = context or {}
    prompt = f"""
Верни только JSON. Не пиши markdown. Не используй ``` обертки. Не пиши bash-команды.
Не используй sudo, subprocess, network запросы к произвольным внешним доменам, абсолютные пути.
Не предлагай менять файлы вне plugins/ и tests/.

Jarvis -- Telegram-бот на python-telegram-bot (см. bot.py). Пользователь просит задачу,
для которой нет существующего workflow. Нужно описать новый safe plugin.

Строгий формат:
{{
  "plugin_name": "snake_case_name",
  "description": "что плагин умеет, по-русски, коротко",
  "files": [
    {{"path": "plugins/snake_case_name.py", "content": "полное содержимое файла"}},
    {{"path": "tests/test_snake_case_name.py", "content": "полное содержимое теста"}}
  ],
  "capabilities": ["короткие теги возможностей"],
  "risks": ["короткие заметки о рисках, если есть"],
  "tests": ["краткое описание, что проверяет test-файл"]
}}

plugins/snake_case_name.py ОБЯЗАН определять:
  PLUGIN_NAME = "snake_case_name"
  PLUGIN_VERSION = "0.1.0"
  PLUGIN_DESCRIPTION = "..."
  def can_handle(user_text: str, context: dict) -> float:  # 0.0..1.0, чисто синтаксическая/семантическая оценка
  async def handle(update, context, parsed_task: dict) -> dict:  # делает работу, возвращает {{"success": bool, ...}}
  def smoke_tests() -> list:  # список пар (name, callable), без сети и без записи вне допустимых путей

Запрещено в любом сгенерированном файле:
  os.system, subprocess, shutil.rmtree, socket, requests/urllib к произвольным внешним доменам
  (если без них не обойтись -- добавь "network" в capabilities и объясни в risks),
  open() с абсолютными путями или путями содержащими "..", eval, exec, __import__, sudo, любые shell-команды.

Контекст:
project_name = {context.get('project_name') or '-'}
has_pending_media = {bool(context.get('has_pending_media'))}

Задача пользователя:
{user_text}
""".strip()
    messages = [
        {"role": "system", "content": SAFE_CODEGEN_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    last_error: Exception | None = None
    for _attempt in range(2):
        raw = _ollama_chat(messages)
        try:
            if _looks_like_fake_codegen_response(raw):
                raise ToolError("Ollama вернула markdown/shell вместо JSON plugin spec")
            data = extract_json_object(raw)
            return validate_plugin_spec(data)
        except ToolError as exc:
            last_error = exc
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {"role": "user", "content": "Невалидный ответ. Верни только JSON без markdown, без пояснений, без ``` оберток."}
            )
    raise last_error or ToolError("Не удалось получить валидный plugin spec от Ollama")


def generate_plugin_code(plugin_spec: dict[str, Any]) -> list[dict[str, str]]:
    """Pure function: re-validates and extracts the files list from an
    already-built spec. Kept separate from propose_plugin so file generation
    is testable without mocking Ollama."""
    return validate_plugin_spec(plugin_spec)["files"]


# --------------------------------------------------------------------------
# sandbox + job report bookkeeping
# --------------------------------------------------------------------------

def _job_dir(job_id: str) -> Path:
    if not job_id or not all(c.isalnum() or c in "_-" for c in job_id):
        raise ToolError(f"Недопустимый job_id: {job_id!r}")
    return config.get_selfdev_proposed_dir() / job_id


def _report_path(job_id: str) -> Path:
    if not job_id or not all(c.isalnum() or c in "_-" for c in job_id):
        raise ToolError(f"Недопустимый job_id: {job_id!r}")
    return config.get_selfdev_jobs_dir() / job_id / "report.json"


def _write_report(job_id: str, report: dict[str, Any]) -> None:
    path = _report_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def get_job_report(job_id: str) -> dict[str, Any] | None:
    path = _report_path(job_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def list_jobs() -> list[dict[str, Any]]:
    jobs_dir = config.get_selfdev_jobs_dir()
    jobs = []
    for entry in sorted(jobs_dir.glob("*/report.json")):
        try:
            jobs.append(json.loads(entry.read_text(encoding="utf-8")))
        except Exception:
            continue
    return jobs


def write_plugin_to_sandbox(job_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Writes spec.json + every generated file into
    data/proposed_plugins/<job_id>/files/ ONLY. Never touches the real
    plugins/ or tests/ directories -- that only happens in install_plugin
    after checks pass and the user explicitly confirms."""
    validated = validate_plugin_spec(spec)
    job_dir = _job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "spec.json").write_text(json.dumps(validated, ensure_ascii=False, indent=2), encoding="utf-8")
    for file_item in validated["files"]:
        dest = (job_dir / "files" / file_item["path"]).resolve()
        if (job_dir / "files").resolve() not in dest.parents:
            raise ToolError(f"Сгенерированный файл вне sandbox: {file_item['path']}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(file_item["content"], encoding="utf-8")
    report = {
        "job_id": job_id,
        "plugin_name": validated["plugin_name"],
        "description": validated["description"],
        "status": "proposed",
        "step": "written_to_sandbox",
        "files": [f["path"] for f in validated["files"]],
        "capabilities": validated["capabilities"],
        "risks": validated["risks"],
        "checks": {},
        "errors": [],
        "created_at": _now_iso(),
    }
    _write_report(job_id, report)
    return report


# --------------------------------------------------------------------------
# static safety scan
# --------------------------------------------------------------------------

def _scan_python_source(source: str, *, allow_network: bool) -> list[str]:
    """AST-based static scan. Returns a list of human-readable violations
    (empty == clean). This is one layer among several (allowlisted install
    paths, py_compile, unit tests, explicit user confirmation) -- not a full
    sandbox, but enough to catch the obvious dangerous patterns the spec
    calls out by name."""
    violations: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"SyntaxError: {exc}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_mod = alias.name.split(".")[0]
                if root_mod in FORBIDDEN_IMPORT_MODULES:
                    violations.append(f"запрещённый import {alias.name}")
                elif root_mod in NETWORK_MODULES and not allow_network:
                    violations.append(f"network import {alias.name} без capabilities=network")
        elif isinstance(node, ast.ImportFrom):
            root_mod = (node.module or "").split(".")[0]
            if root_mod in FORBIDDEN_IMPORT_MODULES:
                violations.append(f"запрещённый import {node.module}")
            elif root_mod in NETWORK_MODULES and not allow_network:
                violations.append(f"network import {node.module} без capabilities=network")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in {"eval", "exec", "__import__"}:
                violations.append(f"запрещённый вызов {func.id}()")
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                pair = (func.value.id, func.attr)
                if pair in FORBIDDEN_CALL_PATTERNS:
                    violations.append(f"запрещённый вызов {pair[0]}.{pair[1]}()")
            if isinstance(func, ast.Name) and func.id == "open" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    raw_path = first.value
                    if raw_path.startswith("/") or ".." in raw_path:
                        violations.append(f"open() с недопустимым путём: {raw_path}")
    return violations


def run_selfdev_checks(job_id: str) -> dict[str, Any]:
    """Runs every safety check against the sandboxed job and overwrites
    report.json with the fresh result. install_plugin always calls this
    again itself rather than trusting a stale report."""
    job_dir = _job_dir(job_id)
    spec_path = job_dir / "spec.json"
    if not spec_path.is_file():
        raise ToolError(f"Job {job_id} не найден в sandbox")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    allow_network = "network" in (spec.get("capabilities") or [])

    checks: dict[str, Any] = {}
    errors: list[str] = []

    try:
        validate_plugin_spec(spec)
        checks["spec_valid"] = True
    except ToolError as exc:
        checks["spec_valid"] = False
        errors.append(str(exc))

    paths_ok = True
    for file_item in spec.get("files", []):
        try:
            _validate_relative_plugin_path(file_item["path"])
        except ToolError as exc:
            paths_ok = False
            errors.append(str(exc))
    checks["paths_allowed"] = paths_ok

    forbidden_findings: dict[str, list[str]] = {}
    for file_item in spec.get("files", []):
        if file_item["path"].endswith(".py"):
            violations = _scan_python_source(file_item["content"], allow_network=allow_network)
            if violations:
                forbidden_findings[file_item["path"]] = violations
    checks["no_forbidden_imports"] = not forbidden_findings
    for path, violations in forbidden_findings.items():
        errors.append(f"{path}: " + "; ".join(violations))

    with tempfile.TemporaryDirectory(prefix=f"jarvis_selfdev_{job_id}_") as tmp:
        tmp_root = Path(tmp)
        plugins_tmp = tmp_root / "plugins"
        tests_tmp = tmp_root / "tests"
        plugins_tmp.mkdir(parents=True, exist_ok=True)
        tests_tmp.mkdir(parents=True, exist_ok=True)
        for file_item in spec.get("files", []):
            dest = tmp_root / file_item["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(file_item["content"], encoding="utf-8")

        compile_ok = True
        for file_item in spec.get("files", []):
            if file_item["path"].endswith(".py"):
                dest = tmp_root / file_item["path"]
                result = subprocess.run(
                    [sys.executable, "-m", "py_compile", str(dest)],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    compile_ok = False
                    errors.append(f"py_compile {file_item['path']}: {result.stderr.strip()}")
        checks["py_compile"] = compile_ok

        import_ok = False
        tests_ok = False
        if compile_ok and paths_ok and checks["no_forbidden_imports"]:
            try:
                plugin_modules, load_errors = plugin_manager.load_plugins(plugins_tmp)
                plugin_name = spec.get("plugin_name")
                match = next((m for m in plugin_modules if getattr(m, "PLUGIN_NAME", None) == plugin_name), None)
                if match is None:
                    errors.append(
                        "plugin_manager не смог загрузить/провалидировать сгенерированный plugin: "
                        + json.dumps(load_errors, ensure_ascii=False)
                    )
                else:
                    import_ok = True
            except Exception as exc:
                errors.append(f"import plugin failed: {exc}")

            test_files = [f for f in spec.get("files", []) if f["path"].startswith("tests/") and f["path"].endswith(".py")]
            if test_files:
                env = dict(os.environ)
                env["PYTHONPATH"] = str(tmp_root) + os.pathsep + env.get("PYTHONPATH", "")
                try:
                    result = subprocess.run(
                        [sys.executable, "-m", "unittest", "discover", "-s", str(tests_tmp), "-p", "test_*.py"],
                        capture_output=True, text=True, cwd=str(tmp_root), env=env, timeout=120,
                    )
                    tests_ok = result.returncode == 0
                    if not tests_ok:
                        errors.append("unittest: " + ((result.stderr or result.stdout) or "")[-2000:])
                except subprocess.TimeoutExpired:
                    errors.append("unittest: timeout")
        checks["plugin_import_ok"] = import_ok
        checks["tests_ok"] = tests_ok

    overall_success = all(
        checks.get(key) for key in ("spec_valid", "paths_allowed", "no_forbidden_imports", "py_compile", "plugin_import_ok", "tests_ok")
    )
    report = get_job_report(job_id) or {"job_id": job_id, "plugin_name": spec.get("plugin_name")}
    report.update(
        {
            "status": "checked",
            "step": "checks_complete",
            "checks": checks,
            "errors": errors,
            "success": overall_success,
            "checked_at": _now_iso(),
        }
    )
    _write_report(job_id, report)
    return report


# --------------------------------------------------------------------------
# install / rollback
# --------------------------------------------------------------------------

def _run_git(args: list[str], *, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return {"dry_run": True, "args": args}
    result = subprocess.run(["git", *args], capture_output=True, text=True, cwd=str(config.PROJECT_ROOT))
    return {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def _current_git_commit() -> str | None:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(config.PROJECT_ROOT))
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _sudo_n(args: list[str], *, timeout: int = 30) -> dict[str, Any]:
    """Runs `sudo -n <args>` -- the -n flag makes sudo fail immediately with
    a clear stderr message ("a password is required") instead of ever
    prompting for/blocking on a password. No password is ever read, stored,
    or logged anywhere in this codebase; if NOPASSWD isn't configured for
    this command in sudoers, the caller is told so honestly instead of the
    process hanging or silently doing nothing."""
    try:
        result = subprocess.run(["sudo", "-n", *args], capture_output=True, text=True, timeout=timeout)
        return {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    except Exception as exc:
        return {"returncode": -1, "stdout": "", "stderr": str(exc)}


def _default_restart() -> dict[str, Any]:
    result = _sudo_n(["systemctl", "restart", "jarvis-bot"])
    if result["returncode"] != 0 and "password is required" in (result.get("stderr") or ""):
        result["stderr"] = (
            "Нужен sudoers NOPASSWD для 'systemctl restart jarvis-bot' -- "
            "без него self-improvement не может перезапустить сервис автоматически."
        )
    return result


def _bot_defines_status_handler() -> bool:
    try:
        source = (config.PROJECT_ROOT / "bot.py").read_text(encoding="utf-8")
    except Exception:
        return False
    return 'CommandHandler("status"' in source


def _health_check(plugin_name: str) -> dict[str, Any]:
    """Post-install/restart health check: service active, bot.py still
    compiles, plugin_manager can load the newly installed plugin, the
    journal shows the app actually started, and the /status command handler
    is still present. Uses `sudo -n` for systemctl/journalctl (see _sudo_n);
    if NOPASSWD isn't configured, those specific checks are reported as
    failed with an honest reason rather than hanging."""
    checks: dict[str, bool] = {}
    errors: list[str] = []

    active = _sudo_n(["systemctl", "is-active", "jarvis-bot"])
    checks["service_active"] = active.get("returncode") == 0 and "active" in (active.get("stdout") or "")
    if not checks["service_active"]:
        errors.append("systemctl is-active jarvis-bot: " + (active.get("stderr") or active.get("stdout") or "не active").strip())

    compile_result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(config.PROJECT_ROOT / "bot.py")], capture_output=True, text=True
    )
    checks["bot_py_compiles"] = compile_result.returncode == 0
    if not checks["bot_py_compiles"]:
        errors.append("bot.py py_compile: " + compile_result.stderr.strip())

    try:
        modules, load_errors = plugin_manager.load_plugins()
        match = next((m for m in modules if getattr(m, "PLUGIN_NAME", None) == plugin_name), None)
        checks["plugin_loads"] = match is not None
        if not checks["plugin_loads"]:
            errors.append(f"plugin_manager не смог загрузить {plugin_name}: {load_errors}")
    except Exception as exc:
        checks["plugin_loads"] = False
        errors.append(f"plugin_manager.load_plugins() упал: {exc}")

    journal = _sudo_n(["journalctl", "-u", "jarvis-bot", "-n", "80", "--no-pager"])
    checks["journal_started"] = "Application started" in (journal.get("stdout") or "")
    if not checks["journal_started"]:
        errors.append("journalctl: 'Application started' не найдено в последних 80 строках (" + (journal.get("stderr") or "").strip() + ")")

    checks["status_handler_present"] = _bot_defines_status_handler()
    if not checks["status_handler_present"]:
        errors.append("bot.py больше не регистрирует /status handler")

    return {"success": all(checks.values()), "checks": checks, "errors": errors}


def install_plugin(
    job_id: str,
    *,
    dry_run: bool = False,
    restart_fn: Callable[[], dict[str, Any]] | None = None,
    health_check_fn: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Installs a sandboxed, freshly re-checked plugin into the real
    plugins/ and tests/ directories. Refuses if SELFDEV_MODE is off, if
    checks fail, or if any file path falls outside
    config.SELFDEV_ALLOWED_WRITE_PREFIXES. After restart, runs a health
    check (service active, bot.py compiles, plugin loads, journal shows
    startup, /status handler present); if either the restart or the health
    check fails, automatically rolls back to the pre-install commit and
    restarts again. restart_fn/health_check_fn are injectable so tests (and
    dry runs) never touch the real systemd service."""
    mode = config.get_selfdev_mode()
    if mode == "off":
        raise ToolError("SELFDEV_MODE=off -- self-improvement выключен")

    report = run_selfdev_checks(job_id)
    if not report.get("success"):
        raise ToolError("Проверки не пройдены, установка отменена: " + "; ".join(report.get("errors") or []))

    job_dir = _job_dir(job_id)
    spec = json.loads((job_dir / "spec.json").read_text(encoding="utf-8"))

    pre_install_commit = _current_git_commit()
    installed_paths: list[str] = []
    for file_item in spec["files"]:
        relative_path = _validate_relative_plugin_path(file_item["path"])
        dest = (config.PROJECT_ROOT / relative_path).resolve()
        if config.PROJECT_ROOT not in dest.parents:
            raise ToolError(f"Установка вне проекта запрещена: {relative_path}")
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(file_item["content"], encoding="utf-8")
        installed_paths.append(relative_path)

    git_add = _run_git(["add", *installed_paths], dry_run=dry_run)
    commit_message = f"Add self-generated plugin {spec['plugin_name']}"
    git_commit = _run_git(["commit", "-m", commit_message], dry_run=dry_run)

    restart = {"dry_run": True} if dry_run else (restart_fn or _default_restart)()
    restart_ok = dry_run or restart.get("returncode") == 0

    health = {"dry_run": True, "success": True} if dry_run else (health_check_fn or _health_check)(spec["plugin_name"])
    health_ok = dry_run or bool(health.get("success"))

    success = restart_ok and health_ok
    report.update(
        {
            "status": "installed" if success else "install_failed",
            "step": "installed" if success else "rollback",
            "installed_paths": installed_paths,
            "pre_install_commit": pre_install_commit,
            "git_add": git_add,
            "git_commit": git_commit,
            "restart": restart,
            "health_check": health,
            "installed_at": _now_iso(),
        }
    )
    _write_report(job_id, report)

    if not success:
        rollback_report = rollback_selfdev(job_id, dry_run=dry_run, restart_fn=restart_fn)
        # Re-read the report rollback_selfdev() just wrote (status=rolled_back,
        # last_rollback=...) instead of re-saving our stale local copy, which
        # would otherwise clobber that update back to "install_failed".
        report = get_job_report(job_id) or report
        report["rollback"] = rollback_report
        _write_report(job_id, report)
        reason = restart.get("stderr") if not restart_ok else "; ".join(health.get("errors") or [])
        raise ToolError(f"Установка не прошла проверку после рестарта, выполнен rollback: {reason}")

    return report


def dry_run_plugin(job_id: str, test_prompt: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Loads the sandboxed (not-yet-installed) plugin straight from
    data/proposed_plugins/<job_id>/files/plugins/ and reports its
    can_handle() score plus a parsed-task preview. Never calls handle() and
    never writes anything -- this is purely a before-you-install preview."""
    job_dir = _job_dir(job_id)
    spec_path = job_dir / "spec.json"
    if not spec_path.is_file():
        raise ToolError(f"Job {job_id} не найден в sandbox")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    plugins_dir = job_dir / "files" / "plugins"
    modules, errors = plugin_manager.load_plugins(plugins_dir)
    plugin_name = spec.get("plugin_name")
    match = next((m for m in modules if getattr(m, "PLUGIN_NAME", None) == plugin_name), None)
    if match is None:
        raise ToolError(f"Не удалось загрузить plugin {plugin_name} из sandbox: {errors}")
    ctx = context or {}
    try:
        score = float(match.can_handle(test_prompt, ctx))
    except Exception as exc:
        raise ToolError(f"can_handle() упал: {exc}")
    return {
        "job_id": job_id,
        "plugin_name": plugin_name,
        "test_prompt": test_prompt,
        "can_handle_score": max(0.0, min(1.0, score)),
        "parsed_task": {"user_text": test_prompt, **ctx},
        "planned": "handle() не вызывался (dry-run) -- реальных изменений не было.",
    }


def rollback_selfdev(
    job_id: str,
    *,
    dry_run: bool = False,
    restart_fn: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    report = get_job_report(job_id) or {}
    pre_commit = report.get("pre_install_commit")
    if not pre_commit:
        raise ToolError(f"Нет сохранённого pre_install_commit для job {job_id}, rollback невозможен")
    reset = _run_git(["reset", "--hard", pre_commit], dry_run=dry_run)
    restart = {"dry_run": True} if dry_run else (restart_fn or _default_restart)()
    rollback_report = {
        "job_id": job_id,
        "reset": reset,
        "restart": restart,
        "rolled_back_to": pre_commit,
        "rolled_back_at": _now_iso(),
    }
    report["status"] = "rolled_back"
    report["last_rollback"] = rollback_report
    _write_report(job_id, report)
    return rollback_report
