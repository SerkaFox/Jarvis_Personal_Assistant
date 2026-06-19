import py_compile
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from tools_fs import search_text
from tools_git import git_status, resolve_repo
from tools_project import PROJECT_EXCLUDED_DIRS


MAX_PY_FILES = 300
MAX_OUTPUT_CHARS = 8000
SECRET_WORDS = ("token", "api_key", "apikey", "password", "passwd", "secret", "authorization", "bearer")


def _safe_text(text: str | None, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    if not text:
        return ""
    lines = []
    for line in text.splitlines():
        lowered = line.lower()
        if any(word in lowered for word in SECRET_WORDS):
            lines.append("[masked secret-like line]")
        else:
            lines.append(line)
    return "\n".join(lines)[:max_chars]


def _run_readonly(command: list[str], cwd: Path, timeout: int = 60) -> dict[str, Any]:
    result = subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return {
        "command": " ".join(command),
        "returncode": result.returncode,
        "stdout": _safe_text(result.stdout),
        "stderr": _safe_text(result.stderr),
    }


def _is_excluded(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    return any(part in PROJECT_EXCLUDED_DIRS for part in parts)


def _python_files(root: Path) -> list[Path]:
    files = []
    for path in root.rglob("*.py"):
        if _is_excluded(path, root):
            continue
        files.append(path)
        if len(files) >= MAX_PY_FILES:
            break
    return files


def detect_project_type(repo_path: str) -> dict[str, Any]:
    repo = resolve_repo(repo_path)
    py_files = _python_files(repo)
    package_json = repo / "package.json"
    if (repo / "manage.py").is_file():
        project_type = "django"
    elif package_json.is_file():
        project_type = "node"
    elif py_files:
        project_type = "python"
    else:
        project_type = "unknown"
    return {
        "path": str(repo),
        "project_type": project_type,
        "has_manage_py": (repo / "manage.py").is_file(),
        "has_package_json": package_json.is_file(),
        "python_file_count_sample": len(py_files),
        "python_file_limit": MAX_PY_FILES,
    }


def _py_compile_check(repo: Path) -> dict[str, Any]:
    errors = []
    files = _python_files(repo)
    with tempfile.TemporaryDirectory(prefix="jarvis_py_compile_") as tmp:
        tmp_path = Path(tmp)
        for index, path in enumerate(files):
            try:
                py_compile.compile(str(path), cfile=str(tmp_path / f"{index}.pyc"), doraise=True)
            except py_compile.PyCompileError as e:
                errors.append(
                    {
                        "path": str(path),
                        "error": _safe_text(e.msg, max_chars=2000),
                    }
                )
                if len(errors) >= 20:
                    break
    return {
        "checked_files": len(files),
        "limit": MAX_PY_FILES,
        "ok": not errors,
        "errors": errors,
    }


def _node_summary(repo: Path) -> dict[str, Any]:
    package_json = repo / "package.json"
    scripts: dict[str, Any] = {}
    if package_json.is_file():
        try:
            import json

            data = json.loads(package_json.read_text(encoding="utf-8"))
            raw_scripts = data.get("scripts")
            if isinstance(raw_scripts, dict):
                scripts = {str(key): str(value) for key, value in raw_scripts.items()}
        except Exception as e:
            scripts = {"error": str(e)}
    return {
        "package_json": str(package_json) if package_json.is_file() else "",
        "scripts": scripts,
        "note": "npm install/test не запускались",
    }


def safe_code_check(repo_name_or_path: str) -> dict[str, Any]:
    repo = resolve_repo(repo_name_or_path)
    project_type = detect_project_type(str(repo))
    status_before = git_status(str(repo))
    checks: dict[str, Any] = {
        "git_status": status_before,
        "project_type": project_type,
    }

    if project_type["project_type"] in {"django", "python"}:
        checks["py_compile"] = _py_compile_check(repo)

    if project_type["project_type"] == "django":
        venv_python = repo / "venv" / "bin" / "python"
        if venv_python.is_file() and (repo / "manage.py").is_file():
            checks["django_check"] = _run_readonly(
                [str(venv_python), "manage.py", "check"],
                cwd=repo,
                timeout=60,
            )
        else:
            checks["django_check"] = {
                "skipped": True,
                "reason": "venv/bin/python или manage.py не найден",
            }

    if project_type["project_type"] == "node":
        checks["node"] = _node_summary(repo)

    try:
        checks["todos"] = search_text(str(repo), "TODO|FIXME|BUG|HACK")
    except Exception as e:
        checks["todos"] = {"error": str(e)}

    status_after = git_status(str(repo))
    return {
        "project_name": repo.name,
        "path": str(repo),
        "checks": checks,
        "git_status_after": status_after,
        "git_status_unchanged": status_before.get("status_short") == status_after.get("status_short"),
    }
