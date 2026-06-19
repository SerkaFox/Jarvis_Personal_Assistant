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
        os.environ["WRITE_MODE_ENABLED"] = "true"
        os.environ["WRITE_ROOT"] = self.tmp.name

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
        from tools_write import write_static_site

        result = write_static_site("test-site")
        project = Path(result["path"])
        for relative in ("index.html", "assets/css/style.css", "assets/js/main.js", "README.md"):
            self.assertTrue((project / relative).is_file(), relative)

    def test_write_static_site_does_not_overwrite_existing_project(self):
        from tools_write import write_static_site

        result = write_static_site("test-site")
        index = Path(result["path"]) / "index.html"
        original = index.read_text(encoding="utf-8")
        with self.assertRaises(ToolError):
            write_static_site("test-site")
        self.assertEqual(index.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
