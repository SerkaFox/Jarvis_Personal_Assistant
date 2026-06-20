import logging
import os
import py_compile
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from tools_fs import ToolError


MAX_WRITE_CHARS = 180_000
MAX_READ_CHARS = 80_000
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}$")
FORBIDDEN_WRITE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".git-credentials",
    "id_rsa",
    "id_ed25519",
}
FORBIDDEN_WRITE_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".sqlite", ".sqlite3", ".db"}
FORBIDDEN_WRITE_FRAGMENTS = {"secret", "token", "password", "passwd", "credential"}
TREE_EXCLUDED = {".git", "__pycache__", "venv", ".venv"}
RESERVED_WORKSPACE_NAMES = {"data", ".git", "__pycache__", "venv", ".venv"}


def _write_mode_enabled() -> bool:
    return config.env_bool("WRITE_MODE_ENABLED", config.WRITE_MODE_ENABLED)


def _write_root() -> Path:
    return config.get_write_root()


def _reject_path_traversal(path: str) -> None:
    if any(part == ".." for part in Path(path).parts):
        raise ToolError(f"Path traversal запрещен: {path}")


def _validate_project_name(name: str) -> str:
    cleaned = name.strip()
    if not SAFE_NAME_RE.match(cleaned):
        raise ToolError("Имя проекта может содержать только буквы, цифры, _, -, . и до 80 символов")
    if cleaned.startswith("."):
        raise ToolError("Скрытые project names запрещены")
    return cleaned


def _is_forbidden_workspace_file(path: Path) -> bool:
    lowered_name = path.name.lower()
    if lowered_name in FORBIDDEN_WRITE_NAMES:
        return True
    if path.suffix.lower() in FORBIDDEN_WRITE_SUFFIXES:
        return True
    return any(fragment in lowered_name for fragment in FORBIDDEN_WRITE_FRAGMENTS)


def _ensure_text_content(content: str) -> None:
    if not isinstance(content, str):
        raise ToolError("Можно писать только текст")
    if "\x00" in content:
        raise ToolError("Бинарное содержимое запрещено")
    if len(content) > MAX_WRITE_CHARS:
        raise ToolError(f"Содержимое слишком большое: {len(content)} > {MAX_WRITE_CHARS}")


def _ensure_text_file(path: Path) -> None:
    if _is_forbidden_workspace_file(path):
        raise ToolError(f"Операция с файлом запрещена политикой безопасности: {path.name}")
    if path.exists() and path.is_file():
        sample = path.read_bytes()[:4096]
        if b"\x00" in sample:
            raise ToolError(f"Бинарный файл запрещен: {path}")


def _log_write(operation: str, path: Path | None = None, extra: dict[str, Any] | None = None) -> None:
    payload = {"operation": operation, "path": str(path) if path else None, **(extra or {})}
    logging.info("write_tool %s", payload)


def ensure_write_root() -> dict[str, Any]:
    if not _write_mode_enabled():
        raise ToolError("WRITE_MODE_ENABLED=false. Write sandbox выключен.")
    root = _write_root()
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise ToolError(f"WRITE_ROOT не директория: {root}")
    _log_write("ensure_write_root", root)
    return {"write_root": str(root), "enabled": True}


def resolve_write_path(path: str) -> Path:
    if not path:
        raise ToolError("Путь не задан")
    _reject_path_traversal(path)
    root = _write_root()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ToolError(f"Запись вне WRITE_ROOT запрещена: {resolved}")
    return resolved


def list_workspace() -> dict[str, Any]:
    enabled = _write_mode_enabled()
    if enabled:
        root = Path(ensure_write_root()["write_root"])
    else:
        root = _write_root()
    projects = []
    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if child.is_dir() and not child.name.startswith(".") and child.name not in RESERVED_WORKSPACE_NAMES:
                projects.append(
                    {
                        "name": child.name,
                        "path": str(child),
                        "is_git_repo": (child / ".git").exists(),
                        "has_index": (child / "index.html").exists(),
                        "has_flask_app": (child / "app.py").exists(),
                    }
                )
    return {"write_root": str(root), "enabled": enabled, "projects": projects, "count": len(projects)}


def list_write_projects() -> dict[str, Any]:
    return list_workspace()


def create_project_dir(name: str) -> dict[str, Any]:
    ensure_write_root()
    project_name = _validate_project_name(name)
    path = resolve_write_path(project_name)
    if path.exists():
        raise ToolError(f"Проект уже существует: {path}")
    path.mkdir(parents=True, exist_ok=False)
    _log_write("create_project_dir", path)
    return {"project_name": project_name, "path": str(path), "created": True}


