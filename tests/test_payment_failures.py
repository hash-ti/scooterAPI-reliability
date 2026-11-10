"""Payment can fail in every supported way, and state is never corrupted.

The property under test, for every failure mode: afterwards `check_invariants` is clean,
the gateway ledger holds at most one charge per ride, and no ride is charged without ending.
"""
import threading

from scooter_api import queries
from scooter_api.faults import FaultInjector, parse_spec

END = "/reservation/end?id=18&lat=23.01&lng=19"


def reserve(api):
    status, body, _ = api("/reservation/start?id=18")
    assert status == 200
    return body["ride_id"]


def test_decline_releases_scooter_and_charges_nothing(api, state, assert_consistent):
    ride_id = reserve(api)
    status, body, _ = api(END, fault="payment=decline")
    assert (status, body["error"]["kind"], body["error"]["retryable"]) == (402, "PAYMENT_DECLINED", False)

    s = state("18")
    assert s["state"]["is_reserved"] == 0, "a declined payment must not leave the scooter stuck reserved"
    assert s["rides"][0]["id"] == ride_id and s["rides"][0]["status"] == "payment_failed"
    assert s["gateway_charges"] == []
    assert_consistent()
    # the scooter is immediately rentable again
    assert api("/reservation/start?id=18")[0] == 200


def test_decline_ends_ride_at_end_location(api, state):
    reserve(api)
    _, _, request_id = api(END, fault="payment=decline")

    s = state("18")
    assert (s["state"]["lat"], s["state"]["lng"]) == (23.01, 19.0), "scooter is released where the ride ended"
    ride = s["rides"][0]
    assert (ride["end_lat"], ride["end_lng"]) == (23.01, 19.0)
    assert ride["distance_m"] > 1000 and ride["cost_cents"] == ride["distance_m"]
    assert ride["txn_id"] is None
    assert ride["ended_at"] is not None and ride["end_request_id"] == request_id


def test_decline_records_state_changes(api, state):
    ride_id = reserve(api)
    _, _, request_id = api(END, fault="payment=decline")

    evs = [e for e in state("18")["events"] if e["request_id"] == request_id]
    types = [e["type"] for e in evs]
    assert types.index("payment.declined") < types.index("ride.state_changed") < types.index("scooter.state_changed")
    ride_change = next(e for e in evs if e["type"] == "ride.state_changed")
    assert ride_change["ride_id"] == ride_id
    assert (ride_change["data"]["from"], ride_change["data"]["to"]) == ("active", "payment_failed")
    scooter_change = next(e for e in evs if e["type"] == "scooter.state_changed")
    assert scooter_change["data"]["to"] == {"is_reserved": False, "lat": 23.01, "lng": 19.0}


def test_retry_after_decline_is_rejected_and_not_charged(api, state, assert_consistent):
    reserve(api)
    api(END, fault="payment=decline")
    status, body, _ = api(END)
    assert status == 409, f"a declined ride must not be endable (and chargeable) again, got {status}: {body}"
    assert body["error"]["kind"] == "RIDE_NOT_ACTIVE"
    assert body["error"]["details"]["last_ride"]["status"] == "payment_failed"
    assert state("18")["gateway_charges"] == []
    assert_consistent()


def test_decline_after_timeout_still_ends_ride(api, state, assert_consistent):
    ride_id = reserve(api)
    assert api(END, fault="payment=timeout_before_charge")[0] == 504
    status, body, _ = api(END, fault="payment=decline")
    assert (status, body["error"]["kind"]) == (402, "PAYMENT_DECLINED")

    s = state("18")
    assert s["state"]["is_reserved"] == 0 and s["active_rides"] == []
    assert s["rides"][0]["id"] == ride_id and s["rides"][0]["status"] == "payment_failed"
    assert s["gateway_charges"] == []
    assert_consistent()


def test_timeout_before_charge_keeps_ride_active_then_retry_charges_once(api, state, assert_consistent):
    reserve(api)
    status, body, _ = api(END, fault="payment=timeout_before_charge")
    assert (status, body["error"]["kind"], body["error"]["retryable"]) == (504, "PAYMENT_TIMEOUT", True)
    s = state("18")
    assert s["state"]["is_reserved"] == 1 and s["gateway_charges"] == []
    assert s["active_rides"][0]["last_payment_error"] == "PAYMENT_TIMEOUT"
    assert_consistent()

    status, body, _ = api(END)
    assert status == 200
    s = state("18")
    assert len(s["gateway_charges"]) == 1 and s["gateway_charges"][0]["txn_id"] == body["txn_id"]
    assert s["state"]["is_reserved"] == 0
    assert_consistent()


