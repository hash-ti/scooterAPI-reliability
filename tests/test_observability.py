"""Every request leaves a persisted, queryable record; failures carry kinds; replays reproduce."""
from scooter_api import cli, create_app, queries
from scooter_api.failures import FailureKind


def test_every_request_has_received_and_completed(api, db):
    for path in ["/view_all_available", "/reservation/start?id=18", "/reservation/start?id=18", "/nope"]:
        _, _, rid = api(path)
        types = [e["type"] for e in queries.events(db, request_id=rid)]
        assert types[0] == "request.received" and types[-1] == "request.completed", types


def test_failed_request_records_kind_everywhere(api, db):
    _, body, rid = api("/reservation/start?id=999")
    t = queries.trace(rid, db)
    assert t["outcome"] == {"status": 404, "failure_kind": "SCOOTER_NOT_FOUND",
                            "duration_ms": t["outcome"]["duration_ms"]}
    failed = [e for e in t["events"] if e["type"] == "request.failed"]
    assert len(failed) == 1 and failed[0]["failure_kind"] == body["error"]["kind"]


def test_all_recorded_failure_kinds_are_known(api, db):
    api("/reservation/start?id=999")
    api("/search?lat=1")
    api("/reservation/start?id=18")
    api("/reservation/end?id=18&lat=1&lng=1", fault="payment=decline")
    known = {k.value for k in FailureKind}
    kinds = {e["failure_kind"] for e in queries.events(db) if e["failure_kind"]}
    assert kinds and kinds <= known


def test_failure_counts_by_kind(api, db):
    api("/reservation/start?id=999")
    api("/reservation/start?id=998")
    api("/search")
    assert queries.failure_counts(db, since_minutes=60)["counts"] == {"SCOOTER_NOT_FOUND": 2, "MISSING_PARAM": 1}
    assert queries.failure_counts(db, since_minutes=0)["counts"] == {}


def test_scooter_history_follows_a_scooter_end_to_end(api, db):
    api("/reservation/start?id=18")
    api("/reservation/end?id=18&lat=23.01&lng=19")
    h = queries.scooter("18", db)
    changes = [e["data"] for e in h["events"] if e["type"] == "scooter.state_changed"]
    assert [c["to"]["is_reserved"] for c in changes] == [True, False]
    types = [e["type"] for e in h["events"]]
    for t in ["ride.state_changed", "payment.attempted", "payment.succeeded"]:
        assert t in types


def test_state_changed_events_record_actual_db_state(api, db):
    """`to` is re-read from the DB after commit, so it shows what was really stored."""
    api("/reservation/start?id=18")
    api("/reservation/end?id=18&lat=23.01&lng=19")
    last = [e for e in queries.events(db, scooter_id="18", type="scooter.state_changed")][-1]
    assert last["data"]["to"] == {"is_reserved": False, "lat": 23.01, "lng": 19.0}


def test_records_survive_restart(db, api):
    _, _, rid = api("/reservation/start?id=999")
    create_app(db, env={})  # a "restart": new app, same DB file
    assert queries.trace(rid, db)["outcome"]["failure_kind"] == "SCOOTER_NOT_FOUND"


def test_unhandled_exception_is_internal_error_with_traceback(app, client, db, monkeypatch):
    svc = app.extensions["scooter"]["service"]
    monkeypatch.setattr(svc, "list_available", lambda: 1 / 0)
    r = client.get("/view_all_available")
    assert r.status_code == 500 and r.get_json()["error"]["kind"] == "INTERNAL_ERROR"
    exc = queries.events(db, request_id=r.headers["X-Request-ID"], type="exception")[0]
    assert exc["data"]["exc_type"] == "ZeroDivisionError" and "Traceback" in exc["data"]["traceback"]


def test_fault_plan_is_recorded_and_replay_reproduces_it(api, db):
    api("/reservation/start?id=18")
    _, body, rid = api("/reservation/end?id=18&lat=23.01&lng=19", fault="payment=timeout_before_charge")
    received = queries.events(db, request_id=rid, type="request.received")[0]
    assert received["data"]["fault_plan"] == {"payment": "timeout_before_charge"}

    again = cli.replay(rid, db)
    assert again["fault"] == "payment=timeout_before_charge"
    assert again["status"] == 504 and again["body"]["error"]["kind"] == body["error"]["kind"]


def test_recent_failures_include_request_inputs(api, db):
    api("/reservation/start?id=999")
    f = queries.recent_failures(db)[-1]
    assert f["failure_kind"] == "SCOOTER_NOT_FOUND" and f["args"] == {"id": "999"}


def test_invariants_detect_corruption(db, api):
    from scooter_api.db import connect
    api("/view_all_available")  # make sure the DB exists
    conn = connect(db)
    conn.execute("UPDATE scooters SET is_reserved = 1 WHERE id = '18'")  # reserved with no ride
    conn.close()
    res = queries.check_invariants(db)
    assert not res["ok"]
    assert res["violations"] == [{"rule": "reserved_iff_one_active_ride", "scooter_id": "18",
                                  "is_reserved": True, "active_rides": 0}]
