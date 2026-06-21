"""Append-only per-project task log (data/learning_log/<project>.jsonl).

Every applied task (success or rollback) gets one JSON line: what was asked,
what the operation plan was, what actually changed, whether checks passed,
and whether a rollback happened. mark_last_feedback() lets a later "да,
правильно"/"нет, сломал" message retroactively tag the most recent entry for
a chat -- raw material for future self-improvement/skills work, not used to
change live behavior yet.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from tools_write import _validate_project_name

MAX_TAIL_SCAN_LINES = 200


def _log_path(project_name: str) -> Path:
    project = _validate_project_name(project_name)
    return config.get_learning_log_dir() / f"{project}.jsonl"


def record(
    *,
    project_name: str,
    chat_id: str | None,
    user_text: str,
    detected_intent: str,
    before_state: dict[str, Any] | None,
    operation_plan: list[dict[str, Any]] | None,
    files_changed: list[str],
    checks: dict[str, Any],
    success: bool,
    rollback_used: bool,
) -> dict[str, Any]:
    project = _validate_project_name(project_name)
    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "project_name": project,
        "chat_id": chat_id,
        "user_text": (user_text or "")[:1000],
        "detected_intent": detected_intent,
        "before_state": before_state,
        "operation_plan": operation_plan,
        "files_changed": files_changed,
        "checks_success": bool((checks or {}).get("success")),
        "failed_checks": (checks or {}).get("failed") or [],
        "success": bool(success),
        "rollback_used": bool(rollback_used),
        "user_feedback": None,
    }
    path = _log_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def mark_last_feedback(project_name: str, *, chat_id: str | None, status: str) -> bool:
    """status: "approved" or "rejected". Rewrites the most recent matching
    entry's user_feedback field. Returns True if an entry was found."""
    path = _log_path(project_name)
    if not path.is_file():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return False
    start = max(0, len(lines) - MAX_TAIL_SCAN_LINES)
    target_idx = None
    for idx in range(len(lines) - 1, start - 1, -1):
        try:
            entry = json.loads(lines[idx])
        except json.JSONDecodeError:
            continue
        if chat_id is None or entry.get("chat_id") == chat_id:
            target_idx = idx
            break
    if target_idx is None:
        return False
    entry = json.loads(lines[target_idx])
    entry["user_feedback"] = status
    lines[target_idx] = json.dumps(entry, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def recent_entries(project_name: str, limit: int = 10) -> list[dict[str, Any]]:
    path = _log_path(project_name)
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    entries: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries
