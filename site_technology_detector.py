"""Detects which stack a workspace project's source files look like
(static_html/php/python_django/flask/react/vue/unknown) -- purely for
reporting context ("Технология: php" in a verification report). This is
deliberately NOT consulted by ui_component_verifier: the verifier only ever
looks at the rendered page, so a component check works identically on a
static HTML project, a PHP project, or a Django template, as long as the
served HTML is reachable.
"""
import json
from pathlib import Path
from typing import Any

from tools_write import _validate_project_name, ensure_write_root, resolve_write_path

TECHNOLOGIES = (
    "python_django",
    "flask",
    "react",
    "vue",
    "php",
    "static_html",
    "unknown",
)


def detect_technology(project_name: str) -> dict[str, Any]:
    ensure_write_root()
    project = _validate_project_name(project_name)
    root = resolve_write_path(project)
    if not root.is_dir():
        return {"project_name": project, "technology": "unknown", "signals": []}

    signals: list[str] = []

    def has(pattern: str) -> bool:
        return next(root.rglob(pattern), None) is not None

    if (root / "manage.py").is_file():
        signals.append("manage.py")
        return {"project_name": project, "technology": "python_django", "signals": signals}

    package_json = root / "package.json"
    if package_json.is_file():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        if any(name == "react" or name.startswith("react-") for name in deps):
            signals.append("package.json:react")
            return {"project_name": project, "technology": "react", "signals": signals}
        if any(name == "vue" or name.startswith("vue-") for name in deps):
            signals.append("package.json:vue")
            return {"project_name": project, "technology": "vue", "signals": signals}
        signals.append("package.json")

    if (root / "app.py").is_file():
        try:
            content = (root / "app.py").read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = ""
        if "flask" in content.lower() or (root / "requirements.txt").is_file():
            signals.append("app.py")
            return {"project_name": project, "technology": "flask", "signals": signals}

    if has("*.php"):
        signals.append("*.php")
        return {"project_name": project, "technology": "php", "signals": signals}

    if (root / "index.html").is_file():
        signals.append("index.html")
        return {"project_name": project, "technology": "static_html", "signals": signals}

    return {"project_name": project, "technology": "unknown", "signals": signals}
