import json
import os
import sqlite3
import time
from pathlib import Path

from langchain_community.chat_message_histories import SQLChatMessageHistory
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage

_DB_PATH = Path(os.getenv("HISTORY_DB_PATH", str(Path(__file__).parent / "chat_history.db")))
_CONN = f"sqlite:///{_DB_PATH}"
HISTORY_WINDOW = 5


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id      TEXT PRIMARY KEY,
            title           TEXT,
            legislation_ids TEXT NOT NULL DEFAULT '[]',
            created_at      REAL NOT NULL
        )
    """)
    conn.commit()
    return conn


def get_history(session_id: str) -> SQLChatMessageHistory:
    return SQLChatMessageHistory(session_id=session_id, connection=_CONN)


def load_recent(session_id: str) -> list[BaseMessage]:
    """Return the last HISTORY_WINDOW exchanges (up to 2*HISTORY_WINDOW messages)."""
    return get_history(session_id).messages[-(HISTORY_WINDOW * 2):]


def save_exchange(
    session_id: str,
    question: str,
    answer: str,
    legislation_ids: list[str] | None = None,
) -> None:
    h = get_history(session_id)
    h.add_user_message(question)
    h.add_ai_message(answer)
    # INSERT OR IGNORE so only the first exchange sets the title / created_at
    conn = _db()
    conn.execute(
        "INSERT OR IGNORE INTO sessions (session_id, title, legislation_ids, created_at) "
        "VALUES (?, ?, ?, ?)",
        (session_id, question[:120], json.dumps(legislation_ids or []), time.time()),
    )
    conn.commit()
    conn.close()


def list_sessions() -> list[dict]:
    """Return all sessions ordered newest-first."""
    conn = _db()
    rows = conn.execute(
        "SELECT session_id, title, legislation_ids, created_at "
        "FROM sessions ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [
        {
            "session_id": r[0],
            "title": r[1],
            "legislation_ids": json.loads(r[2]),
            "created_at": r[3],
        }
        for r in rows
    ]


def get_session_messages(session_id: str) -> list[dict]:
    """Return all messages for a session as plain {role, content} dicts."""
    msgs = get_history(session_id).messages
    return [
        {"role": "user" if isinstance(m, HumanMessage) else "assistant",
         "content": m.content}
        for m in msgs
    ]


def delete_session(session_id: str) -> None:
    get_history(session_id).clear()
    conn = _db()
    conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
    conn.commit()
    conn.close()
