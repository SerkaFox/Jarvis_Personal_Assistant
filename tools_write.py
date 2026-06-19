import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import config
from tools_fs import ToolError


MAX_WRITE_CHARS = 120_000
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
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


def _write_mode_enabled() -> bool:
    return config.env_bool("WRITE_MODE_ENABLED", config.WRITE_MODE_ENABLED)


def _write_root() -> Path:
    return config.get_write_root()


def _reject_path_traversal(path: str) -> None:
    if any(part == ".." for part in Path(path).parts):
        raise ToolError(f"Path traversal запрещен: {path}")


def _is_forbidden_write_file(path: Path) -> bool:
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


def _validate_project_name(name: str) -> str:
    cleaned = name.strip()
    if not SAFE_NAME_RE.match(cleaned):
        raise ToolError("Имя проекта может содержать только буквы, цифры, _, -, . и до 80 символов")
    if cleaned.startswith("."):
        raise ToolError("Скрытые project names запрещены")
    return cleaned


def create_project_dir(name: str) -> dict[str, Any]:
    ensure_write_root()
    project_name = _validate_project_name(name)
    path = resolve_write_path(project_name)
    if path.exists():
        raise ToolError(f"Проект уже существует: {path}")
    path.mkdir(parents=True, exist_ok=False)
    _log_write("create_project_dir", path)
    return {"project_name": project_name, "path": str(path), "created": True}


def write_text_file(path: str, content: str, overwrite: bool = False) -> dict[str, Any]:
    ensure_write_root()
    _ensure_text_content(content)
    file_path = resolve_write_path(path)
    if _is_forbidden_write_file(file_path):
        raise ToolError(f"Запись файла запрещена политикой безопасности: {file_path.name}")
    if file_path.exists() and not overwrite:
        raise ToolError(f"Файл уже существует: {file_path}")
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    _log_write("write_text_file", file_path, {"chars": len(content), "overwrite": overwrite})
    return {"path": str(file_path), "chars": len(content), "overwrite": overwrite}


def append_text_file(path: str, content: str) -> dict[str, Any]:
    ensure_write_root()
    _ensure_text_content(content)
    file_path = resolve_write_path(path)
    if _is_forbidden_write_file(file_path):
        raise ToolError(f"Запись файла запрещена политикой безопасности: {file_path.name}")
    existing_size = file_path.stat().st_size if file_path.exists() else 0
    if existing_size + len(content.encode("utf-8")) > MAX_WRITE_CHARS:
        raise ToolError("Итоговый файл будет слишком большим")
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("a", encoding="utf-8") as file:
        file.write(content)
    _log_write("append_text_file", file_path, {"chars": len(content)})
    return {"path": str(file_path), "appended_chars": len(content)}


def list_write_projects() -> dict[str, Any]:
    root_info = ensure_write_root()
    root = Path(root_info["write_root"])
    projects = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if child.is_dir() and not child.name.startswith("."):
            projects.append(
                {
                    "name": child.name,
                    "path": str(child),
                    "is_git_repo": (child / ".git").exists(),
                }
            )
    return {"write_root": str(root), "enabled": True, "projects": projects, "count": len(projects)}


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


def _static_site_files(title: str, description: str, theme: str) -> dict[str, str]:
    accent = "#2563eb" if theme != "emerald" else "#059669"
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
    <nav class="nav">
      <strong>{title}</strong>
      <a href="#contact">Контакты</a>
    </nav>
    <section class="hero__content">
      <p class="eyebrow">Тестовый проект Jarvis</p>
      <h1>{title}</h1>
      <p>{description}</p>
      <a class="button" href="#features">Смотреть</a>
    </section>
  </header>
  <main>
    <section id="features" class="cards">
      <article><span>01</span><h2>Быстро</h2><p>Готовый адаптивный каркас без внешних CDN.</p></article>
      <article><span>02</span><h2>Чисто</h2><p>HTML, CSS и JavaScript разделены по папкам.</p></article>
      <article><span>03</span><h2>Расширяемо</h2><p>Можно использовать как основу для эксперимента.</p></article>
    </section>
    <section id="contact" class="panel">
      <h2>Следующий шаг</h2>
      <p>Открой index.html в браузере или запусти локальный static server внутри проекта.</p>
    </section>
  </main>
  <script src="assets/js/main.js"></script>
