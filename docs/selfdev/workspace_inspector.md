# Reference skill: `workspace_inspector`

This plugin is the canonical example of the plugin/selfdev pattern in this
project. Use it as the template when adding a new capability instead of
adding another phrase-matching branch to `bot.py`.

## The pattern

1. **Semantic routing decides applicability.** `can_handle(user_text, context)`
   returns a 0.0..1.0 score. `plugin_manager.select_plugin()` scores every
   installed plugin and dispatches to the best match above threshold (see
   `bot.py`'s `_try_installed_plugin`, called early in `handle_text` for
   every incoming message).
2. **A safe, deterministic tool does the work.** `handle()` never asks an
   LLM to improvise an answer that depends on real system state (file
   names, sizes, structure). It calls `list_workspace_project_files` /
   `list_workspace_project_images` / `tree_workspace_project`
   (`tools_write.py` / `tools_media.py`), which only ever report files that
   genuinely exist under `WRITE_ROOT/<project>`.
3. **A human answer, not raw tool output.** `handle()` formats the result
   itself; `bot.py` never shows `tools_called`/debug JSON in normal mode.

The exact trigger phrases in `IMAGE_INTENT_PHRASES` / `FILE_INTENT_PHRASES`
/ `TREE_INTENT_PHRASES` are **not** the architecture -- they're one cheap,
testable signal `can_handle()` uses today. A future revision could replace
them with an embedding or LLM classifier without changing `handle()`, the
plugin interface, or any of the calling code in `bot.py`. Treat phrase lists
as smoke-test fixtures, not as the design.

## Why this fixed a real bug

Before this plugin existed, "какие фото для фона у тебя есть?" was answered
by the normal chat path, which let Ollama guess plausible-sounding file
names (`bg_ru.jpg`, `bg_en.jpg`, ...) that did not exist on disk. Routing
this query class through a plugin that *always* calls a real filesystem tool
makes that class of hallucination structurally impossible: the plugin can
only report files it actually found, and says so honestly ("изображений не
найдено") when there are none.

## Adding a new skill

See `self_improvement.py` for the full pipeline: `propose_plugin()` (Ollama
generates a JSON spec, local model only -- `OLLAMA_URL`/`OLLAMA_MODEL`, no
external API) -> `write_plugin_to_sandbox()` (writes only under
`data/proposed_plugins/<job_id>/`) -> `run_selfdev_checks()` (path
allowlist, forbidden-import AST scan, `py_compile`, real `unittest` run,
`plugin_manager` interface validation) -> `install_plugin()` (copies into
`plugins/`/`tests/` only, commits, restarts, health-checks, auto-rolls-back
on failure). Trigger it from chat with `/selfdev_propose <task>`.
