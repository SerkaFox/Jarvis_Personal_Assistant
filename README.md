# Jarvis Personal Assistant

Telegram bot for local Jarvis responses through Ollama, STT, and optional local TTS.

## Local TTS with Piper

The bot can synthesize voice replies for incoming Telegram `voice` and `audio` messages. Text messages still receive text-only replies.

Required tools:

```bash
sudo apt update
sudo apt install -y ffmpeg
```

Install Piper and place the binary at `/usr/local/bin/piper`:

```bash
mkdir -p /tmp/piper
cd /tmp/piper
wget -O piper.tar.gz https://github.com/rhasspy/piper/releases/latest/download/piper_linux_x86_64.tar.gz
tar -xzf piper.tar.gz
sudo cp piper/piper /usr/local/bin/piper
sudo chmod +x /usr/local/bin/piper
```

Download a Russian Piper voice model and config into:

```text
/home/seradmin/jarvis_bot/models/tts/ru_RU/model.onnx
/home/seradmin/jarvis_bot/models/tts/ru_RU/model.onnx.json
```

Example environment variables:

```env
TTS_ENABLED=true
TTS_ENGINE=piper
PIPER_BIN=/usr/local/bin/piper
PIPER_MODEL=/home/seradmin/jarvis_bot/models/tts/ru_RU/model.onnx
PIPER_CONFIG=/home/seradmin/jarvis_bot/models/tts/ru_RU/model.onnx.json
TTS_TMP_DIR=/tmp/jarvis_tts
```

Restart the bot after changing `.env`.

## Test TTS

Send this command to the bot:

```text
/tts_test
```

The bot should reply with a Telegram voice message:

```text
Jarvis online. Голосовой ответ работает.
```

If TTS is unavailable, the bot returns a text error for the missing component: disabled TTS, missing Piper, missing model/config, or missing ffmpeg.

## Semantic Intent Router

Jarvis classifies free-text messages by meaning, not by matching fixed Russian phrases. `semantic_router.py` sends a small, fast classification prompt to Ollama and requires a single strict JSON object back:

```json
{
  "intent": "where_project",
  "confidence": 0.92,
  "project_name": "sitebota",
  "target": null,
  "needs_tool": true,
  "start_preview": false,
  "language": "en",
  "reason": "asks where to open an existing project"
}
```

How it works:

- **Ollama only classifies, it never acts.** The model's only job is to pick one of a fixed list of intents (`workspace_inventory`, `create_and_preview`, `where_project`, `preview_stop`, `git_repos`, `memory_save`, `normal_chat`, ...) and extract a project name if one is mentioned. It cannot return shell commands or file content, and its classification JSON is never shown to the user as-is.
- **The backend executes the real tool.** `bot.semantic_router_answer()` reads the classified intent and calls the corresponding safe tool function (`workspace_inventory()`, `create_site_workflow()`, `stop_preview()`, `delete_workspace_dir()`, `find_git_repos()`, etc.) the same way the old phrase-matching code did. The user-visible answer always comes from a real tool result or formatter, never from the router's own text.
- **Works across languages.** Because classification is by meaning, "какие проекты в твоей папке?", "what sites are in your workspace?", and "qué proyectos tienes en tu carpeta de trabajo?" all resolve to the same `workspace_inventory` intent and the same backend call.
- **Regex/phrase matching (`intent_router.detect_intent`, `bot.workspace_status_answer`, `bot.stop_delete_answer`, `bot.write_mode_answer`) is now a fallback, not the primary path.** It only runs when the router itself fails — invalid/unparseable JSON, an unlisted intent, or no model response — and the message isn't clearly action-like; in that case the old deterministic Russian-phrase matching still applies so the bot doesn't go fully silent.
- **No fake actions.** If the router fails AND the message looks like an action request (contains verbs like create/start/stop/delete in ru/en/es), or if the router itself returns a confident-but-low-confidence (<0.55) action intent, Jarvis replies with a clarification prompt instead of guessing:
  `"Не смог надёжно распознать действие. Уточни командой /create_and_preview <name> или /workspace_status."`
- **`/router_test <text>`** runs only the classifier (no tool execution) and shows `intent`, `confidence`, `project_name`, `needs_tool`, `start_preview`, `language`, `reason` — useful for tuning the router prompt.
- **`/debug_on`** (alias `/agent_debug_on`) shows the router's classification alongside the regular intent debug line for every message.

## Read-Only Server/Code Agent

Jarvis can optionally inspect local projects, git status, and selected service logs through a read-only tool layer. The tool layer does not expose arbitrary shell commands and does not write files.

Default configuration:

```env
ALLOWED_ROOTS=/home/seradmin,/home/seradmin/jelec,/var/www
ALLOWED_SERVICES=jarvis-bot,j-listoya-stt
MAX_FILE_CHARS=12000
MAX_SEARCH_RESULTS=50
AGENT_TOOLS_ENABLED=true
JARVIS_DB_PATH=/home/seradmin/jarvis_bot/data/jarvis.db
MEMORY_ENABLED=true
HISTORY_LIMIT=12
```

