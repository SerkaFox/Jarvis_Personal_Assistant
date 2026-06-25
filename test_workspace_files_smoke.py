import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Force (not setdefault) -- some other test module imported earlier in a full
# suite run may have already triggered config.py's load_dotenv(), which would
# otherwise populate ALLOWED_USER_ID from the real .env and make is_allowed()
# reject the fake test users below.
os.environ["ALLOWED_USER_ID"] = "123"
os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")

import config
from tools_fs import ToolError


class WorkspaceFileToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jarvis_files_test_")
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

    def test_list_workspace_project_images_finds_real_files(self):
        from tools_media import list_workspace_project_images

        img_dir = Path(self.tmp.name) / "hola" / "assets" / "img"
        img_dir.mkdir(parents=True, exist_ok=True)
        (img_dir / "background-1781991661.webp").write_bytes(b"RIFF0000WEBP")
        (img_dir / "background-1781991700.webp").write_bytes(b"RIFF0000WEBP")

        result = list_workspace_project_images("hola")
        paths = {img["path"] for img in result["images"]}
        self.assertIn("assets/img/background-1781991661.webp", paths)
        self.assertIn("assets/img/background-1781991700.webp", paths)
        self.assertEqual(result["count"], 2)

    def test_list_workspace_project_images_empty_dir_returns_empty_list(self):
        from tools_media import list_workspace_project_images

        result = list_workspace_project_images("hola")
        self.assertEqual(result["images"], [])
        self.assertEqual(result["count"], 0)

    def test_list_workspace_project_images_never_invents_files(self):
        from tools_media import list_workspace_project_images

        img_dir = Path(self.tmp.name) / "hola" / "assets" / "img"
        img_dir.mkdir(parents=True, exist_ok=True)
        (img_dir / "background-real.webp").write_bytes(b"RIFF0000WEBP")

        result = list_workspace_project_images("hola")
        names = {img["path"] for img in result["images"]}
        self.assertIn("assets/img/background-real.webp", names)
        invented = {"assets/img/bg_ru.jpg", "assets/img/bg_en.jpg", "assets/img/bg_es.jpg", "assets/img/default_bg.jpg"}
        self.assertEqual(names & invented, set())

    def test_list_workspace_project_files_lists_real_files(self):
        from tools_write import list_workspace_project_files

        result = list_workspace_project_files("hola", depth=3)
        paths = {f["path"] for f in result["files"]}
        self.assertIn("index.html", paths)
        self.assertIn("assets/css/style.css", paths)
        self.assertIn("assets/js/main.js", paths)

    def test_tree_workspace_project_returns_tree_string(self):
        from tools_write import tree_workspace_project

        result = tree_workspace_project("hola", depth=3)
        self.assertIn("index.html", result["tree"])
        self.assertEqual(result["project_name"], "hola")

    def test_path_traversal_rejected_for_images(self):
        from tools_media import list_workspace_project_images

        with self.assertRaises(ToolError):
            list_workspace_project_images("../../../etc")

    def test_path_traversal_rejected_for_files(self):
        from tools_write import list_workspace_project_files

        with self.assertRaises(ToolError):
            list_workspace_project_files("../../../etc")


class FileListingRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jarvis_files_routing_")
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

    async def test_agent_tool_read_project_files_returns_project_files(self):
        """read_project_files tool must return real project files."""
        import tools_claude_agent

        dispatch = tools_claude_agent._make_dispatcher("456")
        result = dispatch("read_project_files", {"project_name": "hola"})
        files = [f["path"] for f in result.get("files", [])]
        self.assertTrue(any("index.html" in f for f in files))

    async def test_agent_tool_list_workspace_shows_hola_project(self):
        """list_workspace tool must include the hola project."""
        import tools_claude_agent

        dispatch = tools_claude_agent._make_dispatcher(None)
        result = dispatch("list_workspace", {})
        names = [p["name"] for p in result.get("projects", [])]
        self.assertIn("hola", names)

    async def test_handle_text_always_calls_agent(self):
        """All text messages must reach run_claude_agent — no bypasses."""
        import bot
        import tools_claude_agent
        import memory
        from test_error_smoke import FakeContext, FakeUpdate

        memory.set_current_project("456", "hola")
        called = {"n": 0}

        async def fake_agent(text, chat_id, **kwargs):
            called["n"] += 1
            return "ok"

        for msg in [
            "какие фото для фона у тебя есть?",
            "найди файлы в папке сайта hola",
            "какие файлы у тебя есть?",
        ]:
            with patch.object(tools_claude_agent, "run_claude_agent", side_effect=fake_agent):
                update = FakeUpdate(msg)
                await bot.handle_text(update, FakeContext())

        self.assertEqual(called["n"], 3)


if __name__ == "__main__":
    unittest.main()
