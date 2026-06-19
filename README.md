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
