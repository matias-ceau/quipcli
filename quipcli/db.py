import json
import re
import secrets
import sqlite3
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from . import constants


@dataclass
class _UsageStats:
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float | None = None
    # Set when the provider served a different model than the one requested
    # (e.g. a router like openrouter/auto): `model` is then the real one.
    routed_from: str | None = None


def _served_model(requested: str, served: str | None) -> tuple[str, str | None]:
    """(model to report, routed_from). A served id that merely extends the
    requested one (dated snapshot, e.g. gpt-4o → gpt-4o-2024-08-06) isn't
    reported as routing."""
    if not served or served.startswith(requested):
        return requested, None
    return served, requested


# Paths whose schema was already ensured in this process — a single `qp` call
# opens the DB several times (history, messages, session lookup) and the DDL
# only needs to run once.
_SCHEMA_READY: set[str] = set()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS history (
            ts                REAL    NOT NULL,
            model             TEXT    NOT NULL,
            prompt_tokens     INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd          REAL,
            mode              TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON history(ts)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id        TEXT    NOT NULL,
            ts                REAL    NOT NULL,
            role              TEXT    NOT NULL,
            content           TEXT    NOT NULL,
            model             TEXT,
            prompt_tokens     INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd          REAL,
            mode              TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id      TEXT PRIMARY KEY,
            name    TEXT NOT NULL UNIQUE,
            created REAL NOT NULL
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    _migrate_legacy_sessions(conn)
    conn.commit()


def _migrate_legacy_sessions(conn: sqlite3.Connection) -> None:
    """Pre-`sessions`-table rows used the session *name* as session_id: give
    each one a hex id and keep the old value as its name."""
    legacy = conn.execute(
        "SELECT session_id, MIN(ts) FROM messages"
        " WHERE session_id NOT IN (SELECT id FROM sessions) GROUP BY session_id"
    ).fetchall()
    for old, created in legacy:
        new_id = _new_session_id(conn)
        name = _unique_name(conn, old)
        conn.execute(
            "INSERT INTO sessions(id,name,created) VALUES(?,?,?)",
            (new_id, name, created),
        )
        conn.execute(
            "UPDATE messages SET session_id=? WHERE session_id=?", (new_id, old)
        )


@contextmanager
def _db_conn() -> Iterator[sqlite3.Connection]:
    """Open the history DB, commit on success / roll back on error, always close.

    (`with sqlite3.connect(...)` alone only manages the transaction — it never
    closes the connection.)"""
    constants._DATA_DIR.mkdir(parents=True, exist_ok=True)
    key = str(constants._HISTORY_DB)
    if not constants._HISTORY_DB.exists():
        _SCHEMA_READY.discard(key)  # file was removed since — recreate tables
    conn = sqlite3.connect(constants._HISTORY_DB, timeout=5)
    try:
        if key not in _SCHEMA_READY:
            _ensure_schema(conn)
            _SCHEMA_READY.add(key)
        with conn:
            yield conn
    finally:
        conn.close()


def _record_usage(stats: _UsageStats, mode: str) -> None:
    try:
        with _db_conn() as conn:
            conn.execute(
                "INSERT INTO history(ts,model,prompt_tokens,completion_tokens,cost_usd,mode)"
                " VALUES(?,?,?,?,?,?)",
                (
                    time.time(),
                    stats.model,
                    stats.prompt_tokens,
                    stats.completion_tokens,
                    stats.cost_usd,
                    mode,
                ),
            )
    except sqlite3.Error, OSError:
        pass


def _record_message(
    session_id: str,
    role: str,
    content: str | list,
    model: str | None,
    stats: _UsageStats | None,
    mode: str | None,
) -> None:
    content_str = json.dumps(content) if isinstance(content, list) else content
    try:
        with _db_conn() as conn:
            conn.execute(
                "INSERT INTO messages"
                "(session_id,ts,role,content,model,prompt_tokens,completion_tokens,cost_usd,mode)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    time.time(),
                    role,
                    content_str,
                    model,
                    stats.prompt_tokens if stats else 0,
                    stats.completion_tokens if stats else 0,
                    stats.cost_usd if stats else None,
                    mode,
                ),
            )
    except sqlite3.Error, OSError:
        pass


