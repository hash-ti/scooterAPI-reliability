# AGENTS.md: start here

You are working in a small scooter-rental HTTP API (Python 3.10+, Flask, SQLite). It was a 2019 project, rebuilt in 2026 so that an agent with no prior context can debug it from what the system records. This file assumes you know nothing else about it.

## 1. Setup (once)

```bash
python -m venv .venv
# Windows:      .venv/Scripts/python -m pip install -r requirements.txt
# macOS/Linux:  .venv/bin/python -m pip install -r requirements.txt
```

Below, `PY` means `.venv/Scripts/python` (Windows) or `.venv/bin/python` (macOS/Linux).

## 2. Run, test, query

| Task | Command |
|---|---|
| **Run all tests** (the one command) | `PY -m pytest` |
| Run the server on http://localhost:8080 | `PY app.py` |
| Make one API call without a server (recorded like a real one) | `PY -m scooter_api.cli request GET "/reservation/start?id=18"` |
| Failure counts by kind, last hour | `PY -m scooter_api.cli failures` |
| Everything about one scooter | `PY -m scooter_api.cli scooter 18` |
| Everything one request did | `PY -m scooter_api.cli trace <request_id>` |
| Consistency check of all state | `PY -m scooter_api.cli invariants` (exit code 1 if violated) |
| Re-run a recorded request with the same injected fault | `PY -m scooter_api.cli replay <request_id>` |
| All CLI commands | `PY -m scooter_api.cli --help` |

The same queries are available as MCP tools (server `scooter-ops`, see §6).

## 3. Where the recorded data is

Everything is in **one SQLite file: `var/scooter.db`** (gitignored; created and seeded from `scooter_db.json` on first use). Set `SCOOTER_DB=/some/other.db` or pass `--db` to the CLI to use another file. Tables:

- `scooters`: current state (`id`, `lat`, `lng`, `is_reserved`).
- `rides`: one row per reservation. `status` is `active` → `completed` | `payment_failed`. The ride id is also the payment idempotency key.
- `gateway_charges`: the **simulated payment gateway's own ledger**: what was actually charged, independent of what the app believes.
- `events`: append-only log of every request and every decision. Schema and event types: [docs/EVENTS.md](docs/EVENTS.md).

Failure kinds (stable identifiers on every failed response and failure event): [docs/FAILURES.md](docs/FAILURES.md).

## 4. How it works

```
app.py                  entry point (python app.py / gunicorn app:app)
scooter_api/app.py      routes + request lifecycle (request ids, request.* events, error -> typed response)
scooter_api/service.py  domain logic: reserve, end ride, pay, reconcile  <- most bugs will be here
scooter_api/payments.py simulated gateway: idempotent charges, injectable failures
scooter_api/faults.py   fault injection (X-Fault header / env / seeded random)
scooter_api/failures.py FailureKind enum + AppError
scooter_api/db.py       schema, connections, seeding
scooter_api/events.py   event log writer
scooter_api/queries.py  read-only questions (shared by CLI and MCP)
scooter_api/cli.py      CLI
mcp_server.py           MCP server
tests/                  pytest suite
```

**Endpoints** (GET or POST): `/` and `/view_all_available`, `/search?lat=&lng=&radius=` (metres), `/reservation/start?id=`, `/reservation/end?id=&lat=&lng=`, `/health`. Every response has an `X-Request-ID` header; that id is the key into the event log.

**State machine and invariants:**

```
available --start--> reserved + ride 'active'
ride 'active' --end, payment ok--------> 'completed'       + scooter released at end location
ride 'active' --end, payment declined--> 'payment_failed'  + scooter released (no charge)
ride 'active' --end, timeout/5xx/crash-> stays 'active'; client retries (safe), or `reconcile` settles it
```

1. A scooter has `is_reserved = 1` **if and only if** it has exactly one `active` ride.
2. A `completed` ride has exactly one charge in `gateway_charges`, with the same `txn_id`.
3. A `payment_failed` ride has no charge. Every charge belongs to a ride.

`cli invariants` checks all of these. Cost is 1 cent per metre between ride start and end (inherited from 2019).

## 5. Debugging playbook (symptom → root cause → failing test)

1. **Look at what was recorded first.** `cli failures`, then `cli scooter <id>` or `cli trace <request_id>`. The DB may be empty on a fresh checkout. If so, go to step 3.
2. **Check invariants**: `cli invariants`. A violation names the rule and the scooter/ride.
3. **Reproduce on a throwaway DB** so you don't disturb recorded data:
   ```bash
   PY -m scooter_api.cli --db var/repro.db reset --yes
   PY -m scooter_api.cli --db var/repro.db request GET "/reservation/start?id=18"
   PY -m scooter_api.cli --db var/repro.db request GET "/reservation/end?id=18&lat=23.01&lng=19"
   PY -m scooter_api.cli --db var/repro.db scooter 18      # read the events: where does reality diverge?
   ```
   Compare the event sequence with "A normal end-ride" in docs/EVENTS.md. `scooter.state_changed.to` is re-read from the DB after commit, so if it disagrees with what that step should have done, the write is wrong.
