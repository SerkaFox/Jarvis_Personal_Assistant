"""Single decision point for what Jarvis should do with a free-text message.

This sits ABOVE semantic_router / edit_workspace_site / git routing.
bot.handle_text calls resolve_task() once, with real state already gathered
(does pending media exist? does a workspace project by that name exist?),
and dispatches on the returned task_type -- instead of letting several
independent phrase-heuristics and an LLM-based classifier race each other
and sometimes disagree (the "kiki" bug: "на сайт kiki как фон" falling
through to edit_workspace_site because no single check had both the photo
and the target word; "проверь слайдер в kiki" landing in git project
inspection because semantic_router's free-form classification conflated
"project" with "git repository").

The rule that matters most: task_type is picked from STATE (does
pending_media actually exist right now? does "kiki" actually exist as a
workspace project?), never from phrasing alone. Phrases only decide which
state-backed branch applies.
"""
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
import entity_resolver
import memory
import ui_component_model

TASK_TYPES = {
    "apply_media_to_site",
    "create_site",
    "edit_site",
    "check_site",
    "preview_start",
    "delete_project",
    "unknown",
}

# "из папки" style phrases -- an explicit request to use an existing project
# image instead of whatever photo was just sent.
FOLDER_SOURCE_PHRASES = (
    "из папки", "любое изображение из папки", "любую картинку из папки", "любое фото из папки",
    "существующее изображение", "уже загруженн", "any image from the folder", "any photo from the folder",
    "from the folder", "existing image", "cualquier imagen de la carpeta", "de la carpeta",
)

# Words that mean "apply this to the site somewhere" -- background, hero,
# footer, or the site/project itself. Broad on purpose: when pending_media
# exists, any of these is enough to mean "use the photo I just sent",
# matching task spec item 3 ("на сайт kiki как фон" has no photo-reference
# word at all, only "сайт"+"фон").
TARGET_REFERENCE_WORDS = (
    "фон", "background", "hero", "херо", "fondo",
    "footer", "футер", "подвал",
    "сайт", "site", "sitio", "проект", "project",
)

# Narrower -- specifically background/hero/footer, used to gate the
# existing_project_image ("любой фон из папки") branch so a bare "на сайт
# kiki" without any background-ish word doesn't misfire into apply_media.
BACKGROUND_LIKE_WORDS = ("фон", "background", "hero", "херо", "fondo", "footer", "футер", "подвал")

PHOTO_REFERENCE_WORDS = (
    "фото", "фотк", "фотограф", "снимок", "картинк", "изображен",
    "это", "него", "ее", "её", "его",
    "photo", "picture", "image", "esta foto", "esa foto", "la foto",
)

CHECK_SITE_WORDS = (
    "проверь", "проверка", "check", "verifica", "comprueba", "compru",
    "что не так", "работает ли", "работают ли", "что сломалось", "is it working",
)

CREATE_SITE_WORDS = (
    "создай сайт", "создай проект", "сделай сайт", "новый сайт",
    "create a site", "create a project", "new site", "crea un sitio", "nuevo sitio",
)

PREVIEW_START_WORDS = (
    "запусти сервер", "запусти preview", "запусти превью", "start preview", "launch preview",
    "запусти сайт", "inicia el servidor",
)

DELETE_WORDS = (
    "удали проект", "удали сайт", "снеси проект", "delete project", "remove project", "borra el proyecto",
)


def _lower(text: str) -> str:
    return (text or "").lower()


def _wants_folder_source(text: str) -> bool:
    lowered = _lower(text)
    return any(phrase in lowered for phrase in FOLDER_SOURCE_PHRASES)


def _mentions_target_reference(text: str) -> bool:
    lowered = _lower(text)
    return any(word in lowered for word in TARGET_REFERENCE_WORDS)


def _mentions_background_like(text: str) -> bool:
    lowered = _lower(text)
    return any(word in lowered for word in BACKGROUND_LIKE_WORDS)


def _mentions_photo_reference(text: str) -> bool:
    lowered = _lower(text)
    return any(word in lowered for word in PHOTO_REFERENCE_WORDS)


def _mentions_check(text: str) -> bool:
    lowered = _lower(text)
    return any(word in lowered for word in CHECK_SITE_WORDS)


def _mentions_create(text: str) -> bool:
    lowered = _lower(text)
    return any(word in lowered for word in CREATE_SITE_WORDS)


def _mentions_preview_start(text: str) -> bool:
    lowered = _lower(text)
    return any(word in lowered for word in PREVIEW_START_WORDS)


def _mentions_delete(text: str) -> bool:
    lowered = _lower(text)
    return any(word in lowered for word in DELETE_WORDS)


@dataclass
class TaskDecision:
    task_type: str
    workspace_project: str | None = None
    git_repo: str | None = None
    media_source: str | None = None  # "pending_media" | "existing_project_image" | None
    component_kind: str | None = None  # set when check_site names a specific UI component (see ui_component_model)
    reason: str = ""
    confidence: float = 1.0
    entities: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # pending_media's raw dict (telegram file ids, timestamps) is internal
        # plumbing, not useful/safe to dump in /task_debug -- keep only a
        # boolean flag in the persisted/serialized form.
        entities = dict(data.get("entities") or {})
        if "pending_media" in entities:
            entities["pending_media"] = bool(entities["pending_media"])
        data["entities"] = entities
        return data


