import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Force (not setdefault) -- see test_task_orchestrator.py for why this must
# win regardless of import order in a full suite run.
os.environ["ALLOWED_USER_ID"] = "123"
os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")

import config


def _playwright_installed() -> bool:
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except Exception:
        return False
    return True


class _BaseUiComponentTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jarvis_ui_component_test_")
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


class ComponentModelTests(unittest.TestCase):
    def test_carousel_alias_maps_to_slider_kind(self):
        from ui_component_model import build_component_model, normalize_kind

        self.assertEqual(normalize_kind("проверь карусель на сайте"), "slider")
        self.assertEqual(build_component_model("carousel").kind, "slider")
        self.assertEqual(build_component_model("карусель").kind, "slider")

    def test_normalize_kind_handles_all_generic_kinds(self):
        from ui_component_model import normalize_kind

        self.assertEqual(normalize_kind("проверь гармошку"), "accordion")
        self.assertEqual(normalize_kind("проверь бургер меню"), "hamburger_menu")
        self.assertEqual(normalize_kind("проверь вкладки"), "tabs")
        self.assertEqual(normalize_kind("проверь форму обратной связи"), "form")
        self.assertEqual(normalize_kind("какая-то случайная фраза"), None)


class StaticVerificationTests(_BaseUiComponentTest):
    async def test_slider_with_five_static_slides_is_found_not_missing(self):
        from tools_write import create_static_site, write_project_text_file
        from ui_component_model import build_component_model
        from ui_component_verifier import verify_component_static
        from tools_edit import read_workspace_project_files

        create_static_site("staticslider")
        slides = "".join(f'<div class="jarvis-slide"><p>Slide {i}</p></div>' for i in range(5))
        write_project_text_file(
            "staticslider", "index.html", f'<html><body><div class="jarvis-slider">{slides}</div></body></html>', overwrite=True
        )
        files = read_workspace_project_files("staticslider")["files"]
        model = build_component_model("slider")
        result = verify_component_static(files, model)

        self.assertEqual(result["items_found"], 5)
        self.assertIn(result["interactivity_confirmed"], (False, None))
        self.assertNotEqual(result["status"], "missing")

    async def test_missing_component_is_reported_honestly(self):
        from tools_write import create_static_site
        from ui_component_model import build_component_model
        from ui_component_verifier import verify_component_static
        from tools_edit import read_workspace_project_files

        create_static_site("nocomponent")
        files = read_workspace_project_files("nocomponent")["files"]
        result = verify_component_static(files, build_component_model("accordion"))
        self.assertEqual(result["status"], "missing")
        self.assertFalse(result["container_found"])


