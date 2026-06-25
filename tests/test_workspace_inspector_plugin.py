import os
import tempfile
import unittest
from pathlib import Path

# Force (not setdefault) -- some other test module imported earlier in a full
# suite run may have already triggered config.py's load_dotenv(), which would
# otherwise populate ALLOWED_USER_ID from the real .env and make is_allowed()
# reject the fake test users below.
os.environ["ALLOWED_USER_ID"] = "123"
os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")

import config


class WorkspaceInspectorPluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jarvis_inspector_test_")
        self.old_write_mode = os.environ.get("WRITE_MODE_ENABLED")
        self.old_write_root = os.environ.get("WRITE_ROOT")
        os.environ["WRITE_MODE_ENABLED"] = "true"
        os.environ["WRITE_ROOT"] = self.tmp.name
        from tools_write import create_static_site

        create_static_site("hola")

    def tearDown(self):
        if self.old_write_mode is None:
            os.environ.pop("WRITE_MODE_ENABLED", None)
        else:
            os.environ["WRITE_MODE_ENABLED"] = self.old_write_mode
        if self.old_write_root is None:
            os.environ.pop("WRITE_ROOT", None)
        else:
            os.environ["WRITE_ROOT"] = self.old_write_root
        self.tmp.cleanup()

    def _load_plugin(self):
        import plugin_manager

        modules, errors = plugin_manager.load_plugins(config.PROJECT_ROOT / "plugins")
        match = next((m for m in modules if m.PLUGIN_NAME == "workspace_inspector"), None)
        self.assertIsNotNone(match, f"plugin not loaded, errors={errors}")
        return match

    def test_plugin_manager_loads_workspace_inspector(self):
        module = self._load_plugin()
        self.assertEqual(module.PLUGIN_VERSION, "0.1.0")
        self.assertTrue(module.PLUGIN_DESCRIPTION)

    def test_can_handle_scores_image_question_above_threshold(self):
        module = self._load_plugin()
        score = module.can_handle("какие фото есть в проекте hola", {"project_name": "hola"})
        self.assertGreater(score, 0.4)  # plugin_manager.select_plugin default threshold

    def test_can_handle_scores_file_question_above_threshold(self):
        module = self._load_plugin()
        score = module.can_handle("найди файлы в папке сайта hola", {"project_name": "hola"})
        self.assertGreater(score, 0.4)

    def test_can_handle_zero_without_project(self):
        module = self._load_plugin()
        self.assertEqual(module.can_handle("какие файлы у тебя есть?", {}), 0.0)

    def test_can_handle_zero_for_unrelated_text(self):
        module = self._load_plugin()
        self.assertEqual(module.can_handle("привет, как дела?", {"project_name": "hola"}), 0.0)

    def test_plugin_own_smoke_tests_pass(self):
        import plugin_manager

        module = self._load_plugin()
        result = plugin_manager.run_plugin_smoke_tests(module)
        self.assertTrue(result["success"], result["cases"])

    async def test_handle_returns_real_files_and_never_invents_missing_ones(self):
        module = self._load_plugin()
        img_dir = Path(self.tmp.name) / "hola" / "assets" / "img"
        img_dir.mkdir(parents=True, exist_ok=True)
        (img_dir / "background-1781991661.webp").write_bytes(b"RIFF0000WEBP")

        result = await module.handle(
            None, None, {"user_text": "какие фото для фона у тебя есть?", "project_name": "hola"}
        )
        self.assertTrue(result["success"])
        self.assertIn("background-1781991661.webp", result["answer"])
        for invented in ("bg_ru.jpg", "bg_en.jpg", "bg_es.jpg", "default_bg.jpg"):
            self.assertNotIn(invented, result["answer"])

    async def test_handle_empty_image_dir_is_honest(self):
        module = self._load_plugin()
        result = await module.handle(None, None, {"user_text": "какие изображения есть?", "project_name": "hola"})
        self.assertTrue(result["success"])
        self.assertIn("изображений не найдено", result["answer"].lower())

    async def test_handle_lists_real_project_files(self):
        module = self._load_plugin()
        result = await module.handle(
            None, None, {"user_text": "найди файлы в папке сайта hola", "project_name": "hola"}
        )
        self.assertTrue(result["success"])
        self.assertIn("index.html", result["answer"])

    async def test_handle_shows_project_tree(self):
        module = self._load_plugin()
        result = await module.handle(
            None, None, {"user_text": "покажи структуру проекта hola", "project_name": "hola"}
        )
        self.assertTrue(result["success"])
        self.assertIn("index.html", result["answer"])

    async def test_handle_rejects_path_traversal_project_name(self):
        module = self._load_plugin()
        result = await module.handle(
            None, None, {"user_text": "какие файлы у тебя есть?", "project_name": "../../etc"}
        )
        self.assertFalse(result["success"])

    async def test_handle_never_writes_files(self):
        module = self._load_plugin()
        root = Path(self.tmp.name) / "hola"
        before = sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file())

        await module.handle(None, None, {"user_text": "какие файлы у тебя есть?", "project_name": "hola"})
        img_dir = root / "assets" / "img"
        img_dir.mkdir(parents=True, exist_ok=True)
        await module.handle(
            None, None, {"user_text": "какие фото для фона у тебя есть?", "project_name": "hola"}
        )

        after = sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file())
        self.assertEqual(before, after)


class WorkspaceInspectorRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jarvis_inspector_routing_")
        self.old_write_mode = os.environ.get("WRITE_MODE_ENABLED")
        self.old_write_root = os.environ.get("WRITE_ROOT")
        self.old_db_path = os.environ.get("JARVIS_DB_PATH")
        self.old_config_db_path = config.JARVIS_DB_PATH
        os.environ["WRITE_MODE_ENABLED"] = "true"
        os.environ["WRITE_ROOT"] = self.tmp.name
        temp_db = str(Path(self.tmp.name) / "data" / "jarvis.db")
        os.environ["JARVIS_DB_PATH"] = temp_db
        config.JARVIS_DB_PATH = temp_db
        from tools_write import create_static_site

        create_static_site("hola")
        img_dir = Path(self.tmp.name) / "hola" / "assets" / "img"
        img_dir.mkdir(parents=True, exist_ok=True)
        (img_dir / "background-1781991661.webp").write_bytes(b"RIFF0000WEBP")

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

    async def test_handle_text_calls_agent_for_any_text(self):
        """handle_text must reach run_claude_agent for all text messages."""
        from unittest.mock import patch
        import bot
        import tools_claude_agent
        import memory
        from test_error_smoke import FakeContext, FakeUpdate

        memory.set_current_project("456", "hola")
        reached = {"n": 0}

        async def fake_agent(text, chat_id, **kwargs):
            reached["n"] += 1
            return "agent reply"

        for msg in ["какие фото для фона у тебя есть?", "привет, как у тебя дела?"]:
            with patch.object(tools_claude_agent, "run_claude_agent", side_effect=fake_agent):
                update = FakeUpdate(msg)
                await bot.handle_text(update, FakeContext())

        self.assertEqual(reached["n"], 2)


if __name__ == "__main__":
    unittest.main()
