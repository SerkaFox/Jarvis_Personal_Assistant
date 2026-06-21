"""Structured, deterministic site-edit operations executor.

Ollama is only ever allowed to SELECT one of these operations (see
ALLOWED_OPERATIONS/ALLOWED_FEATURES and bot.ask_ollama_for_operation_plan) --
it never authors HTML/CSS/JS content directly, and validate_operation_plan()
strips any param key/value that isn't on the small primitive allowlist. Every
operation below is a generic, marker-delimited, idempotent injector that
works on any workspace project regardless of its specific markup, and only
ever appends/replaces its OWN marker block -- so applying an operation can
never accidentally delete a different feature's content. That's the
"universal system, not a per-site hack" property: there is exactly one
add_slider implementation, reused for every project, never a bespoke
generator written for one site.
"""
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from tools_edit import read_workspace_project_files
from tools_fs import ToolError
from tools_media import list_workspace_project_images, set_fixed_background, set_hero_background
from tools_snapshot import rollback_project
from tools_write import write_project_text_file

ALLOWED_OPERATIONS = {
    "add_feature",
    "update_feature",
    "repair_feature",
    "set_background",
    "add_slider",
    "fix_language_switcher",
    "add_footer",
    "add_weather",
    "verify",
    "rollback",
}
ALLOWED_FEATURES = {"background", "slider", "language_switcher", "weather", "footer"}
ALLOWED_PARAM_KEYS = {"target", "fixed", "image", "snapshot_id"}
MAX_OPERATIONS_PER_PLAN = 8


def validate_operation_plan(plan: Any) -> list[dict[str, Any]]:
    """Strict allowlist validation -- this is the entire trust boundary
    between Ollama's output and real file writes. Anything not explicitly
    recognized here (unknown op, unknown feature, extra param keys, non-dict
    operations, raw HTML/file content) is dropped or rejected, never executed."""
    if not isinstance(plan, dict):
        raise ToolError("operation plan должен быть JSON-объектом")
    operations = plan.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ToolError("operation plan должен содержать непустой список operations")
    if len(operations) > MAX_OPERATIONS_PER_PLAN:
        raise ToolError(f"Слишком много операций за один запрос (> {MAX_OPERATIONS_PER_PLAN})")
    validated: list[dict[str, Any]] = []
    for raw_op in operations:
        if not isinstance(raw_op, dict):
            raise ToolError(f"Некорректная операция: {raw_op!r}")
        op = raw_op.get("op")
        if op not in ALLOWED_OPERATIONS:
            raise ToolError(f"Недопустимая операция: {op!r}")
        feature = raw_op.get("feature")
        if op in ("add_feature", "update_feature", "repair_feature"):
            if feature not in ALLOWED_FEATURES:
                raise ToolError(f"Недопустимая feature: {feature!r}")
        else:
            feature = None
        raw_params = raw_op.get("params") or {}
        if not isinstance(raw_params, dict):
            raise ToolError("params должен быть объектом")
        clean_params = {
            key: value for key, value in raw_params.items() if key in ALLOWED_PARAM_KEYS and isinstance(value, (str, bool, int))
        }
        validated.append({"op": op, "feature": feature, "params": clean_params})
    return validated


# ---- generic marker-block injection helpers (HTML/CSS/JS) ----

BODY_OPEN_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)


def _css_marker_block(marker_name: str, lines: list[str]) -> tuple[str, str, str]:
    start = f"/* jarvis-{marker_name}:start */"
    end = f"/* jarvis-{marker_name}:end */"
    return start, end, "\n".join([start, *lines, end, ""])


def _html_marker_block(marker_name: str, lines: list[str]) -> tuple[str, str, str]:
    start = f"<!-- jarvis-{marker_name}:start -->"
    end = f"<!-- jarvis-{marker_name}:end -->"
    return start, end, "\n".join([start, *lines, end, ""])


def _js_marker_block(marker_name: str, lines: list[str]) -> tuple[str, str, str]:
    start = f"// jarvis-{marker_name}:start"
    end = f"// jarvis-{marker_name}:end"
    return start, end, "\n".join([start, *lines, end, ""])


