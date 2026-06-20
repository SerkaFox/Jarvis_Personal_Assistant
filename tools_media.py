import logging
import mimetypes
import re
import time
import uuid
from pathlib import Path
from typing import Any

from tools_fs import ToolError
from tools_write import _validate_project_name, ensure_write_root, resolve_write_path, write_project_text_file


MAX_IMAGE_BYTES = 15 * 1024 * 1024
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_IMAGE_MIME = {"image/jpeg", "image/png", "image/webp"}
IMG_SUBDIR = Path("assets") / "img"
CSS_PATH = Path("assets") / "css" / "style.css"


def pillow_available() -> bool:
    try:
        import PIL  # noqa: F401
    except Exception:
        return False
    return True


def _detect_image_kind(data: bytes) -> str | None:
    if data[:2] == b"\xff\xd8":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _looks_like_svg(data: bytes) -> bool:
    head = data[:512].lstrip().lower()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head)


def _project_img_dir(project_name: str) -> Path:
    ensure_write_root()
    project = _validate_project_name(project_name)
    root = resolve_write_path(project).resolve()
    if not root.is_dir():
        raise ToolError(f"Проект не найден в WRITE_ROOT: {root}")
    img_dir = (root / IMG_SUBDIR).resolve()
    if img_dir != root and root not in img_dir.parents:
        raise ToolError("img dir вне WRITE_ROOT")
    img_dir.mkdir(parents=True, exist_ok=True)
    return img_dir


def _safe_image_name(original_name: str | None, suffix_hint: str) -> str:
    base = "image"
    ext = (suffix_hint or "").lower()
    if ext == ".jpe":
        ext = ".jpg"
    if original_name:
        candidate = Path(original_name).name
        candidate_ext = Path(candidate).suffix.lower()
        if candidate_ext in ALLOWED_IMAGE_EXTENSIONS:
            ext = candidate_ext
        stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", Path(candidate).stem).strip("-")
        if stem:
            base = stem[:40]
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise ToolError(f"Недопустимое расширение изображения: {ext or '-'}")
    unique = uuid.uuid4().hex[:8]
    return f"{base}-{unique}{ext}"


def save_telegram_image_to_project(
    project_name: str,
    file_bytes: bytes,
    original_name: str | None = None,
    mime_type: str | None = None,
) -> dict[str, Any]:
    if not file_bytes:
        raise ToolError("Пустой файл изображения")
    if len(file_bytes) > MAX_IMAGE_BYTES:
        raise ToolError(f"Файл слишком большой: {len(file_bytes)} > {MAX_IMAGE_BYTES} байт")
    if _looks_like_svg(file_bytes):
        raise ToolError("SVG изображения запрещены (риск встроенного script)")
    if mime_type and mime_type not in ALLOWED_IMAGE_MIME:
        raise ToolError(f"Недопустимый MIME тип: {mime_type}")
    kind = _detect_image_kind(file_bytes)
    if kind is None:
        raise ToolError("Файл не похож на изображение (ожидается jpg/png/webp)")

    suffix_hint = mimetypes.guess_extension(mime_type or f"image/{kind}") or f".{kind}"
    filename = _safe_image_name(original_name, suffix_hint)

    img_dir = _project_img_dir(project_name)
    dest = img_dir / filename
    dest.write_bytes(file_bytes)
    if not dest.is_file() or dest.stat().st_size != len(file_bytes):
        raise ToolError(f"Не удалось сохранить изображение на диск: {dest}")

    project = _validate_project_name(project_name)
    relative = str(IMG_SUBDIR / filename)
    logging.info("tools_media save_telegram_image_to_project %s -> %s", project, dest)
    return {"project_name": project, "path": str(dest), "relative_path": relative, "bytes": len(file_bytes)}


