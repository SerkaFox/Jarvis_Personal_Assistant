import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("ALLOWED_USER_ID", "123")
os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")

import bot
import config
import memory
from tools_errors import latest_error, save_last_error


class FakeUser:
    id = 123


class FakeChat:
    id = 456


class FakeMessage:
    def __init__(self, text: str = ""):
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, text: str = ""):
        self.effective_user = FakeUser()
        self.effective_chat = FakeChat()
        self.message = FakeMessage(text)
        self.effective_message = self.message


class FakeContext:
    def __init__(self):
        self.user_data = {}
        self.error = None


class ErrorSmokeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jarvis_error_test_")
        self.old_db = os.environ.get("JARVIS_DB_PATH")
        self.old_config_db = config.JARVIS_DB_PATH
        temp_db = str(Path(self.tmp.name) / "data" / "jarvis.db")
        os.environ["JARVIS_DB_PATH"] = temp_db
        config.JARVIS_DB_PATH = temp_db
        memory.init_db()

    def tearDown(self):
        if self.old_db is None:
            os.environ.pop("JARVIS_DB_PATH", None)
        else:
            os.environ["JARVIS_DB_PATH"] = self.old_db
        config.JARVIS_DB_PATH = self.old_config_db
        self.tmp.cleanup()

    async def test_maybe_send_intent_debug_empty_does_not_crash(self):
        ctx = FakeContext()
        await bot.maybe_send_intent_debug(None, ctx, None)
        self.assertEqual(ctx.user_data["last_intent"], {})

    def test_save_last_error_and_latest_error(self):
        saved = save_last_error(
            chat_id="456",
            user_id="123",
            handler="test",
            error=RuntimeError("bad bot123:ABCdef_123 token=secret"),
            user_text="hello token=secret",
        )
        self.assertNotIn("ABCdef_123", saved["error_message"])
        self.assertEqual(latest_error("456")["handler"], "test")

    async def test_error_handler_masks_telegram_token(self):
        update = FakeUpdate("/boom")
        ctx = FakeContext()
        ctx.error = RuntimeError("telegram url https://api.telegram.org/bot123:ABCdef_123/getMe")
        await bot.error_handler(update, ctx)
        latest = latest_error()
        self.assertNotIn("ABCdef_123", latest["traceback"])

    async def test_handle_text_error_is_caught_and_reported(self):
        import bot
        import tools_claude_agent

        async def raise_error(*args, **kwargs):
            raise RuntimeError("simulated agent error")

        with patch.object(tools_claude_agent, "run_claude_agent", side_effect=raise_error):
            update = FakeUpdate("обычное сообщение")
            await bot.handle_text(update, FakeContext())
        # _report_handler_error sends an error message to the user
        self.assertTrue(len(update.message.replies) > 0)

    async def test_handle_text_calls_agent_and_returns_its_answer(self):
        import bot
        import tools_claude_agent

        async def fake_agent(text, chat_id, **kwargs):
            return "ответ агента"

        with patch.object(tools_claude_agent, "run_claude_agent", side_effect=fake_agent):
            update = FakeUpdate("создай сайт Demo")
            await bot.handle_text(update, FakeContext())
        self.assertIn("ответ агента", update.message.replies)


if __name__ == "__main__":
    unittest.main()
