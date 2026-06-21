import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools_fs import ToolError
import config
import semantic_router as sr


def fixture_site_edit_spec(user_text, project_name, current_files, requirements=None):
    return {
        "action": "edit_workspace_site",
        "project_name": project_name,
        "summary": "Changed style to green and added Bilbao weather widget",
        "files": [
            {
                "path": "assets/css/style.css",
                "content": "body{background:#0b3d0b;color:#eafbea}\n.weather{color:#bdf7bd}\n",
            },
            {
                "path": "assets/js/main.js",
                "content": (
                    "fetch('https://api.open-meteo.com/v1/forecast?latitude=43.2630&longitude=-2.9350"
                    "&current_weather=true').then(r=>r.json()).then(d=>{"
                    "document.getElementById('weather').textContent=d.current_weather.temperature+\"C\";"
                    "}).catch(()=>{document.getElementById('weather').textContent='weather unavailable';});\n"
                ),
            },
        ],
        "notes": ["green palette", "bilbao weather via open-meteo, no api key, no cdn"],
    }


class EditWorkflowSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jarvis_edit_test_")
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

    # ---- tools_edit ----

    def test_read_workspace_project_files_reads_existing_site(self):
        from tools_write import create_static_site
        from tools_edit import read_workspace_project_files

        create_static_site("sitebota")
        result = read_workspace_project_files("sitebota")
        paths = {f["path"] for f in result["files"]}
        self.assertIn("index.html", paths)
        self.assertIn("assets/css/style.css", paths)
        self.assertEqual(result["missing"], [])

    def test_read_workspace_project_files_rejects_traversal(self):
        from tools_edit import read_workspace_project_files

        with self.assertRaises(ToolError):
            read_workspace_project_files("../outside")

    def test_read_workspace_project_files_missing_project_raises(self):
        from tools_edit import read_workspace_project_files

        with self.assertRaises(ToolError):
            read_workspace_project_files("does-not-exist")

    def test_apply_file_updates_writes_full_content(self):
        from tools_write import create_static_site
        from tools_edit import apply_file_updates

        create_static_site("sitebota")
        result = apply_file_updates(
            "sitebota", [{"path": "assets/css/style.css", "content": "body{background:green}\n"}]
        )
        self.assertTrue(result["success"])
        self.assertTrue(any(p.endswith("assets/css/style.css") for p in result["modified_files"]))
        self.assertEqual(
            (Path(self.tmp.name) / "sitebota" / "assets" / "css" / "style.css").read_text(encoding="utf-8"),
            "body{background:green}\n",
        )

    def test_verify_workspace_project_reports_structure(self):
        from tools_write import create_static_site
        from tools_edit import verify_workspace_project

        create_static_site("sitebota")
        verify = verify_workspace_project("sitebota")
        self.assertTrue(verify["success"])
        self.assertTrue(verify["index_html"])

    # ---- action_schemas ----

    def test_validate_edit_workspace_site_action_accepts_valid_spec(self):
        from action_schemas import validate_edit_workspace_site_action

        spec = validate_edit_workspace_site_action(
            fixture_site_edit_spec("x", "sitebota", []), expected_project_name="sitebota"
        )
        self.assertEqual(spec["project_name"], "sitebota")
        self.assertEqual(len(spec["files"]), 2)

    def test_validate_edit_workspace_site_action_rejects_absolute_path(self):
        from action_schemas import validate_edit_workspace_site_action

        with self.assertRaises(ToolError):
            validate_edit_workspace_site_action(
                {
                    "action": "edit_workspace_site",
                    "project_name": "sitebota",
                    "files": [{"path": "/etc/passwd", "content": "x"}],
                },
                expected_project_name="sitebota",
            )

    def test_validate_edit_workspace_site_action_rejects_project_mismatch(self):
        from action_schemas import validate_edit_workspace_site_action

        with self.assertRaises(ToolError):
            validate_edit_workspace_site_action(
                {
                    "action": "edit_workspace_site",
                    "project_name": "other",
                    "files": [{"path": "index.html", "content": "x"}],
                },
                expected_project_name="sitebota",
            )

    # ---- semantic_router ----

    def test_router_classifies_bug_report_phrase_as_edit_workspace_site(self):
        def ask_model(messages):
            return (
                '{"intent": "edit_workspace_site", "confidence": 0.9, "project_name": "sitebota", '
                '"target": null, "needs_tool": true, "start_preview": false, "language": "ru", '
                '"reason": "user wants style and content changes to an existing site"}'
            )

        result = sr.classify_intent(
            "поменяй стиль сайта на зеленый и добавь погоду в Бильбао",
            current_project="sitebota",
            ask_model=ask_model,
        )
        self.assertEqual(result["intent"], "edit_workspace_site")
        self.assertEqual(result["project_name"], "sitebota")
        self.assertNotEqual(result["intent"], "preview_stop")

    def test_pending_task_for_text_routes_bug_report_phrase(self):
        from bot import pending_task_for_text

        task = pending_task_for_text("поменяй стиль сайта на зеленый и добавь погоду в Бильбао")
        self.assertEqual(task, "edit_workspace_site")

    # ---- bot.edit_workspace_site_workflow ----

    def test_edit_workflow_writes_files_and_never_stops_running_preview(self):
        from bot import create_site_workflow, edit_workspace_site_workflow, fixture_site_spec, get_last_action
        from tools_preview import preview_status, start_preview

        create_site_workflow(
            "создай сайт sitebota",
            project_name="sitebota",
            chat_id="test",
            start_preview_requested=False,
            site_spec_provider=fixture_site_spec,
        )
        started = start_preview("sitebota")

        with patch("bot.ask_ollama_for_site_edit", side_effect=fixture_site_edit_spec):
            answer, debug = edit_workspace_site_workflow(
                "поменяй стиль сайта на зеленый и добавь погоду в Бильбао", "sitebota", chat_id="test"
            )

        self.assertNotIn("stop_preview", debug["tools_called"])
        self.assertIn("apply_file_updates", debug["tools_called"])
        self.assertTrue(any(p.endswith("assets/css/style.css") for p in debug["modified_files"]))
        self.assertTrue(preview_status("sitebota")["running"])
        self.assertEqual(preview_status("sitebota")["port"], started["port"])
        action = get_last_action("test")
        self.assertEqual(action["intent"], "edit_workspace_site")
        self.assertTrue(action["success"])
        self.assertIn("изменил сайт sitebota", answer)
        self.assertNotIn("tools_called", answer)

    def test_edit_workflow_uses_lan_url_from_server_host(self):
        from bot import edit_workspace_site_workflow
        from tools_write import create_static_site
        from tools_preview import start_preview

        create_static_site("sitebota_lan")
        start_preview("sitebota_lan")

        with patch("bot.ask_ollama_for_site_edit", side_effect=fixture_site_edit_spec):
            answer, debug = edit_workspace_site_workflow(
                "измени стиль на зеленый", "sitebota_lan", chat_id="test"
            )

        self.assertTrue(debug["preview_url"].startswith("http://192.168.0.50:"))
        self.assertIn(debug["preview_url"], answer)

    def test_edit_workflow_does_not_start_preview_when_not_running_and_no_restart_word(self):
        from bot import edit_workspace_site_workflow
        from tools_write import create_static_site
        from tools_preview import preview_status

        create_static_site("sitebota_stopped")

        with patch("bot.ask_ollama_for_site_edit", side_effect=fixture_site_edit_spec):
            answer, debug = edit_workspace_site_workflow(
                "поменяй стиль на зеленый", "sitebota_stopped", chat_id="test"
            )

        self.assertNotIn("start_preview", debug["tools_called"])
        self.assertFalse(preview_status("sitebota_stopped")["running"])
        self.assertIn("Preview не запущен", answer)

    def test_edit_workflow_missing_project_returns_error_without_crash(self):
        from bot import edit_workspace_site_workflow

        answer, debug = edit_workspace_site_workflow("поменяй стиль на зеленый", "does-not-exist", chat_id="test")
        self.assertTrue(debug["errors"])
        self.assertIn("Не смог отредактировать", answer)

    # ---- current_task tracking ----

    def test_current_task_clears_on_successful_completion(self):
        from bot import edit_workspace_site_workflow, get_current_task
        from tools_write import create_static_site

        create_static_site("sitebota_task")
        with patch("bot.ask_ollama_for_site_edit", side_effect=fixture_site_edit_spec):
            edit_workspace_site_workflow("поменяй стиль на зеленый", "sitebota_task", chat_id="task_chat")
        self.assertIsNone(get_current_task("task_chat"))

    def test_current_task_round_trip_and_step_updates(self):
        from bot import save_current_task, update_current_task_step, get_current_task, clear_current_task

        save_current_task("chat_x", "edit_workspace_site", "demo", "starting")
        update_current_task_step("chat_x", "reading_files")
        task = get_current_task("chat_x")
        self.assertEqual(task["step"], "reading_files")
        self.assertEqual(task["project_name"], "demo")
        clear_current_task("chat_x")
        self.assertIsNone(get_current_task("chat_x"))

    # ---- /where LAN URL ----

    def test_where_reports_lan_url_not_localhost(self):
        from bot import workspace_where_answer
        from tools_write import create_static_site
        from tools_preview import start_preview

        create_static_site("sitebota_where")
        start_preview("sitebota_where")
        answer, _debug = workspace_where_answer("sitebota_where", chat_id="test")
        url_line = next(line for line in answer.splitlines() if line.startswith("Открыть сайт:"))
        self.assertTrue(url_line.startswith("Открыть сайт: http://192.168.0.50:"))
        self.assertNotIn("tools_called", answer)


if __name__ == "__main__":
    unittest.main()