def convert_to_webp(input_path: str, output_path: str, max_width: int = 1600, quality: int = 82) -> dict[str, Any]:
    if not pillow_available():
        raise ToolError("Pillow не установлен, конвертация в webp недоступна")
    from PIL import Image

    src = Path(input_path)
    dst = Path(output_path)
    if not src.is_file():
        raise ToolError(f"Файл не найден: {src}")
    with Image.open(src) as img:
        if img.mode in ("P", "CMYK", "LA"):
            img = img.convert("RGB")
        elif img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        if img.width > max_width:
            ratio = max_width / float(img.width)
            new_size = (max_width, max(1, int(img.height * ratio)))
            img = img.resize(new_size, Image.LANCZOS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        img.save(dst, "WEBP", quality=quality)
        width, height = img.width, img.height
    logging.info("tools_media convert_to_webp %s -> %s", src, dst)
    return {"path": str(dst), "width": width, "height": height}


def list_project_images(project_name: str) -> dict[str, Any]:
    img_dir = _project_img_dir(project_name)
    images = []
    for path in sorted(img_dir.glob("*")):
        if path.is_file() and path.suffix.lower() in ALLOWED_IMAGE_EXTENSIONS | {".webp"}:
            images.append({"name": path.name, "path": str(path), "bytes": path.stat().st_size})
    return {"project_name": _validate_project_name(project_name), "images": images, "count": len(images)}


def _resolve_project_image_path(project_name: str, image_relative_path: str) -> dict[str, Any]:
    """Read-only: validates project name and that the path stays inside
    assets/img, then resolves it. Does not touch the filesystem and does not
    require the file to exist yet."""
    ensure_write_root()
    project = _validate_project_name(project_name)
    root = resolve_write_path(project).resolve()
    cleaned = (image_relative_path or "").strip().lstrip("/")
    if not cleaned or any(part == ".." for part in Path(cleaned).parts):
        raise ToolError(f"Недопустимый путь изображения: {image_relative_path!r}")
    candidate = (root / cleaned).resolve()
    img_dir = (root / IMG_SUBDIR).resolve()
    if img_dir != candidate and img_dir not in candidate.parents:
        raise ToolError("Изображение должно быть внутри assets/img проекта")
    return {"project_name": project, "relative_path": cleaned, "path": str(candidate)}


def set_hero_background(project_name: str, image_relative_path: str) -> dict[str, Any]:
    info = _resolve_project_image_path(project_name, image_relative_path)
    if not Path(info["path"]).is_file():
        raise ToolError(f"Изображение не найдено: {info['path']}")
    return info


async def download_telegram_file(bot, file_id: str, dest_path: str) -> dict[str, Any]:
    """Downloads a Telegram file to a destination path (inside WRITE_ROOT).
    bot is telegram.Bot from python-telegram-bot."""
    if not file_id:
        raise ToolError("file_id не задан")
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tg_file = await bot.get_file(file_id)
    data = bytes(await tg_file.download_as_bytearray())
    dest.write_bytes(data)
    if not dest.is_file() or dest.stat().st_size != len(data) or len(data) <= 0:
        raise ToolError(f"Не удалось скачать файл Telegram: {dest}")
    return {"path": str(dest), "bytes": len(data)}


def optimize_image_to_webp(
    input_path: str,
    output_path: str,
    max_width: int = 1920,
    quality: int = 82,
) -> dict[str, Any]:
    return convert_to_webp(input_path, output_path, max_width=max_width, quality=quality)


async def save_image_to_project(
    bot,
    project_name: str,
    telegram_file_id: str,
    original_name: str | None = None,
    mime_type: str | None = None,
) -> dict[str, Any]:
    """Downloads from Telegram, saves to WRITE_ROOT/<project>/assets/img, returns paths."""
    ensure_write_root()
    project = _validate_project_name(project_name)
    root = resolve_write_path(project).resolve()
    img_dir = _project_img_dir(project)
    tmp_name = f"tg_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    suffix = Path(original_name or "").suffix.lower()
    if suffix not in ALLOWED_IMAGE_EXTENSIONS:
        suffix = ".jpg"
    tmp_path = (img_dir / f"{tmp_name}{suffix}").resolve()
    if root not in tmp_path.parents:
        raise ToolError("Сохранение изображения вне проекта запрещено")
    downloaded = await download_telegram_file(bot, telegram_file_id, str(tmp_path))
    file_bytes = tmp_path.read_bytes()
    saved = save_telegram_image_to_project(project, file_bytes, original_name=original_name, mime_type=mime_type)
    try:
        if tmp_path.is_file():
            tmp_path.unlink()
    except Exception:
        pass
    return {**saved, "downloaded_bytes": downloaded["bytes"]}


def set_fixed_background(project_name: str, image_relative_path: str) -> dict[str, Any]:
    """Updates assets/css/style.css to reference the image as a fixed background.
    The URL is written as url(\"../img/<file>\") and inserted idempotently."""
    ensure_write_root()
    info = set_hero_background(project_name, image_relative_path)
    project = info["project_name"]
    root = resolve_write_path(project).resolve()
    css_path = (root / CSS_PATH).resolve()
    if not css_path.is_file():
        raise ToolError(f"CSS не найден: {css_path}")
    image_name = Path(info["relative_path"]).name
    css_url = f'url(\"../img/{image_name}\")'
    marker_start = "/* jarvis-fixed-background:start */"
    marker_end = "/* jarvis-fixed-background:end */"
    block = "\n".join(
        [
            marker_start,
            "body{",
            f"  background-image: {css_url};",
            "  background-size: cover;",
            "  background-position: center;",
            "  background-repeat: no-repeat;",
            "  background-attachment: fixed;",
            "}",
            "@media (prefers-reduced-motion: reduce){",
            "  body{ background-attachment: scroll; }",
            "}",
            marker_end,
            "",
        ]
    )
    original = css_path.read_text(encoding="utf-8", errors="replace")
    if marker_start in original and marker_end in original:
        before, _mid = original.split(marker_start, 1)
        _old, after = _mid.split(marker_end, 1)
        updated = before.rstrip() + "\n\n" + block + after.lstrip()
    else:
        updated = original.rstrip() + "\n\n" + block
    css_path.write_text(updated, encoding="utf-8")
    logging.info("tools_media set_fixed_background %s -> %s", project, css_path)
    return {"project_name": project, "css_path": str(css_path), "image_relative_path": info["relative_path"], "css_url": css_url}


def verify_background_asset(project_name: str, image_relative_path: str) -> dict[str, Any]:
    """Read-only check: never creates, modifies, or writes CSS/HTML/image files.
    Reports whether the image exists on disk and whether the CSS already
    references it; does not raise just because the image is missing."""
    info = _resolve_project_image_path(project_name, image_relative_path)
    project = info["project_name"]
    root = resolve_write_path(project).resolve()
    img_path = Path(info["path"])
    css_path = (root / CSS_PATH).resolve()
    ok_img = img_path.is_file() and img_path.stat().st_size > 0
    ok_css = css_path.is_file()
    css_text = css_path.read_text(encoding="utf-8", errors="replace") if ok_css else ""
    image_name = Path(info["relative_path"]).name
    expected = f"../img/{image_name}"
    ok_ref = expected in css_text
    return {
        "project_name": project,
        "image_path": str(img_path),
        "css_path": str(css_path),
        "image_exists": ok_img,
        "css_exists": ok_css,
        "css_references_image": ok_ref,
        "success": bool(ok_img and ok_css and ok_ref),
    }
