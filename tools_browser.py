import os
from pathlib import Path
from time import time
from typing import Any

import config
from tools_write import _validate_project_name


LANG_BUTTON_HINTS = ("ru", "es", "en")


def playwright_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception:
        return False
    return True


def _screenshot_dir() -> Path:
    base = Path(os.getenv("JARVIS_DB_PATH", config.JARVIS_DB_PATH)).resolve().parent / "screenshots"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _find_lang_buttons(page) -> list[tuple[str, Any]]:
    found = []
    for code in LANG_BUTTON_HINTS:
        selectors = (
            f'[data-lang="{code}"]',
            f'[data-lang="{code.upper()}"]',
            f'.lang-{code}',
            f'#lang-{code}',
            f'button:has-text("{code.upper()}")',
        )
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if locator.count() > 0:
                    found.append((code, locator.first))
                    break
            except Exception:
                continue
    return found


def check_site_with_playwright(project_name: str, url: str) -> dict[str, Any]:
    """Best-effort browser smoke check. Never raises; returns success/errors/screenshot_path.

    If Playwright isn't installed, returns skipped=True with success=None so callers can
    distinguish "not checked" from "checked and failed".
    """
    if not playwright_available():
        return {
            "success": None,
            "skipped": True,
            "reason": "Playwright не установлен",
            "project": project_name,
            "url": url,
            "title": "",
            "body_present": False,
            "console_errors": [],
            "errors": [],
            "language_buttons_found": [],
            "language_switch_ok": None,
            "screenshot_path": None,
        }

    from playwright.sync_api import sync_playwright

    project = _validate_project_name(project_name)
    console_errors: list[str] = []
    page_errors: list[str] = []
    result: dict[str, Any] = {
        "success": False,
        "skipped": False,
        "project": project,
        "url": url,
        "title": "",
        "body_present": False,
        "console_errors": console_errors,
        "errors": page_errors,
        "language_buttons_found": [],
        "language_switch_ok": None,
        "screenshot_path": None,
    }

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
                page.on("pageerror", lambda exc: page_errors.append(str(exc)))
                response = page.goto(url, timeout=15000, wait_until="load")
                if response is None or not response.ok:
                    page_errors.append(f"HTTP status: {response.status if response else 'no response'}")

                result["title"] = page.title()
                body_text = page.inner_text("body") if page.locator("body").count() else ""
                result["body_present"] = bool(body_text.strip())

                lang_buttons = _find_lang_buttons(page)
                result["language_buttons_found"] = [code for code, _loc in lang_buttons]
                if lang_buttons:
                    before_text = body_text
                    switched = False
                    for code, locator in lang_buttons:
                        try:
                            locator.click(timeout=3000)
                            page.wait_for_timeout(300)
                            after_text = page.inner_text("body")
                            if after_text.strip() and after_text.strip() != before_text.strip():
                                switched = True
                            before_text = after_text
                        except Exception as exc:
                            page_errors.append(f"language button {code} click failed: {exc}")
                    result["language_switch_ok"] = switched

                screenshot_path = _screenshot_dir() / f"{project}_{int(time())}.png"
                page.screenshot(path=str(screenshot_path))
                result["screenshot_path"] = str(screenshot_path)
            finally:
                browser.close()
    except Exception as exc:
        page_errors.append(str(exc))

    result["success"] = bool(result["body_present"]) and not page_errors and not console_errors
    return result
