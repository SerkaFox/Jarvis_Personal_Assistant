import os
import tempfile
import unittest
import urllib.error
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
        from tools_preview import network_sockets_available, preview_status, start_preview, stop_preview
        from tools_write import create_static_site

        if not network_sockets_available():
            self.skipTest("network sockets not permitted in this test environment")

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
        from tools_preview import network_sockets_available, start_preview, stop_preview
        from tools_write import create_static_site, verify_static_site

        if not network_sockets_available():
            self.skipTest("network sockets not permitted in this test environment")

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
        from tools_preview import network_sockets_available, stop_preview

        if not network_sockets_available():
            self.skipTest("network sockets not permitted in this test environment")

        with patch("bot.ask_ollama_for_site_spec", side_effect=fixture_site_spec):
            answer, debug = write_mode_answer("создай сайт sitebota в рабочей папке и запусти временный сервер", chat_id="test")
        self.assertIn("ask_ollama_for_site_spec", debug["tools_called"])
        self.assertIn("write_text_file", debug["tools_called"])
        self.assertIn("start_preview", debug["tools_called"])
        self.assertIn("Открыть сайт:", answer)
        self.assertTrue((Path(self.tmp.name) / "sitebota" / "index.html").exists())
        stop_preview("sitebota")

    def test_where_missing_project_returns_exists_false(self):
        from bot import workspace_where_answer

        answer, debug = workspace_where_answer("missing_site", chat_id="test")
        self.assertIn("Не нашел проект missing_site", answer)
        self.assertEqual(debug["detected"]["intent"], "where_project")
        self.assertEqual(debug["errors"], ["project not found"])

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
        self.assertIn("Готово! Я создал сайт", answer)
        self.assertNotIn("tools_called", answer)
        self.assertTrue((Path(self.tmp.name) / "sitebota_test" / "index.html").is_file())
        action = get_last_action("test")
        self.assertTrue(action["success"])
        self.assertEqual(action["project_name"], "sitebota_test")
        self.assertTrue(action["path"].endswith("sitebota_test"))

    def test_create_and_preview_workflow_starts_preview_and_last_action(self):
        from bot import create_site_workflow, fixture_site_spec, get_last_action
        from tools_preview import network_sockets_available, stop_preview

        if not network_sockets_available():
            self.skipTest("network sockets not permitted in this test environment")

        answer, debug = create_site_workflow(
            "создай сайт sitebota_preview_test",
            project_name="sitebota_preview_test",
            chat_id="test",
            start_preview_requested=True,
            site_spec_provider=fixture_site_spec,
        )
        self.assertIn("start_preview", debug["tools_called"])
        self.assertIn("curl_localhost", debug["tools_called"])
        self.assertIn("Открыть сайт:", answer)
        action = get_last_action("test")
        self.assertTrue(action["success"])
        self.assertTrue(action["preview_url"].startswith("http://192.168.0.50:"))
        stop_preview("sitebota_preview_test")

    def test_workspace_where_reports_preview_url_after_create_and_preview(self):
        from bot import create_site_workflow, fixture_site_spec, workspace_where_answer
        from tools_preview import network_sockets_available, stop_preview

        if not network_sockets_available():
            self.skipTest("network sockets not permitted in this test environment")

        create_site_workflow(
            "создай сайт sitebota_where_test",
            project_name="sitebota_where_test",
            chat_id="test",
            start_preview_requested=True,
            site_spec_provider=fixture_site_spec,
        )
        answer, debug = workspace_where_answer("sitebota_where_test", chat_id="test")
        self.assertIn("Сайт sitebota_where_test запущен.", answer)
        self.assertIn("Открыть сайт: http://192.168.0.50:", answer)
        self.assertEqual(debug["detected"]["intent"], "where_project")
        stop_preview("sitebota_where_test")

    def test_no_fake_created_claim_without_tool_success(self):
        from bot import save_last_action, workspace_where_answer

        save_last_action("test", {"intent": "create_site", "project_name": "ghost", "success": False, "error": "tool failed"})
        answer, _debug = workspace_where_answer(None, chat_id="test")
        self.assertIn("Не вижу подтвержденного созданного сайта", answer)
        self.assertNotIn("Готово! Я создал сайт", answer)

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

    def test_stop_preview_verifies_port_and_process_closed(self):
        from tools_preview import network_sockets_available, preview_status, start_preview, stop_preview
        from tools_write import create_static_site

        if not network_sockets_available():
            self.skipTest("network sockets not permitted in this test environment")

        create_static_site("stopcheck")
        started = start_preview("stopcheck")
        port = started["port"]
        result = stop_preview("stopcheck")
        self.assertTrue(result["success"])
        self.assertTrue(result["stopped"])
        self.assertFalse(result["checks"]["process_alive"])
        self.assertFalse(result["checks"]["port_listening"])
        self.assertFalse(result["checks"]["curl_responds"])
        self.assertFalse(preview_status("stopcheck")["running"])
        with self.assertRaises(urllib.error.URLError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)

    def test_stop_preview_missing_project_raises(self):
        from tools_preview import stop_preview

        with self.assertRaises(ToolError):
            stop_preview("does-not-exist")

    def test_stop_preview_by_port_rejects_out_of_range_port(self):
        from tools_preview import stop_preview_by_port

        with self.assertRaises(ToolError):
            stop_preview_by_port(22)

    def test_stop_preview_by_port_stops_registered_preview(self):
        from tools_preview import network_sockets_available, port_is_listening, start_preview, stop_preview_by_port
        from tools_write import create_static_site

        if not network_sockets_available():
            self.skipTest("network sockets not permitted in this test environment")

        create_static_site("portstop")
        started = start_preview("portstop")
        port = started["port"]
        self.assertTrue(port_is_listening(port))
        result = stop_preview_by_port(port)
        self.assertTrue(result["success"])
        self.assertFalse(port_is_listening(port))

    def test_delete_workspace_dir_verifies_folder_gone(self):
        from tools_write import create_project_dir, delete_workspace_dir

        create_project_dir("deletecheck")
        self.assertTrue((Path(self.tmp.name) / "deletecheck").is_dir())
        result = delete_workspace_dir("deletecheck", confirm_token="DELETE:deletecheck")
        self.assertTrue(result["success"])
        self.assertTrue(result["verification"]["exists_after"] is False)
        self.assertFalse((Path(self.tmp.name) / "deletecheck").exists())

    def test_delete_workspace_dir_rejects_path_traversal_and_special_names(self):
        from tools_write import delete_workspace_dir

        for bad_name in ("..", ".", "/", "a/b", ""):
            with self.assertRaises(ToolError):
                delete_workspace_dir(bad_name, confirm_token=f"DELETE:{bad_name}")

    def test_delete_workspace_dir_stops_running_preview_first(self):
        from tools_preview import network_sockets_available, port_is_listening, preview_status, start_preview
        from tools_write import create_static_site, delete_workspace_dir

        if not network_sockets_available():
            self.skipTest("network sockets not permitted in this test environment")

        create_static_site("deletewithpreview")
        started = start_preview("deletewithpreview")
        port = started["port"]
        result = delete_workspace_dir("deletewithpreview", confirm_token="DELETE:deletewithpreview")
        self.assertTrue(result["success"])
        self.assertFalse(port_is_listening(port))
        self.assertFalse(preview_status("deletewithpreview")["running"])

    def test_preview_stop_command_does_not_claim_success_on_failure(self):
        import bot

        with patch("bot.stop_preview", return_value={"success": False, "stopped": False, "project": "ghost", "checks": {"process_alive": True, "port_listening": True, "curl_responds": True}, "error": "still running"}):
            text = bot._format_stop_result("stop_preview result:", bot.stop_preview("ghost"))
            debug_text = bot._format_stop_result("stop_preview result:", bot.stop_preview("ghost"), debug=True)
        self.assertIn("Не смог остановить preview ghost", text)
        self.assertIn("Причина: still running", text)
        self.assertNotIn("success: False", text)
        self.assertIn("success: False", debug_text)

    def test_stop_delete_answer_routes_stop_phrase_instead_of_chat(self):
        import bot
        from tools_preview import network_sockets_available

        if not network_sockets_available():
            self.skipTest("network sockets not permitted in this test environment")

        bot.create_static_site("nlstop")
        bot.start_preview("nlstop")
        bot.memory.set_current_project("nl_test_chat", "nlstop")
        answer, debug = bot.stop_delete_answer("останови сервер", chat_id="nl_test_chat")
        self.assertEqual(debug["detected"]["intent"], "stop_preview")
        self.assertIn("Остановил preview nlstop", answer)
        self.assertFalse(bot.preview_status("nlstop")["running"])

    def test_stop_delete_answer_requires_explicit_project_for_vague_delete(self):
        import bot

        answer, debug = bot.stop_delete_answer("удали папки сайтов", chat_id="nl_test_chat_2")
        self.assertIn("явно указать проект", answer)
        self.assertEqual(debug["errors"], ["project not specified"])

    def test_selftest_stop_delete_result_success(self):
        from bot import selftest_stop_delete_result
        from tools_preview import network_sockets_available

        if not network_sockets_available():
            self.skipTest("network sockets not permitted in this test environment")

        result = selftest_stop_delete_result()
        self.assertTrue(result["success"], result)
        self.assertTrue(result["checks"]["stop_success"])
        self.assertTrue(result["checks"]["delete_success"])
        self.assertTrue(result["checks"]["folder_gone"])
        self.assertFalse((Path(self.tmp.name) / "__jarvis_stop_delete_test__").exists())

    def test_workspace_inventory_reports_real_project_and_preview_fields(self):
        from tools_preview import network_sockets_available, stop_preview, start_preview
        from tools_write import create_static_site, workspace_inventory

        if not network_sockets_available():
            self.skipTest("network sockets not permitted in this test environment")

        create_static_site("invproj")
        started = start_preview("invproj")
        data = workspace_inventory()
        self.assertEqual(data["write_root"], self.tmp.name)
        self.assertTrue(data["exists"])
        self.assertTrue(data["writable"])
        project = next(p for p in data["projects"] if p["project_name"] == "invproj")
        self.assertTrue(project["exists"])
        self.assertTrue(project["has_index_html"])
        self.assertTrue(project["required_files"]["index.html"])
        self.assertTrue(project["preview_registered"])
        self.assertEqual(project["preview_port"], started["port"])
        self.assertTrue(project["port_listening"])
        self.assertTrue(project["running"])
        self.assertEqual(project["curl_status"], 200)
        stop_preview("invproj")

    def test_workspace_inventory_empty_write_root_has_no_projects(self):
        from tools_write import workspace_inventory

        data = workspace_inventory()
        self.assertEqual(data["projects"], [])
        self.assertEqual(data["count"], 0)

    def test_workspace_status_text_works_with_empty_previews_json(self):
        import bot

        text = bot._workspace_status_text()
        self.assertIn("Рабочая папка Jarvis:", text)
        self.assertIn(self.tmp.name, text)
        self.assertIn("нет проектов", text)

    def test_scan_listening_ports_structure(self):
        from tools_preview import scan_listening_ports

        ports = scan_listening_ports()
        self.assertIn("range", ports)
        self.assertIsInstance(ports["listening"], list)
        self.assertIsInstance(ports["registered_previews"], list)

    def test_scan_listening_ports_flags_unregistered_preview_as_suspicious(self):
        from tools_preview import scan_listening_ports, stop_preview
        from tools_write import create_static_site, delete_workspace_dir
        from tools_preview import network_sockets_available

        if not network_sockets_available():
            self.skipTest("network sockets not permitted in this test environment")

        create_static_site("suspectproj")
        from tools_preview import start_preview

        started = start_preview("suspectproj")
        port = started["port"]
        registry_path = Path(self.tmp.name) / "data" / "previews.json"
        import json as json_module

        registry_data = json_module.loads(registry_path.read_text(encoding="utf-8"))
        del registry_data["suspectproj"]
        registry_path.write_text(json_module.dumps(registry_data), encoding="utf-8")

        ports = scan_listening_ports()
        match = next((item for item in ports["listening"] if item["port"] == port), None)
        self.assertIsNotNone(match)
        self.assertTrue(match["suspicious"])
        self.assertFalse(match["registered"])

        import os as os_module
        import signal as signal_module

        os_module.killpg(started["pid"], signal_module.SIGKILL)


if __name__ == "__main__":
    unittest.main()
