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
/status
```

Shows Ollama, STT, TTS, selected models, allowed services, and allowed roots.

```text
/agent_on
/agent_off
```

Enables or disables read-only tool use for normal text messages. If the model returns an invalid JSON plan, Jarvis falls back to the normal Ollama answer without tools.

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
