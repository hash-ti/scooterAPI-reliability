"""Read-only questions about recorded data and system state.

This module is the single implementation behind both the CLI (`python -m scooter_api.cli`)
and the MCP server (`mcp_server.py`). Every function returns plain JSON-able data.
"""
import json
from datetime import datetime, timedelta, timezone

from .db import connect, db_path, init_db, row_to_dict


def _connect(path):
    """Queries may run before the app ever has (fresh checkout, MCP server first): create schema if missing."""
    return connect(init_db(path or db_path()))


def _event(row):
    d = dict(row)
    d["data"] = json.loads(d["data"] or "{}")
    return d


def _since(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="milliseconds")


def events(path=None, *, request_id=None, scooter_id=None, ride_id=None, type=None,
           failure_kind=None, since_minutes=None, limit=200):
    """Events matching all given filters, oldest first (the most recent `limit` of them)."""
    where, params = [], []
    for col, val in (("request_id", request_id), ("scooter_id", scooter_id), ("ride_id", ride_id),
                     ("type", type), ("failure_kind", failure_kind)):
        if val is not None:
            where.append(f"{col} = ?")
            params.append(str(val))
    if since_minutes is not None:
        where.append("ts >= ?")
        params.append(_since(since_minutes))
    sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "")
    sql = f"SELECT * FROM ({sql} ORDER BY id DESC LIMIT ?) ORDER BY id ASC"
    conn = _connect(path)
    try:
        return [_event(r) for r in conn.execute(sql, (*params, int(limit))).fetchall()]
    finally:
        conn.close()


def failure_counts(path=None, *, since_minutes=60):
    """{kind: count} of failed requests in the window (one per failed request, from `request.failed`)."""
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT failure_kind, COUNT(*) AS n FROM events WHERE type = 'request.failed' AND ts >= ? "
            "GROUP BY failure_kind ORDER BY n DESC", (_since(since_minutes),)).fetchall()
        return {"since_minutes": since_minutes, "counts": {r["failure_kind"]: r["n"] for r in rows}}
    finally:
        conn.close()


def recent_failures(path=None, *, since_minutes=60, limit=50, failure_kind=None):
    """The most recent `request.failed` events, newest last, each with its request path and args."""
    fails = events(path, type="request.failed", since_minutes=since_minutes, limit=limit, failure_kind=failure_kind)
    out = []
    for f in fails:
        received = events(path, request_id=f["request_id"], type="request.received", limit=1)
        req = received[0]["data"] if received else {}
        out.append({"ts": f["ts"], "request_id": f["request_id"], "failure_kind": f["failure_kind"],
                    "scooter_id": f["scooter_id"], "message": f["data"].get("message"),
                    "method": req.get("method"), "path": req.get("path"), "args": req.get("args"),
                    "fault_plan": req.get("fault_plan")})
    return out


def trace(request_id, path=None):
    """Everything one request did, in order, plus a summary."""
    evs = events(path, request_id=request_id, limit=10_000)
    received = next((e for e in evs if e["type"] == "request.received"), None)
    completed = next((e for e in evs if e["type"] == "request.completed"), None)
    return {
        "request_id": request_id,
        "found": bool(evs),
        "request": received and received["data"],
        "outcome": completed and {"status": completed["data"].get("status"),
                                  "failure_kind": completed["failure_kind"],
                                  "duration_ms": completed["data"].get("duration_ms")},
        "events": evs,
    }


def scooter(scooter_id, path=None, *, event_limit=100):
    """Current state of one scooter, its rides, the gateway's charges for those rides, and recent events."""
    scooter_id = str(scooter_id)
    conn = _connect(path)
    try:
        state = row_to_dict(conn.execute("SELECT * FROM scooters WHERE id = ?", (scooter_id,)).fetchone())
        rides = [row_to_dict(r) for r in conn.execute(
            "SELECT * FROM rides WHERE scooter_id = ? ORDER BY started_at", (scooter_id,)).fetchall()]
        charges = [row_to_dict(r) for r in conn.execute(
            "SELECT g.* FROM gateway_charges g JOIN rides r ON r.id = g.idempotency_key WHERE r.scooter_id = ? "
            "ORDER BY g.created_at", (scooter_id,)).fetchall()]
    finally:
        conn.close()
    return {"scooter_id": scooter_id, "found": state is not None, "state": state,
            "active_rides": [r for r in rides if r["status"] == "active"],
            "rides": rides, "gateway_charges": charges,
            "events": events(path, scooter_id=scooter_id, limit=event_limit)}


def all_scooters(path=None):
    conn = _connect(path)
    try:
        return [row_to_dict(r) for r in conn.execute(
            "SELECT s.*, (SELECT id FROM rides r WHERE r.scooter_id = s.id AND r.status = 'active') AS active_ride_id "
            "FROM scooters s ORDER BY s.id").fetchall()]
    finally:
        conn.close()


def check_invariants(path=None):
    """Cross-checks scooter state, rides and the gateway ledger. Empty `violations` means consistent."""
    conn = _connect(path)
    v = []
    try:
        for r in conn.execute(
                "SELECT s.id, s.is_reserved, COUNT(r.id) AS active FROM scooters s "
                "LEFT JOIN rides r ON r.scooter_id = s.id AND r.status = 'active' GROUP BY s.id"):
            if bool(r["is_reserved"]) != (r["active"] == 1):
                v.append({"rule": "reserved_iff_one_active_ride", "scooter_id": r["id"],
                          "is_reserved": bool(r["is_reserved"]), "active_rides": r["active"]})
        for r in conn.execute(
                "SELECT r.id, r.scooter_id, r.status, r.txn_id, g.txn_id AS ledger_txn FROM rides r "
                "LEFT JOIN gateway_charges g ON g.idempotency_key = r.id"):
            if r["status"] == "completed" and (r["ledger_txn"] is None or r["ledger_txn"] != r["txn_id"]):
                v.append({"rule": "completed_ride_has_matching_charge", "ride_id": r["id"],
                          "scooter_id": r["scooter_id"], "ride_txn": r["txn_id"], "ledger_txn": r["ledger_txn"]})
            if r["status"] == "payment_failed" and r["ledger_txn"] is not None:
                v.append({"rule": "failed_ride_has_no_charge", "ride_id": r["id"],
                          "scooter_id": r["scooter_id"], "ledger_txn": r["ledger_txn"]})
        for r in conn.execute(
                "SELECT g.idempotency_key, g.txn_id FROM gateway_charges g "
                "LEFT JOIN rides r ON r.id = g.idempotency_key WHERE r.id IS NULL"):
            v.append({"rule": "charge_belongs_to_a_ride", "idempotency_key": r["idempotency_key"],
                      "txn_id": r["txn_id"]})
        pending = [row_to_dict(r) for r in conn.execute(
            "SELECT id AS ride_id, scooter_id, last_payment_error, last_payment_at FROM rides "
            "WHERE status = 'active' AND last_payment_error IS NOT NULL")]
    finally:
        conn.close()
    return {"ok": not v, "violations": v,
            "pending_payment_reconciliation": pending}
