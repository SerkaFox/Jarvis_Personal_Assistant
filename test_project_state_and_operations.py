import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from tools_fs import ToolError


class _BaseStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jarvis_state_ops_test_")
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


class OperationPlanValidationTests(unittest.TestCase):
    def test_validate_operation_plan_accepts_known_op(self):
        import tools_site_operations as ops

        validated = ops.validate_operation_plan({"operations": [{"op": "add_slider", "params": {}}]})
        self.assertEqual(validated[0]["op"], "add_slider")

    def test_validate_operation_plan_rejects_unknown_op(self):
        import tools_site_operations as ops

        with self.assertRaises(ToolError):
            ops.validate_operation_plan({"operations": [{"op": "rewrite_whole_site"}]})

    def test_validate_operation_plan_rejects_unknown_feature(self):
        import tools_site_operations as ops

        with self.assertRaises(ToolError):
            ops.validate_operation_plan({"operations": [{"op": "add_feature", "feature": "custom_widget"}]})

    def test_validate_operation_plan_strips_unallowlisted_param_keys(self):
        import tools_site_operations as ops

        validated = ops.validate_operation_plan(
            {"operations": [{"op": "add_slider", "params": {"html": "<script>evil()</script>", "target": "hero"}}]}
        )
        self.assertNotIn("html", validated[0]["params"])
        self.assertEqual(validated[0]["params"]["target"], "hero")

    def test_validate_operation_plan_rejects_too_many_operations(self):
        import tools_site_operations as ops

        with self.assertRaises(ToolError):
            ops.validate_operation_plan({"operations": [{"op": "verify"} for _ in range(20)]})

    def test_validate_operation_plan_rejects_empty_operations(self):
        import tools_site_operations as ops

        with self.assertRaises(ToolError):
            ops.validate_operation_plan({"operations": []})


class OperationExecutorIdempotencyTests(_BaseStateTest):
    def test_marker_blocks_are_idempotent_across_repeated_calls(self):
        import tools_site_operations as ops
        from tools_write import create_static_site
        from tools_edit import read_workspace_project_files

        create_static_site("idempotent")
        ops.op_fix_language_switcher("idempotent", {})
        ops.op_fix_language_switcher("idempotent", {})
        ops.op_add_slider("idempotent", {})
        ops.op_add_slider("idempotent", {})

        files = {f["path"]: f["content"] for f in read_workspace_project_files("idempotent")["files"]}
        self.assertEqual(files["index.html"].count("jarvis-lang-buttons:start"), 1)
        self.assertEqual(files["index.html"].count("jarvis-slider:start"), 1)

    def test_op_add_slider_never_touches_unrelated_files(self):
        import tools_site_operations as ops
        from tools_write import create_static_site
        from tools_edit import read_workspace_project_files

        create_static_site("isolated")
        before = read_workspace_project_files("isolated")
        readme_before = next(f["content"] for f in before["files"] if f["path"] == "README.md")

        ops.op_add_slider("isolated", {})

        after = read_workspace_project_files("isolated")
        readme_after = next(f["content"] for f in after["files"] if f["path"] == "README.md")
        self.assertEqual(readme_before, readme_after)


