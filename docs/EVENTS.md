# Event log

Table `events` in the SQLite DB (`var/scooter.db` unless `SCOOTER_DB` is set). Append-only; written on its own connection so events survive even when the transaction they describe rolls back.

| Column | Meaning |
|---|---|
| `id` | monotonically increasing; order of events |
| `ts` | ISO-8601 UTC, millisecond precision |
| `request_id` | `req_…` for HTTP requests (also the `X-Request-ID` response header); `reconcile_…` for reconcile runs |
| `scooter_id`, `ride_id` | set whenever known, so you can follow one scooter or one ride |
| `type` | see below |
| `failure_kind` | a [failure kind](FAILURES.md) or NULL |
| `data` | JSON object, type-specific |

## Event types

| Type | Emitted when | Key `data` fields |
|---|---|---|
| `request.received` | first thing in every request | `method`, `path`, `args`, `fault_plan`, `fault_source`, `x_fault_header` |
| `request.completed` | last thing in every request | `status`, `duration_ms`; `failure_kind` column set on failure |
| `request.failed` | request ended in an error (exactly one per failed request) | `message`, `status`, `retryable`, `details` |
| `exception` | unhandled exception | `exc_type`, `exc_message`, `traceback` |
| `ride.state_changed` | ride created or finished | `from`, `to` (`active`/`completed`/`payment_failed`), `txn_id` |
| `scooter.state_changed` | scooter reserved or released | `from`, `to`: `{is_reserved, lat, lng}`. **`to` is re-read from the DB after commit**, so it is what was actually stored, not what the code intended |
| `ride.end_requested` | end-ride accepted for processing | `end_lat`, `end_lng`, `distance_m`, `cost_cents`, `previous_payment_error` |
| `fault.injected` | a fault from the request's plan fired | `point`, `mode` |
| `payment.attempted` | about to call the gateway | `idempotency_key`, `amount_cents` |
| `payment.succeeded` | gateway returned a charge | `txn_id`, `amount_cents`, `replayed` (true = key had already been charged) |
| `payment.declined` | gateway declined | `error` |
| `payment.ambiguous` | timeout or gateway error | `error` |
| `ride.already_finished` | another request finished this ride first | |
| `ride.reconciled` | `reconcile` settled a stuck ride | `outcome` |

## A normal end-ride, in order

`request.received` → `ride.end_requested` → `payment.attempted` → `payment.succeeded` → `ride.state_changed (active→completed)` → `scooter.state_changed (is_reserved true→false)` → `request.completed (200)`

If any step is missing or a `to` value disagrees with what the step should have done, that's where to look.

## Querying

```bash
python -m scooter_api.cli events --scooter 18                 # one scooter end-to-end
python -m scooter_api.cli trace req_ab12cd34ef56               # one request
python -m scooter_api.cli events --kind PAYMENT_TIMEOUT --since-minutes 60
sqlite3 var/scooter.db "SELECT ts,type,failure_kind,data FROM events WHERE scooter_id='18' ORDER BY id"
```
