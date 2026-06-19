import os
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from tools_fs import ToolError
import config


class WriteSandboxSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jarvis_write_test_")
        self.old_write_mode = os.environ.get("WRITE_MODE_ENABLED")
        self.old_write_root = os.environ.get("WRITE_ROOT")
        self.old_db_path = os.environ.get("JARVIS_DB_PATH")
        self.old_server_host = os.environ.get("SERVER_HOST")
        self.old_config_db_path = config.JARVIS_DB_PATH
        os.environ["WRITE_MODE_ENABLED"] = "true"
        os.environ["WRITE_ROOT"] = self.tmp.name
        temp_db = str(Path(self.tmp.name) / "data" / "jarvis.db")
        os.environ["JARVIS_DB_PATH"] = temp_db
        config.JARVIS_DB_PATH = temp_db
        os.environ["SERVER_HOST"] = "http://127.0.0.1"

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
        if self.old_server_host is None:
            os.environ.pop("SERVER_HOST", None)
        else:
            os.environ["SERVER_HOST"] = self.old_server_host
        self.tmp.cleanup()

    def test_resolve_write_path_rejects_traversal(self):
        from tools_write import resolve_write_path

        with self.assertRaises(ToolError):
            resolve_write_path("../anna")

    def test_write_text_file_inside_write_root(self):
        from tools_write import write_text_file

        result = write_text_file("demo/README.md", "# Demo\n")
        self.assertEqual(Path(result["path"]).read_text(encoding="utf-8"), "# Demo\n")
        self.assertTrue(str(result["path"]).startswith(self.tmp.name))

    def test_create_project_dir(self):
        from tools_write import create_project_dir

        result = create_project_dir("test-site")
        self.assertTrue(Path(result["path"]).is_dir())

    def test_write_static_site_creates_expected_files(self):
        from tools_write import create_static_site

        result = create_static_site("Botosite")
        project = Path(result["path"])
        for relative in ("index.html", "assets/css/style.css", "assets/js/main.js", "README.md"):
            self.assertTrue((project / relative).is_file(), relative)
        self.assertNotIn("https://cdn", (project / "index.html").read_text(encoding="utf-8"))

    def test_write_static_site_does_not_overwrite_existing_project(self):
        from tools_write import create_static_site

        result = create_static_site("Botosite")
        index = Path(result["path"]) / "index.html"
        original = index.read_text(encoding="utf-8")
        with self.assertRaises(ToolError):
            create_static_site("Botosite")
        self.assertEqual(index.read_text(encoding="utf-8"), original)

    def test_list_workspace_and_tree_show_botosite(self):
        from tools_write import create_static_site, list_workspace, workspace_tree

        create_static_site("Botosite")
        self.assertIn("Botosite", [item["name"] for item in list_workspace()["projects"]])
        tree = workspace_tree("Botosite")["tree"]
        self.assertIn("index.html", tree)
        self.assertIn("assets/", tree)

    def test_preview_start_and_stop_botosite(self):
        from tools_preview import preview_status, start_preview, stop_preview
        from tools_write import create_static_site

        create_static_site("Botosite")
        started = start_preview("Botosite")
        self.assertEqual(started["project"], "Botosite")
        self.assertGreaterEqual(started["port"], 8700)
        html = urllib.request.urlopen(f"http://127.0.0.1:{started['port']}/", timeout=5).read().decode("utf-8")
        self.assertIn("Botosite", html)
        self.assertTrue(preview_status("Botosite")["running"])
        stopped = stop_preview("Botosite")
        self.assertTrue(stopped["stopped"])
        self.assertFalse(preview_status("Botosite")["running"])

    def test_sitebota_create_preview_and_stop(self):
        from tools_preview import start_preview, stop_preview
        from tools_write import create_static_site, verify_static_site

        result = create_static_site("sitebota", title="Sitebota", description="Лендинг о функциях Jarvis")
        self.assertTrue(result["success"])
        verify = verify_static_site("sitebota")
        self.assertTrue(verify["success"])
        for path in verify["files"]:
            self.assertTrue(Path(path).exists(), path)
        preview = start_preview("sitebota")
        html = urllib.request.urlopen(f"http://127.0.0.1:{preview['port']}/", timeout=5).read().decode("utf-8")
        self.assertIn("Sitebota", html)
        stopped = stop_preview("sitebota")
        self.assertTrue(stopped["stopped"])

    def test_forbidden_write_paths_fail(self):
        from tools_write import write_text_file

        with self.assertRaises(ToolError):
            write_text_file("/home/seradmin/anna/test.txt", "bad")
        with self.assertRaises(ToolError):
            write_text_file("Botosite/.env", "SECRET=bad")

    def test_selftest_workspace_result(self):
        from bot import selftest_workspace_result

        result = selftest_workspace_result()
        self.assertTrue(result["success"])
        self.assertTrue(result["stopped"])
        self.assertTrue(result["cleanup"])

    def test_natural_language_sitebota_create_and_preview(self):
        from bot import fixture_site_spec, write_mode_answer
        from tools_preview import stop_preview

        with patch("bot.ask_ollama_for_site_spec", side_effect=fixture_site_spec):
            answer, debug = write_mode_answer("создай сайт sitebota в рабочей папке и запусти временный сервер", chat_id="test")
        self.assertIn("ask_ollama_for_site_spec", debug["tools_called"])
        self.assertIn("write_text_file", debug["tools_called"])
        self.assertIn("start_preview", debug["tools_called"])
        self.assertIn("preview_url:", answer)
        self.assertTrue((Path(self.tmp.name) / "sitebota" / "index.html").exists())
        stop_preview("sitebota")

    def test_where_missing_project_returns_exists_false(self):
        from bot import workspace_where_answer

        answer, debug = workspace_where_answer("missing_site", chat_id="test")
        self.assertIn("exists: false", answer)
        self.assertIn("Проект не найден в WRITE_ROOT", answer)
        self.assertEqual(debug["detected"]["intent"], "where_project")

    def test_create_site_workflow_creates_real_files_and_last_action(self):
        from bot import create_site_workflow, fixture_site_spec, get_last_action

        answer, debug = create_site_workflow(
            "создай сайт sitebota_test",
            project_name="sitebota_test",
            chat_id="test",
            start_preview_requested=False,
            site_spec_provider=fixture_site_spec,
        )
        self.assertIn("ask_ollama_for_site_spec", debug["tools_called"])
        self.assertIn("write_text_file", debug["tools_called"])
        self.assertIn("verify_project_files", debug["tools_called"])
        self.assertIn("Создал проект", answer)
        self.assertTrue((Path(self.tmp.name) / "sitebota_test" / "index.html").is_file())
        action = get_last_action("test")
        self.assertTrue(action["success"])
        self.assertEqual(action["project_name"], "sitebota_test")
        self.assertTrue(action["path"].endswith("sitebota_test"))

    def test_create_and_preview_workflow_starts_preview_and_last_action(self):
        from bot import create_site_workflow, fixture_site_spec, get_last_action
        from tools_preview import stop_preview

        answer, debug = create_site_workflow(
            "создай сайт sitebota_preview_test",
            project_name="sitebota_preview_test",
            chat_id="test",
            start_preview_requested=True,
            site_spec_provider=fixture_site_spec,
        )
        self.assertIn("start_preview", debug["tools_called"])
        self.assertIn("curl_localhost", debug["tools_called"])
        self.assertIn("preview_url:", answer)
        action = get_last_action("test")
        self.assertTrue(action["success"])
        self.assertTrue(action["preview_url"].startswith("http://127.0.0.1:"))
        stop_preview("sitebota_preview_test")

    def test_workspace_where_reports_preview_url_after_create_and_preview(self):
        from bot import create_site_workflow, fixture_site_spec, workspace_where_answer
        from tools_preview import stop_preview

        create_site_workflow(
            "создай сайт sitebota_where_test",
            project_name="sitebota_where_test",
            chat_id="test",
            start_preview_requested=True,
            site_spec_provider=fixture_site_spec,
        )
        answer, debug = workspace_where_answer("sitebota_where_test", chat_id="test")
        self.assertIn("exists: true", answer)
        self.assertIn("preview running: True", answer)
        self.assertIn("url: http://127.0.0.1:", answer)
        self.assertEqual(debug["detected"]["intent"], "where_project")
        stop_preview("sitebota_where_test")

    def test_no_fake_created_claim_without_tool_success(self):
        from bot import save_last_action, workspace_where_answer

        save_last_action("test", {"intent": "create_site", "project_name": "ghost", "success": False, "error": "tool failed"})
        answer, _debug = workspace_where_answer(None, chat_id="test")
        self.assertIn("Я не вижу подтверждения", answer)
        self.assertNotIn("Создал проект", answer)

    def test_ollama_fake_bash_response_is_rejected(self):
        from bot import ask_ollama_for_site_spec

        with patch("bot.ask_ollama_messages", return_value="mkdir sitebota && python3 -m http.server 8700"):
            with self.assertRaises(RuntimeError):
                ask_ollama_for_site_spec("создай сайт sitebota", "sitebota")

    def test_action_schema_rejects_absolute_path(self):
        from action_schemas import validate_create_static_site_action

        with self.assertRaises(ToolError):
            validate_create_static_site_action(
                {
                    "action": "create_static_site",
                    "project_name": "bad",
                    "files": [{"path": "/tmp/index.html", "content": "x"}],
                },
                expected_project_name="bad",
            )


if __name__ == "__main__":
    unittest.main()