class ProjectStateManagerTests(_BaseStateTest):
    def test_load_project_state_has_defaults_for_new_project(self):
        import project_state_manager as psm
        from tools_write import create_static_site

        create_static_site("freshproject")
        state = psm.load_project_state("freshproject")
        self.assertIsNone(state["last_successful_snapshot"])
        self.assertIsNone(state["last_failed_action"])
        self.assertEqual(state["applied_operations_history"], [])
        for name in psm.FEATURE_NAMES:
            self.assertEqual(state["features"][name]["status"], "unknown")

    def test_sync_features_from_inspection_reflects_real_files(self):
        import project_state_manager as psm
        import tools_site_operations as ops
        from tools_write import create_static_site

        create_static_site("syncedproject")
        ops.op_add_footer("syncedproject", {})
        state = psm.sync_features_from_inspection("syncedproject")
        self.assertEqual(state["features"]["footer"]["status"], "present")
        self.assertEqual(state["features"]["background"]["status"], "absent")

    def test_record_applied_operation_updates_last_successful_snapshot_only_on_success(self):
        import project_state_manager as psm
        from tools_write import create_static_site
        from tools_snapshot import snapshot_project

        create_static_site("recordproject")
        snap = snapshot_project("recordproject", reason="test")

        psm.record_applied_operation(
            "recordproject",
            user_text="add slider",
            operations=[{"op": "add_slider"}],
            files_changed=["index.html"],
            checks={"success": True, "failed": []},
            success=True,
            snapshot_id=snap["snapshot_id"],
        )
        state = psm.load_project_state("recordproject")
        self.assertEqual(state["last_successful_snapshot"], snap["snapshot_id"])
        self.assertIsNone(state["last_failed_action"])
        self.assertEqual(len(state["applied_operations_history"]), 1)
        self.assertTrue(state["applied_operations_history"][0]["success"])

    def test_record_applied_operation_does_not_update_last_successful_snapshot_on_failure(self):
        import project_state_manager as psm
        from tools_write import create_static_site
        from tools_snapshot import snapshot_project

        create_static_site("failrecordproject")
        good_snap = snapshot_project("failrecordproject", reason="good")
        psm.record_applied_operation(
            "failrecordproject",
            user_text="add slider",
            operations=[{"op": "add_slider"}],
            files_changed=["index.html"],
            checks={"success": True, "failed": []},
            success=True,
            snapshot_id=good_snap["snapshot_id"],
        )

        bad_snap = snapshot_project("failrecordproject", reason="before failed edit")
        psm.record_applied_operation(
            "failrecordproject",
            user_text="break languages",
            operations=[{"op": "verify"}],
            files_changed=[],
            checks={"success": False, "failed": ["language_switcher_required: missing"]},
            success=False,
            snapshot_id=bad_snap["snapshot_id"],
        )
        state = psm.load_project_state("failrecordproject")
        # last_successful_snapshot must still point at the earlier GOOD snapshot,
        # not the failed attempt's pre-edit snapshot.
        self.assertEqual(state["last_successful_snapshot"], good_snap["snapshot_id"])
        self.assertIsNotNone(state["last_failed_action"])
        self.assertFalse(state["last_failed_action"]["success"])
        self.assertEqual(len(state["applied_operations_history"]), 2)

    def test_diff_against_last_success_reports_real_changes(self):
        import project_state_manager as psm
        from tools_write import create_static_site, write_project_text_file
        from tools_snapshot import snapshot_project

        create_static_site("diffproject")
        snap = snapshot_project("diffproject", reason="baseline")
        psm.record_applied_operation(
            "diffproject",
            user_text="baseline",
            operations=[],
            files_changed=[],
            checks={"success": True, "failed": []},
            success=True,
            snapshot_id=snap["snapshot_id"],
        )
        write_project_text_file("diffproject", "assets/css/style.css", "body{color:red}", overwrite=True)

        result = psm.diff_against_last_success("diffproject")
        self.assertEqual(result["snapshot_id"], snap["snapshot_id"])
        self.assertTrue(any(d["path"] == "assets/css/style.css" for d in result["diffs"]))

    def test_format_functions_do_not_leak_raw_debug(self):
        import project_state_manager as psm
        from tools_write import create_static_site

        create_static_site("formatproject")
        for text in (
            psm.format_history_answer("formatproject"),
            psm.format_last_success_answer("formatproject"),
            psm.format_requirements_answer("formatproject"),
        ):
            self.assertNotIn("tools_called", text)


