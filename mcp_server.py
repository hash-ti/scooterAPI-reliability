"""MCP server: lets an agent ask the scooter system questions as tool calls.

Run (stdio):  .venv/Scripts/python mcp_server.py      (Windows)
              .venv/bin/python mcp_server.py          (macOS/Linux)
Registered for Claude Code in .mcp.json. Every tool is a thin wrapper over
scooter_api.queries / scooter_api.cli, so the CLI gives identical answers.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from scooter_api import cli, queries  # noqa: E402
from scooter_api.failures import FailureKind  # noqa: E402

mcp = MCPServer(
    name="scooter-ops",
    instructions=(
        "Tools over the scooter API's recorded data (SQLite event log + state). Typical debugging flow: "
        "failure_counts / recent_failures -> scooter_history(id) or request_trace(request_id) -> "
        "check_invariants -> call_api / replay_request to reproduce -> run_tests."
    ),
)


@mcp.tool()
def failure_counts(since_minutes: float = 60) -> dict:
    """Count failed requests by failure kind over the last N minutes."""
    return queries.failure_counts(since_minutes=since_minutes)


@mcp.tool()
def recent_failures(since_minutes: float = 60, limit: int = 20, failure_kind: str | None = None) -> list:
    """Most recent failed requests (newest last) with kind, message, path, args and fault plan."""
    return queries.recent_failures(since_minutes=since_minutes, limit=limit, failure_kind=failure_kind)


@mcp.tool()
def scooter_history(scooter_id: str, event_limit: int = 100) -> dict:
    """Current DB state of one scooter, all its rides, the gateway charges for them, and its recent events."""
    return queries.scooter(scooter_id, event_limit=event_limit)


@mcp.tool()
def list_scooters() -> list:
    """Current state of every scooter, including the id of its active ride if any."""
    return queries.all_scooters()


@mcp.tool()
def request_trace(request_id: str) -> dict:
    """Every event one request produced, in order, with its inputs and outcome."""
    return queries.trace(request_id)


@mcp.tool()
def query_events(request_id: str | None = None, scooter_id: str | None = None, ride_id: str | None = None,
                 type: str | None = None, failure_kind: str | None = None,
                 since_minutes: float | None = None, limit: int = 200) -> list:
    """Filter the raw event log. All filters are ANDed. Event types are listed in docs/EVENTS.md."""
    return queries.events(request_id=request_id, scooter_id=scooter_id, ride_id=ride_id, type=type,
                          failure_kind=failure_kind, since_minutes=since_minutes, limit=limit)


@mcp.tool()
def check_invariants() -> dict:
    """Cross-check scooters, rides and the payment ledger. Lists every inconsistency found."""
    return queries.check_invariants()


@mcp.tool()
def list_failure_kinds() -> list:
    """The fixed set of failure kind identifiers."""
    return [k.value for k in FailureKind]


@mcp.tool()
def call_api(method: str, url: str, fault: str | None = None) -> dict:
    """Issue one request to the app in-process (recorded like any other). `url` like
    "/reservation/end?id=18&lat=1&lng=2"; `fault` like "payment=decline". Mutates the local DB."""
    return cli.call_api(method, url, fault=fault)


@mcp.tool()
def replay_request(request_id: str) -> dict:
    """Re-issue a recorded request with the exact fault plan it got the first time (against current state)."""
    return cli.replay(request_id)


@mcp.tool()
def run_tests(pytest_args: list[str] | None = None) -> dict:
    """Run the pytest suite (optionally with extra args such as ["-k", "payment"]). Returns exit code and output."""
    return cli.run_tests(pytest_args or [])


if __name__ == "__main__":
    mcp.run("stdio")
