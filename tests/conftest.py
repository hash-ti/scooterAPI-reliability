import pytest

from scooter_api import create_app, queries


@pytest.fixture
def db(tmp_path):
    """A fresh, seeded DB per test. Seed: scooters 15, 18, 9, 14 available; 33 reserved."""
    return tmp_path / "scooter.db"


@pytest.fixture
def make_app(db):
    def _make(env=None):
        return create_app(db, env=env or {})
    return _make


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def api(client):
    """api(method_path, fault=None) -> (status, json, request_id)."""
    def _call(path, fault=None, method="GET"):
        headers = {"X-Fault": fault} if fault else {}
        r = client.open(path, method=method, headers=headers)
        return r.status_code, r.get_json(), r.headers["X-Request-ID"]
    return _call


@pytest.fixture
def state(db):
    """state(scooter_id) -> queries.scooter(...) for the test DB."""
    return lambda sid: queries.scooter(sid, db)


@pytest.fixture
def assert_consistent(db):
    def _check():
        res = queries.check_invariants(db)
        assert res["ok"], res["violations"]
        return res
    return _check
