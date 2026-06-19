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
```

`WRITE_MODE_ENABLED` is disabled by default. Set it to `true` only when you want Jarvis to create files in the sandbox. `WRITE_ROOT` is the only directory where write tools can create directories, write text files, and run `git init`. Paths with `..` are rejected, and secret-like files such as `.env`, keys, PEM files, sqlite/db files, and token/password filenames are blocked.

Allowed write workspace operations:

```text
create_project_dir(name)
write_text_file(path, content, overwrite=false)
append_text_file(path, content)
list_write_projects()
init_git(path)
write_static_site(project_name, title, description, theme)
run_safe_project_check(path)
```

The write tools do not deploy, do not use sudo, do not install dependencies, and do not write outside `WRITE_ROOT`.

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
```

Shows deterministic intent routing for normal messages: detected intent, selected project, tools called, and errors.

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
запомни, мой день рождения 13 октября 1982
```

```text
сколько мне лет?
```