4. **Cite evidence** from the records: request id, event ids/types, the `from`/`to` values, invariant violations, ledger rows.
5. **Write the failing test** in `tests/` using the fixtures in `tests/conftest.py` (each test gets its own fresh DB):
   ```python
   def test_end_ride_releases_scooter(api, state, assert_consistent):
       assert api("/reservation/start?id=18")[0] == 200
       assert api("/reservation/end?id=18&lat=23.01&lng=19")[0] == 200
       assert state("18")["state"]["is_reserved"] == 0
       assert_consistent()
   ```
   `api(path, fault=None, method="GET")` returns `(status, json, request_id)`. `state(id)` returns the same dict as `cli scooter`. `assert_consistent()` fails on any invariant violation.
6. Run `PY -m pytest` to confirm it fails for the right reason before touching the code.

## 6. Agent tools (MCP)

`.mcp.json` registers `scooter-ops` (started via `scripts/mcp_launch.py`, which picks the venv Python). Tools: `failure_counts`, `recent_failures`, `scooter_history`, `list_scooters`, `request_trace`, `query_events`, `check_invariants`, `list_failure_kinds`, `call_api`, `replay_request`, `run_tests`. They return the same JSON as the CLI. `call_api` and `replay_request` mutate the default DB.

## 7. Making failures happen on purpose

Faults are disabled when `SCOOTER_ENV=production`.

- One request: header `X-Fault: payment=decline` (CLI: `--fault payment=decline`)
- Every request: env `SCOOTER_FAULTS=payment=timeout_after_charge`
- Random but reproducible: `SCOOTER_FAULT_RATE=0.3 SCOOTER_FAULT_SEED=42` (same seed + same request order = same faults)

| Spec | Effect |
|---|---|
| `payment=decline` | no charge; ride `payment_failed`; scooter released; 402 |
| `payment=timeout_before_charge` | no charge; ride stays active; 504, retryable |
| `payment=timeout_after_charge` | **charged**, response lost; ride stays active; 504; retry returns the same txn |
| `payment=error` | no charge; 502, retryable |
| `storage=crash_after_charge` | charged, then the DB write that ends the ride is skipped; 500; retry completes it, still one charge |

The resolved fault plan is stored on every `request.received` event, so `cli replay <request_id>` reproduces it exactly.

## 8. Agent boundaries (enforced in `.claude/settings.json`)

The worst a confused agent could do here: force-push over or rewrite `master`, delete recorded data (`var/scooter.db`) that is evidence for a bug, weaken its own permissions, install arbitrary packages, or call out to the network/deploy (this app once ran on Heroku). The config is built around those.

> The config is authored in [docs/claude-settings.proposed.json](docs/claude-settings.proposed.json) and **a human installs it** by copying it to `.claude/settings.json` (an agent is not allowed to write its own permission file, and this one denies edits to `.claude/` afterwards). If `.claude/settings.json` is missing, the boundaries below are not active yet.

**Permissions** (project-level, so every session in this repo gets them):

- **Allowed without asking:** reading/searching files; editing `scooter_api/`, `tests/`, `docs/`, `*.md`; running pytest, the CLI's read-only commands, `app.py`; read-only git (`status`, `diff`, `log`, `show`, `branch`), `git switch -c`, `git add`, `git commit`; all `scooter-ops` MCP tools.
- **Ask first:** `git push`, opening PRs, `pip install`, `git merge`/`rebase`/`checkout`, `cli reset` (wipes recorded data), editing `requirements.txt`, `scooter_db.json`, `.mcp.json`, `scripts/`.
- **Denied:** force push, push to `master`, `git reset --hard`, `git clean`, `rm -rf`, `curl`/`wget`/WebFetch, `heroku`, reading `.env*`, deleting `var/`, wiping the default DB (`reset --including-default-db`; the CLI also refuses in code without that flag), and **editing `.claude/`**, so an agent cannot loosen its own rules. A human edits that file.
- Bash prefix rules are a guardrail, not a sandbox: `python -c "..."` could still do anything Python can. That is why the allow list doesn't include bare `python`, so anything outside the listed commands prompts.

**Isolation:** work happens on a feature branch (never `master`, and pushes need approval). For unattended runs use a separate git worktree (`git worktree add ../scooter-agent -b agent/<task>`) so the main checkout and its `var/` evidence are untouched. Reproductions go in a throwaway DB (`--db var/repro.db`). The app's only external dependency, the payment gateway, is simulated in-process, so nothing can spend real money. Claude Code's OS sandbox (`sandbox.enabled`) is macOS/Linux/WSL-only; on those platforms enable it in your user settings for an extra layer.

**Reversibility:** everything an agent can do without asking is a local commit on a branch (`git reset` / `git branch -D` by a human undoes it) or a write to a throwaway DB. Recorded data can only be wiped with `reset --yes`, which needs approval. Nothing reaches the remote without a human approving the push.

**Budget:** for headless or cold-agent runs, cap them on the command line:
`claude -p "<symptom>" --max-turns 40 --max-budget-usd 3`. Tool calls are capped in time: the test runner times out after 600 s, and `BASH_MAX_TIMEOUT_MS` is set to 10 minutes in the project settings.

## 9. Conventions

- Every new failure path must raise `AppError(FailureKind.X, ...)`. Add new kinds to `failures.py` **and** `docs/FAILURES.md`; never rename an existing one.
- Every state transition happens in one `transaction(conn)` and emits `*.state_changed` with `from`/`to` re-read after commit.
- Never hold a DB transaction across a payment call.
- Add a test for every failure path you add. `PY -m pytest` must stay green.
- Log where the AI was wrong in `AI_NOTES.md`.