# ── Sessions ──────────────────────────────────────────────────────────────────
# Every prompt belongs to a session: a new one unless -f (continue the current
# session) or -s (pick one by name / id prefix, or create it by name). The
# "current" session — what -f continues — is the last one a message was sent
# to, or one picked with `-S -t`; listing or status calls never change it.

_STOPWORDS_TEXT = (
    # English
    "a about all an and any are as at be but by can could did do does for from"
    " get give has have how i if in into is it its just me my of on or please"
    " should so some tell that the their there these this to use was what when"
    " where which who why will with would you your"
    # French
    " au aux avec ce ces cet cette comment dans de des donne du elle en est et"
    " faire fais il ils je la le les leur ma me mes moi mon ne nous on ou par pas"
    " plus pour pourquoi quel quelle quels quelles que quoi qui sa se ses son"
    " sont sur ta te tes toi ton tu un une vous"
)
_STOPWORDS = frozenset(_STOPWORDS_TEXT.split())


@dataclass
class _Session:
    id: str
    name: str | None  # None → derived from the first prompt when saved
    messages: list[dict]
    is_new: bool = False

    @property
    def label(self) -> str:
        return f"{self.name} ({self.id})" if self.name else self.id


def _new_session_id(conn: sqlite3.Connection) -> str:
    while True:
        sid = secrets.token_hex(3)
        if not conn.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
            return sid


def _unique_name(conn: sqlite3.Connection, base: str) -> str:
    name, n = base, 2
    while conn.execute("SELECT 1 FROM sessions WHERE name=?", (name,)).fetchone():
        name, n = f"{base}-{n}", n + 1
    return name


def _name_from_prompt(text: str) -> str:
    words = [
        w
        for w in re.findall(r"[a-z0-9]+", text.lower())
        if w not in _STOPWORDS and len(w) > 1
    ]
    return "-".join(words[:3])[:30].strip("-") or "session"


def _text_of(content: str) -> str:
    """Plain text of a stored message (multimodal content is a JSON list)."""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError, ValueError:
        return content
    if isinstance(parsed, list):
        return " ".join(
            p.get("text", "")
            for p in parsed
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return content


def _get_session_messages(session_id: str, limit: int | None = None) -> list[dict]:
    """Messages of a session, oldest first. With `limit`, only the last
    `limit` ones, trimmed so the history starts on a user turn."""
    try:
        with _db_conn() as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE session_id=?"
                " ORDER BY ts DESC, id DESC" + (" LIMIT ?" if limit else ""),
                (session_id, limit) if limit else (session_id,),
            ).fetchall()
    except sqlite3.Error, OSError:
        return []
    rows.reverse()
    while limit and rows and rows[0][0] != "user":
        rows.pop(0)
    result = []
    for role, content in rows:
        try:
            parsed = json.loads(content)
            if isinstance(parsed, list):
                result.append({"role": role, "content": parsed})
                continue
        except json.JSONDecodeError, ValueError:
            pass
        result.append({"role": role, "content": content})
    return result


def _current_session_id() -> str | None:
    if not constants._HISTORY_DB.exists():
        return None
    try:
        with _db_conn() as conn:
            row = conn.execute(
                "SELECT s.id FROM meta JOIN sessions s ON s.id = meta.value"
                " WHERE meta.key='current'"
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT session_id FROM messages ORDER BY ts DESC, id DESC LIMIT 1"
                ).fetchone()
        return row[0] if row else None
    except sqlite3.Error, OSError:
        return None


def _set_current_session(session_id: str) -> None:
    try:
        with _db_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('current',?)",
                (session_id,),
            )
    except sqlite3.Error, OSError:
        pass


def _find_session(ref: str) -> tuple[str, str] | None:
    """(id, name) for an exact name, else a unique id prefix. Exits when
    the prefix matches several sessions."""
    if not constants._HISTORY_DB.exists():
        return None
    try:
        with _db_conn() as conn:
            row = conn.execute(
                "SELECT id, name FROM sessions WHERE name=?", (ref,)
            ).fetchone()
            if row:
                return row[0], row[1]
            if not re.fullmatch(r"[0-9a-f]+", ref):
                return None
            rows = conn.execute(
                "SELECT id, name FROM sessions WHERE id LIKE ? || '%'", (ref,)
            ).fetchall()
    except sqlite3.Error, OSError:
        return None
    if len(rows) > 1:
        print(f"Error: '{ref}' matches several session ids:", file=sys.stderr)
        for sid, name in rows:
            print(f"  {sid}  {name}", file=sys.stderr)
        sys.exit(1)
    return (rows[0][0], rows[0][1]) if rows else None


