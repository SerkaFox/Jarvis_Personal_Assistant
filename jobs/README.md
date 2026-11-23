# IT jobs feed for Jarvis

The public file `jobs/feed.json` carries up to 8 verified new IT vacancies from ChatGPT to the server. Never put credentials, CV contact details, Telegram tokens or other private information into this repository.

## Feed format

```json
{
  "schema_version": 1,
  "generated_at": "2026-11-23T08:45:00+01:00",
  "jobs": []
}
```

Each job must have `company`, `title`, `url` (HTTPS); optional fields: `location`, `work_mode`, `published_at`, `match_score` (0–100), `why_match`, `gaps`, `application_es`. The latter is an unsent draft, not an application. Publish only genuinely new verified job postings (not merely refreshed ads). Feed may remain empty when no qualifying jobs exist.

## Deploy to seradmin

The GitHub change **does not itself update the running server**. Review the commit, then update the Jarvis checkout. The documented project path is `/home/seradmin/jarvis_bot`:

```bash
cd /home/seradmin/jarvis_bot
git pull --ff-only
python3 -m unittest tests.test_jobs_receiver
python3 jobs_receiver.py --once --dry-run
```

Run with the same Python interpreter/environment as the existing `jarvis-bot` service; Jarvis already uses python-dotenv. On a fully empty feed, the dry run prints zero vacancies. The receiver uses existing `TELEGRAM_TOKEN` and `ALLOWED_USER_ID` from `.env`.

Once tested, schedule a poll (example, every 10 minutes) with `crontab -e` for the server account that runs Jarvis; check the real venv path first:

```cron
*/10 * * * * cd /home/seradmin/jarvis_bot && /usr/bin/flock -n /tmp/jarvis-jobs-receiver.lock /home/seradmin/jarvis_bot/venv/bin/python jobs_receiver.py --once >> /home/seradmin/jarvis_bot/data/jobs_receiver.log 2>&1
```

Adjust `venv/bin/python` to the interpreter in the actual service. Use a temporary *real, verified* test listing in the feed for end-to-end Telegram validation only after approving a test notification. Do not commit fake advertisements to the production feed.

Receiver keeps the identifiers already sent in `data/jobs_sent.json` so it does not resend on the next poll; it updates that file only after Telegram confirms successful delivery. A failed delivery gets retried on a later run. Telegram formatting is HTML-escaped, and no text in the feed is executed.

The server only reads the GitHub feed; it does not need a GitHub token or an inbound network port. This integration **never sends applications**. To automate publication from ChatGPT, update the existing scheduled job-search prompt to write verified results to `jobs/feed.json` using the connected GitHub integration; do that after confirming the task has GitHub access in its runtime.
