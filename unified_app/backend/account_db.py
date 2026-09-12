"""
backend/account_db.py
SQLite-backed account rotation and usage tracking.
Ensures fair distribution across all authenticated NotebookLM accounts in the pool
by selecting accounts with lowest batch counts and oldest last-used timestamp.
"""

import sqlite3
import time
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any

log = logging.getLogger("nlm.account_db")
DB_PATH = Path(__file__).parent.parent / "data" / "accounts_usage.db"


def _get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize accounts usage table if not present."""
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS profile_usage (
                profile_name TEXT PRIMARY KEY,
                email TEXT DEFAULT '',
                total_batches INTEGER DEFAULT 0,
                total_questions INTEGER DEFAULT 0,
                last_used_at REAL DEFAULT 0,
                total_errors INTEGER DEFAULT 0,
                active_tasks INTEGER DEFAULT 0
            )
        """)
        conn.execute("DELETE FROM profile_usage WHERE profile_name IS NULL OR profile_name = ''")
        conn.commit()


def reset_active_tasks():
    """Reset active_tasks count on server / job start to clear any orphaned states."""
    init_db()
    with _get_conn() as conn:
        conn.execute("UPDATE profile_usage SET active_tasks = 0")
        conn.execute("DELETE FROM profile_usage WHERE profile_name IS NULL OR profile_name = ''")
        conn.commit()


# Initialize on import
init_db()
reset_active_tasks()


def record_batch_start(profile_name: str, count: int = 1, email: str = ""):
    """Record that a profile started working on a batch."""
    now = time.time()
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO profile_usage (profile_name, email, total_batches, total_questions, last_used_at, active_tasks)
            VALUES (?, ?, 1, ?, ?, 1)
            ON CONFLICT(profile_name) DO UPDATE SET
                total_batches = total_batches + 1,
                total_questions = total_questions + excluded.total_questions,
                last_used_at = excluded.last_used_at,
                active_tasks = active_tasks + 1,
                email = CASE WHEN excluded.email != '' THEN excluded.email ELSE email END
        """, (profile_name, email or "", count, now))
        conn.commit()


def record_batch_complete(profile_name: str, success: bool = True):
    """Record batch completion and decrement active task count."""
    with _get_conn() as conn:
        err_inc = 0 if success else 1
        conn.execute("""
            INSERT INTO profile_usage (profile_name, total_errors, active_tasks)
            VALUES (?, ?, 0)
            ON CONFLICT(profile_name) DO UPDATE SET
                total_errors = total_errors + ?,
                active_tasks = MAX(0, active_tasks - 1)
        """, (profile_name, err_inc, err_inc))
        conn.commit()


def get_profiles_ordered_by_usage(available_profile_names: List[str]) -> List[str]:
    """
    Given a list of available (authenticated, non-rate-limited) profile names,
    sort them so that the least-used and oldest-used profiles are returned FIRST.
    This guarantees full round-robin / fair distribution across all profiles.
    """
    if not available_profile_names:
        return []
    
    init_db()
    with _get_conn() as conn:
        placeholders = ",".join(["?"] * len(available_profile_names))
        cursor = conn.execute(f"""
            SELECT profile_name, total_batches, active_tasks, last_used_at
            FROM profile_usage
            WHERE profile_name IN ({placeholders})
        """, available_profile_names)
        rows = {row["profile_name"]: dict(row) for row in cursor.fetchall()}
    
    # Profiles not yet in DB have 0 batches, 0 active tasks, 0 last_used_at
    def sort_key(name: str):
        info = rows.get(name, {"total_batches": 0, "active_tasks": 0, "last_used_at": 0})
        return (info.get("active_tasks", 0), info.get("total_batches", 0), info.get("last_used_at", 0))

    sorted_profiles = sorted(available_profile_names, key=sort_key)
    return sorted_profiles


def get_all_profile_stats() -> List[Dict[str, Any]]:
    """Return all profile stats for telemetry / API inspection."""
    init_db()
    with _get_conn() as conn:
        cursor = conn.execute("""
            SELECT profile_name, email, total_batches, total_questions, last_used_at, total_errors, active_tasks
            FROM profile_usage
            ORDER BY total_batches DESC, last_used_at DESC
        """)
        return [dict(row) for row in cursor.fetchall()]
