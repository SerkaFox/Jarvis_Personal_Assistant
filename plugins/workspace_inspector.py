"""Reference plugin: answers "what files/images/structure do you have"
questions about a workspace project using only the existing deterministic,
read-only WRITE_ROOT tools (list_workspace_project_files,
list_workspace_project_images, tree_workspace_project) -- never lets an LLM
improvise file names.

This is the canonical example of the plugin/selfdev pattern: semantic
routing decides *whether* this plugin applies (can_handle), then it always
calls the same safe tool, then it formats a human answer. The specific
trigger phrases below exist only to make can_handle's judgment concrete and
testable (see tests/test_workspace_inspector_plugin.py) -- they are not the
architecture, just one cheap signal that can_handle uses. A different
implementation could swap them for an embedding/LLM classifier without
changing handle() or the plugin interface at all.
"""

from typing import Any

PLUGIN_NAME = "workspace_inspector"
PLUGIN_VERSION = "0.1.0"
PLUGIN_DESCRIPTION = (
    "Отвечает на вопросы про реальные файлы/изображения/структуру workspace-проекта "
    "(list_workspace_project_files/list_workspace_project_images/tree_workspace_project), "
    "только read-only, никогда не выдумывает файлы."
)

IMAGE_INTENT_PHRASES = (
    "какие фото", "какие фотки", "какие изображения", "какие картинки",
    "какие фоны", "какой фон", "фото для фона", "фотографии для фона",
    "картинки для фона", "изображения для фона", "покажи фото", "покажи изображения",
    "background files", "background images", "what images", "what photos",
    "images in project", "photos in project", "show images", "list images",
    "qué imágenes", "que imagenes", "qué fondos", "que fondos", "imágenes del proyecto",
    "archivos de imagen",
)

TREE_INTENT_PHRASES = (
    "структура проекта", "дерево проекта", "дерево файлов", "project structure",
    "project tree", "estructura del proyecto",
)

FILE_INTENT_PHRASES = (
    "какие файлы", "найди файлы", "что лежит в папке", "что лежит в проекте",
    "список файлов", "покажи файлы", "покажи список файлов", "файлы проекта",
    "list files", "find files", "what files", "files in project", "show files",
    "archivos en el proyecto", "lista de archivos", "qué archivos", "que archivos",
)


def _matches(text: str, phrases: tuple[str, ...]) -> bool:
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in phrases)


def _intent(user_text: str) -> str | None:
    """Returns "images", "tree", "files", or None. Order matters: an image
    question should be answered with the image listing even though it also
    mentions "фото"/"файл"-adjacent words."""
    if _matches(user_text, IMAGE_INTENT_PHRASES):
        return "images"
    if _matches(user_text, TREE_INTENT_PHRASES):
        return "tree"
    if _matches(user_text, FILE_INTENT_PHRASES):
        return "files"
    return None


def can_handle(user_text: str, context: dict[str, Any] | None = None) -> float:
    context = context or {}
    if not context.get("project_name"):
        return 0.0
    return 0.95 if _intent(user_text or "") else 0.0


async def handle(update, context, parsed_task: dict[str, Any]) -> dict[str, Any]:
    from tools_fs import ToolError
    from tools_media import list_workspace_project_images
    from tools_write import list_workspace_project_files, tree_workspace_project

    user_text = parsed_task.get("user_text", "")
    project = parsed_task.get("project_name")
    if not project:
        return {"success": False, "error": "no project resolved", "answer": "На какой сайт смотреть? Уточни имя проекта."}

    intent = _intent(user_text) or "files"
    try:
        if intent == "images":
            result = list_workspace_project_images(project)
            images = result["images"]
            if not images:
                searched = ", ".join(result["searched_dirs"]) or "assets/img, assets/images, static/img, public/img"
                answer = f"В проекте {project} изображений не найдено (искал в: {searched})."
            else:
                lines = [f"В проекте {project} нашёл изображения:"]
                lines.extend(f"- {img['path']}" for img in images)
                answer = "\n".join(lines)
        elif intent == "tree":
            result = tree_workspace_project(project, depth=3)
            answer = f"Структура проекта {project}:\n{result['tree']}"
        else:
            result = list_workspace_project_files(project, depth=3)
            files = result["files"]
            if not files:
                answer = f"В проекте {project} файлов не найдено."
            else:
                shown = files[:100]
                lines = [f"В проекте {project} нашёл файлы:"]
                lines.extend(f"- {f['path']}" for f in shown)
                if len(files) > len(shown):
                    lines.append(f"... и ещё {len(files) - len(shown)} файлов")
                answer = "\n".join(lines)
        return {"success": True, "answer": answer}
    except ToolError as exc:
        return {"success": False, "error": str(exc), "answer": f"Не смог получить список файлов проекта {project}: {exc}"}


def _assert(condition: bool, message: str = "smoke test condition failed") -> None:
    if not condition:
        raise AssertionError(message)


def smoke_tests() -> list:
    def case_scores_image_question():
        _assert(can_handle("какие фото для фона у тебя есть?", {"project_name": "hola"}) > 0.5)

    def case_ignores_unrelated_text():
        _assert(can_handle("привет, как дела?", {"project_name": "hola"}) == 0.0)

    def case_requires_project():
        _assert(can_handle("какие файлы у тебя есть?", {}) == 0.0)

    def case_scores_file_question():
        _assert(can_handle("найди файлы в папке сайта hola", {"project_name": "hola"}) > 0.5)

    return [
        ("can_handle scores image question with project", case_scores_image_question),
        ("can_handle ignores unrelated text", case_ignores_unrelated_text),
        ("can_handle requires project", case_requires_project),
        ("can_handle scores file question with project", case_scores_file_question),
    ]
