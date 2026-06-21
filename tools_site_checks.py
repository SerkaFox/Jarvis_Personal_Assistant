"""Comprehensive, requirement-driven acceptance checks for workspace site edits.

run_acceptance_checks(project, requirements, browser_result=...) is the single
source of truth the transactional edit workflow uses to decide whether an
edit may be kept or must be rolled back. Every check is recorded with an
explicit "critical" flag; a failed critical check is what triggers an
automatic rollback (see tools_snapshot.rollback_project and
bot.edit_workspace_site_workflow).
"""
import re
from typing import Any

from tools_edit import read_workspace_project_files
from tools_site_state import LANGUAGE_CODES
from tools_write import _validate_project_name, resolve_write_path

LOCAL_REF_RE = re.compile(r'(?:src|href)=["\']([^"\']+)["\']')
SKIP_REF_PREFIXES = ("http://", "https://", "data:", "mailto:", "tel:", "#", "javascript:")


def _local_resource_paths(html_text: str) -> list[str]:
    paths = []
    for match in LOCAL_REF_RE.finditer(html_text):
        ref = match.group(1).strip()
        if not ref or ref.startswith(SKIP_REF_PREFIXES):
            continue
        paths.append(ref.split("?")[0].split("#")[0])
    return paths


def _check_no_missing_local_resources(project_name: str, html_text: str) -> tuple[bool, str]:
    project_root = resolve_write_path(project_name)
    missing = []
    for ref in _local_resource_paths(html_text):
        relative = ref.lstrip("/")
        candidate = project_root / relative
        if not candidate.is_file():
            missing.append(ref)
    if missing:
        return False, f"не найдены локальные файлы: {', '.join(missing[:10])}"
    return True, "ok"


def _check_language_markers_static(html_text: str, js_text: str, required_langs: list[str]) -> tuple[bool, str]:
    lowered_html = html_text.lower()
    lowered_js = js_text.lower()

    def has_lang(code: str) -> bool:
        patterns = (f'data-lang="{code}"', f"lang-{code}", f'id="{code}"', f">{code.upper()}<")
        return any(p in lowered_html for p in patterns)

    missing = [code.upper() for code in required_langs if not has_lang(code)]
    if missing:
        return False, f"не найдены языковые маркеры: {', '.join(missing)}"
    has_click_handler = ("addeventlistener" in lowered_js or "onclick" in lowered_html) and "lang" in (lowered_js + lowered_html)
    if not has_click_handler:
        return False, "не найден обработчик клика для переключения языка"
    return True, "ok"


def run_acceptance_checks(
    project_name: str,
    requirements: dict[str, Any],
    *,
    files: list[dict] | None = None,
    browser_result: dict | None = None,
) -> dict[str, Any]:
    project = _validate_project_name(project_name)
    if files is None:
        files = read_workspace_project_files(project)["files"]
    html_text = "\n".join(f["content"] for f in files if f["path"].lower().endswith(".html"))
    css_text = "\n".join(f["content"] for f in files if f["path"].lower().endswith(".css"))
    js_text = "\n".join(f["content"] for f in files if f["path"].lower().endswith(".js"))
    full_text_lower = (html_text + "\n" + css_text + "\n" + js_text).lower()

    checks: dict[str, dict[str, Any]] = {}
    failed: list[str] = []
    critical_failed: list[str] = []

    def record(name: str, ok: bool, detail: str, critical: bool = True) -> None:
        checks[name] = {"ok": ok, "detail": detail, "critical": critical}
        if not ok:
            failed.append(f"{name}: {detail}")
            if critical:
                critical_failed.append(f"{name}: {detail}")

    def record_skipped(name: str, detail: str) -> None:
        checks[name] = {"ok": None, "detail": detail, "critical": False}

    ok, detail = _check_no_missing_local_resources(project, html_text)
    record("no_missing_local_resources", ok, detail)

    have_browser = bool(browser_result and not browser_result.get("skipped"))

    if have_browser:
        failed_resources = browser_result.get("failed_resources") or []
        record("no_404_resources_live", not failed_resources, "; ".join(failed_resources[:10]) or "ok")

        console_errors = browser_result.get("console_errors") or []
        page_errors = browser_result.get("errors") or []
        record(
            "no_console_errors",
            not console_errors and not page_errors,
            "; ".join((console_errors + page_errors)[:10]) or "ok",
        )
    else:
        record_skipped("no_404_resources_live", "браузерная проверка недоступна (preview не запущен / Playwright не установлен)")
        record_skipped("no_console_errors", "браузерная проверка недоступна")

    if requirements.get("background_required"):
        has_bg = bool(re.search(r"background(?:-image)?\s*:\s*url\(", css_text, re.IGNORECASE))
        record("background_present_in_css", has_bg, "ok" if has_bg else "в CSS нет background-image")
        if has_bg and have_browser:
            loaded = browser_result.get("background_image_loaded")
            record(
                "background_visible_in_browser",
                loaded is not False,
                "ok" if loaded is not False else "браузер не подтвердил отображение фона",
            )

    if requirements.get("language_switcher_required"):
        required_langs = [c.lower() for c in (requirements.get("languages") or list(LANGUAGE_CODES))]
        if have_browser:
            found = {c.lower() for c in (browser_result.get("language_buttons_found") or [])}
            missing_buttons = [c.upper() for c in required_langs if c not in found]
            record(
                "language_buttons_exist",
                not missing_buttons,
                "ok" if not missing_buttons else f"не найдены/не видны кнопки: {', '.join(missing_buttons)}",
            )
            if not missing_buttons:
                switch_ok = browser_result.get("language_switch_ok")
                record(
                    "language_switch_changes_text",
                    switch_ok is not False,
                    "ok" if switch_ok is not False else "клик по языковым кнопкам не меняет видимый текст",
                )
        else:
            ok, detail = _check_language_markers_static(html_text, js_text, required_langs)
            record("language_buttons_exist", ok, detail, critical=False)

    if requirements.get("single_language_visible"):
        if have_browser:
            single_ok = browser_result.get("single_language_visible_ok")
            record(
                "single_language_visible",
                single_ok is not False,
                "ok" if single_ok is not False else "одновременно видно больше одного языка",
            )
        else:
            record_skipped("single_language_visible", "требует браузерной проверки")

    if requirements.get("slider_required"):
        ok = any(marker in full_text_lower for marker in ("slider", "carousel", "swiper", "data-slide"))
        record("slider_present", ok, "ok" if ok else "не найден slider/carousel в HTML/CSS/JS")

    if requirements.get("footer_required"):
        ok = "<footer" in full_text_lower or 'class="footer' in full_text_lower or "class='footer" in full_text_lower
        record("footer_present", ok, "ok" if ok else "не найден <footer>")

    if requirements.get("weather_required"):
        ok = "fetch" in js_text.lower() and ("weather" in js_text.lower() or "open-meteo" in js_text.lower())
        record("weather_block_present", ok, "ok" if ok else "не найден JS fetch для погоды")
        if have_browser:
            console_errors = browser_result.get("console_errors") or []
            weather_errors = [e for e in console_errors if any(w in e.lower() for w in ("weather", "fetch", "meteo"))]
            record(
                "weather_does_not_break_js",
                not weather_errors,
                "ok" if not weather_errors else "; ".join(weather_errors[:5]),
            )

    return {
        "success": not critical_failed,
        "failed": failed,
        "critical_failed": critical_failed,
        "checks": checks,
    }
