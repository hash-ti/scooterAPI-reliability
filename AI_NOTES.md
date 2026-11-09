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

### 4. Claimed every test failed for the same reason without reading them (bug-1 session, 2026-11-09)
**What the AI did:** While fixing "scooter 18 still reserved after ending the ride" (bug `e7d46ff`, fix `2da16d9`), it ran `pytest | tail -20`. That showed one full traceback (`is_reserved` `1 == 0`) and 11 truncated summary lines. It then told the user "12 tests failed on exactly this".
**How it was caught:** The user asked what that sentence meant. Asked to explain, the AI admitted it had only inferred the cause, because restoring one line made all 12 pass. It re-planted the bug and ran `pytest --tb=line`. All 12 failures were the stuck-reservation symptom: 8 direct `is_reserved`/event assertions and 4 `reserved_iff_one_active_ride` invariant violations. So the claim was true, but it had not been checked when it was made.
**Fix:** None needed to the code. The claim was verified after the fact.
**Lesson:** "they all pass after the fix" does not show why each one failed. Read every failure (`--tb=line`) before saying what they have in common.

### 5. Added a regression test that duplicated an existing one (bug-1 session, 2026-11-09)
**What the AI did:** Following AGENTS.md §5 ("write the failing test"), it added `test_ended_scooter_is_available_again` before checking existing coverage. The test made the same start/end calls on scooter 18 as `test_full_ride_charges_and_moves_scooter`. Its extra checks (listed in `/view_all_available`, reservable again) depended only on `is_reserved`. That is already asserted there, and re-reserving is already covered by `test_decline_releases_scooter_and_charges_nothing`. Entry 3 above had already recorded that this exact planted bug fails 11 existing tests.
**How it was caught:** The user asked whether the test was necessary. Comparing it with the existing tests showed it added nothing, so it was removed before committing.
**Fix:** Test deleted. The fix commit says existing tests cover the bug.
**Lesson:** "write the failing test" means make sure a failing test exists. First run the suite against the bug. If existing tests already fail for the right reason, cite them instead of adding a duplicate. Also read this file: the answer was already in entry 3.

### 6. Gave wrong bug-planting instructions from memory (rehearsal prep, 2026-11-09)
**What the AI did:** Told the user that planting the "still reserved" bug meant deleting `is_reserved = 0, ` from the UPDATE in `_finish_ride` *and* removing "the first `?` value in that line's tuple so the counts still match." But `is_reserved = 0` is a literal, not a placeholder. Following the instruction would have left 4 `?` and 3 values, so every end-ride would crash with a SQLite binding error, not the intended symptom.
**How it was caught:** The user said they didn't understand the sentence. The AI then grepped the actual line (`service.py:174`) and saw the tuple needs no change.
**Fix:** Corrected the instructions: only the SQL string changes.
**Lesson:** Before telling someone exactly what to edit, open the line. An instruction can be confidently wrong in a way that plants a different bug than intended.

### 7. Repeated entry 5: added a duplicate regression test (bug-2 session, 2026-11-09)
**What the AI did:** While fixing "charged way too much for a short ride on scooter 18" (an uncommitted `* 100` in `calculate_cost`; ride `ride_36bcb9130f15` was charged 109600 cents for 1096 m), it added `test_short_ride_charges_one_cent_per_metre`. It ran the suite and saw that `test_full_ride_charges_and_moves_scooter` *also* failed on `cost_cents == distance_m`, but kept the new test anyway and did not mention the overlap. The only new thing in the test was a check of the gateway's `amount_cents`. Replaying the exact short ride added nothing, because the cost does not depend on ride history.
**How it was caught:** The user asked why the test was needed if the existing one already failed.
**Fix:** Deleted the duplicate test. Added the one useful assertion (gateway `amount_cents == distance_m`) to `test_full_ride_charges_and_moves_scooter`. Verified that it catches the bug on its own: with the bug re-planted and the existing cost assertion disabled, the new line still failed.
**Lesson:** Entry 5 already said this, and the AI did not read this file before starting. Read AI_NOTES.md at the start of every debugging task. Strengthening an existing test (adding assertions, never loosening them) is fine. Check that the added assertion fails on the bug by itself.

<!-- Template for new entries:
### N. <one-line summary>
**What the AI did:**
**How it was caught:** (test / log / review / gut feeling)
**Fix:**
**Lesson:**
-->
