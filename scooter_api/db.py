"""SQLite storage: scooter state, rides, the simulated gateway's ledger, and the event log.

All of it lives in one file (default `var/scooter.db`, override with SCOOTER_DB) so an
agent can open it with any SQLite client. Schema is created on first connect and the
scooters table is seeded from `scooter_db.json` (the 2019 seed data) if it is empty.
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_FILE = REPO_ROOT / "scooter_db.json"
DEFAULT_DB = REPO_ROOT / "var" / "scooter.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS scooters (
    id          TEXT PRIMARY KEY,
    lat         REAL NOT NULL,
    lng         REAL NOT NULL,
    is_reserved INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT
);

-- One row per reservation. status: active -> completed | payment_failed
CREATE TABLE IF NOT EXISTS rides (
    id                 TEXT PRIMARY KEY,   -- also the payment idempotency key
    scooter_id         TEXT NOT NULL REFERENCES scooters(id),
    status             TEXT NOT NULL,
    start_lat          REAL, start_lng REAL,
    end_lat            REAL, end_lng   REAL,
    distance_m         INTEGER,
    cost_cents         INTEGER,
    txn_id             TEXT,
    last_payment_error TEXT,               -- set when a payment attempt was ambiguous (timeout / gateway error)
    last_payment_at    TEXT,
    started_at         TEXT NOT NULL,
    ended_at           TEXT,
    start_request_id   TEXT,
    end_request_id     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS rides_one_active_per_scooter ON rides(scooter_id) WHERE status = 'active';

-- The simulated payment gateway's own ledger ("what the bank thinks happened").
CREATE TABLE IF NOT EXISTS gateway_charges (
    idempotency_key TEXT PRIMARY KEY,
    txn_id          TEXT NOT NULL UNIQUE,
    amount_cents    INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    request_id      TEXT
);

-- Append-only event log. One row per thing that happened; see docs/EVENTS.md.
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,            -- ISO-8601 UTC
    request_id   TEXT,
    scooter_id   TEXT,
    ride_id      TEXT,
    type         TEXT NOT NULL,
    failure_kind TEXT,                     -- a FailureKind value, or NULL
    data         TEXT NOT NULL DEFAULT '{}'  -- JSON object
);
CREATE INDEX IF NOT EXISTS events_request ON events(request_id);
CREATE INDEX IF NOT EXISTS events_scooter ON events(scooter_id, id);
CREATE INDEX IF NOT EXISTS events_kind_ts ON events(failure_kind, ts);
CREATE INDEX IF NOT EXISTS events_ts      ON events(ts);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def db_path() -> Path:
    return Path(os.environ.get("SCOOTER_DB", DEFAULT_DB))


def connect(path=None) -> sqlite3.Connection:
    """Open a connection in autocommit mode; use `transaction()` for multi-statement writes."""
    path = Path(path or db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE takes the write lock up front, so concurrent writers serialize."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db(path=None, *, reset=False) -> Path:
    path = Path(path or db_path())
    if reset:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(path) + suffix)
            if p.exists():
                p.unlink()
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM scooters").fetchone()[0] == 0:
            seed = json.loads(SEED_FILE.read_text())
            with transaction(conn):
                conn.executemany(
                    "INSERT INTO scooters (id, lat, lng, is_reserved, updated_at) VALUES (?, ?, ?, ?, ?)",
                    [(str(s["id"]), float(s["lat"]), float(s["lng"]), int(bool(s["is_reserved"])), now_iso()) for s in seed],
                )
            # Seed scooters that start reserved get a matching ride so state is consistent from day one.
            for s in seed:
                if s["is_reserved"]:
                    conn.execute(
                        "INSERT INTO rides (id, scooter_id, status, start_lat, start_lng, started_at, start_request_id) "
                        "VALUES (?, ?, 'active', ?, ?, ?, 'seed')",
                        (f"ride_seed_{s['id']}", str(s["id"]), float(s["lat"]), float(s["lng"]), now_iso()),
                    )
    finally:
        conn.close()
    return path


def row_to_dict(row):
    return dict(row) if row is not None else None
