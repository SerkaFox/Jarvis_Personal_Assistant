import unittest
from pathlib import Path

from intent_router import detect_intent
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

    def test_secret_and_hidden_paths_are_blocked(self):
        self.assertTrue(is_forbidden_file(Path("/home/seradmin/.env")))
        self.assertTrue(is_forbidden_file(Path("/home/seradmin/.git-credentials")))
        self.assertTrue(is_excluded_dir(Path("/home/seradmin/.ssh")))
        self.assertTrue(is_excluded_dir(Path("/home/seradmin/.codex")))


if __name__ == "__main__":
    unittest.main()
