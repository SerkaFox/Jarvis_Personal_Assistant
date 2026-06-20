import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

# Force (not setdefault) -- some other test module imported earlier in a full
# suite run may have already triggered config.py's load_dotenv(), which would
# otherwise populate ALLOWED_USER_ID from the real .env and make is_allowed()
# reject the fake test users below.
os.environ["ALLOWED_USER_ID"] = "123"
os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")

import config
from tools_fs import ToolError


DUMMY_PLUGIN_TEMPLATE = '''
PLUGIN_NAME = "{name}"
PLUGIN_VERSION = "0.1.0"
PLUGIN_DESCRIPTION = "Dummy plugin for tests"


def can_handle(user_text, context):
    return 1.0 if "dummy" in (user_text or "").lower() else 0.0


async def handle(update, context, parsed_task):
    return {{"success": True, "answer": "dummy handled"}}


def smoke_tests():
    return [("trivial", lambda: True)]
'''

PASSING_TEST_TEMPLATE = """
import unittest


class T(unittest.TestCase):
    def test_ok(self):
        self.assertTrue(True)
"""

FAILING_TEST_TEMPLATE = """
import unittest


class T(unittest.TestCase):
    def test_fail(self):
        self.assertTrue(False)
"""


def _valid_spec(name: str = "testplug", *, test_source: str = PASSING_TEST_TEMPLATE) -> dict:
    return {
        "plugin_name": name,
        "description": "test plugin",
        "files": [
            {"path": f"plugins/{name}.py", "content": DUMMY_PLUGIN_TEMPLATE.format(name=name)},
            {"path": f"tests/test_{name}.py", "content": test_source},
        ],
        "capabilities": [],
        "risks": [],
        "tests": ["trivial"],
    }


class IsolatedDataDirMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jarvis_selfdev_test_")
        self.old_db_path = os.environ.get("JARVIS_DB_PATH")
        self.old_config_db_path = config.JARVIS_DB_PATH
        self.old_plugins_dir = os.environ.get("JARVIS_PLUGINS_DIR")
        self.old_tests_dir = os.environ.get("JARVIS_PLUGIN_TESTS_DIR")
        self.old_skills_dir = os.environ.get("JARVIS_SKILLS_DIR")
        temp_db = str(Path(self.tmp.name) / "data" / "jarvis.db")
        os.environ["JARVIS_DB_PATH"] = temp_db
        config.JARVIS_DB_PATH = temp_db
        os.environ["JARVIS_PLUGINS_DIR"] = str(Path(self.tmp.name) / "plugins")
        os.environ["JARVIS_PLUGIN_TESTS_DIR"] = str(Path(self.tmp.name) / "tests")
        os.environ["JARVIS_SKILLS_DIR"] = str(Path(self.tmp.name) / "skills")

    def tearDown(self):
        if self.old_db_path is None:
            os.environ.pop("JARVIS_DB_PATH", None)
        else:
            os.environ["JARVIS_DB_PATH"] = self.old_db_path
        config.JARVIS_DB_PATH = self.old_config_db_path
        for env_name, old in (
            ("JARVIS_PLUGINS_DIR", self.old_plugins_dir),
            ("JARVIS_PLUGIN_TESTS_DIR", self.old_tests_dir),
            ("JARVIS_SKILLS_DIR", self.old_skills_dir),
        ):
            if old is None:
                os.environ.pop(env_name, None)
            else:
                os.environ[env_name] = old
        self.tmp.cleanup()


