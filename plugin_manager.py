"""Loads and dispatches user-installed plugins (see self_improvement.py for
how plugins get generated/installed in the first place).

A plugin is a single .py file under config.get_plugins_dir() that does not
start with "_" and exposes this module-level interface:

    PLUGIN_NAME: str
    PLUGIN_VERSION: str
    PLUGIN_DESCRIPTION: str
    def can_handle(user_text: str, context: dict) -> float   # 0.0..1.0
    async def handle(update, context, parsed_task: dict) -> dict
    def smoke_tests() -> list   # or a callable returning a list of (name, fn)

Plugins never get the bot's full power directly -- they receive the same
`update`/`context` python-telegram-bot objects normal command handlers get,
so any filesystem/network access they do is exactly as constrained as the
rest of the bot's own code (WRITE_ROOT sandboxing in tools_write.py etc).
This module only handles discovery, interface validation, scoring and safe
dispatch -- it does not grant any additional permissions.
"""

import importlib.util
import inspect
import logging
import sys
import types
from pathlib import Path
from typing import Any

import config


REQUIRED_STR_ATTRS = ("PLUGIN_NAME", "PLUGIN_VERSION", "PLUGIN_DESCRIPTION")
REQUIRED_CALLABLE_ATTRS = ("can_handle", "handle", "smoke_tests")


class PluginValidationError(Exception):
    pass


def _module_name_for(path: Path) -> str:
    return f"jarvis_plugin_{path.stem}"


def _load_module_from_path(path: Path) -> types.ModuleType:
    module_name = _module_name_for(path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PluginValidationError(f"Не удалось загрузить plugin модуль: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise PluginValidationError(f"Ошибка импорта plugin {path.name}: {exc}") from exc
    return module


def validate_plugin_module(module: types.ModuleType) -> None:
    """Raises PluginValidationError if module does not satisfy the plugin
    interface. Pure validation, no side effects."""
    for attr in REQUIRED_STR_ATTRS:
        value = getattr(module, attr, None)
        if not isinstance(value, str) or not value.strip():
            raise PluginValidationError(f"Plugin не определяет {attr} (непустую строку)")
    for attr in REQUIRED_CALLABLE_ATTRS:
        value = getattr(module, attr, None)
        if not callable(value):
            raise PluginValidationError(f"Plugin не определяет {attr}() как вызываемое")
    can_handle = module.can_handle
    if not callable(can_handle):
        raise PluginValidationError("can_handle должен быть функцией")
    handle = module.handle
    if not inspect.iscoroutinefunction(handle):
        raise PluginValidationError("handle должен быть async функцией (async def handle(update, context, parsed_task))")


def load_plugins(plugins_dir: Path | None = None) -> tuple[list[types.ModuleType], list[dict[str, str]]]:
    """Loads every plugins/*.py file (skipping files starting with "_").
    Returns (valid_modules, errors) -- errors is a list of
    {"file": ..., "error": ...} for files that failed to import or didn't
    satisfy the plugin interface; those are skipped, never raised, so one
    broken plugin can't take down plugin discovery for the rest."""
    directory = plugins_dir if plugins_dir is not None else config.get_plugins_dir()
    valid: list[types.ModuleType] = []
    errors: list[dict[str, str]] = []
    if not directory.is_dir():
        return valid, errors
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            module = _load_module_from_path(path)
            validate_plugin_module(module)
        except PluginValidationError as exc:
            errors.append({"file": path.name, "error": str(exc)})
            logging.warning("plugin_manager: rejected %s: %s", path.name, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - never let a bad plugin break loading
            errors.append({"file": path.name, "error": f"unexpected error: {exc}"})
            logging.exception("plugin_manager: unexpected error loading %s", path.name)
            continue
        valid.append(module)
    return valid, errors


def list_plugins(plugins_dir: Path | None = None) -> list[dict[str, str]]:
    modules, _errors = load_plugins(plugins_dir)
    return [
        {
            "name": m.PLUGIN_NAME,
            "version": m.PLUGIN_VERSION,
            "description": m.PLUGIN_DESCRIPTION,
            "file": Path(m.__file__).name if getattr(m, "__file__", None) else "",
        }
        for m in modules
    ]


def get_plugin_by_name(name: str, plugins_dir: Path | None = None) -> types.ModuleType | None:
    modules, _errors = load_plugins(plugins_dir)
    for module in modules:
        if module.PLUGIN_NAME == name:
            return module
    return None


def select_plugin(
    user_text: str,
    context: dict[str, Any] | None = None,
    *,
    plugins_dir: Path | None = None,
    threshold: float = 0.4,
) -> tuple[types.ModuleType, float] | None:
    """Scores every loaded plugin's can_handle(user_text, context) and
    returns the best one if its score clears `threshold`, else None. A
    plugin whose can_handle() raises is treated as a 0.0 score, not a
    crash."""
    modules, _errors = load_plugins(plugins_dir)
    best: tuple[types.ModuleType, float] | None = None
    for module in modules:
        try:
            score = float(module.can_handle(user_text, context or {}))
        except Exception:
            logging.exception("plugin_manager: can_handle raised in %s", module.PLUGIN_NAME)
            score = 0.0
        score = max(0.0, min(1.0, score))
        if best is None or score > best[1]:
            best = (module, score)
    if best and best[1] >= threshold:
        return best
    return None


async def safe_dispatch(module: types.ModuleType, update, context, parsed_task: dict[str, Any]) -> dict[str, Any]:
    """Calls plugin.handle(update, context, parsed_task), never letting a
    plugin exception propagate into the bot's own event loop."""
    try:
        result = await module.handle(update, context, parsed_task)
        if not isinstance(result, dict):
            result = {"success": True, "result": result}
        result.setdefault("success", True)
        return result
    except Exception as exc:  # noqa: BLE001
        logging.exception("plugin_manager: plugin %s raised during handle()", getattr(module, "PLUGIN_NAME", "?"))
        return {"success": False, "error": str(exc)}


def run_plugin_smoke_tests(module: types.ModuleType) -> dict[str, Any]:
    """Runs a plugin's own smoke_tests(). smoke_tests() may return either a
    list of (name, callable) pairs to invoke here, or a plain callable that
    runs its own assertions and returns True/raises. Either way, a single
    failing case is captured (not raised), and reported back."""
    results: list[dict[str, Any]] = []
    try:
        cases = module.smoke_tests()
    except Exception as exc:
        return {"success": False, "error": f"smoke_tests() сам упал: {exc}", "cases": []}

    if callable(cases) and not isinstance(cases, (list, tuple)):
        try:
            cases()
            results.append({"name": "smoke_tests", "success": True})
        except Exception as exc:
            results.append({"name": "smoke_tests", "success": False, "error": str(exc)})
    else:
        for item in cases or []:
            name, fn = item if isinstance(item, (list, tuple)) and len(item) == 2 else (str(item), None)
            if not callable(fn):
                results.append({"name": name, "success": False, "error": "test case не вызываемый"})
                continue
            try:
                fn()
                results.append({"name": name, "success": True})
            except Exception as exc:
                results.append({"name": name, "success": False, "error": str(exc)})

    success = bool(results) and all(r["success"] for r in results)
    return {"success": success, "cases": results}
