import unittest
from pathlib import Path
from unittest.mock import patch

from intent_router import detect_intent, handle_detected_intent
from tools_check import safe_code_check
from tools_fs import is_excluded_dir, is_forbidden_file
from tools_git import find_git_repos, git_status
from tools_project import project_structure


class JarvisSmokeTests(unittest.TestCase):
    def test_project_structure_anna_resolves_project_path(self):
        result = project_structure("anna")
        self.assertEqual(result["path"], "/home/seradmin/anna")
        self.assertNotIn("/home/seradmin/.bash_history", result["tree_summary"])

    def test_recent_anna_context_routes_code_check(self):
        recent = [{"role": "user", "content": "салон анны есть?"}]
        detected = detect_intent("проверь код проекта", recent)
        self.assertEqual(detected["intent"], "safe_code_check")
        self.assertEqual(detected["project"], "anna")

    def test_workspace_where_intent_does_not_list_repos(self):
        detected = detect_intent("где сайт sitebota?")
        self.assertEqual(detected["intent"], "where_project")
        self.assertNotEqual(detected["intent"], "list_projects")
        self.assertEqual(detected["project"], "sitebota")

    def test_workspace_preview_status_intent_does_not_list_repos(self):
        detected = detect_intent("ты запустил сервер sitebota?")
        self.assertEqual(detected["intent"], "preview_status")
        self.assertNotEqual(detected["intent"], "list_projects")
        self.assertEqual(detected["project"], "sitebota")

    def test_workspace_create_and_preview_intent(self):
        detected = detect_intent("создай сайт sitebota и запусти сервер")
        self.assertEqual(detected["intent"], "create_and_preview")
        self.assertEqual(detected["project"], "sitebota")

    def test_safe_code_check_anna_is_structured_and_keeps_git_status(self):
        before = git_status("/home/seradmin/anna")["status_short"]
        result = safe_code_check("anna")
        after = git_status("/home/seradmin/anna")["status_short"]
        self.assertEqual(result["path"], "/home/seradmin/anna")
        self.assertIn("checks", result)
        self.assertEqual(before, after)
        self.assertTrue(result["git_status_unchanged"])

    def test_repos_returns_only_git_repositories(self):
        result = find_git_repos()
        for repo in result["repositories"]:
            path = Path(repo["path"])
            self.assertTrue((path / ".git").exists(), repo["path"])
            self.assertNotIn(".git-credentials", repo["path"])

    def test_workspace_phrases_route_to_workspace_inventory_not_repos(self):
        cases = [
            "проверь, есть ли папки с сайтами, какие у тебя в твоем рабочем каталоге и на каких портах они висят",
            "какие проекты в твоей папке",
            "какие сайты в рабочей папке",
            "есть ли папки с сайтами",
            "на каких портах они висят",
        ]
        for text in cases:
            detected = detect_intent(text)
            self.assertEqual(detected["intent"], "workspace_inventory", text)

    def test_explicit_git_phrase_still_routes_to_repo_listing(self):
        detected = detect_intent("какие git репозитории")
        self.assertEqual(detected["intent"], "list_projects")

    def test_git_status_with_project_name_routes_to_git_status_intent(self):
        detected = detect_intent("git status anna")
        self.assertEqual(detected["intent"], "git_status")
        self.assertEqual(detected["project"], "anna")

    def test_existing_workspace_specific_intents_unaffected_by_inventory_routing(self):
        self.assertEqual(detect_intent("где сайт sitebota?")["intent"], "where_project")
        self.assertEqual(detect_intent("ты запустил сервер sitebota?")["intent"], "preview_status")
        self.assertEqual(detect_intent("создай сайт sitebota и запусти сервер")["intent"], "create_and_preview")

    def test_workspace_inventory_handler_does_not_call_find_git_repos(self):
        with patch("intent_router.find_git_repos") as mocked:
            result = handle_detected_intent({"intent": "workspace_inventory"})
        mocked.assert_not_called()
        self.assertIn("Рабочая папка Jarvis:", result["answer"])
        self.assertEqual(result["tools_called"], ["workspace_inventory", "list_previews", "scan_listening_ports"])

    def test_secret_and_hidden_paths_are_blocked(self):
        self.assertTrue(is_forbidden_file(Path("/home/seradmin/.env")))
        self.assertTrue(is_forbidden_file(Path("/home/seradmin/.git-credentials")))
        self.assertTrue(is_excluded_dir(Path("/home/seradmin/.ssh")))
        self.assertTrue(is_excluded_dir(Path("/home/seradmin/.codex")))


if __name__ == "__main__":
    unittest.main()
