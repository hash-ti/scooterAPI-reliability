# AI_NOTES: where the AI was confidently wrong

These first entries come from the build session (Claude Code, Nov 2026). Add your own below as you work with the repo.

### 1. The MCP server crashed on a fresh checkout ("no such table: events")
**What the AI did:** Wrote `queries.py` assuming the DB schema already existed, because the Flask app creates it on startup. The in-process MCP test passed.
**How it was caught:** Starting the MCP server for real over stdio and calling `check_invariants` failed. The stdio client starts the server with a minimal environment, so `SCOOTER_DB` from the shell never reached it. The server fell back to `var/scooter.db`, which nothing had created yet. The unit test only passed because its fixture DB had already been initialized by the app.
**Fix:** queries create the schema on demand. Regression test: `test_queries_work_on_a_db_that_was_never_created`.
**Lesson:** an in-process test of a tool is not a test of how the tool actually starts. Run the real transport at least once.

### 2. Reached for an API that no longer exists
**What the AI did:** Was about to write `from mcp.server.fastmcp import FastMCP` from memory. In `mcp` 2.x that module is gone (renamed to `mcp.server.mcpserver.MCPServer`).
**How it was caught:** Inspected the installed package before writing the server. The import error even links the migration guide.
**Lesson:** for any dependency installed today, check the installed version's API, not the one the model remembers.

### 3. A test that tested nothing
**What the AI did:** In `test_seeded_random_faults_are_reproducible` it added `seq()`, which built a fresh injector and drew one plan. `seq() == seq()` was trivially true and looked like extra coverage.
**How it was caught:** Re-reading the test file after the suite went green on the first run (49/49 passing on the first try is a smell). Deleted it. Then mutation-checked the suite by planting the activity's example bug (removing `is_reserved = 0` from the ride-release UPDATE): 11 tests failed and `cli invariants` flagged scooter 18, so the real tests do bite.
**Lesson:** a green first run proves nothing until you have seen the suite fail for a planted bug.

<!-- Template for new entries:
### N. <one-line summary>
**What the AI did:**
**How it was caught:** (test / log / review / gut feeling)
**Fix:**
**Lesson:**
-->
