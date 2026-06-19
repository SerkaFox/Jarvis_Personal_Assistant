import json
import re
from typing import Any, Callable


ALLOWED_INTENTS = {
    "normal_chat",
    "workspace_inventory",
    "create_static_site",
    "create_and_preview",
    "where_project",
    "preview_start",
    "preview_stop",
    "preview_stop_all",
    "workspace_delete",
    "project_inspect",
    "safe_code_check",
    "git_repos",
    "git_status",
    "git_diff",
    "memory_save",
    "memory_query",
    "last_action",
    "last_error",
    "unknown",
}

CLARIFY_MESSAGE = (
    "Не смог надёжно распознать действие. "
    "Уточни командой /create_and_preview <name> или /workspace_status."
)

LOW_CONFIDENCE_THRESHOLD = 0.55

ACTION_VERBS_RU = (
    "создай", "создать", "сделай", "сделать", "запусти", "запустить",
    "останови", "остановить", "удали", "удалить", "выключи", "выключить",
    "отключи", "отключить", "стопни", "снеси", "снести",
)
ACTION_VERBS_EN = (
    "create", "make", "build", "start", "launch", "spin up",
    "stop", "delete", "remove", "kill", "shut down", "shutdown", "tear down",
)
ACTION_VERBS_ES = (
    "crea", "crear", "haz", "hacer", "inicia", "iniciar", "levanta", "levantar",
    "para", "parar", "detén", "detener", "elimina", "eliminar", "borra", "borrar",
    "apaga", "apagar",
)
ACTION_VERBS = ACTION_VERBS_RU + ACTION_VERBS_EN + ACTION_VERBS_ES


def is_action_like(text: str) -> bool:
    lowered = (text or "").lower()
    return any(verb in lowered for verb in ACTION_VERBS)


ROUTER_SYSTEM_PROMPT = """
You are a strict semantic intent classifier for Jarvis, a local assistant that manages a
sandboxed website workspace (WRITE_ROOT) and read-only git repository inspection.

You understand user intent by MEANING, in any language (Russian, English, Spanish, or
others) — not by matching fixed phrases. Paraphrases, typos, and mixed languages must
still be classified correctly.

You NEVER execute anything yourself. You NEVER write shell commands, code, or file
content. You ONLY return one JSON object describing what the backend should do. The
backend is the only thing that calls real tools.

Return ONLY a single JSON object, no markdown, no explanation outside the JSON:
{
  "intent": "<one of the allowed intents>",
  "confidence": <number 0.0-1.0>,
  "project_name": <string or null>,
  "target": <string or null>,
  "needs_tool": <true|false>,
  "start_preview": <true|false>,
  "language": "<ru|en|es|other>",
  "reason": "<short explanation, one sentence>"
}

Allowed intents and their MEANING (not literal phrases):

- workspace_inventory: user asks what sites/projects/folders exist in the assistant's
  own workspace, or what ports those previews are running on. Examples (ru/en/es):
  "какие проекты в твоей папке?", "what sites are in your workspace?",
  "qué proyectos tienes en tu carpeta de trabajo?", "на каких портах висят сайты?",
  "what ports are your preview servers running on?".

- create_static_site: user wants a new static site/project created in the workspace,
  WITHOUT asking for a running preview/server right now.

- create_and_preview: user wants a new site created AND a temporary local server/URL
  started immediately. Examples: "создай сайт sitebota и запусти сервер",
  "create a landing page called sitebota and give me a local URL",
  "crea una web sitebota y levanta un servidor temporal".

- where_project: user asks where a specific existing project lives or how to open it
  (path/URL). Examples: "где ты создал sitebota?", "where can I open sitebota?",
  "dónde está el proyecto sitebota?".

- preview_start: user wants the preview/server for an existing project started (project
  is not necessarily newly created).

- preview_stop: user wants the preview/server for one specific project stopped.

- preview_stop_all: user wants ALL running preview servers stopped at once ("останови
  все сервера", "stop all preview servers", "para todos los servidores").

- workspace_delete: user wants a project folder removed from the workspace.

- project_inspect: user wants a summary/status of a real git project (not the
  workspace) — "what's the state of project X", "на чем остановились в проекте X".

- safe_code_check: user wants a read-only code/error check of a real git project.

- git_repos: user asks what git repositories exist on the server in general (not the
  assistant's own workspace). Examples: "какие git репозитории?",
  "show git repositories", "muéstrame los repositorios git".

- git_status: user asks for git status/branch/remote of one specific repository.

- git_diff: user asks for the git diff of one specific repository.

- memory_save: user explicitly tells the assistant to remember a fact about them.

- memory_query: user asks what the assistant remembers about them.

- last_action: user asks what the assistant last did / the result of the last action.

- last_error: user asks about the last error that happened.

- normal_chat: anything else — general conversation, questions, opinions, small talk.
  This is the default when nothing else clearly applies.

- unknown: the message clearly requests SOME action/tool, but you cannot tell which one
  with reasonable confidence. Do not guess a specific tool intent in this case.

Rules:
- "your folder" / "your workspace" / "rabochaya papka" / "tu carpeta de trabajo" always
  refers to the assistant's own WRITE_ROOT workspace, never to git repositories, unless
  the user explicitly says "git" / "repositorio" / "репозиторий" / "branch" / "ветка".
- project_name should be a short slug/name if one is mentioned or clearly implied
  (e.g. from a quoted/capitalized word), otherwise null.
- confidence should reflect how sure you are; use lower values (<0.55) when the message
  is vague or could mean more than one thing.
- needs_tool is true for every intent except normal_chat (and usually unknown).
- start_preview is true only when the user explicitly wants a server/preview running.
- Never include explanations, markdown fences, or text outside the single JSON object.
""".strip()


