import os
import tempfile
import unittest
from pathlib import Path

from tools_fs import ToolError


class WriteSandboxSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jarvis_write_test_")
        self.old_write_mode = os.environ.get("WRITE_MODE_ENABLED")
        self.old_write_root = os.environ.get("WRITE_ROOT")
        self.old_db_path = os.environ.get("JARVIS_DB_PATH")
        self.old_server_host = os.environ.get("SERVER_HOST")
        os.environ["WRITE_MODE_ENABLED"] = "true"
        os.environ["WRITE_ROOT"] = self.tmp.name
        os.environ["JARVIS_DB_PATH"] = str(Path(self.tmp.name) / "data" / "jarvis.db")
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
        self.assertTrue(preview_status("Botosite")["running"])
        stopped = stop_preview("Botosite")
        self.assertTrue(stopped["stopped"])
        self.assertFalse(preview_status("Botosite")["running"])

    def test_forbidden_write_paths_fail(self):
        from tools_write import write_text_file

        with self.assertRaises(ToolError):
            write_text_file("/home/seradmin/anna/test.txt", "bad")
        with self.assertRaises(ToolError):
            write_text_file("Botosite/.env", "SECRET=bad")


if __name__ == "__main__":
    unittest.main()
