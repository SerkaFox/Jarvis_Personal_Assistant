import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config


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

    async def test_text_after_pending_photo_routes_to_background_workflow(self):
        from tools_pending_media import save_pending_media
        import bot
        from test_error_smoke import FakeContext, FakeUpdate

        save_pending_media("456", "123", "file_1", file_unique_id="u1", mime_type="image/jpeg", size_bytes=100)

        called = {}

        async def fake_workflow(message, context, *, project_name, media):
            called["project"] = project_name
            called["media_id"] = media["id"]
            await message.reply_text("ok")

        with patch.object(bot, "add_background_image_workflow", side_effect=fake_workflow):
            update = FakeUpdate("добавь это фото на фон сайта hola")
            await bot.handle_text(update, FakeContext())
        self.assertEqual(called["project"], "hola")
        self.assertTrue(any("ok" in reply for reply in update.message.replies))

    async def test_background_request_without_pending_photo_prompts_user(self):
        import bot
        from test_error_smoke import FakeContext, FakeUpdate

        update = FakeUpdate("помести на фон сайта hola")
        await bot.handle_text(update, FakeContext())
        self.assertTrue(any("не вижу последнего фото" in reply.lower() for reply in update.message.replies))


if __name__ == "__main__":
    unittest.main()

