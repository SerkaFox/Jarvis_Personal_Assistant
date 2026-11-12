"""
Email watcher for info@creaganaweb.es
Monitors the mailcow inbox (read via doveadm, no password needed) and sends Telegram notifications for new emails.
"""
import subprocess
import email
import email.header
import logging
import os
import time
import json
import base64
import html
import re
import shutil
import tempfile
import io
import zipfile
import gzip
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from email.utils import parseaddr, parsedate_to_datetime

import requests
from dotenv import load_dotenv

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────────
MAILBOX       = "info@creaganaweb.es"
DOVECOT       = "mailcowdockerized-dovecot-mailcow-1"

TG_TOKEN      = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID    = os.getenv("ALLOWED_USER_ID")
WEBMAIL_URL   = "https://mail.ybarra-motor.es/SOGo/"

CHECK_INTERVAL = 60   # seconds between IMAP checks
SEEN_FILE      = Path(__file__).parent / "data" / "email_seen_uids_creaganaweb.json"
RENDER_DIR     = Path.home() / ".cache" / "email_watcher"  # Chrome can't write to private /tmp
CHROME         = shutil.which("google-chrome") or shutil.which("chromium-browser")
TG_MAX_FILE    = 49 * 1024 * 1024  # Bot API upload limit is 50 MB
LIMIT_STATE_FILE = Path("/tmp/pushik_usage_limit.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [email_watcher] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)


# ── Persistent seen UIDs ─────────────────────────────────────────────────────
def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            pass
    return set()


def save_seen(seen: set):
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(list(seen)))


def bridge_limit_active() -> bool:
    try:
        if not LIMIT_STATE_FILE.exists():
            return False
        state = json.loads(LIMIT_STATE_FILE.read_text())
        reset_epoch = state.get("usage_limit_until")
        return bool(reset_epoch and reset_epoch > time.time())
    except Exception as e:
        log.error("Could not read bridge limit state: %s", e)
        return False


# ── Header decode helper ──────────────────────────────────────────────────────
def decode_header(raw: str) -> str:
    parts = email.header.decode_header(raw or "")
    result = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            result.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(chunk)
    return "".join(result).strip()


# ── Telegram sender ───────────────────────────────────────────────────────────
def send_telegram(text: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        log.error("TELEGRAM_TOKEN or ALLOWED_USER_ID not set")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        log.error("Telegram send failed: %s", e)


def send_telegram_document(filename: str, data: bytes, caption: str = "") -> bool:
    if not TG_TOKEN or not TG_CHAT_ID:
        log.error("TELEGRAM_TOKEN or ALLOWED_USER_ID not set")
        return False
    if len(data) > TG_MAX_FILE:
        send_telegram(f"⚠️ Adjunto demasiado grande para Telegram: {html.escape(filename)}")
        return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument"
    payload = {"chat_id": TG_CHAT_ID}
    if caption:
        payload.update(caption=caption[:1024], parse_mode="HTML")
    try:
        r = requests.post(url, data=payload, files={"document": (filename, data)}, timeout=120)
        r.raise_for_status()
        return True
    except Exception as e:
        log.error("Telegram document %s failed: %s", filename, e)
        return False


def _part_text(part) -> str:
    payload = part.get_payload(decode=True) or b""
    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")


def email_to_html(msg: email.message.Message, info: dict) -> tuple[str, list]:
    """Standalone HTML of the email; returns (html, [(filename, bytes), ...] attachments)."""
    body_html = body_text = None
    cids, attachments = {}, []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        fname = part.get_filename()
        cid = (part.get("Content-ID") or "").strip("<> ")
        if fname and part.get_content_disposition() == "attachment" or (fname and not cid):
            attachments.append((decode_header(fname), part.get_payload(decode=True) or b""))
        elif cid and ctype.startswith("image/"):
            data = base64.b64encode(part.get_payload(decode=True) or b"").decode()
            cids[cid] = f"data:{ctype};base64,{data}"
        elif ctype == "text/html" and body_html is None:
            body_html = _part_text(part)
        elif ctype == "text/plain" and body_text is None:
            body_text = _part_text(part)

    if body_html:
        body = re.sub(r"(?is)<script.*?</script>", "", body_html)
        body = re.sub(r'(?i)cid:([^"\'\s>)]+)', lambda m: cids.get(m.group(1), m.group(0)), body)
    else:
        body = f'<pre style="white-space:pre-wrap;font-family:inherit">{html.escape(body_text or "")}</pre>'

    to = decode_header(msg.get("To", ""))
    head = "".join(
        f"<tr><td><b>{k}</b></td><td>{html.escape(v)}</td></tr>"
        for k, v in (("De", f"{html.escape(info['from_name'])} <{info['from_addr']}>"), ("Para", to),
                     ("Fecha", info["date_str"]), ("Asunto", info["subject"])) if v)
    if attachments:
        head += "<tr><td><b>Adjuntos</b></td><td>" + html.escape(", ".join(n for n, _ in attachments)) + "</td></tr>"
    page = f"""<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="script-src 'none'">
<style>body{{font-family:Arial,sans-serif;margin:24px}} .hdr{{border-bottom:2px solid #ccc;margin-bottom:16px;padding-bottom:8px}}
.hdr td{{padding:2px 12px 2px 0;vertical-align:top;font-size:14px}} img{{max-width:100%}}</style></head>
<body><table class="hdr">{head}</table>{body}</body></html>"""
    return page, attachments


def html_to_pdf(page: str) -> bytes | None:
    if not CHROME:
        return None
    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=RENDER_DIR) as d:
        src, out = Path(d) / "mail.html", Path(d) / "mail.pdf"
        src.write_text(page, encoding="utf-8")
        try:
            subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                            "--virtual-time-budget=5000",
                            f"--user-data-dir={d}/profile", f"--print-to-pdf={out}", src.as_uri()],
                           capture_output=True, timeout=90)
        except Exception as e:
            log.error("PDF render failed: %s", e)
        return out.read_bytes() if out.exists() else None


