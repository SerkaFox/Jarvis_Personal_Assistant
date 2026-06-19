import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import config


SECRET_RE = re.compile(
    r"(?i)(token|api[_-]?key|password|passwd|secret|authorization|bearer)\s*[:=]\s*([^\s]+)"
)
ISO_DATE_RE = re.compile(r"\b(19\d{2}|20\d{2})-(0[1-9]|1[0-2])-([0-2]\d|3[01])\b")
DOT_DATE_RE = re.compile(r"\b([0-2]?\d|3[01])\.(0?\d|1[0-2])\.(19\d{2}|20\d{2})\b")
RU_DATE_RE = re.compile(
    r"\b([0-2]?\d|3[01])\s+"
    r"(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)"
    r"\s+(19\d{2}|20\d{2})\b",
    re.IGNORECASE,
)

RU_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}


def now_iso() -> str:
    return datetime.now(ZoneInfo("Europe/Madrid")).isoformat(timespec="seconds")


def mask_secrets(text: str | None) -> str:
    if not text:
        return ""
    return SECRET_RE.sub(lambda m: f"{m.group(1)}=[MASKED]", text)


def get_conn() -> sqlite3.Connection:
    db_path = Path(config.JARVIS_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection | None = None) -> None:
    close = conn is None
    if conn is None:
        Path(config.JARVIS_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(config.JARVIS_DB_PATH)
    conn.executescript(
        """
        create table if not exists messages (
            id integer primary key,
            chat_id text,
            user_id text,
            role text,
            content text,
            source text,
            created_at text,
            meta_json text
        );
        create table if not exists memories (
            id integer primary key,
            kind text,
            key text unique,
            value text,
            confidence real,
            created_at text,
            updated_at text,
            source_message_id integer nullable
        );
        create table if not exists project_notes (
            id integer primary key,
            project_name text unique,
            path text,
            summary text,
            last_seen_commit text,
            updated_at text
        );
        create table if not exists sessions (
            chat_id text primary key,
            current_project text,
            updated_at text
        );
        """
    )
    conn.commit()
    if close:
        conn.close()


def save_message(
    chat_id: str,
    user_id: str,
    role: str,
    content: str,
    source: str,
    meta: dict[str, Any] | None = None,
) -> int | None:
    if not config.MEMORY_ENABLED:
        return None
    content = mask_secrets(content)
    with get_conn() as conn:
        cursor = conn.execute(
            """
            insert into messages (chat_id, user_id, role, content, source, created_at, meta_json)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                user_id,
                role,
                content,
                source,
                now_iso(),
                json.dumps(meta or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def recent_messages(chat_id: str, limit: int | None = None) -> list[dict[str, Any]]:
    limit = limit or config.HISTORY_LIMIT
    with get_conn() as conn:
        rows = conn.execute(
            """
            select id, role, content, source, created_at
            from messages
            where chat_id = ?
            order by id desc
            limit ?
            """,
            (chat_id, limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def clear_history(chat_id: str) -> int:
    with get_conn() as conn:
        cursor = conn.execute("delete from messages where chat_id = ?", (chat_id,))
        conn.commit()
        return cursor.rowcount


def upsert_memory(
    kind: str,
    key: str,
    value: str,
    confidence: float = 0.8,
    source_message_id: int | None = None,
) -> None:
    if not config.MEMORY_ENABLED:
        return
    value = mask_secrets(value)
    stamp = now_iso()
    with get_conn() as conn:
        conn.execute(
            """
            insert into memories (kind, key, value, confidence, created_at, updated_at, source_message_id)
            values (?, ?, ?, ?, ?, ?, ?)
            on conflict(key) do update set
              kind=excluded.kind,
              value=excluded.value,
              confidence=excluded.confidence,
              updated_at=excluded.updated_at,
              source_message_id=excluded.source_message_id
            """,
            (kind, key, value, confidence, stamp, stamp, source_message_id),
        )
        conn.commit()


def delete_memory(key: str) -> int:
    with get_conn() as conn:
        cursor = conn.execute("delete from memories where key = ?", (key,))
        conn.commit()
        return cursor.rowcount


def list_memories(limit: int = 20) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            select kind, key, value, confidence, updated_at
            from memories
            order by updated_at desc
            limit ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_memory(key: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "select kind, key, value, confidence, updated_at from memories where key = ?",
            (key,),
        ).fetchone()
    return dict(row) if row else None


def relevant_memories(text: str, limit: int = 8) -> list[dict[str, Any]]:
    tokens = {token.lower() for token in re.findall(r"[\wа-яА-ЯёЁ]{4,}", text)}
    memories = list_memories(100)
    scored = []
    for item in memories:
        haystack = f"{item['key']} {item['value']}".lower()
        score = sum(1 for token in tokens if token in haystack)
        if score or item["key"] in {"birth_date", "name"}:
            scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:limit]]


def parse_birth_date(text: str) -> str | None:
    if match := ISO_DATE_RE.search(text):
        return match.group(0)
    if match := DOT_DATE_RE.search(text):
        day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        return f"{year:04d}-{month:02d}-{day:02d}"
    if match := RU_DATE_RE.search(text):
        day = int(match.group(1))
        month = RU_MONTHS[match.group(2).lower()]
        year = int(match.group(3))
        return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def extract_memory_candidates(text: str, answer: str = "") -> list[dict[str, Any]]:
    lowered = text.lower()
    triggers = (
        "запомни",
        "remember",
        "сохрани",
        "мой день рождения",
        "я родился",
        "меня зовут",
        "мне нравится",
        "предпочитаю",
    )
    if not any(trigger in lowered for trigger in triggers):
        return []
    if SECRET_RE.search(text):
        return []

    candidates = []
    if birth_date := parse_birth_date(text):
        candidates.append(
            {
                "kind": "personal",
                "key": "birth_date",
                "value": birth_date,
                "confidence": 0.95,
            }
        )

    if "меня зовут" in lowered:
        value = re.sub(r"(?i).*меня зовут\s+", "", text).strip(" .")
        if value:
            candidates.append({"kind": "personal", "key": "name", "value": value[:120], "confidence": 0.8})

    if not candidates:
        cleaned = re.sub(r"(?i)^(запомни|remember|сохрани)[:,\s]+", "", text).strip()
        if cleaned:
            key = "_".join(re.findall(r"[\wа-яА-ЯёЁ]+", cleaned.lower())[:5])[:80] or "note"
            candidates.append({"kind": "note", "key": key, "value": cleaned[:1000], "confidence": 0.7})
    return candidates


def save_memory_candidates(text: str, answer: str, source_message_id: int | None = None) -> None:
    for candidate in extract_memory_candidates(text, answer):
        upsert_memory(source_message_id=source_message_id, **candidate)


def age_answer(text: str) -> str | None:
    lowered = text.lower()
    if not any(phrase in lowered for phrase in ("сколько мне лет", "мой возраст", "сколько мне будет")):
        return None
    memory = get_memory("birth_date")
    if not memory:
        return None
    birth = datetime.strptime(memory["value"], "%Y-%m-%d").date()
    today = datetime.now(ZoneInfo("Europe/Madrid")).date()
    age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
    return f"Тебе {age} лет. Дата рождения в памяти: {memory['value']}."


def project_note_for_text(text: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "select project_name, path, summary, last_seen_commit, updated_at from project_notes"
        ).fetchall()
    lowered = text.lower()
    return [dict(row) for row in rows if row["project_name"].lower() in lowered]


def save_project_note(project_name: str, path: str, summary: str, last_seen_commit: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            """
            insert into project_notes (project_name, path, summary, last_seen_commit, updated_at)
            values (?, ?, ?, ?, ?)
            on conflict(project_name) do update set
              path=excluded.path,
              summary=excluded.summary,
              last_seen_commit=excluded.last_seen_commit,
              updated_at=excluded.updated_at
            """,
            (project_name, path, mask_secrets(summary), last_seen_commit, now_iso()),
        )
        conn.commit()


def set_current_project(chat_id: str, project_name: str) -> None:
    if not config.MEMORY_ENABLED or not chat_id or not project_name:
        return
    with get_conn() as conn:
        conn.execute(
            """
            insert into sessions (chat_id, current_project, updated_at)
            values (?, ?, ?)
            on conflict(chat_id) do update set
              current_project=excluded.current_project,
              updated_at=excluded.updated_at
            """,
            (chat_id, project_name, now_iso()),
        )
        conn.commit()


def get_current_project(chat_id: str) -> str | None:
    if not config.MEMORY_ENABLED or not chat_id:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "select current_project from sessions where chat_id = ?",
            (chat_id,),
        ).fetchone()
    if not row:
        return None
    value = str(row["current_project"] or "").strip()
    return value or None


def build_memory_context(chat_id: str, user_text: str) -> str:
    if not config.MEMORY_ENABLED:
        return ""
    parts = []
    current_project = get_current_project(chat_id)
    if current_project:
        parts.append(f"Current project: {current_project}")
    history = recent_messages(chat_id, config.HISTORY_LIMIT)
    if history:
        parts.append("Последние сообщения:\n" + "\n".join(f"{m['role']}: {m['content']}" for m in history))
    memories = relevant_memories(user_text)
    if memories:
        parts.append("Память:\n" + "\n".join(f"{m['key']}: {m['value']}" for m in memories))
    notes = project_note_for_text(user_text)
    if notes:
        parts.append("Project notes:\n" + "\n".join(f"{n['project_name']}: {n['summary']}" for n in notes))
    return "\n\n".join(parts)
