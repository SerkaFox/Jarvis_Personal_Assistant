import asyncio
import concurrent.futures
import os
from pathlib import Path
from time import time
from typing import Any

import config
from tools_write import _validate_project_name


LANG_BUTTON_HINTS = ("ru", "es", "en")


def playwright_available() -> bool:
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except Exception:
        return False
    return True


def _screenshot_dir() -> Path:
    base = Path(os.getenv("JARVIS_DB_PATH", config.JARVIS_DB_PATH)).resolve().parent / "screenshots"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _empty_result(project_name: str, url: str) -> dict[str, Any]:
    return {
        "success": False,
        "skipped": False,
        "project": project_name,
        "url": url,
        "title": "",
        "body_present": False,
        "body_text_length": 0,
        "sections_count": 0,
        "console_errors": [],
        "errors": [],
        "language_buttons_found": [],
        "language_switch_ok": None,
        "background_image_loaded": None,
        "screenshot_path": None,
    }


def _skipped_result(project_name: str, url: str, reason: str = "Playwright не установлен") -> dict[str, Any]:
    result = _empty_result(project_name, url)
    result["success"] = None
    result["skipped"] = True
    result["reason"] = reason
    return result


async def _find_lang_buttons_async(page) -> list[tuple[str, Any]]:
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
                if await locator.count() > 0:
                    found.append((code, locator.first))
                    break
            except Exception:
                continue
    return found


async def _check_site_with_playwright_async(
    project_name: str,
    url: str,
    expect_background_image: str | None = None,
) -> dict[str, Any]:
    """Core implementation using Playwright's ASYNC API (safe to await from inside
    a running asyncio event loop, e.g. a Telegram command handler)."""
    from playwright.async_api import async_playwright

    project = _validate_project_name(project_name)
    console_errors: list[str] = []
    page_errors: list[str] = []
    result = _empty_result(project, url)
    result["console_errors"] = console_errors
    result["errors"] = page_errors

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
                page.on("pageerror", lambda exc: page_errors.append(str(exc)))
                response = await page.goto(url, timeout=15000, wait_until="load")
                if response is None or not response.ok:
                    page_errors.append(f"HTTP status: {response.status if response else 'no response'}")

                result["title"] = await page.title()
                body_present = await page.locator("body").count()
                body_text = await page.inner_text("body") if body_present else ""
                result["body_present"] = bool(body_text.strip())
                result["body_text_length"] = len(body_text.strip())
                try:
                    result["sections_count"] = await page.locator("section").count()
                except Exception:
                    result["sections_count"] = 0

                lang_buttons = await _find_lang_buttons_async(page)
                result["language_buttons_found"] = [code for code, _loc in lang_buttons]
                if lang_buttons:
                    before_text = body_text
                    switched = False
                    for code, locator in lang_buttons:
                        try:
                            await locator.click(timeout=3000)
                            await page.wait_for_timeout(300)
                            after_text = await page.inner_text("body")
                            if after_text.strip() and after_text.strip() != before_text.strip():
                                switched = True
                            before_text = after_text
                        except Exception as exc:
                            page_errors.append(f"language button {code} click failed: {exc}")
                    result["language_switch_ok"] = switched

                if expect_background_image:
                    image_name = Path(expect_background_image).name
                    try:
                        found_bg = await page.evaluate(
                            """(imageName) => {
                                const els = document.querySelectorAll('*');
                                for (const el of els) {
                                    const bg = getComputedStyle(el).backgroundImage || '';
                                    if (bg.includes(imageName)) return true;
                                }
                                return false;
                            }""",
                            image_name,
                        )
                        result["background_image_loaded"] = bool(found_bg)
                        if not found_bg:
                            page_errors.append(f"фоновое изображение {image_name} не отображается через CSS background-image")
                    except Exception as exc:
                        page_errors.append(f"background image check failed: {exc}")
                        result["background_image_loaded"] = False

                screenshot_path = _screenshot_dir() / f"{project}_{int(time())}.png"
                await page.screenshot(path=str(screenshot_path))
                result["screenshot_path"] = str(screenshot_path)
            finally:
                await browser.close()
    except Exception as exc:
        page_errors.append(str(exc))

    result["success"] = bool(result["body_present"]) and not page_errors and not console_errors
    return result


async def check_site_with_playwright_async(
    project_name: str,
    url: str,
    expect_background_image: str | None = None,
) -> dict[str, Any]:
    """Awaitable entry point. Use this from async code (Telegram handlers) that is
    already running inside the bot's event loop."""
    if not playwright_available():
        return _skipped_result(project_name, url)
    return await _check_site_with_playwright_async(project_name, url, expect_background_image)


def check_site_with_playwright(
    project_name: str,
    url: str,
    expect_background_image: str | None = None,
) -> dict[str, Any]:
    """Synchronous entry point for the synchronous edit workflow and plain unit
    tests. Playwright's APIs cannot run on a thread that already has a running
    asyncio event loop, so this runs the async implementation in its own worker
    thread with its own fresh event loop (asyncio.run), never touching whatever
    loop the calling thread might already be inside."""
    if not playwright_available():
        return _skipped_result(project_name, url)

    def _runner() -> dict[str, Any]:
        return asyncio.run(_check_site_with_playwright_async(project_name, url, expect_background_image))

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_runner)
        try:
            return future.result(timeout=60)
        except Exception as exc:
            result = _empty_result(project_name, url)
            result["errors"] = [str(exc)]
            return result


async def playwright_async_smoke_check() -> dict[str, Any]:
    """Minimal liveness check for /status: can we actually launch a browser and
    load a page right now, via the async API, on this event loop?"""
    if not playwright_available():
        return {"ok": False, "reason": "Playwright не установлен"}
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.goto("data:text/html,<html><body>ok</body></html>", timeout=10000)
                await page.title()
            finally:
                await browser.close()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}