def _apply_marker_block(
    original: str, start: str, end: str, block: str, *, insert_before: str | None = None, insert_after_re: re.Pattern | None = None
) -> str:
    if start in original and end in original:
        before, _mid = original.split(start, 1)
        _old, after = _mid.split(end, 1)
        return before.rstrip() + "\n\n" + block + after.lstrip()
    if insert_after_re is not None:
        match = insert_after_re.search(original)
        if match:
            idx = match.end()
            return original[:idx] + "\n" + block + original[idx:]
    if insert_before and insert_before in original:
        idx = original.index(insert_before)
        return original[:idx] + block + "\n" + original[idx:]
    return original.rstrip() + "\n\n" + block


def _read_file(project_name: str, relative_path: str) -> str:
    files = read_workspace_project_files(project_name)["files"]
    match = next((f for f in files if f["path"] == relative_path), None)
    if match is None:
        raise ToolError(f"Файл не найден: {relative_path}")
    return match["content"]


def _write_file(project_name: str, relative_path: str, content: str) -> None:
    write_project_text_file(project_name, relative_path, content, overwrite=True)


# ---- operations ----


def op_set_background(project_name: str, params: dict[str, Any]) -> dict[str, Any]:
    target = (params.get("target") or "hero").lower()
    fixed = bool(params.get("fixed"))
    images = list_workspace_project_images(project_name)["images"]
    if not images:
        raise ToolError("В проекте нет ни одного изображения в assets/img -- сначала пришли фото.")
    requested = params.get("image")
    image_path = None
    if requested:
        image_path = next((img["path"] for img in images if Path(img["path"]).name == Path(str(requested)).name), None)
    if not image_path:
        image_path = sorted(images, key=lambda i: i["modified"])[-1]["path"]
    if target == "whole_page":
        set_fixed_background(project_name, image_path)
    else:
        set_hero_background(project_name, image_path, fixed=fixed)
    return {
        "files_changed": ["assets/css/style.css"],
        "detail": f"background -> {image_path} ({target})",
        "image_path": image_path,
    }


SLIDER_CSS_LINES = [
    ".jarvis-slider{position:relative;overflow:hidden;max-width:100%;border-radius:12px;}",
    ".jarvis-slider .jarvis-slide{display:none;width:100%;}",
    ".jarvis-slider .jarvis-slide.active{display:block;}",
    ".jarvis-slider img{width:100%;height:auto;display:block;}",
    ".jarvis-slider .jarvis-slide-text{padding:1rem;text-align:center;}",
]

SLIDER_JS_LINES = [
    "(function(){",
    "  var slides = document.querySelectorAll('.jarvis-slider .jarvis-slide');",
    "  if(!slides.length) return;",
    "  var current = 0;",
    "  slides.forEach(function(s, i){ s.classList.toggle('active', i === 0); });",
    "  setInterval(function(){",
    "    slides[current].classList.remove('active');",
    "    current = (current + 1) % slides.length;",
    "    slides[current].classList.add('active');",
    "  }, 4000);",
    "})();",
]


def op_add_slider(project_name: str, params: dict[str, Any]) -> dict[str, Any]:
    images = list_workspace_project_images(project_name)["images"]
    if images:
        slide_items = [f'<div class="jarvis-slide"><img src="{img["path"]}" alt="slide"></div>' for img in images[:5]]
    else:
        slide_items = [f'<div class="jarvis-slide jarvis-slide-text"><p>Slide {i + 1}</p></div>' for i in range(3)]

    html_start, html_end, html_block = _html_marker_block("slider", ['<div class="jarvis-slider">', *slide_items, "</div>"])
    index_html = _read_file(project_name, "index.html")
    updated_html = _apply_marker_block(index_html, html_start, html_end, html_block, insert_before="</body>")
    _write_file(project_name, "index.html", updated_html)

    css_start, css_end, css_block = _css_marker_block("slider", SLIDER_CSS_LINES)
    css_text = _read_file(project_name, "assets/css/style.css")
    _write_file(project_name, "assets/css/style.css", _apply_marker_block(css_text, css_start, css_end, css_block))

    js_start, js_end, js_block = _js_marker_block("slider", SLIDER_JS_LINES)
    js_text = _read_file(project_name, "assets/js/main.js")
    _write_file(project_name, "assets/js/main.js", _apply_marker_block(js_text, js_start, js_end, js_block))

    return {
        "files_changed": ["index.html", "assets/css/style.css", "assets/js/main.js"],
        "detail": f"slider with {len(slide_items)} slides",
    }


