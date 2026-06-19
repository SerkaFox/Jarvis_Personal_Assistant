import os
import unittest

import requests

import semantic_router as sr


def _fake_model(response_text):
    def ask_model(messages):
        return response_text

    return ask_model


class SemanticRouterUnitTests(unittest.TestCase):
    def test_classify_intent_normalizes_valid_json(self):
        ask_model = _fake_model(
            '{"intent": "where_project", "confidence": 0.92, "project_name": "sitebota", '
            '"target": null, "needs_tool": true, "start_preview": false, "language": "en", '
            '"reason": "asks where to open an existing project"}'
        )
        result = sr.classify_intent("where can I open sitebota?", ask_model=ask_model)
        self.assertEqual(result["intent"], "where_project")
        self.assertAlmostEqual(result["confidence"], 0.92)
        self.assertEqual(result["project_name"], "sitebota")
        self.assertTrue(result["needs_tool"])
        self.assertFalse(result["start_preview"])
        self.assertEqual(result["language"], "en")

    def test_classify_intent_strips_markdown_fence(self):
        ask_model = _fake_model(
            '```json\n{"intent": "git_repos", "confidence": 0.8, "project_name": null, '
            '"target": null, "needs_tool": true, "start_preview": false, "language": "ru", '
            '"reason": "asks about git repos"}\n```'
        )
        result = sr.classify_intent("какие git репозитории?", ask_model=ask_model)
        self.assertEqual(result["intent"], "git_repos")

    def test_classify_intent_rejects_unknown_intent_value(self):
        ask_model = _fake_model('{"intent": "delete_everything", "confidence": 0.9}')
        result = sr.classify_intent("do something", ask_model=ask_model)
        self.assertEqual(result["intent"], "unknown")
        self.assertTrue(sr.is_router_failure(result))

    def test_classify_intent_handles_garbage_output(self):
        ask_model = _fake_model("I cannot comply with this request.")
        result = sr.classify_intent("create a site", ask_model=ask_model)
        self.assertEqual(result["intent"], "unknown")
        self.assertTrue(sr.is_router_failure(result))

    def test_classify_intent_empty_text_is_failure_without_calling_model(self):
        calls = []

        def ask_model(messages):
            calls.append(messages)
            return "{}"

        result = sr.classify_intent("   ", ask_model=ask_model)
        self.assertEqual(result["intent"], "unknown")
        self.assertTrue(sr.is_router_failure(result))
        self.assertEqual(calls, [])

    def test_classify_intent_confidence_is_clamped(self):
        ask_model = _fake_model('{"intent": "normal_chat", "confidence": 5}')
        result = sr.classify_intent("hi", ask_model=ask_model)
        self.assertEqual(result["confidence"], 1.0)

    def test_is_action_like_detects_ru_en_es_verbs(self):
        self.assertTrue(sr.is_action_like("создай сайт и запусти сервер"))
        self.assertTrue(sr.is_action_like("please stop the preview server"))
        self.assertTrue(sr.is_action_like("borra el proyecto sitebota"))
        self.assertFalse(sr.is_action_like("what's the weather like today"))

    def test_genuine_model_unknown_is_not_router_failure(self):
        # A model that explicitly returns intent=unknown with a real reason is a
        # legitimate classification, distinct from a parsing/connection failure.
        ask_model = _fake_model(
            '{"intent": "unknown", "confidence": 0.3, "project_name": null, "target": null, '
            '"needs_tool": false, "start_preview": false, "language": "en", '
            '"reason": "message could mean several different actions"}'
        )
        result = sr.classify_intent("do the thing", ask_model=ask_model)
        self.assertEqual(result["intent"], "unknown")
        self.assertFalse(sr.is_router_failure(result))


def _ollama_reachable() -> bool:
    url = os.getenv("OLLAMA_URL", "http://192.168.0.145:11434")
    try:
        requests.get(f"{url}/api/version", timeout=3)
        return True
    except requests.exceptions.RequestException:
        return False


class SemanticRouterLiveSmokeTests(unittest.TestCase):
    """Hits the real Ollama router model. Skips gracefully if unreachable."""

    @classmethod
    def setUpClass(cls):
        if not _ollama_reachable():
            raise unittest.SkipTest("Ollama not reachable, skipping live router smoke tests")
        from bot import ask_ollama_messages

        cls.ask_model = staticmethod(ask_ollama_messages)

    def _classify(self, text):
        return sr.classify_intent(text, ask_model=self.ask_model)

    def test_ru_workspace_inventory(self):
        result = self._classify("какие проекты в твоей папке?")
        self.assertEqual(result["intent"], "workspace_inventory")

    def test_ru_create_and_preview(self):
        result = self._classify("создай сайт sitebota и запусти временный сервер")
        self.assertEqual(result["intent"], "create_and_preview")
        self.assertEqual((result.get("project_name") or "").lower(), "sitebota")

    def test_ru_where_project(self):
        result = self._classify("где открыть sitebota?")
        self.assertEqual(result["intent"], "where_project")

    def test_ru_git_repos(self):
        result = self._classify("какие git репозитории у меня есть?")
        self.assertEqual(result["intent"], "git_repos")

    def test_en_workspace_inventory(self):
        result = self._classify("what sites are in your workspace and what ports are they running on?")
        self.assertEqual(result["intent"], "workspace_inventory")

    def test_en_create_and_preview(self):
        result = self._classify("create a landing page called sitebota and start a preview server")
        self.assertEqual(result["intent"], "create_and_preview")

    def test_en_where_project(self):
        result = self._classify("where can I open sitebota?")
        self.assertEqual(result["intent"], "where_project")

    def test_en_git_repos(self):
        result = self._classify("show my git repositories")
        self.assertEqual(result["intent"], "git_repos")

    def test_es_workspace_inventory(self):
        result = self._classify("qué proyectos tienes en tu carpeta de trabajo?")
        self.assertEqual(result["intent"], "workspace_inventory")

    def test_es_create_and_preview(self):
        result = self._classify("crea una web llamada sitebota y levanta un servidor temporal")
        self.assertEqual(result["intent"], "create_and_preview")

    def test_es_where_project(self):
        result = self._classify("dónde puedo abrir sitebota?")
        self.assertEqual(result["intent"], "where_project")

    def test_es_git_repos(self):
        result = self._classify("muéstrame los repositorios git")
        self.assertEqual(result["intent"], "git_repos")


if __name__ == "__main__":
    unittest.main()
