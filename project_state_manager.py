"""Richer per-project manifest layered on top of tools_site_state.py's
requirements file (same data/workspace_state/<project>.json -- one file, one
source of truth). Adds: real file hashes, a per-feature status table,
last_successful_snapshot / last_failed_action, user_constraints, and a capped
history of applied structured operations.

This is what "что делаешь сейчас?" and /site_history//site_last_success/
/site_diff//site_requirements read from -- always real persisted state, never
something Jarvis improvises in the moment.
"""
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import tools_site_state as site_state
from tools_edit import read_workspace_project_files
from tools_write import _validate_project_name

FEATURE_NAMES = ("background", "language_switcher", "slider", "weather", "footer", "media_assets", "sections")
MAX_HISTORY_ENTRIES = 50

DEFAULT_FEATURE_ENTRY: dict[str, Any] = {
    "status": "unknown",
    "selectors": [],
    "related_files": [],
    "last_verified_at": None,
    "verification_result": None,
}

FEATURE_REQUIREMENT_KEY = {
    "background": "background_required",
    "language_switcher": "language_switcher_required",
    "slider": "slider_required",
    "weather": "weather_required",
    "footer": "footer_required",
}


def _state_path(project_name: str):
    return site_state._state_path(project_name)


def _default_features() -> dict[str, Any]:
    return {name: dict(DEFAULT_FEATURE_ENTRY) for name in FEATURE_NAMES}


def load_project_state(project_name: str) -> dict[str, Any]:
    project = _validate_project_name(project_name)
    raw = site_state._read_raw(project)
    features = _default_features()
    stored_features = raw.get("features") if isinstance(raw.get("features"), dict) else {}
    for name in FEATURE_NAMES:
        entry = stored_features.get(name)
        if isinstance(entry, dict):
            features[name].update(entry)
    return {
        "project_name": project,
        "requirements": site_state.get_site_requirements(project),
        "files_hashes": raw.get("files_hashes") or {},
        "features": features,
        "last_successful_snapshot": raw.get("last_successful_snapshot"),
        "last_failed_action": raw.get("last_failed_action"),
        "user_constraints": list(raw.get("user_constraints") or []),
        "applied_operations_history": list(raw.get("applied_operations_history") or []),
        "updated_at": raw.get("updated_at"),
    }


def _write_raw(project_name: str, raw: dict[str, Any]) -> None:
    project = _validate_project_name(project_name)
    raw["project_name"] = project
    raw["updated_at"] = datetime.now(timezone.utc).isoformat()
    path = _state_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")


def compute_file_hashes(project_name: str) -> dict[str, str]:
    project = _validate_project_name(project_name)
    result = read_workspace_project_files(project)
    return {f["path"]: hashlib.sha256(f["content"].encode("utf-8")).hexdigest() for f in result["files"]}


def update_feature_status(
    project_name: str,
    feature_name: str,
    *,
    status: str,
    selectors: list[str] | None = None,
    related_files: list[str] | None = None,
    verification_result: Any = None,
) -> dict[str, Any]:
    if feature_name not in FEATURE_NAMES:
        raise ValueError(f"Unknown feature: {feature_name}")
    project = _validate_project_name(project_name)
    raw = site_state._read_raw(project)
    features = raw.get("features") if isinstance(raw.get("features"), dict) else {}
    entry = dict(DEFAULT_FEATURE_ENTRY)
    entry.update(features.get(feature_name) or {})
    entry["status"] = status
    if selectors is not None:
        entry["selectors"] = selectors
    if related_files is not None:
        entry["related_files"] = related_files
    entry["last_verified_at"] = datetime.now(timezone.utc).isoformat()
    entry["verification_result"] = verification_result
    features[feature_name] = entry
    raw["features"] = features
    _write_raw(project, raw)
    return entry


def _features_from_inspection(inspected: dict[str, Any]) -> dict[str, str]:
    """Maps tools_site_state.inspect_site_state()'s read-only findings onto
    feature status strings ("present"/"absent"). Never invents a feature that
    inspect_site_state didn't actually find in the files."""
    return {
        "background": "present" if inspected.get("has_background") else "absent",
        "language_switcher": "present" if inspected.get("has_language_switcher") else "absent",
        "slider": "present" if inspected.get("has_slider") else "absent",
        "weather": "present" if inspected.get("has_weather_block") else "absent",
        "footer": "present" if inspected.get("has_footer") else "absent",
    }


def sync_features_from_inspection(project_name: str) -> dict[str, Any]:
    """Read-only inspection -> persisted feature status table. Safe to call
    any time (e.g. /site_state, site_check) without an edit happening."""
    project = _validate_project_name(project_name)
    inspected = site_state.inspect_site_state(project)
    statuses = _features_from_inspection(inspected)
    raw = site_state._read_raw(project)
    features = raw.get("features") if isinstance(raw.get("features"), dict) else {}
    now = datetime.now(timezone.utc).isoformat()
    for name, status in statuses.items():
        entry = dict(DEFAULT_FEATURE_ENTRY)
        entry.update(features.get(name) or {})
        entry["status"] = status
        entry["last_verified_at"] = now
        features[name] = entry
    raw["features"] = features
    _write_raw(project, raw)
    return load_project_state(project)