LANGUAGES = ("ru", "en", "es")
LANG_GREETINGS = {"ru": "Добро пожаловать", "en": "Welcome", "es": "Bienvenido"}

LANG_CSS_LINES = [
    ".jarvis-lang-block{display:none;}",
    ".jarvis-lang-block.active{display:block;}",
    ".jarvis-lang-buttons button.active{font-weight:bold;text-decoration:underline;}",
]

LANG_JS_LINES = [
    "(function(){",
    "  function setLang(code){",
    "    document.querySelectorAll('.jarvis-lang-block').forEach(function(el){",
    "      el.classList.toggle('active', el.getAttribute('data-lang') === code);",
    "    });",
    "    document.querySelectorAll('.jarvis-lang-buttons [data-lang]').forEach(function(btn){",
    "      btn.classList.toggle('active', btn.getAttribute('data-lang') === code);",
    "    });",
    "  }",
    "  window.jarvisSetLang = setLang;",
    "  document.addEventListener('click', function(e){",
    "    var btn = e.target.closest('.jarvis-lang-buttons [data-lang]');",
    "    if(btn) setLang(btn.getAttribute('data-lang'));",
    "  });",
    "  setLang('ru');",
    "})();",
]


def op_fix_language_switcher(project_name: str, params: dict[str, Any]) -> dict[str, Any]:
    buttons = "".join(f'<button type="button" data-lang="{code}">{code.upper()}</button>' for code in LANGUAGES)
    btn_start, btn_end, btn_block = _html_marker_block("lang-buttons", [f'<nav class="jarvis-lang-buttons">{buttons}</nav>'])
    blocks = "".join(
        f'<div class="jarvis-lang-block" data-lang="{code}"><p>{LANG_GREETINGS[code]}</p></div>' for code in LANGUAGES
    )
    blk_start, blk_end, blk_block = _html_marker_block("lang-blocks", [f'<div class="jarvis-lang-content">{blocks}</div>'])

    index_html = _read_file(project_name, "index.html")
    updated = _apply_marker_block(index_html, btn_start, btn_end, btn_block, insert_after_re=BODY_OPEN_RE)
    updated = _apply_marker_block(updated, blk_start, blk_end, blk_block, insert_before="</body>")
    _write_file(project_name, "index.html", updated)

    css_start, css_end, css_block = _css_marker_block("lang-switcher", LANG_CSS_LINES)
    css_text = _read_file(project_name, "assets/css/style.css")
    _write_file(project_name, "assets/css/style.css", _apply_marker_block(css_text, css_start, css_end, css_block))

    js_start, js_end, js_block = _js_marker_block("lang-switcher", LANG_JS_LINES)
    js_text = _read_file(project_name, "assets/js/main.js")
    _write_file(project_name, "assets/js/main.js", _apply_marker_block(js_text, js_start, js_end, js_block))

    return {"files_changed": ["index.html", "assets/css/style.css", "assets/js/main.js"], "detail": "language switcher RU/EN/ES"}


def op_add_footer(project_name: str, params: dict[str, Any]) -> dict[str, Any]:
    footer_html = f'<footer class="jarvis-footer"><p>&copy; {datetime.now().year} {project_name}</p></footer>'
    start, end, block = _html_marker_block("footer", [footer_html])
    index_html = _read_file(project_name, "index.html")
    _write_file(project_name, "index.html", _apply_marker_block(index_html, start, end, block, insert_before="</body>"))

    css_start, css_end, css_block = _css_marker_block("footer", [".jarvis-footer{padding:1.5rem;text-align:center;opacity:.8;}"])
    css_text = _read_file(project_name, "assets/css/style.css")
    _write_file(project_name, "assets/css/style.css", _apply_marker_block(css_text, css_start, css_end, css_block))
    return {"files_changed": ["index.html", "assets/css/style.css"], "detail": "footer"}