`ALLOWED_ROOTS` is a comma-separated allowlist. File and directory tools only work inside these roots after resolving symlinks. Paths with `..` are rejected. Secret-like files such as `.env`, keys, PEM files, sqlite/db files, and `media`/`uploads` content are not readable.

Allowed read-only tools:

```text
list_dir(path)
read_file(path, max_chars=12000)
search_text(root, query, glob=None)
find_git_repos(root)
git_status(repo_path)
tree_summary(path, depth=2)
service_status(name)
read_journal(service_name, lines=100)
```

Shell access is limited to:

```text
systemctl status <allowed-service>.service --no-pager
journalctl -u <allowed-service>.service -n <lines> --no-pager
```

No `sudo`, `restart`, `deploy`, `rm`, `mv`, `cp`, `git pull`, `git push`, or arbitrary `shell=True` commands are available to the agent.

## Safe Write Workspace

Jarvis can create brand-new test projects only inside a dedicated workspace. This is not deploy mode and does not allow writes to real projects.

Default configuration:

```env
WRITE_MODE_ENABLED=false
WRITE_ROOT=/home/seradmin/jarvis_workspace
PREVIEW_PORT_MIN=8700
PREVIEW_PORT_MAX=8799
SERVER_HOST=http://192.168.0.XXX
```

`WRITE_MODE_ENABLED` is disabled by default. Set it to `true` only when you want Jarvis to create files in the sandbox. `WRITE_ROOT` is the only directory where write tools can create directories, write text files, and run `git init`. Paths with `..` are rejected, and secret-like files such as `.env`, keys, PEM files, sqlite/db files, and token/password filenames are blocked.

Allowed write workspace operations:

```text
create_project_dir(name)
write_text_file(path, content, overwrite=false)
append_text_file(path, content)
read_workspace_file(path)
delete_workspace_file(path)
delete_workspace_dir(path, confirm_token)
list_workspace()
init_git(path)
create_static_site(project_name, title, description, theme)
create_flask_site(project_name, title, description, theme)
run_safe_project_check(path)
```

The write tools do not deploy, do not use sudo, do not install dependencies, and do not write outside `WRITE_ROOT`. Jarvis must not say that it created, wrote, deleted, or started something unless the corresponding tool completed successfully.

Preview tools run only direct local preview processes from projects inside `WRITE_ROOT`. They do not open nginx/systemd and do not kill processes unless Jarvis started and recorded them in `data/previews.json`.

### Commands

```text
/roots
```

Shows configured roots and output limits.

```text
/repos
```

Finds git repositories under `ALLOWED_ROOTS` and shows path, branch, origin, and `git status --short`.

```text
/projects
```

Alias for `/repos`. Normal text such as `какие есть у меня проекты сейчас?` is routed to server tools before Ollama.

```text
/git <repo_name_or_path>
```

Shows branch, remotes, and `git status --short` for one repository.

```text
/diff <repo_name_or_path>
```

Shows read-only `git diff` for one repository with an output limit.

```text
/find <query>
```

Searches text across all `ALLOWED_ROOTS` with ripgrep.

```text
/tree <path>
```

Shows a short directory tree, excluding virtualenvs, caches, git internals, media, uploads, and staticfiles.

```text
/workspace
```

Shows `WRITE_ROOT` and projects inside the safe workspace.

```text
/write_mode
```

Shows whether safe write mode is enabled.

```text
/new_static <name>
```

Creates a static test site inside `WRITE_ROOT` with `index.html`, `assets/css/style.css`, `assets/js/main.js`, and `README.md`.

```text
/new_flask <name>
```

Creates a minimal Flask project inside `WRITE_ROOT` with `app.py`, `requirements.txt`, templates, static assets, and `README.md`. The generated app uses `FLASK_DEBUG=1` only when explicitly set in the environment.

```text
/write_file <project>/<file> <content>
```

Writes a text file inside `WRITE_ROOT`. Existing files are overwritten only through this explicit command path.

```text
/delete_file <project>/<file>
```

Deletes one file inside `WRITE_ROOT`. Directory deletion is not exposed as a Telegram command.

```text
/preview_start <project>
/preview_stop <project>
/preview_stop_port <port>
/preview_stop_all
/preview_list
/preview_status <project>
```

Starts, stops, lists, and checks direct preview processes for workspace projects. Static projects run with `python3 -m http.server` on a port from `PREVIEW_PORT_MIN..PREVIEW_PORT_MAX`. Flask projects are started only when a `venv` already exists; dependencies are not installed automatically. Stop commands only report success after verifying the process/port/curl are actually down.

```text
/workspace_status
/ports
/workspace_delete <project>
/workspace_clean_stopped
```

`/workspace_status` (also the body of `/workspace`) shows a full WRITE_ROOT inventory: every project's files, required-file checklist, preview registration, live port-listening state, and curl status — not just what's cached in memory. `/ports` lists registered previews, real listening ports in `PREVIEW_PORT_MIN..PREVIEW_PORT_MAX`, and any unregistered `http.server` process as suspicious. `/workspace_delete` removes a project from `WRITE_ROOT` only after verifying the folder is actually gone. `/workspace_clean_stopped` prunes dead entries from the preview registry.

