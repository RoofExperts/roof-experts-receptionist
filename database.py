"""
Call logging database for Roof Experts AI Receptionist.
Uses SQLite for zero-config persistence on Render with disk mount.
"""

import os
import sqlite3
import json
from datetime import datetime, timezone
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", "/opt/render/project/data/calls.db")

# Fallback for local dev
if not os.path.exists(os.path.dirname(DB_PATH)):
    DB_PATH = os.path.join(os.path.dirname(__file__), "calls.db")


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create tables if they don't exist."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_sid TEXT UNIQUE,
                caller_number TEXT,
                caller_name TEXT DEFAULT '',
                started_at TEXT NOT NULL,
                ended_at TEXT,
                duration_seconds INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                outcome TEXT DEFAULT '',
                recording_url TEXT DEFAULT '',
                recording_duration INTEGER DEFAULT 0,
                is_spam INTEGER DEFAULT 0,
                is_new_customer INTEGER DEFAULT 1,
                lead_id TEXT DEFAULT '',
                estimator TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS call_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                event_data TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (call_id) REFERENCES calls(id)
            );

            CREATE TABLE IF NOT EXISTS blocked_numbers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_number TEXT UNIQUE NOT NULL,
                reason TEXT DEFAULT '',
                blocked_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_calls_started ON calls(started_at);
            CREATE INDEX IF NOT EXISTS idx_calls_status ON calls(status);
            CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_number);
            CREATE INDEX IF NOT EXISTS idx_events_call ON call_events(call_id);
            CREATE INDEX IF NOT EXISTS idx_blocked_number ON blocked_numbers(phone_number);
        """)
    print(f"[database] Initialized at {DB_PATH}")


# ââ Call CRUD ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def log_call_start(call_sid: str, caller_number: str, caller_name: str = "",
                   is_spam: bool = False) -> int:
    """Log a new inbound call. Returns the call ID."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO calls (call_sid, caller_number, caller_name, started_at,
               status, is_spam, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (call_sid, caller_number, caller_name, now,
             "spam_blocked" if is_spam else "active", int(is_spam), now)
        )
        return cursor.lastrowid


