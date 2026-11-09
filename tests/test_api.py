"""Happy paths and validation failures for every endpoint."""
import pytest


def test_view_all_available_excludes_reserved(api):
    status, body, _ = api("/view_all_available")
    assert status == 200
    assert sorted(s["id"] for s in body) == ["14", "15", "18", "9"]


def test_root_lists_available(api):
    assert api("/")[1] == api("/view_all_available")[1]


def test_search_finds_scooters_in_radius(api):
    status, body, _ = api("/search?lat=23&lng=19&radius=1000")
    assert status == 200
    assert body == [{"id": "18", "lat": 23.0, "lng": 19.0}]


def test_search_excludes_reserved(api):
    api("/reservation/start?id=18")
    assert api("/search?lat=23&lng=19&radius=1000")[1] == []


@pytest.mark.parametrize("query, kind", [
    ("/search?lat=1&lng=2", "MISSING_PARAM"),
    ("/search?lat=abc&lng=2&radius=3", "INVALID_PARAM"),
    ("/search?lat=91&lng=2&radius=3", "INVALID_PARAM"),
    ("/search?lat=1&lng=-181&radius=3", "INVALID_PARAM"),
    ("/search?lat=nan&lng=2&radius=3", "INVALID_PARAM"),
    ("/reservation/start", "MISSING_PARAM"),
    ("/reservation/end?id=18&lat=1", "MISSING_PARAM"),
    ("/reservation/end?id=18&lat=x&lng=1", "INVALID_PARAM"),
])
def test_validation_failures_have_kinds(api, query, kind):
    status, body, rid = api(query)
    assert status == 422
    assert body["ok"] is False
    assert body["error"]["kind"] == kind
    assert body["request_id"] == rid


def test_full_ride_charges_and_moves_scooter(api, state, assert_consistent):
    status, body, _ = api("/reservation/start?id=18")
    assert status == 200 and body["ride_id"].startswith("ride_")
    ride_id = body["ride_id"]

    status, body, _ = api("/reservation/end?id=18&lat=23.01&lng=19")
    assert status == 200
    assert body["txn_id"].startswith("txn_")
    assert body["ride_id"] == ride_id
    assert body["distance_m"] > 1000 and body["cost_cents"] == body["distance_m"]

    s = state("18")
    assert s["state"]["is_reserved"] == 0
    assert (s["state"]["lat"], s["state"]["lng"]) == (23.01, 19.0)
    assert [r["status"] for r in s["rides"]] == ["completed"]
    assert [c["txn_id"] for c in s["gateway_charges"]] == [body["txn_id"]]
    # What the gateway actually charged, not just what the app reported.
    assert [c["amount_cents"] for c in s["gateway_charges"]] == [body["distance_m"]]
    assert_consistent()


def test_post_is_accepted_for_mutations(api):
    assert api("/reservation/start?id=18", method="POST")[0] == 200
    assert api("/reservation/end?id=18&lat=23&lng=19", method="POST")[0] == 200


def test_reserve_unknown_scooter(api):
    status, body, _ = api("/reservation/start?id=999")
    assert (status, body["error"]["kind"]) == (404, "SCOOTER_NOT_FOUND")


def test_reserve_twice_conflicts(api):
    api("/reservation/start?id=18")
    status, body, _ = api("/reservation/start?id=18")
    assert (status, body["error"]["kind"]) == (409, "SCOOTER_ALREADY_RESERVED")
    assert body["error"]["details"]["active_ride_id"].startswith("ride_")


def test_end_without_reservation(api):
    status, body, _ = api("/reservation/end?id=18&lat=1&lng=1")
    assert (status, body["error"]["kind"]) == (409, "RIDE_NOT_ACTIVE")


def test_end_unknown_scooter(api):
    status, body, _ = api("/reservation/end?id=999&lat=1&lng=1")
    assert (status, body["error"]["kind"]) == (404, "SCOOTER_NOT_FOUND")


def test_seeded_reserved_scooter_can_be_ended(api, assert_consistent):
    assert_consistent()  # seed data is consistent: scooter 33 starts reserved with an active ride
    assert api("/reservation/end?id=33&lat=24&lng=-28.001")[0] == 200
    assert_consistent()


def test_unknown_route_and_method(api):
    assert api("/nope")[1]["error"]["kind"] == "ROUTE_NOT_FOUND"
    status, body, _ = api("/search", method="DELETE")
    assert (status, body["error"]["kind"]) == (405, "METHOD_NOT_ALLOWED")


def test_request_id_is_honoured(client):
    r = client.get("/view_all_available", headers={"X-Request-ID": "req_mine"})
    assert r.headers["X-Request-ID"] == "req_mine"
