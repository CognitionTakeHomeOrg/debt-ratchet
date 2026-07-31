"""Persisted run state.

SQLite rather than memory for one reason: the budget ceiling has to survive a
crash. An orchestrator that forgets what it has already spent is not a budget
control, and an uncapped retry loop is the one realistic way to burn a fixed ACU
allocation by accident.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    issue_number INTEGER NOT NULL,
    fingerprint  TEXT NOT NULL,
    workstream   TEXT NOT NULL,
    rule         TEXT NOT NULL,
    area         TEXT NOT NULL,
    findings     INTEGER NOT NULL,
    status       TEXT NOT NULL,      -- queued running verifying pr_open merged escalated failed
    devin_status TEXT,               -- raw status_enum from the API
    acu_spent    REAL DEFAULT 0,
    pr_url       TEXT,
    structured   TEXT,
    session_url  TEXT,
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at   TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_open_fingerprint
    ON sessions(fingerprint) WHERE status NOT IN ('failed','escalated');
"""


# Columns added after the table shipped. sqlite has no ADD COLUMN IF NOT EXISTS,
# and the alternative -- dropping and recreating -- would take the ACU ledger
# with it, which is the one piece of state that must not be lost.
LATE_COLUMNS = {
    "progress_comment_id": "INTEGER",  # the rolling status comment we edit in place
    "last_event_id": "TEXT",           # newest Devin message already rendered
}


def connect(root: Path) -> sqlite3.Connection:
    con = sqlite3.connect(root / "ratchet" / "state.db")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    existing = {r["name"] for r in con.execute("PRAGMA table_info(sessions)")}
    for col, decl in LATE_COLUMNS.items():
        if col not in existing:
            con.execute(f"ALTER TABLE sessions ADD COLUMN {col} {decl}")
    con.commit()
    return con


def total_acu(con: sqlite3.Connection) -> float:
    return con.execute("SELECT COALESCE(SUM(acu_spent),0) FROM sessions").fetchone()[0]


def active_count(con: sqlite3.Connection) -> int:
    return con.execute(
        "SELECT COUNT(*) FROM sessions WHERE status IN ('queued','running','verifying')"
    ).fetchone()[0]


def record(con: sqlite3.Connection, **kw) -> None:
    cols = ",".join(kw)
    marks = ",".join("?" for _ in kw)
    con.execute(f"INSERT OR REPLACE INTO sessions ({cols}) VALUES ({marks})", list(kw.values()))
    con.commit()


def update(con: sqlite3.Connection, session_id: str, **kw) -> None:
    sets = ",".join(f"{k}=?" for k in kw)
    con.execute(
        f"UPDATE sessions SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE session_id=?",
        [*kw.values(), session_id],
    )
    con.commit()
