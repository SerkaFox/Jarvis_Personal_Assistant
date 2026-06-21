"""Deterministic entity resolution: given free text + chat context, figure
out which REAL thing the user means -- by checking what actually exists
(workspace projects in WRITE_ROOT, git repos under ALLOWED_ROOTS, allowed
services, pending Telegram media, the chat's current_project) -- never by
guessing from a token's spelling alone. This is what task_orchestrator.py
uses to decide where a name like "kiki" actually points before picking a
task type, so a workspace site and a same-named git repo can never be
confused.
"""
import re
from typing import Any

import memory
from tools_pending_media import get_latest_available_media
from tools_system import get_allowed_services
from tools_write import list_workspace

GIT_REFERENCE_WORDS = ("git", "репозитор", "repo", "branch", "ветка", "commit", "коммит")


def _workspace_project_names() -> list[str]:
    try:
        return [str(item.get("name") or "") for item in list_workspace().get("projects", []) if item.get("name")]
    except Exception:
        return []


def resolve_workspace_project(text: str, chat_id: str | None = None) -> str | None:
    """Returns a real WRITE_ROOT project name mentioned in `text`, the
    last/current project from chat context if still valid, or None. Never
    returns a token that isn't an actual project on disk."""
    lowered = (text or "").lower()
    names = _workspace_project_names()

    for name in names:
        if re.search(rf"(?<![A-Za-z0-9_.-]){re.escape(name.lower())}(?![A-Za-z0-9_.-])", lowered):
            return name

    if chat_id:
        current = memory.get_current_project(chat_id)
        if current and current in names:
            return current
    return None


def resolve_git_repo(text: str, *, allow_implicit: bool = False) -> str | None:
    """Returns a real git repo name found under ALLOWED_ROOTS and mentioned in
    `text`. Only called when the text actually looks git-related (mentions
    git/repo/branch/commit) unless allow_implicit=True, since walking
    ALLOWED_ROOTS is comparatively expensive and shouldn't run on every
    message just to rule git out."""
    lowered = (text or "").lower()
    if not allow_implicit and not any(word in lowered for word in GIT_REFERENCE_WORDS):
        return None
    try:
        from tools_git import find_git_repos

        repos = find_git_repos()["repositories"]
    except Exception:
        return None
    for repo in repos:
        name = str(repo.get("name") or "")
        if name and re.search(rf"(?<![A-Za-z0-9_.-]){re.escape(name.lower())}(?![A-Za-z0-9_.-])", lowered):
            return name
    return None


def resolve_service(text: str) -> str | None:
    lowered = (text or "").lower()
    for service in get_allowed_services():
        if service.lower() in lowered:
            return service
    return None


def resolve_pending_media(chat_id: str | None) -> dict[str, Any] | None:
    if not chat_id:
        return None
    return get_latest_available_media(chat_id)


def resolve_current_project(chat_id: str | None) -> str | None:
    if not chat_id:
        return None
    return memory.get_current_project(chat_id)


def resolve_entities(text: str, chat_id: str | None = None) -> dict[str, Any]:
    """One-shot resolution of every entity kind task_orchestrator cares about.
    git_repo resolution is skipped unless the text actually looks git-related
    (cheap by default; see resolve_git_repo)."""
    return {
        "workspace_project": resolve_workspace_project(text, chat_id),
        "git_repo": resolve_git_repo(text),
        "service": resolve_service(text),
        "pending_media": resolve_pending_media(chat_id),
        "current_project": resolve_current_project(chat_id),
    }
