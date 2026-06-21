"""Persistent per-project site requirements (data/workspace_state/<project>.json).

The whole point of this module is that a new edit task must never silently
drop a feature an earlier task already established (background, language
switcher, slider, weather, footer, ...). Requirements only ever grow: once a
flag is True it stays True until a human explicitly asks to remove that
feature -- a new task touching something unrelated cannot reset it.

inspect_site_state() is the read-only counterpart: it looks at what the
project's files actually contain right now, independent of what's recorded
as "required". /site_state and "where is X" questions use it to answer
honestly from real file content instead of guessing.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from tools_edit import read_workspace_project_files
from tools_fs import ToolError
from tools_write import _validate_project_name

DEFAULT_REQUIREMENTS: dict[str, Any] = {
    "background_required": False,
    "language_switcher_required": False,
    "languages": [],
    "single_language_visible": False,
    "slider_required": False,
    "weather_required": False,
    "footer_required": False,
}

LANGUAGE_CODES = ("ru", "en", "es")

REQUIREMENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "background_required": ("фон", "background", "hero", "wallpaper", "обои", "fondo"),
    "language_switcher_required": (
        "язык", "language", "idioma", "ru/en/es", "en/es/ru", "мультиязыч", "переключ", "switch lang",
    ),
    "slider_required": ("слайдер", "карусель", "slider", "carousel"),
    "weather_required": ("погод", "weather", "open-meteo", "bilbao", "бильбао"),
    "footer_required": ("футер", "подвал", "footer"),
}


def _state_path(project_name: str) -> Path:
    project = _validate_project_name(project_name)
    return config.get_workspace_state_dir() / f"{project}.json"


def _read_raw(project_name: str) -> dict[str, Any]:
    path = _state_path(project_name)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def load_site_state(project_name: str) -> dict[str, Any]:
    """Returns the persisted requirements for a project, defaults if none saved yet."""
    project = _validate_project_name(project_name)
    raw = _read_raw(project)
    requirements = dict(DEFAULT_REQUIREMENTS)
    stored = raw.get("requirements") if isinstance(raw.get("requirements"), dict) else {}
    for key, default_value in DEFAULT_REQUIREMENTS.items():
        if key in stored:
            requirements[key] = stored[key]
    requirements["languages"] = sorted(set(requirements.get("languages") or []))
    return {
        "project_name": project,
        "requirements": requirements,
        "updated_at": raw.get("updated_at"),
    }


def get_site_requirements(project_name: str) -> dict[str, Any]:
    return load_site_state(project_name)["requirements"]


def save_site_state(project_name: str, updates: dict[str, Any], *, merge: bool = True) -> dict[str, Any]:
    """Merges `updates` into the persisted requirements and writes them back.

    merge=True (the default, and the only mode used by the edit workflow):
    booleans are OR'd with whatever is already stored, languages are unioned.
    A task that doesn't mention background can never turn background_required
    back to False -- only an explicit merge=False call (not currently wired
    to any user-facing command) could do that.
    """
    project = _validate_project_name(project_name)
    current = get_site_requirements(project)
    merged = dict(current)
    for key, value in (updates or {}).items():
        if key not in DEFAULT_REQUIREMENTS:
            continue
        if key == "languages":
            if merge:
                merged["languages"] = sorted(set(current.get("languages") or []) | set(value or []))
            else:
                merged["languages"] = sorted(set(value or []))
            continue
        if isinstance(DEFAULT_REQUIREMENTS[key], bool):
            if merge:
                merged[key] = bool(current.get(key)) or bool(value)
            else:
                merged[key] = bool(value)
    payload = {
        "project_name": project,
        "requirements": merged,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    path = _state_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def infer_requirements_from_text(user_text: str) -> dict[str, Any]:
    """Best-effort keyword scan of a task description -> partial requirements
    dict suitable for save_site_state(). Never used to *remove* a requirement,
    only ever merged in additively by the caller."""
    lowered = (user_text or "").lower()
    inferred: dict[str, Any] = {}
    for key, keywords in REQUIREMENT_KEYWORDS.items():
        if any(word in lowered for word in keywords):
            inferred[key] = True
    if inferred.get("language_switcher_required"):
        found_languages = [code for code in LANGUAGE_CODES if re.search(rf"\b{code}\b", lowered)]
        inferred["languages"] = found_languages or list(LANGUAGE_CODES)
        inferred["single_language_visible"] = True
    return inferred


def _project_haystacks(project_name: str) -> tuple[str, str, str]:
    """Returns (html_text, css_text, js_text) concatenated from project files."""
    result = read_workspace_project_files(project_name)
    html_parts, css_parts, js_parts = [], [], []
    for f in result["files"]:
        lowered_path = f["path"].lower()
        if lowered_path.endswith(".html"):
            html_parts.append(f["content"])
        elif lowered_path.endswith(".css"):
            css_parts.append(f["content"])
        elif lowered_path.endswith(".js"):
            js_parts.append(f["content"])
    return "\n".join(html_parts), "\n".join(css_parts), "\n".join(js_parts)


def inspect_site_state(project_name: str) -> dict[str, Any]:
    """Read-only inspection of what the project's real files currently contain.
    Never invents a feature that isn't actually present in the files."""
    project = _validate_project_name(project_name)
    html_text, css_text, js_text = _project_haystacks(project)
    lowered_css = css_text.lower()
    lowered_html = html_text.lower()
    lowered_js = js_text.lower()

    bg_match = re.search(r"background(?:-image)?\s*:\s*url\(['\"]?([^'\")]+)['\"]?\)", css_text, re.IGNORECASE)
    has_background = bool(bg_match)
    background_image_path = bg_match.group(1).strip() if bg_match else None

    languages_found = [
        code for code in LANGUAGE_CODES
        if any(p in lowered_html for p in (f'data-lang="{code}"', f"lang-{code}", f'id="{code}"', f">{code.upper()}<"))
    ]
    has_lang_click_handler = ("addeventlistener" in lowered_js or "onclick" in lowered_html) and "lang" in (lowered_js + lowered_html)
    has_language_switcher = bool(languages_found) and has_lang_click_handler

    has_slider = any(
        marker in (lowered_html + lowered_css + lowered_js)
        for marker in ("slider", "carousel", "swiper", "data-slide")
    )
    has_footer = "<footer" in lowered_html or 'class="footer' in lowered_html or "class='footer" in lowered_html
    has_weather_block = "weather" in (lowered_html + lowered_js) or "open-meteo" in lowered_js

    return {
        "project_name": project,
        "has_background": has_background,
        "background_image_path": background_image_path,
        "languages_found": languages_found,
        "has_language_switcher": has_language_switcher,
        "has_slider": has_slider,
        "has_footer": has_footer,
        "has_weather_block": has_weather_block,
    }


