"""Send new curated job listings from a GitHub JSON feed to the Jarvis Telegram chat.

Run: python3 jobs_receiver.py --once
Optional: python3 jobs_receiver.py --once --dry-run
Environment: TELEGRAM_TOKEN, ALLOWED_USER_ID, JOBS_FEED_URL, JOBS_STATE_FILE.
No job applications are submitted by this script.
"""
import argparse
import hashlib
import html
import json
import logging
import os
from pathlib import Path
import tempfile
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from dotenv import load_dotenv

load_dotenv()
LOG = logging.getLogger("jobs_receiver")
DEFAULT_FEED = (
    "https://raw.githubusercontent.com/SerkaFox/"
    "Jarvis_Personal_Assistant/main/jobs/feed.json"
)
DEFAULT_STATE = Path(__file__).resolve().parent / "data" / "jobs_sent.json"


def _https_url(value, *, hosts=None):
    if not isinstance(value, str):
        raise ValueError("URL must be text")
    p = urlparse(value.strip())
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        raise ValueError("Only HTTPS URLs without credentials are accepted")
    if hosts and p.hostname.lower() not in hosts:
        raise ValueError("Unexpected feed host")
    return value.strip()


def _load_json_url(url):
    req = Request(url, headers={"User-Agent": "Jarvis-Jobs-Receiver/1.0",
                                "Accept": "application/json"})
    with urlopen(req, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError("Feed HTTP status: %s" % response.status)
        body = response.read(256 * 1024 + 1)
    if len(body) > 256 * 1024:
        raise ValueError("Feed too large")
    return json.loads(body.decode("utf-8"))


def validate_feed(payload):
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("Unsupported feed schema")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or len(jobs) > 50:
        raise ValueError("Feed jobs must be a list with at most 50 entries")
    valid = []
    for job in jobs:
        if not isinstance(job, dict):
            raise ValueError("Invalid job record")
        for required in ("company", "title", "url"):
            if not isinstance(job.get(required), str) or not job[required].strip():
                raise ValueError("Missing job field: " + required)
        link = _https_url(job["url"])
        score = job.get("match_score")
        if score is not None and (type(score) is not int or not 0 <= score <= 100):
            raise ValueError("match_score must be 0..100")
        valid.append({**job, "url": link})
    return valid


def job_key(job):
    # Deduplicate across daily feeds, independent of job ordering and description.
    return hashlib.sha256(
        (job["company"].strip().casefold() + "\n" +
         job["title"].strip().casefold() + "\n" +
         job["url"].strip().rstrip("/")).encode("utf-8")
    ).hexdigest()


def read_seen(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return set(x for x in data if isinstance(x, str))
    except FileNotFoundError:
        pass
    except (ValueError, OSError):
        LOG.warning("Unable to read jobs state; refusing to send to avoid duplicates")
        raise
    return set()


def save_seen(path, seen):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".jobs-", delete=False
    ) as stream:
        tmp = Path(stream.name)
        json.dump(sorted(seen), stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def render_job(job):
    def esc(value, limit=600):
        return html.escape(str(value or "")[:limit], quote=True)
    out = ["💼 <b>" + esc(job["title"], 150) + "</b>",
           "🏢 " + esc(job["company"], 120)]
    place = " · ".join(str(job.get(k) or "") for k in ("location", "work_mode") if job.get(k))
    if place:
        out.append("📍 " + esc(place, 180))
    if job.get("published_at"):
        out.append("📅 " + esc(job["published_at"], 40))
    if job.get("match_score") is not None:
        out.append("🎯 " + str(job["match_score"]) + "/100")
    if job.get("why_match"):
        out.append("✅ " + esc(job["why_match"]))
    if job.get("gaps"):
        out.append("⚠️ " + esc(job["gaps"], 350))
    out.append("🔗 " + esc(job["url"], 1100))
    if job.get("application_es"):
        out.append("\n📝 <b>Borrador de candidatura (sin enviar):</b>\n" +
                   esc(job["application_es"], 1200))
    return "\n".join(out)[:3900]


def send_telegram(message, token, chat_id):
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_TOKEN and ALLOWED_USER_ID must be configured")
    url = "https://api.telegram.org/bot" + token + "/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = Request(url, data=payload, headers={"Content-Type": "application/json"},
                  method="POST")
    with urlopen(req, timeout=20) as response:
        result = json.loads(response.read(64 * 1024).decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError("Telegram did not confirm delivery")


def run_once(feed_url, state_file, *, dry_run=False, sender=send_telegram):
    feed_url = _https_url(feed_url, hosts={"raw.githubusercontent.com"})
    jobs = validate_feed(_load_json_url(feed_url))
    seen = read_seen(state_file)
    new_count = 0
    for job in jobs:
        key = job_key(job)
        if key in seen:
            continue
        if dry_run:
            print(render_job(job))
        else:
            sender(render_job(job), os.getenv("TELEGRAM_TOKEN"),
                   os.getenv("ALLOWED_USER_ID"))
            # Only persist after Telegram reports success.
            seen.add(key)
            save_seen(state_file, seen)
        new_count += 1
    LOG.info("New job listings: %d (dry_run=%s)", new_count, dry_run)
    return new_count


def main():
    p = argparse.ArgumentParser(description="Jarvis GitHub jobs feed receiver")
    p.add_argument("--once", action="store_true", help="Read feed and send unseen listings")
    p.add_argument("--dry-run", action="store_true", help="Print without notifying or saving")
    args = p.parse_args()
    if not args.once:
        p.error("Specify --once")
    logging.basicConfig(level=logging.INFO)
    url = os.getenv("JOBS_FEED_URL", DEFAULT_FEED)
    state = Path(os.getenv("JOBS_STATE_FILE", str(DEFAULT_STATE))).expanduser()
    try:
        run_once(url, state, dry_run=args.dry_run)
    except (ValueError, RuntimeError, HTTPError, URLError, OSError) as exc:
        LOG.error("Jobs receiver failed: %s", exc)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
