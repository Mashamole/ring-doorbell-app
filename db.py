"""SQLite persistence layer for OAuth tokens, unclaimed sessions, and webhook events."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterator


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS unclaimed_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    token_type TEXT NOT NULL DEFAULT 'Bearer',
    expires_at REAL NOT NULL,
    scope TEXT,
    ring_account_id TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_unclaimed_created_at
    ON unclaimed_tokens (created_at);

CREATE TABLE IF NOT EXISTS linked_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ring_account_id TEXT UNIQUE,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    token_type TEXT NOT NULL DEFAULT 'Bearer',
    expires_at REAL NOT NULL,
    integration_status TEXT NOT NULL DEFAULT 'completed',
    linked_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_events (
    event_id TEXT PRIMARY KEY,
    device_id TEXT,
    event_type TEXT,
    status TEXT NOT NULL,
    clip_path TEXT,
    error_message TEXT,
    created_at REAL NOT NULL,
    processed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_processed_events_created_at
    ON processed_events (created_at);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(db_path: Path) -> None:
    """Create tables and indexes if they do not exist."""
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.commit()


@contextmanager
def get_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield an initialized SQLite connection and commit/close cleanly."""
    conn = _connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cleanup_expired_unclaimed_tokens(
    db_path: Path,
    ttl_seconds: int = 900,
    now: float | None = None,
) -> int:
    """Delete unclaimed tokens older than ttl_seconds (default 15 minutes)."""
    current_time = now if now is not None else time.time()
    cutoff = current_time - ttl_seconds
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM unclaimed_tokens WHERE created_at < ?",
            (cutoff,),
        )
        return cursor.rowcount


def save_unclaimed_token(
    db_path: Path,
    access_token: str,
    refresh_token: str,
    expires_in: int | float,
    token_type: str = "Bearer",
    scope: str | None = None,
    ring_account_id: str | None = None,
    ttl_seconds: int = 900,
    now: float | None = None,
) -> int:
    """Cleanup expired unclaimed tokens and store a newly exchanged unclaimed token."""
    current_time = now if now is not None else time.time()
    expires_at = current_time + float(expires_in)
    cutoff = current_time - ttl_seconds

    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM unclaimed_tokens WHERE created_at < ?", (cutoff,))
        cursor = conn.execute(
            """
            INSERT INTO unclaimed_tokens (
                access_token, refresh_token, token_type, expires_at,
                scope, ring_account_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                access_token,
                refresh_token,
                token_type,
                expires_at,
                scope,
                ring_account_id,
                current_time,
            ),
        )
        return int(cursor.lastrowid)


def list_unclaimed_tokens(db_path: Path) -> list[dict[str, Any]]:
    """Return all currently valid unclaimed tokens ordered newest-first."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM unclaimed_tokens ORDER BY created_at DESC, id DESC"
        ).fetchall()
        return [dict(row) for row in rows]


def update_unclaimed_token_account_id(
    db_path: Path,
    unclaimed_id: int,
    ring_account_id: str,
) -> None:
    """Attach a resolved Ring account ID to an unclaimed token."""
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE unclaimed_tokens SET ring_account_id = ? WHERE id = ?",
            (ring_account_id, unclaimed_id),
        )


def claim_token(
    db_path: Path,
    unclaimed_id: int,
    ring_account_id: str | None,
    integration_status: str = "completed",
    now: float | None = None,
) -> dict[str, Any] | None:
    """Promote an unclaimed token into linked_accounts and remove it from unclaimed_tokens."""
    current_time = now if now is not None else time.time()
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM unclaimed_tokens WHERE id = ?",
            (unclaimed_id,),
        ).fetchone()
        if row is None:
            return None

        resolved_account_id = ring_account_id or row["ring_account_id"] or f"ring-user-{unclaimed_id}"
        conn.execute(
            """
            INSERT INTO linked_accounts (
                ring_account_id, access_token, refresh_token, token_type,
                expires_at, integration_status, linked_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ring_account_id) DO UPDATE SET
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                token_type = excluded.token_type,
                expires_at = excluded.expires_at,
                integration_status = excluded.integration_status,
                updated_at = excluded.updated_at
            """,
            (
                resolved_account_id,
                row["access_token"],
                row["refresh_token"],
                row["token_type"],
                row["expires_at"],
                integration_status,
                current_time,
                current_time,
            ),
        )
        conn.execute("DELETE FROM unclaimed_tokens WHERE id = ?", (unclaimed_id,))
        linked = conn.execute(
            "SELECT * FROM linked_accounts WHERE ring_account_id = ?",
            (resolved_account_id,),
        ).fetchone()
        return dict(linked) if linked else None


