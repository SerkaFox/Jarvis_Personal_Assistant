import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config


def _playwright_installed() -> bool:
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except Exception:
        return False
    return True


class _BaseSiteTxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jarvis_site_tx_test_")
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
        os.environ["SERVER_HOST"] = "http://192.168.0.50"

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


class SnapshotRollbackTests(_BaseSiteTxTest):
    def test_snapshot_and_rollback_round_trip(self):
        from tools_write import create_static_site, write_project_text_file
        from tools_snapshot import snapshot_project, rollback_project
        from tools_edit import read_workspace_project_files

        create_static_site("snaptest")
        before = read_workspace_project_files("snaptest")
        before_css = next(f["content"] for f in before["files"] if f["path"] == "assets/css/style.css")

        snapshot = snapshot_project("snaptest", reason="test")
        self.assertTrue(snapshot["snapshot_id"])
        self.assertIn("assets/css/style.css", snapshot["files"])

        write_project_text_file("snaptest", "assets/css/style.css", "body{color:red}", overwrite=True)
        changed = read_workspace_project_files("snaptest")
        changed_css = next(f["content"] for f in changed["files"] if f["path"] == "assets/css/style.css")
        self.assertNotEqual(before_css, changed_css)

        result = rollback_project("snaptest", snapshot["snapshot_id"])
        self.assertTrue(result["success"])

        after = read_workspace_project_files("snaptest")
        after_css = next(f["content"] for f in after["files"] if f["path"] == "assets/css/style.css")
        self.assertEqual(before_css, after_css)


class SiteStateRequirementsTests(_BaseSiteTxTest):
    def test_requirements_only_grow_never_reset_to_false_via_merge(self):
        from tools_site_state import save_site_state, get_site_requirements

        save_site_state("growtest", {"background_required": True})
        self.assertTrue(get_site_requirements("growtest")["background_required"])

        # A later task that doesn't mention background must not be able to
        # silently turn the requirement back off via the default merge path.
        save_site_state("growtest", {"background_required": False, "slider_required": True})
        requirements = get_site_requirements("growtest")
        self.assertTrue(requirements["background_required"])
        self.assertTrue(requirements["slider_required"])

    def test_infer_requirements_from_text_detects_languages(self):
        from tools_site_state import infer_requirements_from_text

        inferred = infer_requirements_from_text("сделай переключение языка ru/en/es")
        self.assertTrue(inferred["language_switcher_required"])
        self.assertEqual(set(inferred["languages"]), {"ru", "en", "es"})
        self.assertTrue(inferred["single_language_visible"])

    def test_site_state_command_shows_requirements(self):
        from tools_write import create_static_site
        from tools_site_state import save_site_state, format_site_state_answer

        create_static_site("statetest")
        save_site_state("statetest", {"background_required": True, "footer_required": True})
        answer = format_site_state_answer("statetest")
        self.assertIn("фон: нужен", answer)
        self.assertIn("footer: нужен", answer)
        self.assertNotIn("tools_called", answer)


def _verify_only_plan(user_text, project_name, project_state):
    """Never satisfies any requirement -- proposes only a no-op verify, so
    whatever requirement the task text implied stays unmet and acceptance
    must fail every time. Used to exercise rollback/keep-anyway behavior
    without depending on a real operation being capable of "failing"."""
    return {"operations": [{"op": "verify", "feature": None, "params": {}}], "summary": "checking"}