def _safe_name(text: str) -> str:
    return (re.sub(r'[\\/:*?"<>|\s]+', "_", text).strip("_.") or "correo")[:60]


def notify(msg: email.message.Message):
    info = parse_message(msg)
    xml = dmarc_payload(msg)
    if xml is not None:
        summary = format_dmarc(xml, info)
        if summary:
            send_telegram(summary)
            return info
    caption = format_notification(info)
    page, attachments = email_to_html(msg, info)
    name = _safe_name(info["subject"])
    pdf = html_to_pdf(page)
    if pdf:
        sent = send_telegram_document(f"{name}.pdf", pdf, caption)
    else:
        sent = send_telegram_document(f"{name}.html", page.encode(), caption)
    if not sent:
        send_telegram(caption)
    for fname, data in attachments:
        send_telegram_document(fname, data)
    return info


# ── DMARC aggregate reports ───────────────────────────────────────────────────
def dmarc_payload(msg: email.message.Message) -> bytes | None:
    """Return the report XML if this message is a DMARC aggregate report."""
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = (part.get_filename() or "").lower()
        ctype = part.get_content_type()
        data = part.get_payload(decode=True) or b""
        if not data:
            continue
        try:
            if name.endswith(".zip") or ctype == "application/zip":
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    return z.read(z.namelist()[0])
            if name.endswith(".gz") or ctype in ("application/gzip", "application/x-gzip"):
                return gzip.decompress(data)
            if name.endswith(".xml") and b"<feedback" in data[:4000]:
                return data
        except Exception:
            return None
    return None


def format_dmarc(xml: bytes, info: dict) -> str | None:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    if root.tag != "feedback":
        return None
    org = root.findtext("report_metadata/org_name") or "?"
    domain = root.findtext("policy_published/domain") or "?"
    lines = [f"📊 <b>Informe DMARC — {html.escape(domain)}</b>",
             f"🏢 <b>Enviado por:</b> {html.escape(org)}", ""]
    total = 0
    for rec in root.findall("record"):
        count = int(rec.findtext("row/count") or 0)
        total += count
        ip = rec.findtext("row/source_ip") or "?"
        dkim = rec.findtext("row/policy_evaluated/dkim") or "?"
        spf = rec.findtext("row/policy_evaluated/spf") or "?"
        disp = rec.findtext("row/policy_evaluated/disposition") or "?"
        mark = "✅" if (dkim == "pass" or spf == "pass") else "⚠️"
        lines.append(f"{mark} <code>{ip}</code> — {count} correo(s)")
        lines.append(f"    DKIM: {dkim} · SPF: {spf} · acción: {disp}")
    lines += ["", f"Total: {total} correo(s). Nada que hacer si todo sale ✅."]
    return "\n".join(lines)