def record_applied_operation(
    project_name: str,
    *,
    user_text: str,
    operations: list[dict[str, Any]],
    files_changed: list[str],
    checks: dict[str, Any],
    success: bool,
    snapshot_id: str | None,
) -> dict[str, Any]:
    project = _validate_project_name(project_name)
    raw = site_state._read_raw(project)
    history = list(raw.get("applied_operations_history") or [])
    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "user_text": user_text[:500],
        "operations": operations,
        "files_changed": files_changed,
        "checks_success": bool(checks.get("success")),
        "failed_checks": checks.get("failed") or [],
        "success": bool(success),
        "snapshot_id": snapshot_id,
    }
    history.append(entry)
    raw["applied_operations_history"] = history[-MAX_HISTORY_ENTRIES:]
    if success:
        raw["last_successful_snapshot"] = snapshot_id
        raw["last_failed_action"] = None
    else:
        raw["last_failed_action"] = entry
    raw["files_hashes"] = compute_file_hashes(project)
    _write_raw(project, raw)
    sync_features_from_inspection(project)
    return entry


def diff_against_last_success(project_name: str) -> dict[str, Any]:
    import difflib

    from tools_snapshot import get_snapshot

    project = _validate_project_name(project_name)
    state = load_project_state(project)
    snapshot_id = state.get("last_successful_snapshot")
    if not snapshot_id:
        return {"project_name": project, "snapshot_id": None, "diffs": [], "note": "Нет успешного снапшота для сравнения."}
    snapshot = get_snapshot(project, snapshot_id)
    snapshot_files = {f["path"]: f["content"] for f in snapshot["loaded_files"]}
    current_files = {f["path"]: f["content"] for f in read_workspace_project_files(project)["files"]}

    diffs = []
    for path in sorted(set(snapshot_files) | set(current_files)):
        before = snapshot_files.get(path, "")
        after = current_files.get(path, "")
        if before == after:
            continue
        unified = list(
            difflib.unified_diff(
                before.splitlines(keepends=True), after.splitlines(keepends=True), fromfile=f"a/{path}", tofile=f"b/{path}"
            )
        )
        diffs.append({"path": path, "diff": "".join(unified)[:4000]})
    return {"project_name": project, "snapshot_id": snapshot_id, "diffs": diffs}


def format_requirements_answer(project_name: str) -> str:
    return site_state.format_site_state_answer(project_name)


def format_history_answer(project_name: str, limit: int = 10) -> str:
    state = load_project_state(project_name)
    history = state["applied_operations_history"][-limit:]
    if not history:
        return f"История изменений {project_name} пуста."
    lines = [f"История изменений {project_name} (последние {len(history)}):"]
    for entry in reversed(history):
        ops = ", ".join(op.get("op", "?") for op in entry.get("operations") or []) or "-"
        mark = "OK" if entry["success"] else "ROLLBACK"
        lines.append(f"- [{mark}] {entry['at']}: {ops} -- {entry['user_text'][:80]}")
    return "\n".join(lines)


def format_last_success_answer(project_name: str) -> str:
    state = load_project_state(project_name)
    snapshot_id = state.get("last_successful_snapshot")
    if not snapshot_id:
        return f"Для {project_name} ещё нет ни одной успешно принятой правки."
    return f"Последний успешный снапшот {project_name}: {snapshot_id}"


def format_diff_answer(project_name: str) -> str:
    result = diff_against_last_success(project_name)
    if not result.get("snapshot_id"):
        return result["note"]
    if not result["diffs"]:
        return f"Файлы {project_name} не отличаются от последнего успешного снапшота {result['snapshot_id']}."
    lines = [f"Diff {project_name} относительно последнего успешного снапшота {result['snapshot_id']}:"]
    for item in result["diffs"]:
        lines.append(f"--- {item['path']} ---")
        lines.append(item["diff"] or "(бинарное/без текстового diff)")
    return "\n".join(lines)


def format_current_activity_answer(project_name: str | None, current_task: dict[str, Any] | None) -> str:
    """Used by the "что делаешь сейчас?" handler -- reads real current_task /
    project_state, never invents an activity."""
    if current_task:
        return (
            f"Сейчас выполняю: {current_task.get('intent')} для проекта {current_task.get('project_name')}, "
            f"шаг: {current_task.get('step')}."
        )
    if not project_name:
        return "Сейчас не выполняю никакой задачи по сайтам."
    state = load_project_state(project_name)
    history = state["applied_operations_history"]
    if not history:
        return f"Сейчас не выполняю задач. По проекту {project_name} ещё не было применённых изменений."
    last = history[-1]
    mark = "успешно применено" if last["success"] else "не прошло проверку и было откачено"
    return f"Сейчас не выполняю задач. Последнее действие по {project_name}: {last['user_text'][:120]} -- {mark}."
