# Failure kinds

Source of truth: `scooter_api/failures.py` (`FailureKind`). Identifiers are stable; new kinds are added, never renamed.

Every failed response looks like:

```json
{"ok": false, "msg": "Error 402 - ...", "request_id": "req_...",
 "error": {"kind": "PAYMENT_DECLINED", "message": "...", "retryable": false, "details": {...}}}
```

The same kind is stored in the event log as `failure_kind` on the `request.failed` and `request.completed` events (plus the domain event that caused it, e.g. `payment.declined`).

| Kind | HTTP | Retryable | Meaning | State afterwards |
|---|---|---|---|---|
| `MISSING_PARAM` | 422 | no | Required query param absent | unchanged |
| `INVALID_PARAM` | 422 | no | Param malformed, NaN or out of range | unchanged |
| `INVALID_FAULT_SPEC` | 400 | no | `X-Fault` header could not be parsed | unchanged |
| `ROUTE_NOT_FOUND` | 404 | no | No such endpoint | unchanged |
| `METHOD_NOT_ALLOWED` | 405 | no | Wrong HTTP method | unchanged |
| `SCOOTER_NOT_FOUND` | 404 | no | Unknown scooter id | unchanged |
| `SCOOTER_ALREADY_RESERVED` | 409 | no | Reserve on a reserved scooter; `details.active_ride_id` | unchanged |
| `RIDE_NOT_ACTIVE` | 409 | no | End on a scooter with no active ride; `details.last_ride` | unchanged |
| `PAYMENT_DECLINED` | 402 | no | Gateway said no. Nothing charged | ride `payment_failed`, scooter **released** |
| `PAYMENT_TIMEOUT` | 504 | yes | Gateway didn't answer. Charge may or may not exist | ride still `active`, scooter reserved, `last_payment_error` set |
| `PAYMENT_GATEWAY_ERROR` | 502 | yes | Gateway 5xx. Treated like a timeout | as above |
| `STORAGE_ERROR` | 500 | yes | DB write failed after the charge | ride still `active`; retry completes it without charging again |
| `INTERNAL_ERROR` | 500 | no | Unhandled exception. `exception` event has the traceback | unknown: run `invariants` |

"Retryable" means repeating the identical request is safe: payment uses the ride id as an idempotency key, so a ride can never be charged twice.

Count them: `python -m scooter_api.cli failures --since-minutes 60` or the MCP tool `failure_counts`.
