"""Append-only event log.

Events are written on their own autocommit connection, so they persist even when the
domain transaction they describe rolls back. That is the point: the log records what
the system *tried* and *decided*, not only what it managed to commit.
"""
import json
import logging
import sqlite3

from .db import connect, now_iso

log = logging.getLogger("scooter_api.events")


class EventLog:
    def __init__(self, path):
        self.path = path

    def record(self, type: str, *, request_id=None, scooter_id=None, ride_id=None,
               failure_kind=None, **data) -> int:
        kind = getattr(failure_kind, "value", failure_kind)
        payload = json.dumps(data, default=str, sort_keys=True)
        conn = connect(self.path)
        try:
            cur = conn.execute(
                "INSERT INTO events (ts, request_id, scooter_id, ride_id, type, failure_kind, data) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now_iso(), request_id, None if scooter_id is None else str(scooter_id), ride_id, type, kind, payload),
            )
            event_id = cur.lastrowid
        except sqlite3.Error:
            # Never let logging take down a request; fall back to the process log.
            log.exception("failed to persist event %s", type)
            event_id = -1
        finally:
            conn.close()
        log.info(json.dumps({"type": type, "request_id": request_id, "scooter_id": scooter_id,
                             "ride_id": ride_id, "failure_kind": kind, **data}, default=str))
        return event_id


class RequestEvents:
    """An EventLog bound to one request id, so service code only names what happened."""

    def __init__(self, log: EventLog, request_id: str):
        self.log = log
        self.request_id = request_id

    def __call__(self, type: str, **kw) -> int:
        return self.log.record(type, request_id=self.request_id, **kw)
