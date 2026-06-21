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


class TransactionalEditWorkflowTests(_BaseSiteTxTest):
    def test_failed_edit_restores_original_files(self):
        from bot import edit_workspace_site_workflow
        from tools_write import create_static_site
        from tools_edit import read_workspace_project_files

        create_static_site("rollbacksite")
        original = read_workspace_project_files("rollbacksite")
        original_html = next(f["content"] for f in original["files"] if f["path"] == "index.html")

        def breaks_sections(user_text, project_name, current_files, requirements=None):
            return {
                "action": "edit_workspace_site",
                "project_name": project_name,
                "summary": "removes all sections",
                "files": [{"path": "index.html", "content": "<html><body>empty</body></html>"}],
                "notes": [],
            }

        with patch("bot.ask_ollama_for_site_edit", side_effect=breaks_sections):
            answer, debug = edit_workspace_site_workflow("поменяй стиль на синий", "rollbacksite", chat_id="rb")

        self.assertFalse(debug["acceptance"]["success"])
        self.assertTrue(debug["rolled_back"])
        self.assertIn("проверка не прошла, изменения откатил", answer)
        self.assertNotIn("Готово", answer)
        self.assertNotIn("tools_called", answer)

        after = read_workspace_project_files("rollbacksite")
        after_html = next(f["content"] for f in after["files"] if f["path"] == "index.html")
        self.assertEqual(original_html, after_html)

    def test_language_fix_must_preserve_existing_background(self):
        from bot import edit_workspace_site_workflow
        from tools_write import create_static_site
        from tools_site_state import get_site_requirements

        create_static_site("langbgsite")

        def add_background(user_text, project_name, current_files, requirements=None):
            return {
                "action": "edit_workspace_site",
                "project_name": project_name,
                "summary": "added hero background",
                "files": [
                    {"path": "assets/css/style.css", "content": ".hero{background-image:url('../img/bg.jpg');}"}
                ],
                "notes": [],
            }

        with patch("bot.ask_ollama_for_site_edit", side_effect=add_background):
            answer, debug = edit_workspace_site_workflow("сделай фон сайта hero", "langbgsite", chat_id="lb")
        self.assertTrue(debug["acceptance"]["success"], debug["acceptance"]["failed"])
        self.assertTrue(get_site_requirements("langbgsite")["background_required"])

        def fix_language_drops_background(user_text, project_name, current_files, requirements=None):
            # Misbehaving edit: "fixes" languages but silently drops the
            # background CSS that an earlier task established.
            self.assertTrue(requirements.get("background_required"))  # must_preserve was passed in
            return {
                "action": "edit_workspace_site",
                "project_name": project_name,
                "summary": "fixed language switching",
                "files": [
                    {
                        "path": "index.html",
                        "content": (
                            '<header><button data-lang="ru">RU</button><button data-lang="en">EN</button>'
                            '<button data-lang="es">ES</button></header>'
                            "<section>1</section><section>2</section><section>3</section>"
                            "<section>4</section><section class=\"cards\"><div class=\"card\">card</div></section>"
                            "<script>document.addEventListener('click', function(e){ if(e.target.dataset.lang)"
                            " setLang(e.target.dataset.lang); });</script>"
                        ),
                    },
                    {"path": "assets/css/style.css", "content": "body{color:black}"},
                ],
                "notes": [],
            }

        with patch("bot.ask_ollama_for_site_edit", side_effect=fix_language_drops_background):
            answer, debug = edit_workspace_site_workflow(
                "почини переключение языков ru/en/es", "langbgsite", chat_id="lb"
            )

        self.assertFalse(debug["acceptance"]["success"])
        self.assertTrue(debug["rolled_back"])
        self.assertTrue(any("background_present_in_css" in item for item in debug["acceptance"]["failed"]))
        self.assertIn("проверка не прошла, изменения откатил", answer)

    def test_adding_slider_preserves_language_switcher_and_background(self):
        from bot import edit_workspace_site_workflow
        from tools_write import create_static_site
        from tools_site_state import save_site_state, get_site_requirements

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

        def add_slider_keep_everything(user_text, project_name, current_files, requirements=None):
            self.assertTrue(requirements.get("background_required"))
            self.assertTrue(requirements.get("language_switcher_required"))
            return {
                "action": "edit_workspace_site",
                "project_name": project_name,
                "summary": "added slider",
                "files": [
                    {
                        "path": "index.html",
                        "content": (
                            '<header><button data-lang="ru">RU</button><button data-lang="en">EN</button>'
                            '<button data-lang="es">ES</button></header>'
                            '<section class="hero">hero</section>'
                            '<section class="features">features</section>'
                            '<section class="slider"><div class="slide">1</div><div class="slide">2</div></section>'
                            '<section class="cards"><div class="card">card</div></section>'
                            '<section class="contact">contact</section>'
                            "<script>document.addEventListener('click', function(e){ if(e.target.dataset.lang)"
                            " setLang(e.target.dataset.lang); });</script>"
                        ),
                    },
                    {"path": "assets/css/style.css", "content": ".hero{background-image:url('../img/bg.jpg');}"},
                ],
                "notes": [],
            }

        with patch("bot.ask_ollama_for_site_edit", side_effect=add_slider_keep_everything):
            answer, debug = edit_workspace_site_workflow("добавь слайдер с фото", "slidersite", chat_id="sl")

        self.assertTrue(debug["acceptance"]["success"], debug["acceptance"]["failed"])
        self.assertIn("Готово", answer)
        self.assertFalse(debug["rolled_back"])
        requirements = get_site_requirements("slidersite")
        self.assertTrue(requirements["slider_required"])
        self.assertTrue(requirements["background_required"])
        self.assertTrue(requirements["language_switcher_required"])

    @unittest.skipUnless(_playwright_installed(), "Playwright not installed")
    def test_hidden_language_buttons_fail_browser_check_and_roll_back(self):
        from bot import edit_workspace_site_workflow
        from tools_write import create_static_site
        from tools_site_state import save_site_state
        from tools_preview import start_preview
        from tools_edit import read_workspace_project_files

        create_static_site("hiddenlangsite")
        save_site_state(
            "hiddenlangsite",
            {"language_switcher_required": True, "languages": ["ru", "en", "es"], "single_language_visible": True},
        )
        start_preview("hiddenlangsite")
        original = read_workspace_project_files("hiddenlangsite")
        original_html = next(f["content"] for f in original["files"] if f["path"] == "index.html")

        def hidden_buttons_spec(user_text, project_name, current_files, requirements=None):
            return {
                "action": "edit_workspace_site",
                "project_name": project_name,
                "summary": "language buttons present but not clickable",
                "files": [
                    {
                        "path": "index.html",
                        "content": (
                            "<header>"
                            '<button data-lang="ru" style="display:none">RU</button>'
                            '<button data-lang="en" style="display:none">EN</button>'
                            '<button data-lang="es" style="display:none">ES</button>'
                            "</header>"
                            "<section>1</section><section>2</section><section>3</section>"
                            "<section>4</section><section class=\"cards\"><div class=\"card\">card</div></section>"
                            "<script>document.addEventListener('click', function(e){ if(e.target.dataset.lang)"
                            " setLang(e.target.dataset.lang); });</script>"
                        ),
                    }
                ],
                "notes": [],
            }

        with patch("bot.ask_ollama_for_site_edit", side_effect=hidden_buttons_spec):
            answer, debug = edit_workspace_site_workflow(
                "почини переключение языков ru/en/es", "hiddenlangsite", chat_id="hl"
            )

        self.assertFalse(debug["acceptance"]["success"])
        self.assertTrue(debug["rolled_back"])
        self.assertIn("проверка не прошла, изменения откатил", answer)

        after = read_workspace_project_files("hiddenlangsite")
        after_html = next(f["content"] for f in after["files"] if f["path"] == "index.html")
        self.assertEqual(original_html, after_html)

    def test_keep_despite_failure_phrase_skips_rollback(self):
        from bot import edit_workspace_site_workflow
        from tools_write import create_static_site
        from tools_edit import read_workspace_project_files

        create_static_site("keepsite")

        def breaks_sections(user_text, project_name, current_files, requirements=None):
            return {
                "action": "edit_workspace_site",
                "project_name": project_name,
                "summary": "removes all sections",
                "files": [{"path": "index.html", "content": "<html><body>empty</body></html>"}],
                "notes": [],
            }

        with patch("bot.ask_ollama_for_site_edit", side_effect=breaks_sections):
            answer, debug = edit_workspace_site_workflow(
                "поменяй стиль на синий, оставь всё равно", "keepsite", chat_id="ks"
            )

        self.assertFalse(debug["acceptance"]["success"])
        self.assertFalse(debug["rolled_back"])
        self.assertNotIn("Готово", answer)
        self.assertNotIn("tools_called", answer)

        after = read_workspace_project_files("keepsite")
        after_html = next(f["content"] for f in after["files"] if f["path"] == "index.html")
        self.assertEqual(after_html, "<html><body>empty</body></html>")


if __name__ == "__main__":
    unittest.main()
