import json
import os
import time
from pathlib import Path

from sqlalchemy import create_engine, event, text
from langchain_community.chat_message_histories import SQLChatMessageHistory
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage

_DB_PATH = Path(os.getenv("HISTORY_DB_PATH", str(Path(__file__).parent / "chat_history.db")))
HISTORY_WINDOW = 5

_engine = create_engine(
    f"sqlite:///{_DB_PATH}",
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(_engine, "connect")
def _set_wal(dbapi_conn, _):
    dbapi_conn.execute("PRAGMA journal_mode=WAL")
    dbapi_conn.execute("PRAGMA busy_timeout=30000")


def _ensure_sessions_table():
    with _engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id      TEXT PRIMARY KEY,
                title           TEXT,
                legislation_ids TEXT NOT NULL DEFAULT '[]',
                created_at      REAL NOT NULL
            )
        """))
        conn.commit()


_ensure_sessions_table()


def get_history(session_id: str) -> SQLChatMessageHistory:
    return SQLChatMessageHistory(session_id=session_id, connection=_engine)


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
    with _engine.connect() as conn:
        conn.execute(
            text(
                "INSERT OR IGNORE INTO sessions (session_id, title, legislation_ids, created_at) "
                "VALUES (:sid, :title, :legs, :ts)"
            ),
            {"sid": session_id, "title": question[:120],
             "legs": json.dumps(legislation_ids or []), "ts": time.time()},
        )
        conn.commit()


def list_sessions() -> list[dict]:
    """Return all sessions ordered newest-first."""
    with _engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT session_id, title, legislation_ids, created_at "
                "FROM sessions ORDER BY created_at DESC"
            )
        ).fetchall()
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
    with _engine.connect() as conn:
        conn.execute(text("DELETE FROM sessions WHERE session_id = :sid"), {"sid": session_id})
        conn.commit()
