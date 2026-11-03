"""Concurrent reservations, the CLI, and the MCP server's tool surface."""
import asyncio
import json
import threading

import pytest

from scooter_api import cli


def test_concurrent_reservations_exactly_one_wins(app, db, assert_consistent):
    barrier = threading.Barrier(16)
    results = []

    def reserve():
        client = app.test_client()
        barrier.wait()
        results.append(client.get("/reservation/start?id=18").status_code)

    threads = [threading.Thread(target=reserve) for _ in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [200] + [409] * 15
    assert_consistent()


def test_cli_request_and_queries(db, capsys):
    assert cli.main(["--db", str(db), "request", "GET", "/reservation/start?id=999"]) == 0
    rid = json.loads(capsys.readouterr().out)["request_id"]
    cli.main(["--db", str(db), "trace", rid])
    assert json.loads(capsys.readouterr().out)["outcome"]["failure_kind"] == "SCOOTER_NOT_FOUND"
    cli.main(["--db", str(db), "failures"])
    assert json.loads(capsys.readouterr().out)["counts"] == {"SCOOTER_NOT_FOUND": 1}
    assert cli.main(["--db", str(db), "invariants"]) == 0


def test_cli_reset_requires_confirmation(db):
    assert cli.main(["--db", str(db), "reset"]) == 2


def test_mcp_server_exposes_tools(db, monkeypatch):
    pytest.importorskip("mcp")
    monkeypatch.setenv("SCOOTER_DB", str(db))
    import mcp_server

    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert {"failure_counts", "recent_failures", "scooter_history", "request_trace", "query_events",
            "check_invariants", "call_api", "replay_request", "run_tests", "list_scooters"} <= names

    mcp_server.call_api("GET", "/reservation/start?id=999")
    assert mcp_server.failure_counts()["counts"] == {"SCOOTER_NOT_FOUND": 1}
    assert mcp_server.scooter_history("18")["found"] is True


def test_queries_work_on_a_db_that_was_never_created(tmp_path):
    from scooter_api import queries
    fresh = tmp_path / "never_created.db"
    assert queries.check_invariants(fresh)["ok"] is True
    assert queries.failure_counts(fresh)["counts"] == {}


def test_cli_reset_refuses_default_db_without_extra_flag(monkeypatch):
    from scooter_api.db import DEFAULT_DB
    monkeypatch.delenv("SCOOTER_DB", raising=False)
    assert cli.main(["--db", str(DEFAULT_DB), "reset", "--yes"]) == 2


def test_cli_reset_wipes_a_throwaway_db(db, capsys):
    cli.main(["--db", str(db), "request", "GET", "/reservation/start?id=18"])
    assert cli.main(["--db", str(db), "reset", "--yes"]) == 0
    capsys.readouterr()
    cli.main(["--db", str(db), "scooter", "18"])
    assert json.loads(capsys.readouterr().out)["rides"] == []