```text
/preview_info <name>
```

Shows local preview commands for a workspace project.

```text
/workspace_tree <name>
```

Shows the tree for a project inside `WRITE_ROOT`.

```text
/logs <service>
```

Shows the last 80 journal lines for a service from `ALLOWED_SERVICES`.

```text
/memory
/remember <text>
/forget <key>
/history
/clear_history
```

Persistent SQLite memory. Jarvis stores user and assistant messages in `messages`, long-lived facts in `memories`, and project summaries in `project_notes`. The database path is configured with `JARVIS_DB_PATH` and defaults to `/home/seradmin/jarvis_bot/data/jarvis.db`. The DB is local and ignored by git.

Jarvis automatically stores simple memory candidates when messages contain phrases such as `запомни`, `remember`, `мой день рождения`, `я родился`, `меня зовут`, `мне нравится`, or `предпочитаю`. Birth dates are normalized to ISO format and stored as `birth_date`.

```text
/project <repo>
```

Runs read-only project inspection: git status, branch, remote, diff stat, recent git log, README/TODO/CHANGELOG/docs, TODO/FIXME/HACK/BUG search, Django layout signals, and a short directory tree. Jarvis then asks Ollama for a human summary and saves it to `project_notes`.

```text
/status
```

Shows Ollama, STT, TTS, selected models, allowed services, and allowed roots.

```text
/agent_on
/agent_off
```

Enables or disables read-only tool use for normal text messages. If the model returns an invalid JSON plan, Jarvis falls back to the normal Ollama answer without tools.

```text
/agent_debug_on
/agent_debug_off
/debug_last_intent
/router_test <text>
```

`/agent_debug_on` (alias `/debug_on`) shows, per message: the semantic router's classification (intent, confidence, project, language, reason) plus the detected intent, selected project, tools called, and errors from whichever backend workflow handled it. `/router_test <text>` runs only the semantic router classifier on arbitrary text without executing any tool, for tuning and debugging the router prompt.

```text
/patch <repo> <task>
/apply_patch <id>
/test <repo>
/deploy <repo>
```

These commands are intentionally disabled at the current read-only stage. They do not change files, run deploys, or commit anything.

### Example Requests

```text
какие проекты есть на сервере?
```

```text
найди где используется booking_calendar_day
```

```text
покажи git status всех репозиториев
```

```text
посмотри проект anna, на чем остановились
```

```text
создай тестовый сайт demo-site
```

Creates a static site in `WRITE_ROOT` when `WRITE_MODE_ENABLED=true`; otherwise Jarvis explains that write mode is disabled.

```text
создай сайт в папке Botosite
```

Creates real files under `/home/seradmin/jarvis_workspace/Botosite` when write mode is enabled, then replies with `tools_called`, actual path, created files, and preview instructions.

```text
запомни, мой день рождения 13 октября 1982
```

```text
сколько мне лет?
```

## Plugins & Self-Improvement

Jarvis can be extended with plugins instead of growing more phrase-matching
branches in `bot.py`. A plugin is a single file under `plugins/<name>.py`
exposing `PLUGIN_NAME`, `PLUGIN_VERSION`, `PLUGIN_DESCRIPTION`,
`can_handle(user_text, context) -> float`, `async handle(update, context,
parsed_task) -> dict`, and `smoke_tests()`. `plugin_manager.py` loads every
`plugins/*.py` file, validates the interface, and scores `can_handle()`
against incoming text -- the best match above threshold gets dispatched
before the normal semantic router runs (see `_try_installed_plugin` in
`bot.py`).

See [`docs/selfdev/workspace_inspector.md`](docs/selfdev/workspace_inspector.md)
for the reference example: `plugins/workspace_inspector.py` answers "what
files/images do you have" questions using only deterministic, read-only
WRITE_ROOT tools, so it can never hallucinate a file name.

If no installed plugin or existing workflow can handle a request, Jarvis can
propose a brand-new one via `self_improvement.py`'s controlled pipeline:

```text
/selfdev_on, /selfdev_off       -- enable/disable proposing new skills (default: suggest)
/selfdev_propose <task>         -- ask Ollama (local only) for a new plugin spec, sandboxed
/selfdev_run <job_id> <prompt>  -- dry-run: can_handle score + parsed task, no changes
/selfdev_test <job_id>          -- py_compile + forbidden-import scan + real unittest run
/selfdev_install <job_id>       -- copy into plugins/+tests/, commit, restart, health-check
/selfdev_rollback <job_id>      -- git reset --hard to the pre-install commit
/plugins, /plugin_show <name>   -- list/inspect installed plugins
```

Self-improvement only ever writes inside `plugins/`, `tests/`, `skills/`,
`docs/selfdev/` (an allowlist, not a denylist) -- `.env`, `bot.py`,
`config.py`, `venv/`, `data/*.db`, and anything outside the project root are
never reachable by generated code. `install_plugin` always re-runs the
safety checks fresh, and rolls back automatically if the post-install
restart or health check fails.
