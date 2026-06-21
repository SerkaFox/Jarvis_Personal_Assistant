import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools_fs import ToolError


def _pillow_installed() -> bool:
    try:
        import PIL  # noqa: F401
    except Exception:
        return False
    return True


def _playwright_installed() -> bool:
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except Exception:
        return False
    return True


class FormatResultTests(unittest.TestCase):
    def test_format_user_result_hides_tools_called(self):
        from bot import format_user_result

        text = format_user_result(
            "Готово!",
            {
                "tools_called": ["apply_file_updates", "curl_check"],
                "resolved_path": "/tmp/x",
                "preview_url": "http://192.168.0.50:8700",
                "modified_files": ["/tmp/x/assets/css/style.css"],
            },
        )
        self.assertNotIn("tools_called", text)
        self.assertNotIn("WRITE_ROOT", text)
        self.assertIn("Готово!", text)
        self.assertIn("Папка: /tmp/x", text)
        self.assertIn("Изменил файлы:", text)
        self.assertIn("Открыть сайт: http://192.168.0.50:8700", text)

    def test_format_debug_result_shows_tools_called(self):
        from bot import format_debug_result

        text = format_debug_result({"tools_called": ["apply_file_updates"], "resolved_path": "/tmp/x"})
        self.assertIn("tools_called: apply_file_updates", text)
        self.assertIn("actual_path: /tmp/x", text)
        self.assertIn("рабочая папка Jarvis", text)