def resolve_task(user_text: str, chat_id: str | None = None) -> TaskDecision:
    entities = entity_resolver.resolve_entities(user_text, chat_id)
    workspace_project = entities["workspace_project"] or entities["current_project"]
    has_pending_media = bool(entities["pending_media"])
    wants_folder_source = _wants_folder_source(user_text)

    # task_orchestrator is the single place that remembers which workspace
    # project a chat is talking about -- every resolved decision updates it,
    # so the next turn's "current_project" fallback is always accurate.
    if chat_id and workspace_project:
        memory.set_current_project(chat_id, workspace_project)

    # 1) A just-sent photo always wins over edit_site when the text references
    #    the photo itself OR any site/background/hero/footer target -- unless
    #    the user explicitly asked for an existing image from the folder.
    if has_pending_media and not wants_folder_source:
        if _mentions_photo_reference(user_text) or _mentions_target_reference(user_text):
            decision = TaskDecision(
                task_type="apply_media_to_site",
                workspace_project=workspace_project,
                media_source="pending_media",
                reason="pending_media + photo/target reference in text",
                entities=entities,
            )
            _save_last_decision(chat_id, decision)
            return decision

    # 2) Explicit "existing image from the folder" background request, or a
    #    background request with no pending photo to fall back on.
    if _mentions_background_like(user_text) and (wants_folder_source or not has_pending_media):
        decision = TaskDecision(
            task_type="apply_media_to_site",
            workspace_project=workspace_project,
            media_source="existing_project_image",
            reason="folder-sourced background request" if wants_folder_source else "background request, no pending media",
            entities=entities,
        )
        _save_last_decision(chat_id, decision)
        return decision

    # 3) check_site -- requires a resolvable workspace project, otherwise this
    #    isn't a site check at all (could be a git/code question instead).
    # "проверь слайдер/карусель/меню/гармонь/форму/языки/фон" names a specific
    # UI component (ui_component_model.normalize_kind) -- this routes to
    # verify_ui_component (a real DOM/Playwright probe), not git tools and not
    # normal chat, regardless of which component it is or how it's built.
    if _mentions_check(user_text) and workspace_project:
        component_kind = ui_component_model.normalize_kind(user_text)
        decision = TaskDecision(
            task_type="check_site",
            workspace_project=workspace_project,
            component_kind=component_kind,
            reason=(
                f"check phrase + component kind={component_kind} + resolvable workspace project"
                if component_kind
                else "check phrase + resolvable workspace project"
            ),
            entities=entities,
        )
        _save_last_decision(chat_id, decision)
        return decision

    if _mentions_create(user_text):
        decision = TaskDecision(
            task_type="create_site", workspace_project=workspace_project, reason="create phrase", entities=entities
        )
        _save_last_decision(chat_id, decision)
        return decision

    if _mentions_preview_start(user_text) and workspace_project:
        decision = TaskDecision(
            task_type="preview_start", workspace_project=workspace_project, reason="preview phrase", entities=entities
        )
        _save_last_decision(chat_id, decision)
        return decision

    if _mentions_delete(user_text) and workspace_project:
        decision = TaskDecision(
            task_type="delete_project", workspace_project=workspace_project, reason="delete phrase", entities=entities
        )
        _save_last_decision(chat_id, decision)
        return decision

    if workspace_project and entities["git_repo"] is None:
        decision = TaskDecision(
            task_type="edit_site",
            workspace_project=workspace_project,
            reason="resolvable workspace project, no more specific match",
            confidence=0.5,
            entities=entities,
        )
        _save_last_decision(chat_id, decision)
        return decision

    decision = TaskDecision(task_type="unknown", reason="no deterministic match", confidence=0.0, entities=entities)
    _save_last_decision(chat_id, decision)
    return decision


def _decisions_path() -> Path:
    path = config._data_dir() / "task_orchestrator_decisions.json"
    return path


def _load_decisions() -> dict[str, Any]:
    path = _decisions_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_last_decision(chat_id: str | None, decision: TaskDecision) -> None:
    key = str(chat_id or "global")
    data = _load_decisions()
    payload = decision.to_dict()
    payload["user_text_at"] = datetime.now(timezone.utc).isoformat()
    data[key] = payload
    _decisions_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_last_decision(chat_id: str | None) -> dict[str, Any] | None:
    return _load_decisions().get(str(chat_id or "global"))


def format_last_decision(chat_id: str | None) -> str:
    decision = get_last_decision(chat_id)
    if not decision:
        return "task_orchestrator: нет сохранённых решений для этого чата."
    lines = [
        f"task_type: {decision.get('task_type')}",
        f"workspace_project: {decision.get('workspace_project') or '-'}",
        f"git_repo: {decision.get('git_repo') or '-'}",
        f"media_source: {decision.get('media_source') or '-'}",
        f"reason: {decision.get('reason') or '-'}",
        f"confidence: {decision.get('confidence')}",
        f"at: {decision.get('user_text_at') or '-'}",
    ]
    return "\n".join(lines)
