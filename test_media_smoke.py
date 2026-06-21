import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Force (not setdefault) -- some other test module imported earlier in a full
# suite run may have already triggered config.py's load_dotenv(), which would
# otherwise populate ALLOWED_USER_ID from the real .env and make is_allowed()
# reject the fake test users below. This must win regardless of import order.
os.environ["ALLOWED_USER_ID"] = "123"
os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")

import config


def _pillow_installed() -> bool:
    try:
        import PIL  # noqa: F401
    except Exception:
        return False
    return True


class MediaSmokeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jarvis_media_test_")
        self.old_write_mode = os.environ.get("WRITE_MODE_ENABLED")
        self.old_write_root = os.environ.get("WRITE_ROOT")
        self.old_db_path = os.environ.get("JARVIS_DB_PATH")
        self.old_config_db_path = config.JARVIS_DB_PATH
        os.environ["WRITE_MODE_ENABLED"] = "true"
        os.environ["WRITE_ROOT"] = self.tmp.name
        temp_db = str(Path(self.tmp.name) / "data" / "jarvis.db")
        os.environ["JARVIS_DB_PATH"] = temp_db
        config.JARVIS_DB_PATH = temp_db

    def tearDown(self):
        if self.old_write_mode is None:
            os.environ.pop("WRITE_MODE_ENABLED", None)
        else:
            os.environ["WRITE_MODE_ENABLED"] = self.old_write_mode
        if self.old_write_root is None:
            os.environ.pop("WRITE_ROOT", None)
        else:
            os.environ["WRITE_ROOT"] = self.old_write_root
        if self.old_db_path is None:
            os.environ.pop("JARVIS_DB_PATH", None)
        else:
            os.environ["JARVIS_DB_PATH"] = self.old_db_path
        config.JARVIS_DB_PATH = self.old_config_db_path
        self.tmp.cleanup()

    def test_pending_media_save_get_mark(self):
        from tools_pending_media import get_latest_pending_media, mark_media_used, save_pending_media

        item = save_pending_media("456", "123", "file_1", file_unique_id="u1", mime_type="image/jpeg", size_bytes=100)
        latest = get_latest_pending_media("456")
        self.assertIsNotNone(latest)
        self.assertEqual(latest["id"], item["id"])
        mark_media_used(int(item["id"]), "hola", "/tmp/x.webp")
        latest2 = get_latest_pending_media("456")
        self.assertIsNone(latest2)

    def test_set_fixed_background_writes_correct_css_url(self):
        from tools_write import create_project_dir, write_text_file
        from tools_media import set_fixed_background, verify_background_asset

        create_project_dir("hola")
        write_text_file("hola", "assets/css/style.css", "body{color:#000}\n", overwrite=False)
        img_path = Path(self.tmp.name) / "hola" / "assets" / "img" / "background.webp"
        img_path.parent.mkdir(parents=True, exist_ok=True)
        img_path.write_bytes(b"RIFF0000WEBP")  # header-like, not real but non-empty
        result = set_fixed_background("hola", "assets/img/background.webp")
        self.assertIn('../img/background.webp', Path(result["css_path"]).read_text(encoding="utf-8"))
        verify = verify_background_asset("hola", "assets/img/background.webp")
        self.assertTrue(verify["success"])

    def test_css_path_for_assets_img_bg_webp_resolves_to_parent_img(self):
        from tools_write import create_project_dir, write_text_file
        from tools_media import set_fixed_background

        create_project_dir("pathcheck")
        write_text_file("pathcheck", "assets/css/style.css", "body{color:#000}\n", overwrite=False)
        img_path = Path(self.tmp.name) / "pathcheck" / "assets" / "img" / "bg.webp"
        img_path.parent.mkdir(parents=True, exist_ok=True)
        img_path.write_bytes(b"RIFF0000WEBP")
        result = set_fixed_background("pathcheck", "assets/img/bg.webp")
        self.assertEqual(result["css_url"], 'url("../img/bg.webp")')

    def test_verify_background_asset_is_read_only(self):
        from tools_write import create_project_dir, write_text_file
        from tools_media import verify_background_asset

        create_project_dir("readonly")
        write_text_file("readonly", "assets/css/style.css", "body{color:#000}\n", overwrite=False)
        css_path = Path(self.tmp.name) / "readonly" / "assets" / "css" / "style.css"
        before = css_path.read_text(encoding="utf-8")
        before_mtime = css_path.stat().st_mtime_ns

        # Image does not even exist yet: verify must report failure, not raise,
        # and must not create/modify any files.
        result = verify_background_asset("readonly", "assets/img/missing.webp")
        self.assertFalse(result["success"])
        self.assertFalse(result["image_exists"])
        self.assertEqual(css_path.read_text(encoding="utf-8"), before)
        self.assertEqual(css_path.stat().st_mtime_ns, before_mtime)
        img_dir = Path(self.tmp.name) / "readonly" / "assets" / "img"
        self.assertFalse(img_dir.exists() and any(img_dir.iterdir()))

    def test_mark_media_failed_does_not_mark_used(self):
        from tools_pending_media import get_latest_pending_media, mark_media_failed, save_pending_media

        item = save_pending_media("789", "123", "file_2", file_unique_id="u2", mime_type="image/jpeg", size_bytes=100)
        mark_media_failed(int(item["id"]), "hola", "")
        # 'failed' must not be returned by get_latest_pending_media (which only
        # returns status='pending'), and must not be 'used'.
        self.assertIsNone(get_latest_pending_media("789"))
        import sqlite3
        import config

        conn = sqlite3.connect(config.JARVIS_DB_PATH)
        row = conn.execute("select status from pending_media where id=?", (item["id"],)).fetchone()
        conn.close()
        self.assertEqual(row[0], "failed")

    def test_add_background_image_workflow_marks_failed_not_used_on_early_error(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        from tools_pending_media import save_pending_media
        from tools_write import create_static_site
        import sqlite3
        import config

        create_static_site("failearly")
        media = save_pending_media("999", "1", "file_3", file_unique_id="u3", mime_type="image/jpeg", size_bytes=10)

        import bot

        message = AsyncMock()
        message.chat = type("C", (), {"id": "999"})()
        message.from_user = type("U", (), {"id": "1"})()
        message.text = ""
        message.caption = ""

        with patch.object(bot, "save_image_to_project", AsyncMock(side_effect=RuntimeError("boom"))):
            asyncio.run(
                bot.add_background_image_workflow(
                    message, context=AsyncMock(), project_name="failearly", media=media
                )
            )

        conn = sqlite3.connect(config.JARVIS_DB_PATH)
        row = conn.execute("select status from pending_media where id=?", (media["id"],)).fetchone()
        conn.close()
        self.assertEqual(row[0], "failed")

    async def test_text_after_pending_photo_routes_to_background_workflow(self):
        from tools_pending_media import save_pending_media
        from tools_write import create_static_site
        import bot
        from test_error_smoke import FakeContext, FakeUpdate

        create_static_site("hola")
        save_pending_media("456", "123", "file_1", file_unique_id="u1", mime_type="image/jpeg", size_bytes=100)

        called = {}

        async def fake_workflow(message, context, *, project_name, media, target="whole_page_background", fixed=False):
            called["project"] = project_name
            called["media_id"] = media["id"]
            called["target"] = target
            await message.reply_text("ok")

        with patch.object(bot, "add_background_image_workflow", side_effect=fake_workflow):
            update = FakeUpdate("добавь это фото на фон сайта hola")
            await bot.handle_text(update, FakeContext())
        self.assertEqual(called["project"], "hola")
        self.assertTrue(any("ok" in reply for reply in update.message.replies))

    async def test_background_request_without_pending_photo_prompts_for_project(self):
        # No pending photo, no project mentioned that actually exists --
        # task_orchestrator can't resolve a workspace_project, so it must ask
        # which site, not guess.
        import bot
        from test_error_smoke import FakeContext, FakeUpdate

        update = FakeUpdate("помести на фон сайта doesnotexist")
        await bot.handle_text(update, FakeContext())
        self.assertTrue(any("на какой сайт" in reply.lower() for reply in update.message.replies))

    async def test_background_request_without_pending_photo_falls_back_to_existing_image_source(self):
        # No pending photo but a real project -- task_orchestrator picks
        # media_source=existing_project_image (spec item 5). No image saved
        # yet, so it must fail honestly, never claim "Готово".
        import bot
        from tools_write import create_static_site
        from test_error_smoke import FakeContext, FakeUpdate

        create_static_site("hola")
        update = FakeUpdate("помести на фон сайта hola")
        await bot.handle_text(update, FakeContext())
        self.assertTrue(any("не завершено" in reply.lower() for reply in update.message.replies))
        self.assertFalse(any("готово" in reply.lower() for reply in update.message.replies))

    async def test_hero_phrase_with_pending_photo_routes_to_media_workflow_not_edit(self):
        from tools_pending_media import save_pending_media
        import bot
        import memory
        from test_error_smoke import FakeContext, FakeUpdate

        save_pending_media("456", "123", "file_hero1", file_unique_id="uh1", mime_type="image/jpeg", size_bytes=100)
        memory.set_current_project("456", "hola")

        called = {}

        async def fake_workflow(message, context, *, project_name, media, target="whole_page_background", fixed=False):
            called["project"] = project_name
            called["target"] = target
            await message.reply_text("ok")

        with patch.object(bot, "add_background_image_workflow", side_effect=fake_workflow), \
             patch.object(bot, "edit_workspace_site_workflow") as edit_mock:
            update = FakeUpdate("вот фото, поставь его фоном на блок херо на сайте")
            await bot.handle_text(update, FakeContext())

        self.assertEqual(called.get("project"), "hola")
        self.assertEqual(called.get("target"), "hero_background")
        edit_mock.assert_not_called()

    async def test_hero_phrase_with_explicit_project_extracts_project_and_hero_target(self):
        from tools_pending_media import save_pending_media
        from tools_write import create_static_site
        import bot
        from test_error_smoke import FakeContext, FakeUpdate

        create_static_site("hola")
        save_pending_media("456", "123", "file_hero2", file_unique_id="uh2", mime_type="image/jpeg", size_bytes=100)

        called = {}

        async def fake_workflow(message, context, *, project_name, media, target="whole_page_background", fixed=False):
            called["project"] = project_name
            called["target"] = target
            await message.reply_text("ok")

        with patch.object(bot, "add_background_image_workflow", side_effect=fake_workflow), \
             patch.object(bot, "edit_workspace_site_workflow") as edit_mock:
            update = FakeUpdate("поставь фото фоном на hero сайта hola")
            await bot.handle_text(update, FakeContext())

        self.assertEqual(called.get("project"), "hola")
        self.assertEqual(called.get("target"), "hero_background")
        edit_mock.assert_not_called()

    def test_failed_media_can_be_retried_via_get_latest_available_media(self):
        from tools_pending_media import (
            get_latest_available_media,
            get_latest_pending_media,
            mark_media_failed,
            save_pending_media,
        )

        item = save_pending_media("456", "123", "file_retry", file_unique_id="uretry", mime_type="image/jpeg", size_bytes=10)
        mark_media_failed(int(item["id"]), "hola", "", reason="boom")

        self.assertIsNone(get_latest_pending_media("456"))
        retried = get_latest_available_media("456")
        self.assertIsNotNone(retried)
        self.assertEqual(retried["id"], item["id"])
        self.assertEqual(retried["status"], "failed")

    def test_set_hero_background_writes_correct_url_and_selector(self):
        from tools_write import create_project_dir, write_text_file
        from tools_media import set_hero_background

        create_project_dir("herourl")
        write_text_file("herourl", "assets/css/style.css", "body{color:#000}\n", overwrite=False)
        write_text_file(
            "herourl", "index.html", '<html><body><header class="hero"></header></body></html>', overwrite=False
        )
        img_path = Path(self.tmp.name) / "herourl" / "assets" / "img" / "bg.webp"
        img_path.parent.mkdir(parents=True, exist_ok=True)
        img_path.write_bytes(b"RIFF0000WEBP")

        result = set_hero_background("herourl", "assets/img/bg.webp")
        css_text = Path(result["css_path"]).read_text(encoding="utf-8")
        self.assertIn('url("../img/bg.webp")', css_text)
        self.assertEqual(result["target_selector"], ".hero")

    def test_set_hero_background_does_not_touch_js(self):
        from tools_write import create_static_site
        from tools_media import set_hero_background

        create_static_site("herojs")
        js_path = Path(self.tmp.name) / "herojs" / "assets" / "js" / "main.js"
        before = js_path.read_text(encoding="utf-8")
        img_path = Path(self.tmp.name) / "herojs" / "assets" / "img" / "bg.webp"
        img_path.parent.mkdir(parents=True, exist_ok=True)
        img_path.write_bytes(b"RIFF0000WEBP")

        set_hero_background("herojs", "assets/img/bg.webp")
        after = js_path.read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_add_background_image_workflow_never_calls_ollama_or_full_site_edit(self):
        import inspect
        import bot

        source = inspect.getsource(bot.add_background_image_workflow)
        self.assertNotIn("ask_ollama_for_site_edit", source)
        self.assertNotIn("edit_workspace_site_workflow", source)

    @unittest.skipUnless(_pillow_installed(), "Pillow not installed")
    async def test_add_background_image_workflow_success_message_has_no_internal_details(self):
        from tools_write import create_static_site
        from tools_pending_media import save_pending_media
        from tools_preview import stop_preview
        import bot

        create_static_site("fullflow")
        media = save_pending_media("456", "123", "file_full", file_unique_id="ufull", mime_type="image/jpeg", size_bytes=10)
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (10, 10), color=(0, 0, 255)).save(buf, format="JPEG")
        fake_bytes = buf.getvalue()

        async def fake_save_image_to_project(bot_obj, project_name, file_id, original_name=None, mime_type=None):
            from tools_media import save_telegram_image_to_project

            return save_telegram_image_to_project(project_name, fake_bytes, original_name="x.jpg", mime_type="image/jpeg")

        message = AsyncMock()
        message.chat = type("C", (), {"id": "456"})()
        message.from_user = type("U", (), {"id": "123"})()
        message.text = ""
        message.caption = ""

        try:
            with patch.object(bot, "save_image_to_project", fake_save_image_to_project), \
                 patch.object(bot, "playwright_available", return_value=False):
                await bot.add_background_image_workflow(
                    message, context=AsyncMock(), project_name="fullflow", media=media, target="hero_background"
                )
        finally:
            try:
                stop_preview("fullflow")
            except Exception:
                pass

        replies = [call.args[0] for call in message.reply_text.call_args_list]
        final = replies[-1] if replies else ""
        self.assertIn("Готово", final)
        for forbidden in ("tools_called", "WRITE_ROOT", "curl_check", "set_hero_background", "verify_background_asset"):
            self.assertNotIn(forbidden, final)


class PlaywrightAsyncOnlyTests(unittest.TestCase):
    def test_check_site_with_playwright_async_source_has_no_sync_api(self):
        import inspect

        import tools_browser

        source = inspect.getsource(tools_browser._check_site_with_playwright_async)
        source += inspect.getsource(tools_browser.check_site_with_playwright_async)
        self.assertNotIn("from playwright.sync_api", source)
        self.assertIn("from playwright.async_api import async_playwright", source)


if __name__ == "__main__":
    unittest.main()