class LearningLogTests(_BaseStateTest):
    def test_record_and_recent_entries(self):
        import learning_log
        from tools_write import create_static_site

        create_static_site("loggedproject")
        learning_log.record(
            project_name="loggedproject",
            chat_id="chat1",
            user_text="add slider",
            detected_intent="edit_workspace_site",
            before_state={},
            operation_plan=[{"op": "add_slider"}],
            files_changed=["index.html"],
            checks={"success": True, "failed": []},
            success=True,
            rollback_used=False,
        )
        learning_log.record(
            project_name="loggedproject",
            chat_id="chat1",
            user_text="break languages",
            detected_intent="edit_workspace_site",
            before_state={},
            operation_plan=[{"op": "verify"}],
            files_changed=[],
            checks={"success": False, "failed": ["x"]},
            success=False,
            rollback_used=True,
        )
        entries = learning_log.recent_entries("loggedproject", limit=10)
        self.assertEqual(len(entries), 2)
        self.assertTrue(entries[0]["success"])
        self.assertFalse(entries[1]["success"])
        self.assertTrue(entries[1]["rollback_used"])

    def test_mark_last_feedback_tags_most_recent_entry_for_chat(self):
        import learning_log
        from tools_write import create_static_site

        create_static_site("feedbackproject")
        learning_log.record(
            project_name="feedbackproject",
            chat_id="chatA",
            user_text="task one",
            detected_intent="edit_workspace_site",
            before_state={},
            operation_plan=[],
            files_changed=[],
            checks={"success": True, "failed": []},
            success=True,
            rollback_used=False,
        )
        updated = learning_log.mark_last_feedback("feedbackproject", chat_id="chatA", status="approved")
        self.assertTrue(updated)
        entries = learning_log.recent_entries("feedbackproject")
        self.assertEqual(entries[-1]["user_feedback"], "approved")

    def test_mark_last_feedback_returns_false_when_no_entries(self):
        import learning_log

        self.assertFalse(learning_log.mark_last_feedback("nosuchproject", chat_id="x", status="approved"))


class WorkflowIntegrationTests(_BaseStateTest):
    def test_successful_edit_records_learning_log_and_project_state(self):
        from bot import edit_workspace_site_workflow
        from tools_write import create_static_site
        import project_state_manager as psm
        import learning_log

        create_static_site("integrationsite")

        def add_slider_plan(user_text, project_name, project_state):
            return {"operations": [{"op": "add_slider", "feature": None, "params": {}}], "summary": "added slider"}

        with patch("bot.ask_ollama_for_operation_plan", side_effect=add_slider_plan):
            answer, debug = edit_workspace_site_workflow("добавь слайдер", "integrationsite", chat_id="int1")

        self.assertTrue(debug["acceptance"]["success"])
        state = psm.load_project_state("integrationsite")
        self.assertIsNotNone(state["last_successful_snapshot"])
        self.assertEqual(len(state["applied_operations_history"]), 1)

        entries = learning_log.recent_entries("integrationsite")
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["success"])
        self.assertFalse(entries[0]["rollback_used"])

    def test_failed_edit_records_rollback_in_learning_log_and_does_not_advance_last_success(self):
        from bot import edit_workspace_site_workflow
        from tools_write import create_static_site
        import project_state_manager as psm
        import learning_log

        create_static_site("failintegration")

        def verify_only_plan(user_text, project_name, project_state):
            return {"operations": [{"op": "verify", "feature": None, "params": {}}], "summary": "checking"}

        with patch("bot.ask_ollama_for_operation_plan", side_effect=verify_only_plan):
            answer, debug = edit_workspace_site_workflow(
                "почини переключение языков ru/en/es", "failintegration", chat_id="int2"
            )

        self.assertFalse(debug["acceptance"]["success"])
        self.assertTrue(debug["rolled_back"])

        state = psm.load_project_state("failintegration")
        self.assertIsNone(state["last_successful_snapshot"])
        self.assertIsNotNone(state["last_failed_action"])

        entries = learning_log.recent_entries("failintegration")
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["success"])
        self.assertTrue(entries[0]["rollback_used"])
        self.assertNotIn("tools_called", answer)


if __name__ == "__main__":
    unittest.main()