</body>
</html>
""",
        "assets/css/style.css": f""":root {{
  color-scheme: light;
  --accent: {accent};
  --ink: #111827;
  --muted: #5b6472;
  --line: #d7dde7;
  --surface: #f7f9fc;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: var(--ink);
  background: #ffffff;
}}
.hero {{
  min-height: 84vh;
  padding: 28px clamp(20px, 6vw, 72px);
  display: grid;
  grid-template-rows: auto 1fr;
  background:
    linear-gradient(135deg, rgba(37, 99, 235, .16), rgba(5, 150, 105, .12)),
    var(--surface);
}}
.nav {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 20px;
}}
.nav a {{ color: var(--ink); text-decoration: none; }}
.hero__content {{
  align-self: center;
  max-width: 780px;
  animation: rise .7s ease both;
}}
.eyebrow {{ color: var(--accent); font-weight: 700; text-transform: uppercase; }}
h1 {{ font-size: clamp(42px, 8vw, 92px); line-height: .95; margin: 0 0 24px; }}
p {{ color: var(--muted); font-size: 18px; line-height: 1.65; }}
.button {{
  display: inline-flex;
  margin-top: 18px;
  padding: 12px 18px;
  border-radius: 8px;
  background: var(--accent);
  color: white;
  text-decoration: none;
  font-weight: 700;
}}
.cards {{
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 18px;
  padding: 56px clamp(20px, 6vw, 72px);
}}
.cards article, .panel {{
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 24px;
}}
.cards span {{ color: var(--accent); font-weight: 800; }}
.panel {{ margin: 0 clamp(20px, 6vw, 72px) 56px; }}
@keyframes rise {{ from {{ opacity: 0; transform: translateY(18px); }} to {{ opacity: 1; transform: translateY(0); }} }}
@media (max-width: 760px) {{
  .cards {{ grid-template-columns: 1fr; }}
  .hero {{ min-height: 76vh; }}
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
""",
        "README.md": f"""# {title}

{description}

## Preview

Open `index.html` in a browser, or run inside this directory:

```bash
python3 -m http.server 8000
```
""",
    }


def write_static_site(project_name: str, title: str = "", description: str = "", theme: str = "blue") -> dict[str, Any]:
    project = create_project_dir(project_name)
    name = project["project_name"]
    site_title = title or name.replace("-", " ").replace("_", " ").title()
    site_description = description or "Современный статический сайт, созданный в безопасной workspace-песочнице."
    created = []
    for relative, content in _static_site_files(site_title, site_description, theme).items():
        result = write_text_file(f"{name}/{relative}", content, overwrite=False)
        created.append(result["path"])
    check = run_safe_project_check(name)
    return {"project_name": name, "path": project["path"], "created_files": created, "check": check}


def _flask_files(project_name: str) -> dict[str, str]:
    title = project_name.replace("-", " ").replace("_", " ").title()
    static = _static_site_files(title, "Минимальный Flask-проект в безопасной workspace-песочнице.", "emerald")
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
        "templates/index.html": static["index.html"].replace('href="assets/css/style.css"', 'href="{{ url_for(\'static\', filename=\'css/style.css\') }}"').replace('src="assets/js/main.js"', 'src="{{ url_for(\'static\', filename=\'js/main.js\') }}"'),
        "static/css/style.css": static["assets/css/style.css"],
        "static/js/main.js": static["assets/js/main.js"],
        "README.md": f"""# {title}

Minimal Flask project created inside Jarvis WRITE_ROOT.

## Run

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python app.py
```

Set `FLASK_DEBUG=1` only for local debugging.
""",
    }


def write_flask_project(project_name: str) -> dict[str, Any]:
    project = create_project_dir(project_name)
    name = project["project_name"]
    created = []
    for relative, content in _flask_files(name).items():
        result = write_text_file(f"{name}/{relative}", content, overwrite=False)
        created.append(result["path"])
    check = run_safe_project_check(name)
    return {"project_name": name, "path": project["path"], "created_files": created, "check": check}


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
        compile_result = subprocess.run(
            ["python3", "-m", "py_compile", *[str(path) for path in py_files]],
            cwd=str(project),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        result["python_compile"] = {
            "returncode": compile_result.returncode,
            "stdout": compile_result.stdout.strip()[:4000],
            "stderr": compile_result.stderr.strip()[:4000],
        }
    return result


def workspace_tree(project_name: str, depth: int = 3) -> dict[str, Any]:
    ensure_write_root()
    project = resolve_write_path(_validate_project_name(project_name))
    if not project.is_dir():
        raise ToolError(f"Проект не найден: {project}")
    depth = max(1, min(int(depth), 5))
    lines = [project.name]

    def walk(directory: Path, current_depth: int, prefix: str) -> None:
        if current_depth >= depth:
            return
        children = [
            child
            for child in sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            if child.name not in {".git", "__pycache__", "venv", ".venv"}
        ][:80]
        for index, child in enumerate(children):
            connector = "`-- " if index == len(children) - 1 else "|-- "
            lines.append(f"{prefix}{connector}{child.name}{'/' if child.is_dir() else ''}")
            if child.is_dir():
                walk(child, current_depth + 1, prefix + ("    " if index == len(children) - 1 else "|   "))

    walk(project, 0, "")
    return {"path": str(project), "tree": "\n".join(lines)}