def get_active_account(db_path: Path) -> dict[str, Any] | None:
    """Return the most recently updated linked Ring account, if any."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM linked_accounts ORDER BY updated_at DESC, id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def update_account_tokens(
    db_path: Path,
    account_row_id: int,
    access_token: str,
    refresh_token: str,
    expires_in: int | float,
    now: float | None = None,
) -> None:
    """Persist refreshed OAuth tokens for a linked account."""
    current_time = now if now is not None else time.time()
    expires_at = current_time + float(expires_in)
    with get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE linked_accounts
            SET access_token = ?, refresh_token = ?, expires_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (access_token, refresh_token, expires_at, current_time, account_row_id),
        )


def record_event_queued(
    db_path: Path,
    event_id: str,
    device_id: str | None = None,
    event_type: str | None = None,
    now: float | None = None,
) -> bool:
    """Insert an incoming webhook event idempotently. Returns False if duplicate."""
    current_time = now if now is not None else time.time()
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO processed_events (
                event_id, device_id, event_type, status, created_at
            ) VALUES (?, ?, ?, 'queued', ?)
            """,
            (event_id, device_id, event_type, current_time),
        )
        return cursor.rowcount > 0


def update_event_status(
    db_path: Path,
    event_id: str,
    status: str,
    clip_path: str | None = None,
    error_message: str | None = None,
    now: float | None = None,
) -> None:
    """Update status and metadata of a tracked webhook event."""
    current_time = now if now is not None else time.time()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE processed_events
            SET status = ?,
                clip_path = COALESCE(?, clip_path),
                error_message = ?,
                processed_at = ?
            WHERE event_id = ?
            """,
            (status, clip_path, error_message, current_time, event_id),
        )


def get_dashboard_stats(
    db_path: Path,
    recent_hours: int = 24,
    now: float | None = None,
) -> dict[str, Any]:
    """Return account linking state and recent clip processing metrics for /home."""
    current_time = now if now is not None else time.time()
    recent_cutoff = current_time - (recent_hours * 3600)

    with get_connection(db_path) as conn:
        account = conn.execute(
            "SELECT ring_account_id, integration_status, linked_at, updated_at "
            "FROM linked_accounts ORDER BY updated_at DESC, id DESC LIMIT 1"
        ).fetchone()

        recent_row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM processed_events
            WHERE status IN ('downloaded', 'analyzed')
              AND created_at >= ?
            """,
            (recent_cutoff,),
        ).fetchone()

        total_row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM processed_events
            WHERE status IN ('downloaded', 'analyzed')
            """
        ).fetchone()

        unclaimed_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM unclaimed_tokens"
        ).fetchone()

        recent_events = conn.execute(
            """
            SELECT event_id, device_id, event_type, status, clip_path, created_at, processed_at
            FROM processed_events
            ORDER BY created_at DESC
            LIMIT 10
            """
        ).fetchall()

        return {
            "is_linked": account is not None,
            "linked_account": dict(account) if account else None,
            "pending_unclaimed_tokens": int(unclaimed_row["cnt"]),
            "recent_clips_count": int(recent_row["cnt"]),
            "total_clips_count": int(total_row["cnt"]),
            "recent_events": [dict(row) for row in recent_events],
        }
