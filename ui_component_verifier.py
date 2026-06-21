"""Universal UI component verifier.

Checks whether a generic ui_component_model.ComponentModel is actually
present and (where possible) interactive -- via a real rendered-DOM probe
through Playwright when a preview is reachable (verify_components_async,
backed by tools_browser.check_site_components_async), or via an approximate
static HTML/CSS/JS text fallback otherwise (verify_component_static). Both
paths share the same result shape and the same status classification, so
callers don't need kind-specific logic: there is one slider checker, one
accordion checker, etc. -- all the SAME generic code, parameterized by the
component's selectors.

This deliberately never fails an otherwise-present component just because no
nav/toggle control was found -- per the design brief, a missing next/prev
control means "static block, interactivity not confirmed", not "missing".
"""
import re
from typing import Any

from ui_component_model import ComponentModel

STATUS_HUMAN_PREFIX = {
    "ok": "Найден и работает",
    "found_static": "Найден (статическая проверка)",
    "interactivity_unconfirmed": "Найден, интерактивность не подтверждена",
    "interactivity_missing": "Найден, но не реагирует на взаимодействие",
    "items_mismatch": "Найден, но элементов меньше ожидаемого",
    "missing": "Не найден",
    "skipped": "Проверка недоступна",
}

_CLASS_RE_CACHE: dict[str, re.Pattern] = {}


def _class_regex(name: str) -> re.Pattern:
    if name not in _CLASS_RE_CACHE:
        _CLASS_RE_CACHE[name] = re.compile(rf'class\s*=\s*"[^"]*\b{re.escape(name)}\b[^"]*"', re.IGNORECASE)
    return _CLASS_RE_CACHE[name]


def _matches_for_selector(selector: str, html: str) -> list:
    """Approximate, non-CSS-engine matcher for a single simple selector
    token against raw HTML text -- used only by the no-browser static
    fallback. Supports .class, #id, [attr]/[attr=value], bare tag names, and
    a loose "descendant" (space-separated, all tokens must appear somewhere)
    form -- intentionally approximate, never claims more than it can know."""
    selector = selector.strip()
    if not selector:
        return []
    if selector.startswith("."):
        return _class_regex(selector[1:]).findall(html)
    if selector.startswith("#"):
        return re.findall(rf'id\s*=\s*"{re.escape(selector[1:])}"', html, re.IGNORECASE)
    if selector.startswith("[") and selector.endswith("]"):
        inner = selector[1:-1]
        attr = inner.split("=")[0].strip()
        return re.findall(rf"<[^>]*\b{re.escape(attr)}\b", html, re.IGNORECASE)
    if " " in selector:
        tokens = selector.split()
        if all(_matches_for_selector(token, html) for token in tokens):
            return _matches_for_selector(tokens[-1], html)
        return []
    return re.findall(rf"<{re.escape(selector)}\b", html, re.IGNORECASE)


def _selector_present(selector: str, html: str) -> bool:
    return bool(_matches_for_selector(selector, html))


def _count_selector(selector: str, html: str) -> int:
    return len(_matches_for_selector(selector, html))


def _classify_status(
    model: ComponentModel,
    *,
    container_found: bool,
    items_found: int,
    interactivity_confirmed: bool | None,
    nav_found: bool,
    dynamic: bool,
) -> tuple[str, str]:
    if not container_found:
        hint = ", ".join(model.selectors[:4]) or "-"
        return "missing", f"{model.kind}: компонент не найден (искал селекторы: {hint})"

    if model.expected_items is not None and items_found < model.expected_items:
        return "items_mismatch", f"{model.kind}: найдено элементов {items_found}, ожидалось минимум {model.expected_items}"

    if model.required_interactivity and interactivity_confirmed is False:
        return "interactivity_missing", f"{model.kind}: компонент найден, но взаимодействие не дало эффекта"

    if model.required_interactivity and interactivity_confirmed is None:
        reason = "нет элемента управления" if not nav_found else "проверка недоступна"
        return "interactivity_unconfirmed", f"{model.kind}: компонент найден, но интерактивность не подтверждена ({reason})"

    if dynamic and interactivity_confirmed:
        return "ok", f"{model.kind}: найден, элементов {items_found}, интерактивность подтверждена"

    if dynamic and interactivity_confirmed is False:
        return "found_static", f"{model.kind}: найден ({items_found} элементов), статический блок -- интерактивность не подтверждена"

    if not dynamic:
        return "found_static", f"{model.kind}: найден в файлах ({items_found} элементов) -- статическая проверка, интерактивность не проверялась"

    return "ok", f"{model.kind}: найден, элементов {items_found}"