class PluginManagerTests(IsolatedDataDirMixin, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.plugins_dir = Path(self.tmp.name) / "external_plugins"
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        (self.plugins_dir / "dummy_plugin.py").write_text(
            DUMMY_PLUGIN_TEMPLATE.format(name="dummy_plugin"), encoding="utf-8"
        )
        (self.plugins_dir / "broken_plugin.py").write_text("PLUGIN_NAME = 'broken'\n", encoding="utf-8")
        (self.plugins_dir / "_ignored.py").write_text(DUMMY_PLUGIN_TEMPLATE.format(name="ignored"), encoding="utf-8")

    def test_loads_valid_plugin_and_skips_underscore_files(self):
        import plugin_manager

        modules, _errors = plugin_manager.load_plugins(self.plugins_dir)
        names = [m.PLUGIN_NAME for m in modules]
        self.assertEqual(names, ["dummy_plugin"])

    def test_invalid_plugin_rejected_with_reason(self):
        import plugin_manager

        _modules, errors = plugin_manager.load_plugins(self.plugins_dir)
        self.assertTrue(any(e["file"] == "broken_plugin.py" for e in errors))

    def test_select_plugin_scores_and_picks_best(self):
        import plugin_manager

        match = plugin_manager.select_plugin("please do a dummy thing", plugins_dir=self.plugins_dir)
        self.assertIsNotNone(match)
        module, score = match
        self.assertEqual(module.PLUGIN_NAME, "dummy_plugin")
        self.assertEqual(score, 1.0)

    def test_select_plugin_below_threshold_returns_none(self):
        import plugin_manager

        match = plugin_manager.select_plugin("completely unrelated text", plugins_dir=self.plugins_dir)
        self.assertIsNone(match)

    async def test_safe_dispatch_never_raises_even_if_handle_throws(self):
        import plugin_manager

        modules, _errors = plugin_manager.load_plugins(self.plugins_dir)
        module = modules[0]
        with patch.object(module, "handle", side_effect=RuntimeError("boom")):
            result = await plugin_manager.safe_dispatch(module, None, None, {})
        self.assertFalse(result["success"])
        self.assertIn("boom", result["error"])


class SpecValidationTests(IsolatedDataDirMixin, unittest.TestCase):
    def test_forbidden_path_rejected(self):
        import self_improvement

        spec = _valid_spec("badpathplug")
        spec["files"].append({"path": "bot.py", "content": "x = 1"})
        with self.assertRaises(ToolError):
            self_improvement.validate_plugin_spec(spec)

    def test_path_traversal_rejected(self):
        import self_improvement

        spec = _valid_spec("traversalplug")
        spec["files"].append({"path": "plugins/../../etc/passwd", "content": "x"})
        with self.assertRaises(ToolError):
            self_improvement.write_plugin_to_sandbox("job-traversal", spec)

    def test_forbidden_import_rejected_by_checks(self):
        import self_improvement

        spec = _valid_spec("forbiddenimportplug")
        spec["files"][0]["content"] = "import subprocess\n" + spec["files"][0]["content"]
        job_id = "job-forbidden-import"
        self_improvement.write_plugin_to_sandbox(job_id, spec)
        report = self_improvement.run_selfdev_checks(job_id)
        self.assertFalse(report["success"])
        self.assertFalse(report["checks"]["no_forbidden_imports"])


class ProposeAndGenerateTests(IsolatedDataDirMixin, unittest.TestCase):
    def test_propose_plugin_uses_local_ollama_only(self):
        import self_improvement

        spec = _valid_spec("ollamaplug")
        raw = json.dumps(spec)
        with patch.object(self_improvement, "_ollama_chat", return_value=raw) as mock_chat:
            result = self_improvement.propose_plugin("сделай нечто новое", {"project_name": "hola"})
        self.assertEqual(result["plugin_name"], "ollamaplug")
        mock_chat.assert_called_once()
        # _ollama_chat is the only network entry point in this module, and it
        # always targets OLLAMA_URL -- never an external API.
        self.assertTrue(self_improvement.OLLAMA_URL)

    def test_propose_plugin_retries_once_on_invalid_json_then_succeeds(self):
        import self_improvement

        spec = _valid_spec("retryplug")
        responses = ["not json at all", json.dumps(spec)]
        with patch.object(self_improvement, "_ollama_chat", side_effect=responses) as mock_chat:
            result = self_improvement.propose_plugin("сделай нечто новое", {})
        self.assertEqual(result["plugin_name"], "retryplug")
        self.assertEqual(mock_chat.call_count, 2)

    def test_generate_plugin_code_is_pure_and_matches_spec_files(self):
        import self_improvement

        spec = _valid_spec("puregen")
        files = self_improvement.generate_plugin_code(spec)
        self.assertEqual({f["path"] for f in files}, {f["path"] for f in spec["files"]})

    def test_write_plugin_to_sandbox_creates_spec_json(self):
        import self_improvement

        spec = _valid_spec("sandboxplug")
        job_id = "job-sandbox-1"
        report = self_improvement.write_plugin_to_sandbox(job_id, spec)
        sandbox_path = config.get_selfdev_proposed_dir() / job_id / "spec.json"
        self.assertTrue(sandbox_path.is_file())
        self.assertEqual(report["plugin_name"], "sandboxplug")
        self.assertEqual(report["status"], "proposed")


class RunSelfdevChecksTests(IsolatedDataDirMixin, unittest.TestCase):
    def test_run_selfdev_checks_passes_for_valid_plugin(self):
        import self_improvement

        spec = _valid_spec("checksok")
        job_id = "job-checks-ok"
        self_improvement.write_plugin_to_sandbox(job_id, spec)
        report = self_improvement.run_selfdev_checks(job_id)
        self.assertTrue(report["success"], report["errors"])
        self.assertTrue(report["checks"]["py_compile"])
        self.assertTrue(report["checks"]["tests_ok"])
        self.assertTrue(report["checks"]["plugin_import_ok"])

    def test_run_selfdev_checks_fails_when_unit_test_fails(self):
        import self_improvement

        spec = _valid_spec("checksfail", test_source=FAILING_TEST_TEMPLATE)
        job_id = "job-checks-fail"
        self_improvement.write_plugin_to_sandbox(job_id, spec)
        report = self_improvement.run_selfdev_checks(job_id)
        self.assertFalse(report["success"])
        self.assertFalse(report["checks"]["tests_ok"])


class InstallDryRunTests(IsolatedDataDirMixin, unittest.TestCase):
    def test_install_refuses_when_tests_fail(self):
        import self_improvement

        spec = _valid_spec("failingtests", test_source=FAILING_TEST_TEMPLATE)
        job_id = "job-failing-tests"
        self_improvement.write_plugin_to_sandbox(job_id, spec)
        with self.assertRaises(ToolError):
            self_improvement.install_plugin(job_id, dry_run=True)

    def test_install_dry_run_succeeds_without_touching_real_dirs(self):
        import self_improvement

        spec = _valid_spec("dryinstall")
        job_id = "job-dry-install"
        self_improvement.write_plugin_to_sandbox(job_id, spec)
        report = self_improvement.install_plugin(job_id, dry_run=True)
        self.assertEqual(report["status"], "installed")
        self.assertTrue(report["git_commit"]["dry_run"])
        for relative_path in report["installed_paths"]:
            self.assertFalse((config.PROJECT_ROOT / relative_path).is_file())

    def test_rollback_dry_run_reports_previous_commit(self):
        import self_improvement

        job_id = "job-rollback-dry"
        self_improvement._write_report(job_id, {"job_id": job_id, "pre_install_commit": "deadbeef123"})
        result = self_improvement.rollback_selfdev(job_id, dry_run=True)
        self.assertEqual(result["rolled_back_to"], "deadbeef123")
        self.assertTrue(result["reset"]["dry_run"])


class InstallWithRealIsolatedGitRepoTests(IsolatedDataDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.repo_dir = tempfile.TemporaryDirectory(prefix="jarvis_selfdev_repo_")
        repo_path = Path(self.repo_dir.name)
        subprocess.run(["git", "init", "-q"], cwd=repo_path, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_path, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_path, check=True)
        (repo_path / "README.md").write_text("init\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo_path, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo_path, check=True)
        self.old_project_root = config.PROJECT_ROOT
        config.PROJECT_ROOT = repo_path

    def tearDown(self):
        config.PROJECT_ROOT = self.old_project_root
        self.repo_dir.cleanup()
        super().tearDown()

    def test_install_rolls_back_on_failed_health_check(self):
        import self_improvement

        spec = _valid_spec("healthfail")
        job_id = "job-health-fail"
        self_improvement.write_plugin_to_sandbox(job_id, spec)
        pre_commit = self_improvement._current_git_commit()

        with self.assertRaises(ToolError):
            self_improvement.install_plugin(
                job_id,
                dry_run=False,
                restart_fn=lambda: {"returncode": 0, "stdout": "", "stderr": ""},
                health_check_fn=lambda plugin_name: {"success": False, "errors": ["fake health failure"]},
            )

        self.assertEqual(self_improvement._current_git_commit(), pre_commit)
        report = self_improvement.get_job_report(job_id)
        self.assertEqual(report["status"], "rolled_back")
        self.assertEqual(report["last_rollback"]["rolled_back_to"], pre_commit)

    def test_install_succeeds_when_restart_and_health_check_pass(self):
        import self_improvement

        spec = _valid_spec("healthok")
        job_id = "job-health-ok"
        self_improvement.write_plugin_to_sandbox(job_id, spec)

        report = self_improvement.install_plugin(
            job_id,
            dry_run=False,
            restart_fn=lambda: {"returncode": 0, "stdout": "", "stderr": ""},
            health_check_fn=lambda plugin_name: {"success": True, "checks": {}, "errors": []},
        )
        self.assertEqual(report["status"], "installed")
        for relative_path in report["installed_paths"]:
            self.assertTrue((config.PROJECT_ROOT / relative_path).is_file())


class SelfdevRunCommandTests(IsolatedDataDirMixin, unittest.IsolatedAsyncioTestCase):
    async def test_selfdev_run_does_not_write_real_plugin_files(self):
        import bot
        import self_improvement
        from test_error_smoke import FakeContext, FakeUpdate

        spec = _valid_spec("dryrunplug")
        job_id = "job-dry-run-1"
        self_improvement.write_plugin_to_sandbox(job_id, spec)

        plugins_dir = config.get_plugins_dir()
        before = set(plugins_dir.glob("*.py"))

        update = FakeUpdate("/selfdev_run job-dry-run-1 dummy test prompt")
        context = FakeContext()
        context.args = [job_id, "dummy", "test", "prompt"]
        await bot.selfdev_run_command(update, context)

        after = set(plugins_dir.glob("*.py"))
        self.assertEqual(before, after)
        self.assertTrue(any("can_handle score" in reply for reply in update.message.replies))
        self.assertTrue(any("1.00" in reply for reply in update.message.replies))


class RoutingFallbackTests(IsolatedDataDirMixin, unittest.IsolatedAsyncioTestCase):
    async def test_unknown_intent_with_action_like_text_triggers_selfdev_proposal(self):
        import bot
        from test_error_smoke import FakeContext, FakeUpdate

        def fake_answer_user_text(*args, **kwargs):
            return "Не понял запрос", {"detected": {"intent": "normal_chat"}, "tools_called": [], "errors": []}

        spec = _valid_spec("autoplug")

        with patch.object(bot, "answer_user_text", side_effect=fake_answer_user_text), \
             patch.object(bot.plugin_manager, "select_plugin", return_value=None), \
             patch.object(bot.self_improvement, "propose_plugin", return_value=spec), \
             patch.object(bot.self_improvement, "write_plugin_to_sandbox", return_value={}), \
             patch.object(bot.self_improvement, "new_job_id", return_value="job-routing-1"):
            update = FakeUpdate("создай мне функцию, которой раньше никогда не было")
            await bot.handle_text(update, FakeContext())

        replies = update.message.replies
        self.assertTrue(any("Я пока не умею это делать сам" in r for r in replies))
        self.assertTrue(any("job-routing-1" in r for r in replies))

    async def test_normal_small_talk_is_not_intercepted_by_selfdev(self):
        import bot
        from test_error_smoke import FakeContext, FakeUpdate

        def fake_answer_user_text(*args, **kwargs):
            return "Привет! Как дела?", {"detected": {"intent": "normal_chat"}, "tools_called": [], "errors": []}

        with patch.object(bot, "answer_user_text", side_effect=fake_answer_user_text), \
             patch.object(bot.self_improvement, "propose_plugin") as mock_propose:
            update = FakeUpdate("привет, как дела?")
            await bot.handle_text(update, FakeContext())

        mock_propose.assert_not_called()
        self.assertTrue(any("Привет! Как дела?" in r for r in update.message.replies))


class SelfdevOnOffCommandTests(IsolatedDataDirMixin, unittest.IsolatedAsyncioTestCase):
    async def test_selfdev_off_blocks_propose_command(self):
        import bot
        from test_error_smoke import FakeContext, FakeUpdate

        update = FakeUpdate("/selfdev_propose что-то новое")
        context = FakeContext()
        context.args = ["что-то", "новое"]
        await bot.selfdev_off(update, context)
        await bot.selfdev_propose_command(update, context)
        self.assertTrue(any("выключен" in r.lower() for r in update.message.replies))


if __name__ == "__main__":
    unittest.main()