class TransactionalEditWorkflowTests(_BaseSiteTxTest):
    def test_failed_edit_restores_original_files(self):
        from bot import edit_workspace_site_workflow
        from tools_write import create_static_site
        from tools_edit import read_workspace_project_files

        create_static_site("rollbacksite")
        original = read_workspace_project_files("rollbacksite")
        original_html = next(f["content"] for f in original["files"] if f["path"] == "index.html")

        with patch("bot.ask_ollama_for_operation_plan", side_effect=_verify_only_plan):
            answer, debug = edit_workspace_site_workflow(
                "почини переключение языков ru/en/es", "rollbacksite", chat_id="rb"
            )

        self.assertFalse(debug["acceptance"]["success"])
        self.assertTrue(debug["rolled_back"])
        self.assertIn("проверка не прошла, изменения откатил", answer)
        self.assertNotIn("Готово", answer)
        self.assertNotIn("tools_called", answer)

        after = read_workspace_project_files("rollbacksite")
        after_html = next(f["content"] for f in after["files"] if f["path"] == "index.html")
        self.assertEqual(original_html, after_html)

    def test_language_fix_preserves_existing_background_via_real_operations(self):
        from bot import edit_workspace_site_workflow
        from tools_write import create_static_site
        from tools_site_state import get_site_requirements, inspect_site_state

        create_static_site("langbgsite")

        def add_background_plan(user_text, project_name, project_state):
            return {"operations": [{"op": "set_background", "feature": None, "params": {}}], "summary": "added background"}

        with patch("bot.ask_ollama_for_operation_plan", side_effect=add_background_plan):
            # set_background needs a real image -- save one directly first.
            import io

            from PIL import Image

            from tools_media import save_telegram_image_to_project

            buf = io.BytesIO()
            Image.new("RGB", (10, 10), color=(0, 0, 255)).save(buf, format="JPEG")
            save_telegram_image_to_project("langbgsite", buf.getvalue(), original_name="hero.jpg", mime_type="image/jpeg")

            answer, debug = edit_workspace_site_workflow("сделай фон сайта hero", "langbgsite", chat_id="lb")
        self.assertTrue(debug["acceptance"]["success"], debug["acceptance"]["failed"])
        self.assertTrue(get_site_requirements("langbgsite")["background_required"])

        def fix_language_plan(user_text, project_name, project_state):
            self.assertTrue(project_state["requirements"].get("background_required"))  # must_preserve context was passed
            return {"operations": [{"op": "fix_language_switcher", "feature": None, "params": {}}], "summary": "fixed languages"}

        with patch("bot.ask_ollama_for_operation_plan", side_effect=fix_language_plan):
            answer, debug = edit_workspace_site_workflow(
                "почини переключение языков ru/en/es", "langbgsite", chat_id="lb"
            )

        self.assertTrue(debug["acceptance"]["success"], debug["acceptance"]["failed"])
        self.assertIn("Готово", answer)
        self.assertTrue(inspect_site_state("langbgsite")["has_background"])
        self.assertTrue(inspect_site_state("langbgsite")["has_language_switcher"])

    def test_adding_slider_preserves_language_switcher_and_background(self):
        from bot import edit_workspace_site_workflow
        from tools_write import create_static_site
        from tools_site_state import save_site_state, get_site_requirements, inspect_site_state

        create_static_site("slidersite")
        save_site_state(
            "slidersite",
            {
                "background_required": True,
                "language_switcher_required": True,
                "languages": ["ru", "en", "es"],
                "single_language_visible": True,
            },
        )
        import tools_site_operations as ops

        ops.op_fix_language_switcher("slidersite", {})
        # set_background needs an image; use a CSS-only marker write instead so
        # this test stays focused on "does add_slider preserve what's already there".
        from tools_write import write_project_text_file

        write_project_text_file(
            "slidersite",
            "assets/css/style.css",
            "/* jarvis-hero-background:start */\n.hero{background-image:url('../img/bg.jpg');}\n/* jarvis-hero-background:end */\n",
            overwrite=True,
        )

        def add_slider_plan(user_text, project_name, project_state):
            self.assertTrue(project_state["requirements"].get("background_required"))
            self.assertTrue(project_state["requirements"].get("language_switcher_required"))
            return {"operations": [{"op": "add_slider", "feature": None, "params": {}}], "summary": "added slider"}

        with patch("bot.ask_ollama_for_operation_plan", side_effect=add_slider_plan):
            answer, debug = edit_workspace_site_workflow("добавь слайдер с фото", "slidersite", chat_id="sl")

        self.assertTrue(debug["acceptance"]["success"], debug["acceptance"]["failed"])
        self.assertIn("Готово", answer)
        self.assertFalse(debug["rolled_back"])
        requirements = get_site_requirements("slidersite")
        self.assertTrue(requirements["slider_required"])
        inspected = inspect_site_state("slidersite")
        self.assertTrue(inspected["has_background"])
        self.assertTrue(inspected["has_language_switcher"])
        self.assertTrue(inspected["has_slider"])

    def test_feature_regression_safety_net_rolls_back_even_if_an_operation_is_buggy(self):
        """Defense-in-depth: even if a (hypothetically buggy) operation wipes out
        an existing feature, detect_feature_regressions must catch it and the
        workflow must roll back -- not just trust that operations are additive."""
        from bot import edit_workspace_site_workflow
        from tools_write import create_static_site
        import tools_site_operations as ops

        create_static_site("regressionsite")
        ops.op_fix_language_switcher("regressionsite", {})

        def buggy_repair_feature(project_name, params):
            # Simulates a bug: instead of repairing, it wipes the HTML file,
            # removing the language buttons/content blocks entirely.
            from tools_write import write_project_text_file

            write_project_text_file(project_name, "index.html", "<html><body>empty</body></html>", overwrite=True)
            return {"files_changed": ["index.html"], "detail": "buggy repair"}

        def buggy_plan(user_text, project_name, project_state):
            return {"operations": [{"op": "fix_language_switcher", "feature": None, "params": {}}], "summary": "repair"}

        with patch.object(ops, "OP_DISPATCH", {**ops.OP_DISPATCH, "fix_language_switcher": buggy_repair_feature}):
            with patch("bot.ask_ollama_for_operation_plan", side_effect=buggy_plan):
                answer, debug = edit_workspace_site_workflow(
                    "почини переключение языков ru/en/es", "regressionsite", chat_id="rg"
                )

        self.assertFalse(debug["acceptance"]["success"])
        self.assertTrue(debug["rolled_back"])
        self.assertTrue(any("language_switcher" in item for item in debug["acceptance"]["failed"]))

    def test_keep_despite_failure_phrase_skips_rollback(self):
        from bot import edit_workspace_site_workflow
        from tools_write import create_static_site
        from tools_edit import read_workspace_project_files

        create_static_site("keepsite")

        with patch("bot.ask_ollama_for_operation_plan", side_effect=_verify_only_plan):
            answer, debug = edit_workspace_site_workflow(
                "почини переключение языков ru/en/es, оставь всё равно", "keepsite", chat_id="ks"
            )

        self.assertFalse(debug["acceptance"]["success"])
        self.assertFalse(debug["rolled_back"])
        self.assertNotIn("Готово", answer)
        self.assertNotIn("tools_called", answer)


if __name__ == "__main__":
    unittest.main()
