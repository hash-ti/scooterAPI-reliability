"""Command-line access to recorded data and system state. All output is JSON.

    python -m scooter_api.cli --help
    python -m scooter_api.cli failures --since-minutes 60
    python -m scooter_api.cli scooter 18
    python -m scooter_api.cli trace req_ab12cd34ef56
    python -m scooter_api.cli events --scooter 18 --type scooter.state_changed
    python -m scooter_api.cli invariants
    python -m scooter_api.cli request GET "/reservation/end?id=18&lat=1&lng=2" --fault payment=decline
    python -m scooter_api.cli replay req_ab12cd34ef56
    python -m scooter_api.cli reconcile --older-than-seconds 0
    python -m scooter_api.cli --db var/repro.db reset --yes   # fresh throwaway DB for reproducing a bug

Uses SCOOTER_DB (default var/scooter.db) unless --db is given.
"""
import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path

from . import queries
from .db import DEFAULT_DB, REPO_ROOT, db_path, init_db
from .events import EventLog, RequestEvents
from .faults import format_spec


def _out(obj):
    print(json.dumps(obj, indent=2, default=str))


def call_api(method, url, *, fault=None, path=None, request_id=None):
    """Issue one request in-process against the app (same DB, same event log). Returns a summary."""
    from .app import create_app
    app = create_app(path or db_path())
    headers = {}
    if fault:
        headers["X-Fault"] = fault
    if request_id:
        headers["X-Request-ID"] = request_id
    resp = app.test_client().open(url, method=method.upper(), headers=headers)
    return {"status": resp.status_code, "request_id": resp.headers.get("X-Request-ID"), "body": resp.get_json()}


def replay(request_id, path=None):
    """Re-issue a recorded request with the exact fault plan it originally got. Runs against CURRENT state."""
    rec = queries.events(path, request_id=request_id, type="request.received", limit=1)
    if not rec:
        return {"error": f"no request.received event for {request_id}"}
    data = rec[0]["data"]
    from urllib.parse import urlencode
    url = data["path"] + ("?" + urlencode(data["args"]) if data.get("args") else "")
    fault = format_spec(data.get("fault_plan") or {}) or None
    result = call_api(data["method"], url, fault=fault, path=path)
    return {"replayed": request_id, "method": data["method"], "url": url, "fault": fault, **result}


def run_tests(args=()):
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=600)
    return {"exit_code": proc.returncode, "passed": proc.returncode == 0,
            "output": (proc.stdout + proc.stderr)[-12000:]}


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m scooter_api.cli", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", help="path to the SQLite DB (default: $SCOOTER_DB or var/scooter.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("events", help="query the event log")
    e.add_argument("--request"); e.add_argument("--scooter"); e.add_argument("--ride")
    e.add_argument("--type"); e.add_argument("--kind", help="failure kind, e.g. PAYMENT_DECLINED")
    e.add_argument("--since-minutes", type=float); e.add_argument("--limit", type=int, default=200)

    f = sub.add_parser("failures", help="failure counts by kind, plus the most recent failures")
    f.add_argument("--since-minutes", type=float, default=60); f.add_argument("--limit", type=int, default=20)

    s = sub.add_parser("scooter", help="state, rides, charges and events for one scooter")
    s.add_argument("id")
    sub.add_parser("scooters", help="current state of every scooter")

    t = sub.add_parser("trace", help="everything one request did")
    t.add_argument("request_id")

    sub.add_parser("invariants", help="check scooters/rides/ledger consistency")

    r = sub.add_parser("request", help="issue one API request in-process (recorded like any other)")
    r.add_argument("method"); r.add_argument("url", help='e.g. "/reservation/start?id=18"')
    r.add_argument("--fault", help="X-Fault spec, e.g. payment=decline")

    rp = sub.add_parser("replay", help="re-issue a recorded request with the same fault plan")
    rp.add_argument("request_id")

    rc = sub.add_parser("reconcile", help="settle rides stuck after an ambiguous payment")
    rc.add_argument("--older-than-seconds", type=float, default=300)

    rs = sub.add_parser("reset", help="delete the DB and reseed from scooter_db.json")
    rs.add_argument("--yes", action="store_true", help="required: confirms you want to wipe recorded data")
    rs.add_argument("--including-default-db", action="store_true",
                    help="also required to wipe the default var/scooter.db (humans only; denied for agents)")

    tt = sub.add_parser("test", help="run the test suite")
    tt.add_argument("pytest_args", nargs="*")

    a = p.parse_args(argv)
    path = a.db or db_path()

    if a.cmd == "events":
        _out(queries.events(path, request_id=a.request, scooter_id=a.scooter, ride_id=a.ride, type=a.type,
                            failure_kind=a.kind, since_minutes=a.since_minutes, limit=a.limit))
    elif a.cmd == "failures":
        _out({**queries.failure_counts(path, since_minutes=a.since_minutes),
              "recent": queries.recent_failures(path, since_minutes=a.since_minutes, limit=a.limit)})
    elif a.cmd == "scooter":
        _out(queries.scooter(a.id, path))
    elif a.cmd == "scooters":
        _out(queries.all_scooters(path))
    elif a.cmd == "trace":
        _out(queries.trace(a.request_id, path))
    elif a.cmd == "invariants":
        res = queries.check_invariants(path)
        _out(res)
        return 0 if res["ok"] else 1
    elif a.cmd == "request":
        _out(call_api(a.method, a.url, fault=a.fault, path=path))
    elif a.cmd == "replay":
        _out(replay(a.request_id, path))
    elif a.cmd == "reconcile":
        from .app import create_app
        svc = create_app(path).extensions["scooter"]["service"]
        emit = RequestEvents(EventLog(path), "reconcile_" + uuid.uuid4().hex[:8])
        _out({"run_id": emit.request_id, "settled": svc.reconcile(emit, a.older_than_seconds)})
    elif a.cmd == "reset":
        if not a.yes:
            print("Refusing to wipe recorded data without --yes", file=sys.stderr)
            return 2
        if Path(path).resolve() == DEFAULT_DB.resolve() and not a.including_default_db:
            print(f"Refusing to wipe the default DB {DEFAULT_DB}: it holds the recorded evidence. "
                  "Use --db var/repro.db for a throwaway copy, or ask a human to pass --including-default-db.",
                  file=sys.stderr)
            return 2
        _out({"reset": str(init_db(path, reset=True))})
    elif a.cmd == "test":
        res = run_tests(a.pytest_args)
        print(res["output"])
        return res["exit_code"]
    return 0


if __name__ == "__main__":
    sys.exit(main())
