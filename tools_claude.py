"""Anthropic Claude API backend — drop-in replacement for the Ollama calls
in bot.py. The public functions (ask_claude, ask_claude_messages) have the
same signatures and return types as their ask_ollama* counterparts so the
rest of the codebase needs no structural changes.

Ollama and Anthropic differ in one layout detail: Anthropic separates the
system message from the conversation turns. ask_claude_messages handles that
automatically: any leading {"role": "system"} entries are joined and passed as
the `system` kwarg; the remaining turns go into `messages`.
"""
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
_MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "16384"))

_client = None


def _get_client():
    global _client
    if _client is None:
        import anthropic  # lazy — only imported when actually needed
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY не задан. Добавь его в .env: ANTHROPIC_API_KEY=sk-ant-..."
            )
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def ask_claude_messages(messages: list[dict[str, str]]) -> str:
    """Send a list of messages (Ollama-style: may include a leading system
    turn) to Claude and return the assistant's reply as a plain string."""
    system_parts: list[str] = []
    turns: list[dict[str, str]] = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "system":
            system_parts.append(content)
        else:
            turns.append({"role": role, "content": content})

    system_text = "\n\n".join(system_parts) if system_parts else None

    max_tokens = int(os.getenv("CLAUDE_MAX_TOKENS", "16384"))
    kwargs: dict[str, Any] = {
        "model": os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"),
        "max_tokens": max_tokens,
        "messages": turns,
    }
    if system_text:
        kwargs["system"] = system_text

    client = _get_client()
    response = client.messages.create(**kwargs)
    return response.content[0].text