WEATHER_JS_LINES = [
    "(function(){",
    "  var el = document.getElementById('jarvis-weather');",
    "  if(!el) return;",
    "  fetch('https://api.open-meteo.com/v1/forecast?latitude=43.2630&longitude=-2.9350&current_weather=true')",
    "    .then(function(r){ return r.json(); })",
    "    .then(function(d){ el.textContent = d.current_weather.temperature + ' C'; })",
    "    .catch(function(){ el.textContent = 'weather unavailable'; });",
    "})();",
]


def op_add_weather(project_name: str, params: dict[str, Any]) -> dict[str, Any]:
    start, end, block = _html_marker_block(
        "weather", ['<div id="jarvis-weather" class="jarvis-weather">weather loading...</div>']
    )
    index_html = _read_file(project_name, "index.html")
    _write_file(project_name, "index.html", _apply_marker_block(index_html, start, end, block, insert_before="</body>"))

    js_start, js_end, js_block = _js_marker_block("weather", WEATHER_JS_LINES)
    js_text = _read_file(project_name, "assets/js/main.js")
    _write_file(project_name, "assets/js/main.js", _apply_marker_block(js_text, js_start, js_end, js_block))
    return {"files_changed": ["index.html", "assets/js/main.js"], "detail": "weather widget (open-meteo, Bilbao)"}


def op_verify(project_name: str, params: dict[str, Any]) -> dict[str, Any]:
    from tools_site_checks import run_acceptance_checks
    from tools_site_state import get_site_requirements

    requirements = get_site_requirements(project_name)
    result = run_acceptance_checks(project_name, requirements)
    return {"files_changed": [], "detail": "verify", "check_result": result}


def op_rollback(project_name: str, params: dict[str, Any]) -> dict[str, Any]:
    import project_state_manager

    snapshot_id = params.get("snapshot_id") or project_state_manager.load_project_state(project_name).get(
        "last_successful_snapshot"
    )
    if not snapshot_id:
        raise ToolError("Нет snapshot_id и нет last_successful_snapshot для отката")
    result = rollback_project(project_name, snapshot_id)
    return {"files_changed": result["restored_files"], "detail": f"rollback -> {snapshot_id}"}


FEATURE_DISPATCH = {
    "background": op_set_background,
    "slider": op_add_slider,
    "language_switcher": op_fix_language_switcher,
    "footer": op_add_footer,
    "weather": op_add_weather,
}

OP_DISPATCH = {
    "set_background": op_set_background,
    "add_slider": op_add_slider,
    "fix_language_switcher": op_fix_language_switcher,
    "add_footer": op_add_footer,
    "add_weather": op_add_weather,
    "verify": op_verify,
    "rollback": op_rollback,
}


def apply_operation(project_name: str, operation: dict[str, Any]) -> dict[str, Any]:
    op = operation["op"]
    params = operation.get("params") or {}
    if op in ("add_feature", "update_feature", "repair_feature"):
        feature = operation.get("feature")
        handler = FEATURE_DISPATCH.get(feature)
        if handler is None:
            raise ToolError(f"Нет executor'а для feature {feature!r}")
    else:
        handler = OP_DISPATCH.get(op)
        if handler is None:
            raise ToolError(f"Нет executor'а для операции {op!r}")
    result = handler(project_name, params)
    return {"op": op, "feature": operation.get("feature"), **result}


def apply_operation_plan(project_name: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
    applied: list[dict[str, Any]] = []
    files_changed: list[str] = []
    for operation in operations:
        result = apply_operation(project_name, operation)
        applied.append(result)
        for path in result.get("files_changed") or []:
            if path not in files_changed:
                files_changed.append(path)
    return {"applied": applied, "files_changed": files_changed}