def _empty_result(model: ComponentModel, *, mode: str, status: str, detail: str) -> dict[str, Any]:
    return {
        "kind": model.kind,
        "mode": mode,
        "container_found": False,
        "container_visible": None,
        "items_found": 0,
        "items_expected": model.expected_items,
        "nav_found": False,
        "interactivity_confirmed": None,
        "console_errors": [],
        "failed_resources": [],
        "status": status,
        "detail": detail,
    }


def verify_component_static(files: list[dict[str, Any]], model: ComponentModel) -> dict[str, Any]:
    """No-browser fallback: text-based presence/count check against the
    project's real saved HTML/CSS/JS. Never claims interactivity is
    confirmed -- that requires an actual DOM interaction."""
    html = "\n".join(f["content"] for f in files if f["path"].lower().endswith((".html", ".htm")))
    css = "\n".join(f["content"] for f in files if f["path"].lower().endswith(".css"))
    js = "\n".join(f["content"] for f in files if f["path"].lower().endswith(".js"))
    full = "\n".join([html, css, js])

    container_found = any(_selector_present(sel, full) for sel in model.selectors)
    items_found = max((_count_selector(sel, full) for sel in model.item_selectors), default=0)
    nav_found = any(_selector_present(sel, full) for sel in model.nav_selectors)

    status, detail = _classify_status(
        model,
        container_found=container_found,
        items_found=items_found,
        interactivity_confirmed=None,
        nav_found=nav_found,
        dynamic=False,
    )
    return {
        "kind": model.kind,
        "mode": "static",
        "container_found": container_found,
        "container_visible": None,
        "items_found": items_found,
        "items_expected": model.expected_items,
        "nav_found": nav_found,
        "interactivity_confirmed": None,
        "console_errors": [],
        "failed_resources": [],
        "status": status,
        "detail": detail,
    }


async def verify_components_async(project_name: str, url: str, models: list[ComponentModel]) -> dict[str, dict[str, Any]]:
    """Real rendered-DOM check for every model in one Playwright session --
    works identically regardless of what server-side stack produced the
    page (static HTML, PHP, Django, Flask, React, Vue, ...), because it only
    ever looks at what the browser actually renders."""
    from tools_browser import check_site_components_async

    raw = await check_site_components_async(project_name, url, [m.to_dict() for m in models])
    output: dict[str, dict[str, Any]] = {}
    for model in models:
        if raw.get("skipped"):
            output[model.kind] = _empty_result(
                model, mode="skipped", status="skipped", detail=raw.get("reason") or "Playwright недоступен"
            )
            continue
        probe = raw["results"].get(model.kind, {})
        status, detail = _classify_status(
            model,
            container_found=bool(probe.get("container_found")),
            items_found=int(probe.get("items_found") or 0),
            interactivity_confirmed=probe.get("interactivity_confirmed"),
            nav_found=bool(probe.get("nav_found")),
            dynamic=True,
        )
        output[model.kind] = {
            "kind": model.kind,
            "mode": "dynamic",
            "container_found": bool(probe.get("container_found")),
            "container_visible": probe.get("container_visible"),
            "items_found": int(probe.get("items_found") or 0),
            "items_expected": model.expected_items,
            "nav_found": bool(probe.get("nav_found")),
            "interactivity_confirmed": probe.get("interactivity_confirmed"),
            "console_errors": list(raw.get("console_errors") or []),
            "failed_resources": list(raw.get("failed_resources") or []),
            "status": status,
            "detail": detail,
        }
    return output


def format_verification_human(result: dict[str, Any]) -> str:
    prefix = STATUS_HUMAN_PREFIX.get(result["status"], result["status"])
    return f"{prefix}: {result['detail']}"