def write_text_file(path: str, relative_or_content: str, content: str | None = None, overwrite: bool = False) -> dict[str, Any]:
    ensure_write_root()
    if content is None:
        file_path = resolve_write_path(path)
        text_content = relative_or_content
    else:
        project = _validate_project_name(path)
        _reject_path_traversal(relative_or_content)
        relative = Path(relative_or_content)
        if relative.is_absolute():
            raise ToolError(f"Файл проекта должен быть относительным: {relative_or_content}")
        if any(part.startswith(".") for part in relative.parts):
            raise ToolError(f"Скрытые файлы в workspace-проекте запрещены: {relative_or_content}")
        file_path = resolve_write_path(str(Path(project) / relative))
        text_content = content
    _ensure_text_content(text_content)
    _ensure_text_file(file_path)
    if file_path.exists() and not overwrite:
        raise ToolError(f"Файл уже существует: {file_path}")
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(text_content, encoding="utf-8")
    _log_write("write_text_file", file_path, {"chars": len(text_content), "overwrite": overwrite})
    return {"path": str(file_path), "chars": len(text_content), "overwrite": overwrite}


def write_project_text_file(project_name: str, relative_path: str, content: str, overwrite: bool = False) -> dict[str, Any]:
    return write_text_file(project_name, relative_path, content, overwrite=overwrite)


def append_text_file(path: str, content: str) -> dict[str, Any]:
    ensure_write_root()
    _ensure_text_content(content)
    file_path = resolve_write_path(path)
    _ensure_text_file(file_path)
    existing_size = file_path.stat().st_size if file_path.exists() else 0
    if existing_size + len(content.encode("utf-8")) > MAX_WRITE_CHARS:
        raise ToolError("Итоговый файл будет слишком большим")
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("a", encoding="utf-8") as file:
        file.write(content)
    _log_write("append_text_file", file_path, {"chars": len(content)})
    return {"path": str(file_path), "appended_chars": len(content)}


def read_workspace_file(path: str) -> dict[str, Any]:
    ensure_write_root()
    file_path = resolve_write_path(path)
    if not file_path.is_file():
        raise ToolError(f"Файл не найден: {file_path}")
    _ensure_text_file(file_path)
    text = file_path.read_text(encoding="utf-8")
    return {"path": str(file_path), "content": text[:MAX_READ_CHARS], "truncated": len(text) > MAX_READ_CHARS}


def delete_workspace_file(path: str) -> dict[str, Any]:
    ensure_write_root()
    file_path = resolve_write_path(path)
    if not file_path.is_file():
        raise ToolError(f"Файл не найден: {file_path}")
    _ensure_text_file(file_path)
    file_path.unlink()
    _log_write("delete_workspace_file", file_path)
    return {"path": str(file_path), "deleted": True}


def delete_workspace_dir(path: str, confirm_token: str | None = None) -> dict[str, Any]:
    ensure_write_root()
    cleaned = (path or "").strip()
    if not cleaned or cleaned in {".", "/", ".."} or "/" in cleaned or "\\" in cleaned:
        raise ToolError(f"Недопустимое имя проекта для удаления: {path!r}")
    project_name = _validate_project_name(cleaned)

    root = _write_root().resolve()
    candidate = root / project_name
    if candidate.is_symlink():
        raise ToolError(f"Удаление symlink запрещено: {candidate}")
    directory = candidate.resolve()
    if directory == root or root not in directory.parents:
        raise ToolError(f"Удаление вне WRITE_ROOT запрещено: {directory}")
    if not directory.is_dir():
        raise ToolError(f"Директория не найдена: {directory}")
    expected = f"DELETE:{project_name}"
    if confirm_token != expected:
        raise ToolError(f"Для удаления директории нужен confirm_token={expected}")

    stop_result = None
    try:
        import tools_preview

        stop_result = tools_preview.stop_preview(project_name)
    except ToolError:
        stop_result = None

    shutil.rmtree(directory)
    exists_after = directory.exists()
    deleted = not exists_after
    _log_write("delete_workspace_dir", directory, {"success": deleted})

    result = {
        "path": str(directory),
        "project_name": project_name,
        "deleted": deleted,
        "success": deleted,
        "verification": {"exists_after": exists_after},
    }
    if stop_result is not None:
        result["preview_stop"] = stop_result
    if not deleted:
        result["error"] = f"Директория все еще существует после удаления: {directory}"
    return result


