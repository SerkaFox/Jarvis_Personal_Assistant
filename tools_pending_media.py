import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import config


def _now_utc_iso() -> str:
    return datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds").replace("+00:00", "Z")


def _get_conn() -> sqlite3.Connection:
    db_path = Path(config.JARVIS_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _init_db(conn)
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists pending_media (
            id integer primary key,
            chat_id text,
            user_id text,
            telegram_file_id text,
            file_unique_id text,
            mime_type text,
            size_bytes integer,
            caption text,
            received_at text,
            status text,
            used_at text,
            used_project text,
            saved_path text
        );
        create index if not exists idx_pending_media_chat_received on pending_media(chat_id, received_at);
        """
    )
    conn.commit()


def save_pending_media(
    chat_id: str,
    user_id: str,
    telegram_file_id: str,
    *,
    file_unique_id: str = "",
    mime_type: str = "",
    size_bytes: int | None = None,
    caption: str = "",
) -> dict[str, Any]:
    payload = {
        "chat_id": str(chat_id),
        "user_id": str(user_id),
        "telegram_file_id": str(telegram_file_id),
        "file_unique_id": str(file_unique_id or ""),
        "mime_type": str(mime_type or ""),
        "size_bytes": int(size_bytes or 0),
        "caption": str(caption or ""),
        "received_at": _now_utc_iso(),
        "status": "pending",
    }
    with _get_conn() as conn:
        cursor = conn.execute(
            """
            insert into pending_media (
                chat_id, user_id, telegram_file_id, file_unique_id,
                mime_type, size_bytes, caption, received_at, status
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["chat_id"],
                payload["user_id"],
                payload["telegram_file_id"],
                payload["file_unique_id"],
                payload["mime_type"],
                payload["size_bytes"],
                payload["caption"],
                payload["received_at"],
                payload["status"],
            ),
        )
        payload["id"] = int(cursor.lastrowid)
    return payload


def get_latest_pending_media(chat_id: str, max_age_minutes: int = 60) -> dict[str, Any] | None:
    cutoff = datetime.now(ZoneInfo("UTC")) - timedelta(minutes=max_age_minutes)
    cutoff_iso = cutoff.isoformat(timespec="seconds").replace("+00:00", "Z")
    with _get_conn() as conn:
        row = conn.execute(
            """
            select *
            from pending_media
            where chat_id = ?
              and status = 'pending'
              and received_at >= ?
            order by id desc
            limit 1
            """,
            (str(chat_id), cutoff_iso),
        ).fetchone()
        return dict(row) if row else None


def mark_media_used(media_id: int, project_name: str, saved_path: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            """
            update pending_media
               set status = 'used',
                   used_at = ?,
                   used_project = ?,
                   saved_path = ?
             where id = ?
            """,
            (_now_utc_iso(), str(project_name), str(saved_path), int(media_id)),
        )
        conn.commit()


def mark_media_failed(media_id: int, project_name: str = "", saved_path: str = "") -> None:
    """Marks a pending media item as failed (workflow did not complete). Never
    sets status='used', so a failed attempt doesn't silently consume the photo."""
    with _get_conn() as conn:
        conn.execute(
            """
            update pending_media
               set status = 'failed',
                   used_at = ?,
                   used_project = ?,
                   saved_path = ?
             where id = ?
            """,
            (_now_utc_iso(), str(project_name), str(saved_path), int(media_id)),
        )
        conn.commit()


def clear_old_pending_media(max_age_minutes: int = 240) -> int:
    cutoff = datetime.now(ZoneInfo("UTC")) - timedelta(minutes=max_age_minutes)
    cutoff_iso = cutoff.isoformat(timespec="seconds").replace("+00:00", "Z")
    with _get_conn() as conn:
        cursor = conn.execute(
            """
            delete from pending_media
             where status != 'pending'
                or received_at < ?
            """,
            (cutoff_iso,),
        )
        conn.commit()
        return int(cursor.rowcount or 0)

