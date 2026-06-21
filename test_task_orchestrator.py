import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Force (not setdefault) -- some other test module imported earlier in a full
# suite run may have already triggered config.py's load_dotenv(), which would
# otherwise populate ALLOWED_USER_ID from the real .env and make is_allowed()
# reject the fake test users below. This must win regardless of import order.
os.environ["ALLOWED_USER_ID"] = "123"
os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")

import config


class _BaseOrchestratorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jarvis_orchestrator_test_")
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
        try:
            from tools_preview import list_previews, stop_preview

            for item in list_previews()["previews"]:
                stop_preview(item["project"])
        except Exception:
            pass
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


class TaskOrchestratorDecisionTests(_BaseOrchestratorTest):
    def test_kiki_background_phrase_with_pending_media_is_apply_media_to_site(self):
        from tools_write import create_static_site
        from tools_pending_media import save_pending_media
        import task_orchestrator

        create_static_site("kiki")
        save_pending_media("chat1", "user1", "file_1", file_unique_id="u1", mime_type="image/jpeg", size_bytes=100)

        # No "это"/"фото"/"его" word at all -- only "сайт" + "фон" -- this is
        # exactly the phrase the bug report says used to fall through to
        # edit_workspace_site.
        decision = task_orchestrator.resolve_task("на сайт kiki как фон", chat_id="chat1")
        self.assertEqual(decision.task_type, "apply_media_to_site")
        self.assertEqual(decision.media_source, "pending_media")
        self.assertEqual(decision.workspace_project, "kiki")

    def test_folder_source_phrase_without_pending_media_is_existing_project_image(self):
        from tools_write import create_static_site
        import task_orchestrator

        create_static_site("kiki")
        decision = task_orchestrator.resolve_task(
            "поставь любое изображение из папки на фон сайта kiki", chat_id="chat2"
        )
        self.assertEqual(decision.task_type, "apply_media_to_site")
        self.assertEqual(decision.media_source, "existing_project_image")

    def test_folder_source_wins_over_pending_media_when_explicitly_requested(self):
        from tools_write import create_static_site
        from tools_pending_media import save_pending_media
        import task_orchestrator

        create_static_site("kiki")
        save_pending_media("chat2b", "user1", "file_1", file_unique_id="u1", mime_type="image/jpeg", size_bytes=100)
        decision = task_orchestrator.resolve_task(
            "поставь любое изображение из папки на фон сайта kiki", chat_id="chat2b"
        )
        self.assertEqual(decision.media_source, "existing_project_image")

    def test_check_slider_in_kiki_is_check_site_not_git(self):
        from tools_write import create_static_site
        import task_orchestrator

        create_static_site("kiki")
        decision = task_orchestrator.resolve_task("проверь слайдер в kiki", chat_id="chat3")
        self.assertEqual(decision.task_type, "check_site")
        self.assertIsNone(decision.git_repo)
        self.assertEqual(decision.workspace_project, "kiki")

    def test_resolve_task_saves_current_project(self):
        from tools_write import create_static_site
        import task_orchestrator
        import memory

        create_static_site("kiki")
        task_orchestrator.resolve_task("проверь слайдер в kiki", chat_id="chat4")
        self.assertEqual(memory.get_current_project("chat4"), "kiki")

    def test_entity_resolver_never_returns_nonexistent_project(self):
        import entity_resolver

        self.assertIsNone(entity_resolver.resolve_workspace_project("на сайт ghost-project как фон"))


class TaskOrchestratorHandleTextIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jarvis_orchestrator_handletext_")
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
        try:
            from tools_preview import list_previews, stop_preview

            for item in list_previews()["previews"]:
                stop_preview(item["project"])
        except Exception:
            pass
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

    async def test_kiki_phrase_with_pending_media_routes_to_apply_media_not_edit_site(self):
        import bot
        from tools_write import create_static_site
        from tools_pending_media import save_pending_media
        from test_error_smoke import FakeContext, FakeUpdate

        create_static_site("kiki")
        save_pending_media("456", "123", "file_1", file_unique_id="u1", mime_type="image/jpeg", size_bytes=100)

        called = {}

        async def fake_workflow(message, context, *, project_name, media, target="whole_page_background", fixed=False):
            called["project"] = project_name
            await message.reply_text("ok")

        with patch.object(bot, "add_background_image_workflow", side_effect=fake_workflow), patch.object(
            bot, "edit_workspace_site_workflow"
        ) as edit_mock:
            update = FakeUpdate("на сайт kiki как фон")
            await bot.handle_text(update, FakeContext())

        self.assertEqual(called.get("project"), "kiki")
        edit_mock.assert_not_called()

    async def test_vot_foto_phrase_with_pending_media_does_not_call_edit_site(self):
        import bot
        from tools_write import create_static_site
        from tools_pending_media import save_pending_media
        import memory
        from test_error_smoke import FakeContext, FakeUpdate

        create_static_site("kiki")
        memory.set_current_project("456", "kiki")
        save_pending_media("456", "123", "file_2", file_unique_id="u2", mime_type="image/jpeg", size_bytes=100)

        async def fake_workflow(message, context, *, project_name, media, target="whole_page_background", fixed=False):
            await message.reply_text("ok")

        with patch.object(bot, "add_background_image_workflow", side_effect=fake_workflow), patch.object(
            bot, "edit_workspace_site_workflow"
        ) as edit_mock:
            update = FakeUpdate("вот фото, поставь на фон")
            await bot.handle_text(update, FakeContext())

        edit_mock.assert_not_called()

    async def test_check_slider_in_kiki_routes_to_check_site_not_git_tools(self):
        import bot
        from tools_write import create_static_site
        from test_error_smoke import FakeContext, FakeUpdate

        create_static_site("kiki")

        with patch.object(bot, "find_git_repos") as git_mock, patch.object(
            bot, "edit_workspace_site_workflow"
        ) as edit_mock:
            update = FakeUpdate("проверь слайдер в kiki")
            await bot.handle_text(update, FakeContext())

        git_mock.assert_not_called()
        edit_mock.assert_not_called()
        replies = update.message.replies
        self.assertTrue(any("kiki" in r.lower() for r in replies))

    async def test_failed_verification_never_returns_gotovo(self):
        import bot
        from tools_write import create_static_site
        from test_error_smoke import FakeContext, FakeUpdate

        # Real project, no image saved anywhere -- set_background must fail
        # honestly via apply_existing_image_background_workflow.
        create_static_site("kiki")
        update = FakeUpdate("поставь любое изображение из папки на фон сайта kiki")
        await bot.handle_text(update, FakeContext())

        replies = update.message.replies
        self.assertTrue(any("не завершено" in r.lower() for r in replies))
        self.assertFalse(any("готово" in r.lower() for r in replies))


if __name__ == "__main__":
    unittest.main()