@unittest.skipUnless(_playwright_installed(), "Playwright not installed")
class DynamicVerificationTests(_BaseUiComponentTest):
    async def test_slider_with_working_button_confirms_interactivity(self):
        from tools_write import create_static_site, write_project_text_file
        from tools_preview import start_preview
        from ui_component_model import build_component_model
        from ui_component_verifier import verify_components_async

        create_static_site("workingslider")
        html = """<html><body>
<div class="jarvis-slider">
  <div class="jarvis-slide active">1</div>
  <div class="jarvis-slide">2</div>
  <div class="jarvis-slide">3</div>
</div>
<button class="next" onclick="document.querySelectorAll('.jarvis-slide')[0].classList.remove('active');document.querySelectorAll('.jarvis-slide')[1].classList.add('active');">Next</button>
</body></html>"""
        write_project_text_file("workingslider", "index.html", html, overwrite=True)
        status = start_preview("workingslider")

        model = build_component_model("slider")
        results = await verify_components_async("workingslider", f"http://127.0.0.1:{status['port']}/", [model])
        result = results["slider"]

        self.assertEqual(result["items_found"], 3)
        self.assertTrue(result["nav_found"])
        self.assertTrue(result["interactivity_confirmed"])
        self.assertEqual(result["status"], "ok")

    async def test_accordion_open_close_is_detected(self):
        from tools_write import create_static_site, write_project_text_file
        from tools_preview import start_preview
        from ui_component_model import build_component_model
        from ui_component_verifier import verify_components_async

        create_static_site("accordionsite")
        html = """<html><body>
<div class="jarvis-accordion">
  <details class="jarvis-accordion-item"><summary>Q1</summary><p>A1</p></details>
  <details class="jarvis-accordion-item"><summary>Q2</summary><p>A2</p></details>
</div>
</body></html>"""
        write_project_text_file("accordionsite", "index.html", html, overwrite=True)
        status = start_preview("accordionsite")

        model = build_component_model("accordion")
        results = await verify_components_async("accordionsite", f"http://127.0.0.1:{status['port']}/", [model])
        result = results["accordion"]

        self.assertTrue(result["container_found"])
        self.assertEqual(result["items_found"], 2)
        self.assertNotEqual(result["status"], "missing")

    async def test_hamburger_menu_toggle_is_detected(self):
        from tools_write import create_static_site, write_project_text_file
        from tools_preview import start_preview
        from ui_component_model import build_component_model
        from ui_component_verifier import verify_components_async

        create_static_site("burgersite")
        html = """<html><body>
<nav class="jarvis-nav">
  <button class="hamburger" onclick="document.querySelector('.jarvis-nav').classList.toggle('open');">menu</button>
</nav>
</body></html>"""
        write_project_text_file("burgersite", "index.html", html, overwrite=True)
        status = start_preview("burgersite")

        model = build_component_model("hamburger_menu")
        results = await verify_components_async("burgersite", f"http://127.0.0.1:{status['port']}/", [model])
        result = results["hamburger_menu"]

        self.assertTrue(result["container_found"])
        self.assertTrue(result["interactivity_confirmed"])

    async def test_php_project_still_verifies_via_rendered_html(self):
        from tools_write import create_static_site, write_project_text_file
        from tools_preview import start_preview
        from ui_component_model import build_component_model
        from ui_component_verifier import verify_components_async
        from site_technology_detector import detect_technology

        create_static_site("phpsite")
        # A .php file makes site_technology_detector report "php", but the
        # verifier never reads it -- it only checks what http.server actually
        # serves (index.html), simulating PHP's eventual rendered output.
        write_project_text_file("phpsite", "legacy.php", "<?php echo 'old code'; ?>", overwrite=True)
        slides = "".join(f'<div class="jarvis-slide"><p>Slide {i}</p></div>' for i in range(3))
        write_project_text_file(
            "phpsite", "index.html", f'<html><body><div class="jarvis-slider">{slides}</div></body></html>', overwrite=True
        )

        tech = detect_technology("phpsite")
        self.assertEqual(tech["technology"], "php")

        status = start_preview("phpsite")
        model = build_component_model("slider")
        results = await verify_components_async("phpsite", f"http://127.0.0.1:{status['port']}/", [model])
        result = results["slider"]
        self.assertTrue(result["container_found"])
        self.assertEqual(result["items_found"], 3)
        self.assertNotEqual(result["status"], "missing")


class HandleTextIntegrationTests(_BaseUiComponentTest):
    async def test_handle_text_does_not_call_git_tools_directly(self):
        """All routing goes through run_claude_agent — git tools are never
        called directly from handle_text regardless of the message text."""
        import bot
        import tools_claude_agent
        from test_error_smoke import FakeContext, FakeUpdate

        async def fake_agent(text, chat_id, **kwargs):
            return "slider result"

        with patch.object(tools_claude_agent, "run_claude_agent", side_effect=fake_agent), \
             patch.object(bot, "find_git_repos") as git_mock:
            update = FakeUpdate("проверь слайдер в kuki")
            await bot.handle_text(update, FakeContext())

        git_mock.assert_not_called()
        self.assertIn("slider result", update.message.replies)

    async def test_handle_text_passes_message_to_agent(self):
        """Agent receives the exact user text."""
        import bot
        import tools_claude_agent
        from test_error_smoke import FakeContext, FakeUpdate

        received = {}

        async def fake_agent(text, chat_id, **kwargs):
            received["text"] = text
            return "ok"

        with patch.object(tools_claude_agent, "run_claude_agent", side_effect=fake_agent):
            update = FakeUpdate("проверь гармошку в kuki2")
            await bot.handle_text(update, FakeContext())

        self.assertIn("kuki2", received.get("text", ""))


if __name__ == "__main__":
    unittest.main()