def _session_info(session_id: str) -> tuple[str, int] | None:
    """(name, message count) of a stored session."""
    try:
        with _db_conn() as conn:
            row = conn.execute(
                "SELECT s.name, COUNT(m.id) FROM sessions s"
                " LEFT JOIN messages m ON m.session_id = s.id WHERE s.id=?"
                " GROUP BY s.id",
                (session_id,),
            ).fetchone()
    except sqlite3.Error, OSError:
        return None
    return (row[0], row[1]) if row else None


def _list_sessions(limit: int = 50) -> list[dict]:
    """Most recently active sessions first."""
    if not constants._HISTORY_DB.exists():
        return []
    try:
        with _db_conn() as conn:
            rows = conn.execute(
                "SELECT s.id, s.name, MAX(m.ts), COUNT(m.id),"
                " (SELECT content FROM messages WHERE session_id = s.id"
                "  AND role = 'user' ORDER BY ts, id LIMIT 1)"
                " FROM sessions s JOIN messages m ON m.session_id = s.id"
                " GROUP BY s.id ORDER BY MAX(m.ts) DESC LIMIT ?",
                (limit,),
            ).fetchall()
    except sqlite3.Error, OSError:
        return []
    return [
        {
            "id": sid,
            "name": name,
            "updated": updated,
            "messages": count,
            "first_prompt": _text_of(first or ""),
        }
        for sid, name, updated, count, first in rows
    ]


def _save_session(session: _Session, prompt_text: str) -> None:
    """Create a new session's row (naming it from the prompt if unnamed) and
    make it the current one."""
    try:
        with _db_conn() as conn:
            if session.is_new:
                session.name = _unique_name(
                    conn, session.name or _name_from_prompt(prompt_text)
                )
                conn.execute(
                    "INSERT INTO sessions(id,name,created) VALUES(?,?,?)",
                    (session.id, session.name, time.time()),
                )
                session.is_new = False
            conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('current',?)",
                (session.id,),
            )
    except sqlite3.Error, OSError:
        pass


def _fresh_session(name: str | None = None) -> _Session:
    try:
        with _db_conn() as conn:
            sid = _new_session_id(conn)
    except sqlite3.Error, OSError:
        sid = secrets.token_hex(3)
    return _Session(id=sid, name=name, messages=[], is_new=True)


def _resolve_session(
    session_arg: str | None,
    follow_up: bool,
    quiet: bool = False,
    history_limit: int | None = None,
) -> _Session:
    if session_arg and follow_up:
        print(
            "Error: --session and --follow-up are mutually exclusive.", file=sys.stderr
        )
        sys.exit(1)

    def _announce(session: _Session, note: str = "") -> None:
        if not quiet:
            print(f"\033[2mSession: {session.label}{note}\033[0m", file=sys.stderr)

    if follow_up:
        sid = _current_session_id()
        found = _session_info(sid) if sid else None
        if not sid or not found:
            print("Error: no previous session found.", file=sys.stderr)
            sys.exit(1)
        session = _Session(sid, found[0], _get_session_messages(sid, history_limit))
        _announce(session, f" · {found[1]} messages")
        return session

    if session_arg is None:
        return _fresh_session()

    found = _find_session(session_arg)
    if found is None:
        session = _fresh_session(session_arg)
        _announce(session, " · new")
        return session
    sid, name = found
    session = _Session(sid, name, _get_session_messages(sid, history_limit))
    _announce(session, f" · {len(session.messages)} messages loaded")
    return session


def _cost_summary(days: int) -> dict:
    if not constants._HISTORY_DB.exists():
        return {}
    since = 0.0 if days == 0 else time.time() - days * 86400
    try:
        with _db_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*), SUM(cost_usd), SUM(prompt_tokens), SUM(completion_tokens)"
                " FROM history WHERE ts > ?",
                (since,),
            ).fetchone()
    except sqlite3.Error, OSError:
        return {}
    return {
        "requests": row[0] or 0,
        "cost_usd": row[1] or 0.0,
        "prompt_tokens": row[2] or 0,
        "completion_tokens": row[3] or 0,
    }