def update_call_end(call_sid: str, status: str = "completed",
                    outcome: str = "", duration: int = 0):
    """Mark a call as ended with final status and outcome."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """UPDATE calls SET ended_at = ?, status = ?, outcome = ?,
               duration_seconds = ? WHERE call_sid = ?""",
            (now, status, outcome, duration, call_sid)
        )


def update_call_recording(call_sid: str, recording_url: str,
                          recording_duration: int = 0):
    """Attach recording URL to a call."""
    with get_db() as conn:
        conn.execute(
            """UPDATE calls SET recording_url = ?, recording_duration = ?
               WHERE call_sid = ?""",
            (recording_url, recording_duration, call_sid)
        )


def update_call_lead(call_sid: str, lead_id: str, estimator: str = ""):
    """Attach CRM lead info to a call."""
    with get_db() as conn:
        conn.execute(
            "UPDATE calls SET lead_id = ?, estimator = ? WHERE call_sid = ?",
            (lead_id, estimator, call_sid)
        )


def log_event(call_id: int, event_type: str, event_data: dict = None):
    """Log a call event (greeting, lead_captured, transfer, etc.)."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO call_events (call_id, event_type, event_data, created_at)
               VALUES (?, ?, ?, ?)""",
            (call_id, event_type, json.dumps(event_data or {}), now)
        )


# ââ Query Functions (for Dashboard API) ââââââââââââââââââââââââââââââââââââââ

def get_calls(limit: int = 100, offset: int = 0, date_from: str = None,
              date_to: str = None, status: str = None):
    """Get calls with optional filters."""
    query = "SELECT * FROM calls WHERE 1=1"
    params = []

    if date_from:
        query += " AND started_at >= ?"
        params.append(date_from)
    if date_to:
        query += " AND started_at <= ?"
        params.append(date_to)
    if status:
        query += " AND status = ?"
        params.append(status)

    query += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def get_call_by_sid(call_sid: str) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM calls WHERE call_sid = ?", (call_sid,)
        ).fetchone()
        return dict(row) if row else None


def get_call_events(call_id: int) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM call_events WHERE call_id = ? ORDER BY created_at",
            (call_id,)
        ).fetchall()
        return [dict(row) for row in rows]


def get_stats(date_from: str = None, date_to: str = None) -> dict:
    """Get aggregate call statistics."""
    where = "WHERE 1=1"
    params = []
    if date_from:
        where += " AND started_at >= ?"
        params.append(date_from)
    if date_to:
        where += " AND started_at <= ?"
        params.append(date_to)

    with get_db() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM calls {where}", params
        ).fetchone()[0]

        spam = conn.execute(
            f"SELECT COUNT(*) FROM calls {where} AND is_spam = 1", params
        ).fetchone()[0]

        leads = conn.execute(
            f"SELECT COUNT(*) FROM calls {where} AND lead_id != ''", params
        ).fetchone()[0]

        transfers = conn.execute(
            f"SELECT COUNT(*) FROM calls {where} AND outcome = 'transferred'",
            params
        ).fetchone()[0]

        emergencies = conn.execute(
            f"SELECT COUNT(*) FROM calls {where} AND outcome = 'emergency'",
            params
        ).fetchone()[0]

        avg_dur = conn.execute(
            f"""SELECT AVG(duration_seconds) FROM calls {where}
                AND is_spam = 0 AND duration_seconds > 0""",
            params
        ).fetchone()[0] or 0

        silence = conn.execute(
            f"SELECT COUNT(*) FROM calls {where} AND status = 'silence_timeout'",
            params
        ).fetchone()[0]

        completed = conn.execute(
            f"SELECT COUNT(*) FROM calls {where} AND status = 'completed'",
            params
        ).fetchone()[0]

        # Hourly distribution
        hourly = conn.execute(
            f"""SELECT CAST(strftime('%H', started_at) AS INTEGER) as hour,
                COUNT(*) as count
                FROM calls {where}
                GROUP BY hour ORDER BY hour""",
            params
        ).fetchall()

        # Daily volume (last 30 days)
        daily = conn.execute(
            f"""SELECT DATE(started_at) as day, COUNT(*) as count,
                SUM(is_spam) as spam_count
                FROM calls {where}
                GROUP BY day ORDER BY day DESC LIMIT 30""",
            params
        ).fetchall()

        # Outcome breakdown
        outcomes = conn.execute(
            f"""SELECT outcome, COUNT(*) as count FROM calls {where}
                AND outcome != '' GROUP BY outcome ORDER BY count DESC""",
            params
        ).fetchall()

        return {
            "total_calls": total,
            "spam_blocked": spam,
            "leads_captured": leads,
            "transfers": transfers,
            "emergencies": emergencies,
            "avg_duration_seconds": round(avg_dur),
            "silence_timeouts": silence,
            "completed_calls": completed,
            "legitimate_calls": total - spam,
            "hourly": [dict(r) for r in hourly],
            "daily": [dict(r) for r in daily],
            "outcomes": [dict(r) for r in outcomes],
        }


# ââ Blocked Numbers ââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def get_blocked_numbers() -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM blocked_numbers ORDER BY blocked_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]


def block_number(phone_number: str, reason: str = "") -> bool:
    """Block a phone number. Returns True if newly blocked, False if already blocked."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO blocked_numbers (phone_number, reason, blocked_at) VALUES (?, ?, ?)",
                (phone_number, reason, now)
            )
            return True
        except sqlite3.IntegrityError:
            return False


def unblock_number(phone_number: str) -> bool:
    """Unblock a phone number. Returns True if removed, False if not found."""
    with get_db() as conn:
        cursor = conn.execute(
            "DELETE FROM blocked_numbers WHERE phone_number = ?",
            (phone_number,)
        )
        return cursor.rowcount > 0


def is_number_blocked(phone_number: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM blocked_numbers WHERE phone_number = ?",
            (phone_number,)
        ).fetchone()
        return row is not None