def init_git(path: str) -> dict[str, Any]:
    ensure_write_root()
    repo = resolve_write_path(path)
    if not repo.is_dir():
        raise ToolError(f"Это не директория: {repo}")
    result = subprocess.run(
        ["git", "init"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    _log_write("init_git", repo, {"returncode": result.returncode})
    return {
        "path": str(repo),
        "returncode": result.returncode,
        "stdout": result.stdout.strip()[:4000],
        "stderr": result.stderr.strip()[:4000],
    }


def _theme_values(theme: str | None) -> dict[str, str]:
    themes = {
        "emerald": {"accent": "#059669", "accent2": "#2563eb", "soft": "#ecfdf5"},
        "violet": {"accent": "#7c3aed", "accent2": "#0891b2", "soft": "#f5f3ff"},
        "slate": {"accent": "#0f766e", "accent2": "#334155", "soft": "#f8fafc"},
    }
    return themes.get((theme or "slate").lower(), themes["slate"])


def _static_site_files(title: str, description: str, theme: str | None) -> dict[str, str]:
    values = _theme_values(theme)
    return {
        "index.html": f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <link rel="stylesheet" href="assets/css/style.css">
</head>
<body>
  <header class="hero">
    <nav class="nav" aria-label="Главная навигация">
      <strong>{title}</strong>
      <div>
        <a href="#features">Возможности</a>
        <a href="#workflow">Процесс</a>
        <a href="#contact">Контакт</a>
      </div>
    </nav>
    <section class="hero__content">
      <p class="eyebrow">Telegram / AI Automation</p>
      <h1>{title}</h1>
      <p>{description}</p>
      <div class="hero__actions">
        <a class="button" href="#contact">Запустить проект</a>
        <a class="button button--ghost" href="#use-cases">Сценарии</a>
      </div>
    </section>
  </header>
  <main>
    <section id="features" class="section cards">
      <article><span>01</span><h2>Диалоги</h2><p>Бот отвечает клиентам, уточняет детали и передает заявки команде.</p></article>
      <article><span>02</span><h2>Автоматизация</h2><p>AI-помощник соединяет Telegram, CRM и внутренние процессы.</p></article>
      <article><span>03</span><h2>Контроль</h2><p>Логи, сценарии и настройки остаются под управлением владельца.</p></article>
    </section>
    <section id="workflow" class="section split">
      <div>
        <p class="eyebrow">Workflow</p>
        <h2>От сообщения до готовой заявки</h2>
      </div>
      <ol class="steps">
        <li><strong>Понять запрос</strong><span>AI классифицирует намерение и собирает вводные.</span></li>
        <li><strong>Согласовать действие</strong><span>Бот предлагает следующий шаг без ручной рутины.</span></li>
        <li><strong>Передать результат</strong><span>Команда получает структурированные данные.</span></li>
      </ol>
    </section>
    <section id="use-cases" class="section use-cases">
      <h2>Где применить</h2>
      <div class="case-grid">
        <article><h3>Запись клиентов</h3><p>Салоны, сервисы, консультации и частные специалисты.</p></article>
        <article><h3>Поддержка</h3><p>Первичная линия ответов с маршрутизацией сложных вопросов.</p></article>
        <article><h3>Продажи</h3><p>Квалификация лидов и аккуратная передача менеджеру.</p></article>
      </div>
    </section>
    <section id="contact" class="section cta">
      <h2>Готово к тестовому запуску</h2>
      <p>Этот landing создан Jarvis внутри безопасного WRITE_ROOT и не использует внешние CDN.</p>
      <a class="button" href="mailto:hello@example.local">Обсудить запуск</a>
    </section>
  </main>
  <script src="assets/js/main.js"></script>
</body>
</html>
""",
        "assets/css/style.css": f""":root {{
  color-scheme: light;
  --accent: {values['accent']};
  --accent-2: {values['accent2']};
  --ink: #111827;
  --muted: #566174;
  --line: #d9e0ea;
  --surface: #f8fafc;
  --soft: {values['soft']};
}}
* {{ box-sizing: border-box; }}
html {{ scroll-behavior: smooth; }}
body {{
  margin: 0;
  font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: var(--ink);
  background: #fff;
}}
.hero {{
  min-height: 88vh;
  padding: 28px clamp(20px, 6vw, 76px);
  display: grid;
  grid-template-rows: auto 1fr;
  background:
    radial-gradient(circle at 78% 22%, rgba(15, 118, 110, .18), transparent 28%),
    linear-gradient(135deg, var(--soft), #fff 58%);
  overflow: hidden;
}}
.nav {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 20px;
}}
.nav div {{ display: flex; gap: 18px; flex-wrap: wrap; }}
.nav a {{ color: var(--ink); text-decoration: none; font-weight: 650; }}
.hero__content {{
  align-self: center;
  max-width: 860px;
  animation: rise .7s ease both;
}}
.eyebrow {{
  color: var(--accent);
  font-weight: 800;
  text-transform: uppercase;
  letter-spacing: 0;
}}
h1 {{
  font-size: clamp(44px, 8vw, 94px);
  line-height: .95;
  margin: 0 0 24px;
  max-width: 900px;
}}
h2 {{ font-size: clamp(28px, 4vw, 52px); line-height: 1.05; margin: 0 0 18px; }}
h3 {{ margin-bottom: 8px; }}
p, li span {{ color: var(--muted); font-size: 18px; line-height: 1.65; }}
.hero__actions {{ display: flex; gap: 12px; flex-wrap: wrap; margin-top: 24px; }}
.button {{
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 46px;
  padding: 12px 18px;
  border-radius: 8px;
  background: var(--accent);
  color: white;
  text-decoration: none;
  font-weight: 800;
  transition: transform .2s ease, box-shadow .2s ease;
}}
.button:hover {{ transform: translateY(-2px); box-shadow: 0 14px 30px rgba(15, 23, 42, .14); }}
.button--ghost {{ color: var(--ink); background: white; border: 1px solid var(--line); }}
.section {{ padding: 62px clamp(20px, 6vw, 76px); }}
.cards, .case-grid {{
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 18px;
}}
.cards article, .case-grid article, .cta {{
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 24px;
  background: white;
}}
.cards span {{ color: var(--accent); font-weight: 900; }}
.split {{
  display: grid;
  grid-template-columns: minmax(0, .9fr) minmax(0, 1.1fr);
  gap: 32px;
  background: var(--surface);
}}
.steps {{ margin: 0; padding-left: 22px; }}
.steps li {{ margin-bottom: 18px; }}
.steps strong {{ display: block; margin-bottom: 4px; }}
.use-cases {{ background: white; }}
.cta {{
  margin: 0 clamp(20px, 6vw, 76px) 64px;
  background: linear-gradient(135deg, var(--ink), #233044);
  color: white;
}}
.cta p {{ color: #dbe3ef; }}
@keyframes rise {{ from {{ opacity: 0; transform: translateY(18px); }} to {{ opacity: 1; transform: translateY(0); }} }}
@media (max-width: 820px) {{
  .cards, .case-grid, .split {{ grid-template-columns: 1fr; }}
  .hero {{ min-height: 78vh; }}
  .nav {{ align-items: flex-start; flex-direction: column; }}
}}
@media (prefers-reduced-motion: reduce) {{
  *, *::before, *::after {{ animation-duration: .01ms !important; scroll-behavior: auto !important; transition: none !important; }}
}}
""",
        "assets/js/main.js": """document.querySelectorAll('a[href^="#"]').forEach((link) => {
  link.addEventListener("click", (event) => {
    const target = document.querySelector(link.getAttribute("href"));
    if (!target) return;
    event.preventDefault();
    target.scrollIntoView({ behavior: "smooth", block: "start" });
  });
});

const observer = new IntersectionObserver((entries) => {
  entries.forEach((entry) => {
    if (entry.isIntersecting) entry.target.classList.add("is-visible");
  });
}, { threshold: 0.16 });

document.querySelectorAll(".cards article, .case-grid article, .steps li").forEach((item) => {
  item.classList.add("reveal");
  observer.observe(item);
});
""",
        "README.md": f"""# {title}

{description}

## Preview

Open `index.html` in a browser, or run inside this directory:

```bash
python3 -m http.server 8700
```
""",
    }


def create_static_site(
    project_name: str,
    title: str = "",
    description: str = "",
    theme: str | None = None,
) -> dict[str, Any]:
    project = create_project_dir(project_name)
    name = project["project_name"]
    site_title = title or name.replace("-", " ").replace("_", " ").title()
    site_description = description or "Landing page для Telegram/AI bot автоматизации."
    created = []
    for relative, content in _static_site_files(site_title, site_description, theme).items():
        result = write_text_file(f"{name}/{relative}", content, overwrite=False)
        created.append(result["path"])
    check = run_safe_project_check(name)
    return {"success": True, "project_name": name, "path": project["path"], "created_files": created, "check": check}


def write_static_site(project_name: str, title: str = "", description: str = "", theme: str = "slate") -> dict[str, Any]:
    return create_static_site(project_name, title, description, theme)


def _flask_files(project_name: str, title: str, description: str, theme: str | None) -> dict[str, str]:
    static = _static_site_files(title, description, theme)
    return {
        "app.py": '''import os

from flask import Flask, render_template


app = Flask(__name__)


@app.get("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG") == "1")
''',
        "requirements.txt": "Flask>=3.0,<4.0\n",
        "templates/index.html": static["index.html"]
        .replace('href="assets/css/style.css"', 'href="{{ url_for(\'static\', filename=\'css/style.css\') }}"')
        .replace('src="assets/js/main.js"', 'src="{{ url_for(\'static\', filename=\'js/main.js\') }}"'),
        "static/css/style.css": static["assets/css/style.css"],
        "static/js/main.js": static["assets/js/main.js"],
        "README.md": f"""# {title}

{description}

## Run

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python app.py
```

Set `FLASK_DEBUG=1` only for local debugging.
""",
    }


def create_flask_site(
    project_name: str,
    title: str = "",
    description: str = "",
    theme: str | None = None,
) -> dict[str, Any]:
    project = create_project_dir(project_name)
    name = project["project_name"]
    site_title = title or name.replace("-", " ").replace("_", " ").title()
    site_description = description or "Минимальный Flask-сайт для Telegram/AI bot автоматизации."
    created = []
    for relative, content in _flask_files(name, site_title, site_description, theme).items():
        result = write_text_file(f"{name}/{relative}", content, overwrite=False)
        created.append(result["path"])
    check = run_safe_project_check(name)
    return {"success": True, "project_name": name, "path": project["path"], "created_files": created, "check": check}


def verify_static_site(project_name: str, required_files: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
    project = _validate_project_name(project_name)
    root = resolve_write_path(project)
    required_relative = required_files or ["index.html", "assets/css/style.css", "assets/js/main.js", "README.md"]
    required = [root / relative for relative in required_relative]
    missing = [str(path) for path in required if not path.is_file()]
    return {
        "success": not missing,
        "project_name": project,
        "path": str(root),
        "files": [str(path) for path in required],
        "missing": missing,
    }


def verify_project_files(project_name: str, required_files: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
    return verify_static_site(project_name, required_files=required_files)


def write_flask_project(project_name: str) -> dict[str, Any]:
    return create_flask_site(project_name)


def update_static_site_file(
    project_name: str,
    relative_path: str,
    content: str,
    overwrite: bool = True,
) -> dict[str, Any]:
    project = _validate_project_name(project_name)
    _reject_path_traversal(relative_path)
    return write_text_file(f"{project}/{relative_path}", content, overwrite=overwrite)


def run_safe_project_check(path: str) -> dict[str, Any]:
    ensure_write_root()
    project = resolve_write_path(path)
    if not project.exists():
        raise ToolError(f"Проект не найден: {project}")
    files = [p for p in project.rglob("*") if p.is_file() and ".git" not in p.parts]
    result: dict[str, Any] = {
        "path": str(project),
        "file_count": len(files),
        "python_compile": None,
    }
    py_files = [p for p in files if p.suffix == ".py"][:100]
    if py_files:
        errors = []
        with tempfile.TemporaryDirectory(prefix="jarvis_workspace_py_compile_") as tmp:
            tmp_path = Path(tmp)
            for index, py_file in enumerate(py_files):
                try:
                    py_compile.compile(str(py_file), cfile=str(tmp_path / f"{index}.pyc"), doraise=True)
                except py_compile.PyCompileError as e:
                    errors.append({"path": str(py_file), "error": e.msg[:2000]})
        result["python_compile"] = {"returncode": 1 if errors else 0, "errors": errors}
    return result


def workspace_inventory() -> dict[str, Any]:
    root = _write_root()
    exists = root.is_dir()
    writable = exists and os.access(root, os.W_OK)
    projects: list[dict[str, Any]] = []

    if exists:
        import tools_preview

        registry = tools_preview.get_registry_snapshot()
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir() or child.name.startswith(".") or child.name in RESERVED_WORKSPACE_NAMES:
                continue
            files = [p for p in child.rglob("*") if p.is_file()]
            dirs = [p for p in child.rglob("*") if p.is_dir()]
            required_files = {
                "index.html": (child / "index.html").is_file(),
                "assets/css/style.css": (child / "assets" / "css" / "style.css").is_file(),
                "assets/js/main.js": (child / "assets" / "js" / "main.js").is_file(),
                "README.md": (child / "README.md").is_file(),
            }
            record = registry.get(child.name)
            preview_port = int(record["port"]) if record and record.get("port") else None
            preview_pid = int(record["pid"]) if record and record.get("pid") else None
            port_listening = bool(preview_port) and tools_preview.port_is_listening(preview_port)
            running = bool(record) and tools_preview.is_own_preview_process(record)
            curl_status: Any = None
            url = None
            if preview_port and port_listening:
                curl = tools_preview.curl_check(preview_port)
                curl_status = curl.get("status") if curl.get("success") else (curl.get("error") or "failed")
                url = record.get("url") if record else None

            projects.append(
                {
                    "project_name": child.name,
                    "path": str(child),
                    "exists": True,
                    "files_count": len(files),
                    "dirs_count": len(dirs),
                    "has_index_html": required_files["index.html"],
                    "required_files": required_files,
                    "preview_registered": record is not None,
                    "preview_port": preview_port,
                    "preview_pid": preview_pid,
                    "port_listening": port_listening,
                    "running": running,
                    "curl_status": curl_status,
                    "url": url,
                }
            )

    return {
        "write_root": str(root),
        "exists": exists,
        "writable": writable,
        "projects": projects,
        "count": len(projects),
    }


def workspace_tree(path: str | None = None, depth: int = 3) -> dict[str, Any]:
    ensure_write_root()
    root = _write_root().resolve()
    target = resolve_write_path(path) if path else root
    if not target.is_dir():
        raise ToolError(f"Директория не найдена: {target}")
    depth = max(1, min(int(depth), 5))
    lines = [target.name or str(target)]

    def walk(directory: Path, current_depth: int, prefix: str) -> None:
        if current_depth >= depth:
            return
        children = [
            child
            for child in sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            if child.name not in TREE_EXCLUDED and not child.name.startswith(".")
        ][:80]
        for index, child in enumerate(children):
            connector = "`-- " if index == len(children) - 1 else "|-- "
            lines.append(f"{prefix}{connector}{child.name}{'/' if child.is_dir() else ''}")
            if child.is_dir():
                walk(child, current_depth + 1, prefix + ("    " if index == len(children) - 1 else "|   "))

    walk(target, 0, "")
    return {"path": str(target), "tree": "\n".join(lines)}


def tree_workspace_project(project_name: str, depth: int = 3) -> dict[str, Any]:
    """Read-only ASCII tree of WRITE_ROOT/<project>, same exclusions as
    workspace_tree (.git/venv/__pycache__/hidden dirs)."""
    project = _validate_project_name(project_name)
    root = resolve_write_path(project).resolve()
    if not root.is_dir():
        raise ToolError(f"Проект не найден в WRITE_ROOT: {root}")
    result = workspace_tree(project, depth=depth)
    return {"project_name": project, **result}


def list_workspace_project_files(project_name: str, depth: int = 3) -> dict[str, Any]:
    """Read-only: lists real files inside WRITE_ROOT/<project> up to `depth`
    levels -- relative path, size in bytes, last-modified UTC timestamp.
    Excludes .git/venv/__pycache__/hidden dirs and any db/secret/key/token
    files via the same policy as the write tools. Never invents files --
    an empty/missing subdirectory just yields an empty list."""
    ensure_write_root()
    project = _validate_project_name(project_name)
    root = resolve_write_path(project).resolve()
    if not root.is_dir():
        raise ToolError(f"Проект не найден в WRITE_ROOT: {root}")
    depth = max(1, min(int(depth or 3), 6))

    files: list[dict[str, Any]] = []

    def walk(directory: Path, current_depth: int) -> None:
        if current_depth > depth:
            return
        try:
            children = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        for child in children:
            if child.name in TREE_EXCLUDED or child.name.startswith("."):
                continue
            if child.is_dir():
                walk(child, current_depth + 1)
            elif child.is_file():
                if _is_forbidden_workspace_file(child):
                    continue
                try:
                    stat = child.stat()
                except OSError:
                    continue
                files.append(
                    {
                        "path": str(child.relative_to(root)),
                        "bytes": stat.st_size,
                        "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                        .isoformat(timespec="seconds")
                        .replace("+00:00", "Z"),
                    }
                )

    walk(root, 1)
    files.sort(key=lambda f: f["path"])
    return {"project_name": project, "root": str(root), "depth": depth, "files": files, "count": len(files)}