# ── Parse message ─────────────────────────────────────────────────────────────
def parse_message(msg: email.message.Message) -> dict:
    subject  = decode_header(msg.get("Subject", "(sin asunto)"))
    from_raw = msg.get("From", "")
    from_name, from_addr = parseaddr(from_raw)
    from_name = decode_header(from_name) if from_name else from_addr

    try:
        date_dt = parsedate_to_datetime(msg.get("Date", ""))
        date_str = date_dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        date_str = msg.get("Date", "")

    # Extract short body preview
    preview = ""
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        preview = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                        break
        elif msg.get_content_maintype() == "text":
            payload = msg.get_payload(decode=True)
            if payload:
                preview = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
        # Clean and truncate
        preview = " ".join(html.unescape(preview).split())[:200]
        if len(preview) == 200:
            preview += "…"
    except Exception:
        preview = ""

    return {"subject": subject, "from_name": from_name, "from_addr": from_addr,
            "date_str": date_str, "preview": preview}


# ── Format notification ───────────────────────────────────────────────────────
def format_notification(info: dict) -> str:
    lines = [
        "📬 <b>Nuevo correo — " + MAILBOX + "</b>",
        "",
        f"👤 <b>De:</b> {info['from_name']}",
        f"📌 <b>Asunto:</b> {html.escape(info['subject'])}",
        f"🕐 <b>Fecha:</b> {html.escape(info['date_str'])}",
    ]
    if info["preview"]:
        lines += ["", f"<i>{html.escape(info['preview'])}</i>"]
    return "\n".join(lines)


# ── Mailbox access via doveadm ───────────────────────────────────────────────
def _doveadm(*args) -> bytes:
    return subprocess.run(["docker", "exec", DOVECOT, "doveadm", *args],
                          check=True, capture_output=True, timeout=60).stdout


def list_uids() -> list[str]:
    out = _doveadm("search", "-u", MAILBOX, "mailbox", "INBOX", "ALL").decode().split()
    return out[1::2]  # "<mailbox-guid> <uid>" pairs


def fetch_raw(uid: str) -> bytes:
    raw = _doveadm("fetch", "-u", MAILBOX, "text", "mailbox", "INBOX", "uid", uid)
    return raw.split(b"\n", 1)[1].rstrip(b"\f\n") + b"\n"  # strip "text:" prefix


def check_inbox(seen: set) -> set:
    new_seen = set(seen)
    try:
        new_uids = [uid for uid in list_uids() if uid not in seen]
        if not new_uids:
            return new_seen

        log.info("Found %d new message(s)", len(new_uids))

        for uid_str in new_uids:
            try:
                msg = email.message_from_bytes(fetch_raw(uid_str))
                info = notify(msg)
                log.info("Notified for UID %s: %s", uid_str, info["subject"])
            except Exception as e:
                log.error("Error processing UID %s: %s", uid_str, e)
            finally:
                new_seen.add(uid_str)

    except Exception as e:
        log.error("Mailbox check error: %s", e)

    return new_seen


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    log.info("Starting email watcher for %s (check every %ds)", MAILBOX, CHECK_INTERVAL)

    if not TG_TOKEN:
        log.error("TELEGRAM_TOKEN not set — notifications will not work")

    seen = load_seen()
    log.info("Loaded %d previously seen UIDs", len(seen))

    # On first run, mark all existing emails as seen (don't spam with old mail)
    if not seen:
        log.info("First run — marking all existing emails as seen (no notification)")
        try:
            seen = set(list_uids())
            log.info("Marked %d existing emails as seen", len(seen))
        except Exception as e:
            log.error("First-run seed failed: %s", e)
        save_seen(seen)

    while True:
        try:
            if bridge_limit_active():
                log.info("Pushik limit cooldown active — skipping mailbox check")
            else:
                seen = check_inbox(seen)
                save_seen(seen)
        except Exception as e:
            log.error("check_inbox crashed: %s", e)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