def test_timeout_after_charge_retry_never_double_charges(api, state, assert_consistent):
    reserve(api)
    status, body, _ = api(END, fault="payment=timeout_after_charge")
    assert (status, body["error"]["kind"]) == (504, "PAYMENT_TIMEOUT")
    s = state("18")
    assert len(s["gateway_charges"]) == 1, "the gateway did charge"
    assert s["state"]["is_reserved"] == 1, "but we could not confirm it, so the ride is still open"
    first_txn = s["gateway_charges"][0]["txn_id"]

    status, body, _ = api(END)
    assert status == 200 and body["txn_id"] == first_txn
    s = state("18")
    assert len(s["gateway_charges"]) == 1
    assert s["rides"][0]["status"] == "completed" and s["rides"][0]["txn_id"] == first_txn
    assert_consistent()


def test_gateway_error_is_retryable(api, state, assert_consistent):
    reserve(api)
    status, body, _ = api(END, fault="payment=error")
    assert (status, body["error"]["kind"], body["error"]["retryable"]) == (502, "PAYMENT_GATEWAY_ERROR", True)
    assert state("18")["gateway_charges"] == []
    assert api(END)[0] == 200
    assert len(state("18")["gateway_charges"]) == 1
    assert_consistent()


def test_storage_crash_after_charge_then_retry_charges_once(api, state, assert_consistent):
    reserve(api)
    status, body, _ = api(END, fault="storage=crash_after_charge")
    assert (status, body["error"]["kind"], body["error"]["retryable"]) == (500, "STORAGE_ERROR", True)
    s = state("18")
    assert len(s["gateway_charges"]) == 1 and s["state"]["is_reserved"] == 1
    assert_consistent()

    status, body, _ = api(END)
    assert status == 200
    s = state("18")
    assert len(s["gateway_charges"]) == 1 and s["rides"][0]["status"] == "completed"
    assert_consistent()


def test_retry_after_success_is_rejected_and_not_charged(api, state):
    reserve(api)
    assert api(END)[0] == 200
    status, body, _ = api(END)
    assert (status, body["error"]["kind"]) == (409, "RIDE_NOT_ACTIVE")
    assert body["error"]["details"]["last_ride"]["status"] == "completed"
    assert len(state("18")["gateway_charges"]) == 1


def test_concurrent_end_retries_charge_exactly_once(app, state, assert_consistent, api):
    reserve(api)
    results = []

    def end():
        r = app.test_client().get(END)
        results.append(r.status_code)

    threads = [threading.Thread(target=end) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert 200 in results
    assert set(results) <= {200, 409}
    assert len(state("18")["gateway_charges"]) == 1
    assert_consistent()


def test_reconcile_settles_ambiguous_rides(api, db, state, app, assert_consistent):
    from scooter_api.events import EventLog, RequestEvents
    svc = app.extensions["scooter"]["service"]
    emit = RequestEvents(EventLog(db), "reconcile_test")

    # scooter 18: gateway charged, response lost -> reconcile completes the ride
    reserve(api)
    api(END, fault="payment=timeout_after_charge")
    # scooter 15: gateway never charged -> reconcile marks payment_failed and frees the scooter
    api("/reservation/start?id=15")
    api("/reservation/end?id=15&lat=0&lng=-129.01", fault="payment=timeout_before_charge")
    assert len(queries.check_invariants(db)["pending_payment_reconciliation"]) == 2

    assert svc.reconcile(emit, older_than_seconds=3600) == [], "recent attempts are left for the client to retry"
    settled = {r["scooter_id"]: r["outcome"] for r in svc.reconcile(emit, older_than_seconds=0)}
    assert settled == {"18": "completed", "15": "payment_failed"}
    assert state("18")["state"]["is_reserved"] == 0 and state("15")["state"]["is_reserved"] == 0
    res = assert_consistent()
    assert res["pending_payment_reconciliation"] == []


def test_invalid_fault_spec_is_a_typed_failure(api):
    status, body, _ = api("/view_all_available", fault="payment=explode")
    assert (status, body["error"]["kind"]) == (400, "INVALID_FAULT_SPEC")


def test_faults_disabled_in_production(make_app):
    client = make_app({"SCOOTER_ENV": "production"}).test_client()
    client.get("/reservation/start?id=18")
    r = client.get(END, headers={"X-Fault": "payment=decline"})
    assert r.status_code == 200


def test_env_fault_applies_to_every_request(make_app):
    client = make_app({"SCOOTER_FAULTS": "payment=decline"}).test_client()
    client.get("/reservation/start?id=18")
    assert client.get(END).get_json()["error"]["kind"] == "PAYMENT_DECLINED"


def test_seeded_random_faults_are_reproducible():
    env = {"SCOOTER_FAULT_RATE": "0.5", "SCOOTER_FAULT_SEED": "1234"}
    a, b = FaultInjector(env), FaultInjector(env)
    plans_a = [a.plan_for(None)[0] for _ in range(50)]
    plans_b = [b.plan_for(None)[0] for _ in range(50)]
    assert plans_a == plans_b
    assert any(plans_a) and not all(plans_a)


def test_parse_spec_round_trip():
    assert parse_spec("payment=decline, storage=crash_after_charge") == {
        "payment": "decline", "storage": "crash_after_charge"}
    assert parse_spec("") == {}