class PreviewUrlTests(unittest.TestCase):
    def setUp(self):
        self.old_server_host = os.environ.get("SERVER_HOST")

    def tearDown(self):
        if self.old_server_host is None:
            os.environ.pop("SERVER_HOST", None)
        else:
            os.environ["SERVER_HOST"] = self.old_server_host

    def test_preview_url_uses_server_host_env_not_127001(self):
        import tools_preview

        os.environ["SERVER_HOST"] = "http://192.168.0.77"
        url = tools_preview.preview_url_for_port(8701)
        self.assertEqual(url, "http://192.168.0.77:8701")
        self.assertNotIn("127.0.0.1", url)

    def test_preview_url_falls_back_to_lan_ip_when_unset(self):
        import tools_preview

        os.environ.pop("SERVER_HOST", None)
        tools_preview._LAN_IP_CACHE.clear()
        try:
            with patch.object(tools_preview.config, "SERVER_HOST", "http://127.0.0.1"):
                with patch("tools_preview.subprocess.run") as mock_run:
                    mock_run.return_value.stdout = "192.168.0.159 172.17.0.1\n"
                    url = tools_preview.preview_url_for_port(8701)
        finally:
            tools_preview._LAN_IP_CACHE.clear()
        self.assertNotIn("127.0.0.1", url)
        self.assertTrue(url.startswith("http://192.168.0.159:8701"))

    def test_curl_check_still_uses_127001_internally(self):
        import tools_preview

        with patch("tools_preview.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("connection refused")
            result = tools_preview.curl_check(59999)
        self.assertIn("127.0.0.1", result["url"])


class JsonRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jarvis_jsonrepair_test_")
        self.old_write_mode = os.environ.get("WRITE_MODE_ENABLED")
        self.old_write_root = os.environ.get("WRITE_ROOT")
        self.old_db_path = os.environ.get("JARVIS_DB_PATH")
        os.environ["WRITE_MODE_ENABLED"] = "true"
        os.environ["WRITE_ROOT"] = self.tmp.name
        import config

        self.old_config_db_path = config.JARVIS_DB_PATH
        temp_db = str(Path(self.tmp.name) / "data" / "jarvis.db")
        os.environ["JARVIS_DB_PATH"] = temp_db
        config.JARVIS_DB_PATH = temp_db

    def tearDown(self):
        import config

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

    def test_extract_json_object_handles_fenced_json(self):
        from action_schemas import extract_json_object

        raw = '```json\n{"a": 1, "b": "x"}\n```'
        data = extract_json_object(raw)
        self.assertEqual(data["a"], 1)
        self.assertEqual(data["b"], "x")

    def test_ask_ollama_for_site_edit_repairs_after_invalid_fenced_response(self):
        from bot import ask_ollama_for_site_edit
        from tools_write import create_static_site

        create_static_site("repairsite")
        responses = [
            "```json\n{this is not valid json at all}\n```",
            (
                '{"action": "edit_workspace_site", "project_name": "repairsite", "summary": "fix", '
                '"files": [{"path": "assets/css/style.css", "content": "body{color:red}"}], "notes": []}'
            ),
        ]
        calls = {"n": 0}

        def fake_ask(messages):
            idx = min(calls["n"], len(responses) - 1)
            calls["n"] += 1
            return responses[idx]

        with patch("bot.ask_ollama_messages", side_effect=fake_ask):
            spec = ask_ollama_for_site_edit(
                "change color", "repairsite", [{"path": "index.html", "content": "<html></html>"}]
            )
        self.assertEqual(spec["project_name"], "repairsite")
        self.assertEqual(calls["n"], 2)

    def test_ask_ollama_for_site_edit_gives_up_after_two_invalid_attempts(self):
        from bot import ask_ollama_for_site_edit

        with patch("bot.ask_ollama_messages", return_value="I cannot comply with this request."):
            with self.assertRaises(ToolError):
                ask_ollama_for_site_edit(
                    "change color", "anyproject", [{"path": "index.html", "content": "<html></html>"}]
                )

    def test_edit_workflow_invalid_json_does_not_modify_files_and_is_friendly(self):
        from bot import edit_workspace_site_workflow
        from tools_write import create_static_site

        create_static_site("nojsonsite")
        css_path = Path(self.tmp.name) / "nojsonsite" / "assets" / "css" / "style.css"
        original_css = css_path.read_text(encoding="utf-8")

        with patch("bot.ask_ollama_messages", return_value="I cannot produce JSON for this request."):
            answer, debug = edit_workspace_site_workflow("сделай неонового цвета", "nojsonsite", chat_id="t2")

        self.assertIn("Не смог получить корректный план изменений от модели", answer)
        self.assertNotIn("WRITE_ROOT", answer)
        self.assertEqual(css_path.read_text(encoding="utf-8"), original_css)
        self.assertTrue(debug["errors"])


class AcceptanceCheckTests(unittest.TestCase):
    def test_run_acceptance_checks_catches_missing_es_language(self):
        from bot import run_acceptance_checks

        before = [{"path": "index.html", "content": "<html><body></body></html>"}]
        after = [
            {
                "path": "index.html",
                "content": (
                    '<header><button data-lang="ru">RU</button><button data-lang="en">EN</button></header>'
                    "<script>document.addEventListener('click', function(e){ if(e.target.dataset.lang){"
                    " setLang(e.target.dataset.lang); }});</script>"
                ),
            }
        ]
        result = run_acceptance_checks("сделай переключение языка ru/en/es", before, after)
        self.assertFalse(result["success"])
        self.assertTrue(any("ES" in f for f in result["failed"]), result["failed"])

    def test_run_acceptance_checks_catches_missing_rotate_animation(self):
        from bot import run_acceptance_checks

        before = [{"path": "assets/css/style.css", "content": ""}]
        after = [{"path": "assets/css/style.css", "content": ".card{transition:0.3s}"}]
        result = run_acceptance_checks("добавь анимацию вращения карточек на 360 градусов", before, after)
        self.assertFalse(result["success"])
        self.assertTrue(any("анимация" in f for f in result["failed"]), result["failed"])

    def test_run_acceptance_checks_passes_with_full_language_and_animation_support(self):
        from bot import run_acceptance_checks

        before = [{"path": "index.html", "content": "<html></html>"}]
        html = (
            '<header><button data-lang="ru">RU</button><button data-lang="es">ES</button>'
            '<button data-lang="en">EN</button></header>'
            '<main><section><div class="card">x</div></section></main>'
            "<script>document.addEventListener('click', function(e){ if(e.target.dataset.lang)"
            " setLang(e.target.dataset.lang); });</script>"
            "<style>.card{animation: spin 1s;} @keyframes spin{ from{transform:rotate(0);}"
            " to{transform:rotateY(360deg);} }</style>"
        )
        after = [{"path": "index.html", "content": html}]
        result = run_acceptance_checks("сделай переключение языков ru/en/es и анимацию вращения 360", before, after)
        self.assertTrue(result["success"], result["failed"])

    def test_run_acceptance_checks_catches_dropped_sections(self):
        from bot import run_acceptance_checks

        before = [{"path": "index.html", "content": "<section>a</section><section>b</section>"}]
        after = [{"path": "index.html", "content": "<section>a</section>"}]
        result = run_acceptance_checks("поменяй стиль сайта на зеленый", before, after)
        self.assertFalse(result["success"])
        self.assertTrue(any("секции" in f for f in result["failed"]), result["failed"])


class ToolsMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jarvis_media_test_")
        self.old_write_mode = os.environ.get("WRITE_MODE_ENABLED")
        self.old_write_root = os.environ.get("WRITE_ROOT")
        os.environ["WRITE_MODE_ENABLED"] = "true"
        os.environ["WRITE_ROOT"] = self.tmp.name
        from tools_write import create_static_site

        create_static_site("mediasite")

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

    def _tiny_jpeg_bytes(self) -> bytes:
        if _pillow_installed():
            from PIL import Image

            buf = io.BytesIO()
            Image.new("RGB", (10, 10), color=(255, 0, 0)).save(buf, format="JPEG")
            return buf.getvalue()
        # Minimal real JPEG signature + padding, enough to pass magic-byte sniffing
        # without needing Pillow installed.
        return b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"

    def test_save_telegram_image_sanitizes_path_traversal_in_name(self):
        from tools_media import save_telegram_image_to_project

        data = self._tiny_jpeg_bytes()
        result = save_telegram_image_to_project("mediasite", data, original_name="../../evil.jpg", mime_type="image/jpeg")
        saved_path = Path(result["path"])
        img_dir = (Path(self.tmp.name) / "mediasite" / "assets" / "img").resolve()
        self.assertEqual(saved_path.parent.resolve(), img_dir)

    def test_save_telegram_image_rejects_non_image_bytes(self):
        from tools_media import save_telegram_image_to_project

        with self.assertRaises(ToolError):
            save_telegram_image_to_project("mediasite", b"not an image, just text" * 10, original_name="fake.jpg", mime_type="image/jpeg")

    def test_save_telegram_image_rejects_svg(self):
        from tools_media import save_telegram_image_to_project

        svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
        with self.assertRaises(ToolError):
            save_telegram_image_to_project("mediasite", svg, original_name="bad.svg", mime_type="image/svg+xml")

    def test_save_telegram_image_rejects_oversized_file(self):
        from tools_media import MAX_IMAGE_BYTES, save_telegram_image_to_project

        oversized = b"\xff\xd8\xff\xe0" + b"\x00" * (MAX_IMAGE_BYTES + 1)
        with self.assertRaises(ToolError):
            save_telegram_image_to_project("mediasite", oversized, original_name="big.jpg", mime_type="image/jpeg")

    def test_set_hero_background_rejects_path_traversal(self):
        from tools_media import set_hero_background

        with self.assertRaises(ToolError):
            set_hero_background("mediasite", "../../../etc/passwd")

    def test_set_hero_background_rejects_missing_file(self):
        from tools_media import set_hero_background

        with self.assertRaises(ToolError):
            set_hero_background("mediasite", "assets/img/does-not-exist.jpg")

    @unittest.skipUnless(_pillow_installed(), "Pillow not installed")
    def test_convert_to_webp_produces_webp_file(self):
        from tools_media import convert_to_webp, save_telegram_image_to_project

        data = self._tiny_jpeg_bytes()
        saved = save_telegram_image_to_project("mediasite", data, original_name="photo.jpg", mime_type="image/jpeg")
        webp_path = str(Path(saved["path"]).with_suffix(".webp"))
        result = convert_to_webp(saved["path"], webp_path)
        self.assertTrue(Path(webp_path).is_file())
        self.assertEqual(result["width"], 10)

    def test_convert_to_webp_raises_clean_error_without_pillow(self):
        from tools_media import convert_to_webp

        with patch("tools_media.pillow_available", return_value=False):
            with self.assertRaises(ToolError):
                convert_to_webp("/tmp/whatever.jpg", "/tmp/whatever.webp")

    def test_list_project_images_reports_saved_image(self):
        from tools_media import list_project_images, save_telegram_image_to_project

        save_telegram_image_to_project("mediasite", self._tiny_jpeg_bytes(), original_name="a.jpg", mime_type="image/jpeg")
        result = list_project_images("mediasite")
        self.assertEqual(result["count"], 1)


class BrowserCheckTests(unittest.TestCase):
    def test_check_site_with_playwright_skips_gracefully_when_not_installed(self):
        import tools_browser

        with patch("tools_browser.playwright_available", return_value=False):
            result = tools_browser.check_site_with_playwright("anyproject", "http://127.0.0.1:9999/")
        self.assertTrue(result["skipped"])
        self.assertIsNone(result["success"])
        self.assertEqual(result["errors"], [])

    @unittest.skipUnless(_playwright_installed(), "Playwright not installed")
    def test_sync_check_does_not_crash_inside_running_asyncio_loop(self):
        import asyncio

        from tools_browser import check_site_with_playwright

        async def runner():
            return check_site_with_playwright("loopcheck", "data:text/html,<html><body>hi</body></html>")

        result = asyncio.run(runner())
        joined_errors = " ".join(result.get("errors") or [])
        self.assertNotIn("asyncio loop", joined_errors)
        self.assertNotIn("Sync API", joined_errors)

    @unittest.skipUnless(_playwright_installed(), "Playwright not installed")
    def test_async_check_works_against_real_server(self):
        import asyncio
        import http.server
        import threading

        from tools_browser import check_site_with_playwright_async

        tmp = tempfile.TemporaryDirectory(prefix="jarvis_pw_http_")
        (Path(tmp.name) / "index.html").write_text("<html><head><title>T</title></head><body><h1>hi</h1></body></html>", encoding="utf-8")
        handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(*args, directory=tmp.name, **kwargs)
        httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            result = asyncio.run(check_site_with_playwright_async("pwasync", f"http://127.0.0.1:{port}/"))
        finally:
            httpd.shutdown()
            tmp.cleanup()
        self.assertTrue(result["success"], result.get("errors"))
        self.assertEqual(result["title"], "T")
        self.assertTrue(result["body_present"])


class RepairLoopAndBackgroundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jarvis_repair_test_")
        self.old_write_mode = os.environ.get("WRITE_MODE_ENABLED")
        self.old_write_root = os.environ.get("WRITE_ROOT")
        self.old_db_path = os.environ.get("JARVIS_DB_PATH")
        os.environ["WRITE_MODE_ENABLED"] = "true"
        os.environ["WRITE_ROOT"] = self.tmp.name
        import config

        self.old_config_db_path = config.JARVIS_DB_PATH
        temp_db = str(Path(self.tmp.name) / "data" / "jarvis.db")
        os.environ["JARVIS_DB_PATH"] = temp_db
        config.JARVIS_DB_PATH = temp_db

    def tearDown(self):
        import config

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

    def test_repair_loop_caps_at_two_iterations(self):
        from bot import edit_workspace_site_workflow
        from tools_write import create_static_site

        create_static_site("repairloop")

        calls = {"n": 0}

        def always_missing_es(user_text, project_name, current_files, requirements=None):
            calls["n"] += 1
            return {
                "action": "edit_workspace_site",
                "project_name": project_name,
                "summary": "tries but never adds ES",
                "files": [
                    {
                        "path": "index.html",
                        "content": (
                            '<header><button data-lang="ru">RU</button><button data-lang="en">EN</button></header>'
                            "<script>document.addEventListener('click', function(e){ if(e.target.dataset.lang)"
                            " setLang(e.target.dataset.lang); });</script>"
                        ),
                    }
                ],
                "notes": [],
            }

        with patch("bot.ask_ollama_for_site_edit", side_effect=always_missing_es):
            answer, debug = edit_workspace_site_workflow(
                "сделай переключение языков ru/en/es", "repairloop", chat_id="rl"
            )

        # initial attempt + MAX_REPAIR_ITERATIONS(2) repairs = 3 generation calls total
        self.assertEqual(calls["n"], 3)
        self.assertFalse(debug["acceptance"]["success"])
        self.assertIn("проверка не прошла, изменения откатил", answer)
        self.assertNotIn("Готово", answer)
        self.assertNotIn("tools_called", answer)
        self.assertTrue(debug["rolled_back"])

        from tools_edit import read_workspace_project_files

        after_rollback = read_workspace_project_files("repairloop")
        index_html = next(f["content"] for f in after_rollback["files"] if f["path"] == "index.html")
        self.assertNotIn("setLang", index_html)

    @unittest.skipUnless(_pillow_installed() and _playwright_installed(), "Pillow/Playwright not installed")
    def test_background_image_workflow_passes_when_css_references_real_visible_image(self):
        from PIL import Image

        from bot import edit_workspace_site_workflow
        from tools_media import save_telegram_image_to_project
        from tools_preview import start_preview
        from tools_write import create_static_site

        create_static_site("bgsite")
        start_preview("bgsite")

        buf = io.BytesIO()
        Image.new("RGB", (10, 10), color=(0, 0, 255)).save(buf, format="JPEG")
        saved = save_telegram_image_to_project("bgsite", buf.getvalue(), original_name="hero.jpg", mime_type="image/jpeg")
        image_name = Path(saved["relative_path"]).name

        def bg_spec(user_text, project_name, current_files, requirements=None):
            return {
                "action": "edit_workspace_site",
                "project_name": project_name,
                "summary": "added hero background",
                "files": [
                    {
                        "path": "assets/css/style.css",
                        "content": f".hero{{background-image:url('../img/{image_name}');background-size:cover;}}",
                    }
                ],
                "notes": [],
            }

        with patch("bot.ask_ollama_for_site_edit", side_effect=bg_spec):
            answer, debug = edit_workspace_site_workflow(
                f"используй изображение assets/img/{image_name} как фон hero-секции",
                "bgsite",
                chat_id="bg",
                expect_background_image=saved["relative_path"],
            )

        self.assertTrue(debug["acceptance"]["success"], debug["acceptance"]["failed"])
        self.assertIn("Готово!", answer)
        self.assertTrue(debug["browser_check"]["background_image_loaded"])

    def test_background_image_workflow_fails_when_css_does_not_reference_image(self):
        from bot import edit_workspace_site_workflow
        from tools_write import create_static_site

        create_static_site("bgsite_bad")

        def bad_bg_spec(user_text, project_name, current_files, requirements=None):
            return {
                "action": "edit_workspace_site",
                "project_name": project_name,
                "summary": "did nothing useful",
                "files": [{"path": "assets/css/style.css", "content": "body{color:red}"}],
                "notes": [],
            }

        with patch("bot.ask_ollama_for_site_edit", side_effect=bad_bg_spec):
            answer, debug = edit_workspace_site_workflow(
                "используй изображение assets/img/missing-hero.jpg как фон",
                "bgsite_bad",
                chat_id="bg2",
                expect_background_image="assets/img/missing-hero.jpg",
            )

        self.assertFalse(debug["acceptance"]["success"])
        self.assertTrue(any("фон" in f for f in debug["acceptance"]["failed"]))


if __name__ == "__main__":
    unittest.main()