def build_router_messages(
    user_text: str,
    recent_messages: list[dict[str, Any]] | None = None,
    current_project: str | None = None,
    last_action: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    context_lines = []
    if current_project:
        context_lines.append(f"current_project: {current_project}")
    if last_action:
        context_lines.append(
            "last_action: intent={} project={} success={}".format(
                last_action.get("intent"), last_action.get("project_name"), last_action.get("success")
            )
        )
    if recent_messages:
        tail = recent_messages[-4:]
        history = "; ".join(f"{m.get('role')}: {str(m.get('content', ''))[:120]}" for m in tail)
        if history:
            context_lines.append(f"recent_history: {history}")
    context_block = ("\n".join(context_lines) + "\n\n") if context_lines else ""
    return [
        {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
        {"role": "user", "content": f"{context_block}User message:\n{user_text}"},
    ]


def _extract_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.removeprefix("json").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        raw = raw[start : end + 1]
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("router response must be a JSON object")
    return parsed


def _normalize_classification(data: dict[str, Any]) -> dict[str, Any]:
    intent = data.get("intent")
    if intent not in ALLOWED_INTENTS:
        raise ValueError(f"unknown intent: {intent!r}")

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    project_name = data.get("project_name")
    if not isinstance(project_name, str) or not project_name.strip():
        project_name = None
    else:
        project_name = project_name.strip()

    target = data.get("target")
    if not isinstance(target, str) or not target.strip():
        target = None
    else:
        target = target.strip()

    needs_tool = bool(data.get("needs_tool", intent not in ("normal_chat", "unknown")))
    start_preview = bool(data.get("start_preview", False))

    language = data.get("language")
    if language not in ("ru", "en", "es", "other"):
        language = "other"

    reason = data.get("reason")
    if not isinstance(reason, str):
        reason = ""

    return {
        "intent": intent,
        "confidence": confidence,
        "project_name": project_name,
        "target": target,
        "needs_tool": needs_tool,
        "start_preview": start_preview,
        "language": language,
        "reason": reason[:300],
    }


def _failure_result(reason: str) -> dict[str, Any]:
    return {
        "intent": "unknown",
        "confidence": 0.0,
        "project_name": None,
        "target": None,
        "needs_tool": False,
        "start_preview": False,
        "language": "other",
        "reason": reason,
    }


def is_router_failure(classification: dict[str, Any]) -> bool:
    reason = classification.get("reason") or ""
    return classification.get("intent") == "unknown" and (
        reason.startswith("router_error") or reason == "no_model_or_empty_text"
    )


def classify_intent(
    user_text: str,
    recent_messages: list[dict[str, Any]] | None = None,
    current_project: str | None = None,
    last_action: dict[str, Any] | None = None,
    ask_model: Callable[[list[dict[str, str]]], str] | None = None,
) -> dict[str, Any]:
    if not (user_text or "").strip():
        return _failure_result("no_model_or_empty_text")
    if ask_model is None:
        return _failure_result("no_model_or_empty_text")

    try:
        messages = build_router_messages(user_text, recent_messages, current_project, last_action)
        raw = ask_model(messages)
        data = _extract_json(raw)
        return _normalize_classification(data)
    except Exception as e:
        return _failure_result(f"router_error: {e}")