def format_site_state_answer(project_name: str) -> str:
    state = load_site_state(project_name)
    inspected = inspect_site_state(project_name)
    req = state["requirements"]
    lines = [f"Состояние сайта {project_name}:", "", "Требования (накапливаются, не сбрасываются новыми задачами):"]
    lines.append(f"- фон: {'нужен' if req['background_required'] else 'не требуется'}")
    lines.append(
        f"- переключатель языков: {'нужен' if req['language_switcher_required'] else 'не требуется'}"
        + (f" ({', '.join(req['languages'])})" if req["languages"] else "")
    )
    lines.append(f"- одновременно виден один язык: {'да' if req['single_language_visible'] else 'нет'}")
    lines.append(f"- слайдер: {'нужен' if req['slider_required'] else 'не требуется'}")
    lines.append(f"- погода: {'нужна' if req['weather_required'] else 'не требуется'}")
    lines.append(f"- footer: {'нужен' if req['footer_required'] else 'не требуется'}")
    lines.append("")
    lines.append("Что реально есть в файлах сейчас:")
    lines.append(f"- фон: {'есть' if inspected['has_background'] else 'нет'}" + (f" ({inspected['background_image_path']})" if inspected["background_image_path"] else ""))
    lines.append(f"- языковые кнопки: {', '.join(inspected['languages_found']) or 'не найдены'}")
    lines.append(f"- слайдер: {'есть' if inspected['has_slider'] else 'нет'}")
    lines.append(f"- footer: {'есть' if inspected['has_footer'] else 'нет'}")
    lines.append(f"- погодный блок: {'есть' if inspected['has_weather_block'] else 'нет'}")
    return "\n".join(lines)
