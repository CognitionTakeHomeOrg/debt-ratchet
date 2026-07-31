"""Persisted run state.

SQLite rather than memory for one reason: the budget ceiling has to survive a
crash. An orchestrator that forgets what it has already spent is not a budget
control, and an uncapped retry loop is the one realistic way to burn a fixed ACU
allocation by accident.
"""

from __future__ import annotations

import os
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
    # STATE_DB lets the container put the ledger on a named volume instead of a
    # bind-mounted host file. Bind-mounting a file that does not exist yet makes
    # Docker create a *directory* at that path, and the first clone of this repo
    # correctly has no state.db -- so the documented `docker compose up` failed
    # with "unable to open database file" on every fresh checkout.
    db = Path(os.environ["STATE_DB"]) if os.environ.get("STATE_DB") else root / "ratchet" / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    # The ledger has concurrent writers by design: the webhook launches, the
    # reconciler launches, and the poller settles, each in its own process. A
    # burst of webhook deliveries makes them collide, and the default behaviour
    # is to raise "database is locked" immediately -- turning contention into a
    # crash rather than a wait. WAL lets readers run while one writer commits.
    con = sqlite3.connect(db, timeout=15)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=15000")
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


def claim(con: sqlite3.Connection, **kw) -> bool:
    """Reserve a fingerprint before any money is spent. False if already taken.

    `idx_open_fingerprint` exists to stop two sessions working the same unit, but
    an index only helps if the write can fail: `INSERT OR REPLACE` resolves the
    conflict by deleting the row it collides with, so the guard silently did
    nothing. Worse, the insert happened *after* `create_session`, so by the time
    the ledger noticed a duplicate the ACUs were already committed.

    Creating an issue with two labels emits three webhook deliveries -- `opened`
    plus one `labeled` each -- and all three arrive within the same second, so no
    check that reads before writing can separate them. This is the write that
    decides, and only one caller can win it.
    """
    cols = ",".join(kw)
    marks = ",".join("?" for _ in kw)
    try:
        con.execute(f"INSERT INTO sessions ({cols}) VALUES ({marks})", list(kw.values()))
        con.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def promote(con: sqlite3.Connection, claim_id: str, session_id: str, url: str) -> None:
    """Swap a claim for the session it reserved."""
    con.execute(
        "UPDATE sessions SET session_id=?, session_url=?, status='running',"
        " updated_at=CURRENT_TIMESTAMP WHERE session_id=?",
        (session_id, url, claim_id),
    )
    con.commit()


def release(con: sqlite3.Connection, claim_id: str) -> None:
    """Give the fingerprint back when the session was never created."""
    con.execute("DELETE FROM sessions WHERE session_id=?", (claim_id,))
    con.commit()


def update(con: sqlite3.Connection, session_id: str, **kw) -> None:
    sets = ",".join(f"{k}=?" for k in kw)
    con.execute(
        f"UPDATE sessions SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE session_id=?",
        [*kw.values(), session_id],
    )
    con.commit()
