"""Transactional snapshot/rollback for workspace site edits.

Every edit to an existing workspace project must be preceded by a snapshot of
its current text files (data/workspace_snapshots/<project>/<snapshot_id>/).
If the edit's acceptance checks fail, rollback_project() restores those exact
files, so a failed automatic edit can never leave the live site broken.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from tools_edit import read_workspace_project_files
from tools_fs import ToolError
from tools_write import _validate_project_name, write_project_text_file

SNAPSHOT_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}_[0-9]{6}$")
MAX_SNAPSHOTS_PER_PROJECT = 30


def _project_snapshots_dir(project_name: str) -> Path:
    project = _validate_project_name(project_name)
    path = config.get_workspace_snapshots_dir() / project
    path.mkdir(parents=True, exist_ok=True)
    return path


def _validate_snapshot_id(snapshot_id: str) -> str:
    if not snapshot_id or not SNAPSHOT_ID_RE.match(snapshot_id):
        raise ToolError(f"Некорректный snapshot_id: {snapshot_id}")
    return snapshot_id


def _new_snapshot_id() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%S_") + f"{now.microsecond:06d}"


def _prune_old_snapshots(project_name: str) -> None:
    snapshots = sorted(_project_snapshots_dir(project_name).iterdir(), key=lambda p: p.name)
    excess = len(snapshots) - MAX_SNAPSHOTS_PER_PROJECT
    if excess <= 0:
        return
    import shutil

    for stale_dir in snapshots[:excess]:
        if stale_dir.is_dir():
            shutil.rmtree(stale_dir, ignore_errors=True)


def snapshot_project(project_name: str, *, reason: str = "") -> dict[str, Any]:
    """Captures the project's current editable text files (the same set
    edit_workspace_site_workflow reads/writes) into a new timestamped
    snapshot directory. Read-only with respect to the live project."""
    project = _validate_project_name(project_name)
    read_result = read_workspace_project_files(project)
    snapshot_id = _new_snapshot_id()
    snapshot_dir = _project_snapshots_dir(project) / snapshot_id
    files_dir = snapshot_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    saved_files: list[str] = []
    for f in read_result["files"]:
        relative = f["path"]
        target = files_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f["content"], encoding="utf-8")
        saved_files.append(relative)

    manifest = {
        "snapshot_id": snapshot_id,
        "project_name": project,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "files": saved_files,
    }
    (snapshot_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    _prune_old_snapshots(project)
    return manifest


def list_snapshots(project_name: str) -> list[dict[str, Any]]:
    project = _validate_project_name(project_name)
    base = _project_snapshots_dir(project)
    manifests: list[dict[str, Any]] = []
    for child in base.iterdir():
        manifest_path = child / "manifest.json"
        if child.is_dir() and manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            manifests.append(manifest)
    manifests.sort(key=lambda m: m.get("snapshot_id", ""), reverse=True)
    return manifests


def get_snapshot(project_name: str, snapshot_id: str) -> dict[str, Any]:
    project = _validate_project_name(project_name)
    sid = _validate_snapshot_id(snapshot_id)
    snapshot_dir = _project_snapshots_dir(project) / sid
    manifest_path = snapshot_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ToolError(f"Snapshot не найден: {sid}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = []
    for relative in manifest.get("files", []):
        file_path = snapshot_dir / "files" / relative
        if file_path.is_file():
            files.append({"path": relative, "content": file_path.read_text(encoding="utf-8")})
    manifest["loaded_files"] = files
    return manifest


def rollback_project(project_name: str, snapshot_id: str) -> dict[str, Any]:
    """Restores every file recorded in the snapshot back into the live
    project, overwriting whatever a failed edit left behind."""
    project = _validate_project_name(project_name)
    snapshot = get_snapshot(project, snapshot_id)
    restored: list[str] = []
    errors: list[str] = []
    for entry in snapshot["loaded_files"]:
        try:
            write_project_text_file(project, entry["path"], entry["content"], overwrite=True)
            restored.append(entry["path"])
        except ToolError as e:
            errors.append(f"{entry['path']}: {e}")
    return {
        "project_name": project,
        "snapshot_id": snapshot_id,
        "restored_files": restored,
        "errors": errors,
        "success": bool(restored) and not errors,
    }
