"""Domain logic: searching, reserving, ending rides, paying, reconciling.

State machine (AGENTS.md has the full picture):

    scooter available --reserve--> scooter reserved + ride 'active'
    ride 'active' --end, payment ok-----------> ride 'completed',      scooter released at end location
    ride 'active' --end, payment declined-----> ride 'payment_failed', scooter released at end location
    ride 'active' --end, payment ambiguous----> ride stays 'active' (last_payment_error set); client retries,
                                                 or `reconcile` settles it against the gateway ledger

Invariant: a scooter is reserved if and only if it has exactly one 'active' ride.
Every write to scooters+rides for a transition happens in ONE transaction.
"""
import uuid

from geopy.distance import distance as geodesic

from .db import connect, now_iso, row_to_dict, transaction
from .failures import AppError, FailureKind
from .payments import GatewayError, PaymentDeclined, PaymentGateway, PaymentTimeout


def calculate_cost(distance_m: int) -> int:
    """Cost in cents. Inherited from 2019: one cent per metre ridden."""
    return int(distance_m)


def _scooter(conn, scooter_id):
    return row_to_dict(conn.execute("SELECT * FROM scooters WHERE id = ?", (scooter_id,)).fetchone())


def _snapshot(scooter):
    return None if scooter is None else {
        "is_reserved": bool(scooter["is_reserved"]), "lat": scooter["lat"], "lng": scooter["lng"]}


def _active_ride(conn, scooter_id):
    return row_to_dict(conn.execute(
        "SELECT * FROM rides WHERE scooter_id = ? AND status = 'active'", (scooter_id,)).fetchone())


class ScooterService:
    def __init__(self, path, gateway: PaymentGateway):
        self.path = path
        self.gateway = gateway

    # ------------------------------------------------------------------ reads
    def list_available(self):
        conn = connect(self.path)
        try:
            rows = conn.execute("SELECT id, lat, lng, is_reserved FROM scooters WHERE is_reserved = 0 ORDER BY id").fetchall()
            return [{"id": r["id"], "lat": r["lat"], "lng": r["lng"], "is_reserved": False} for r in rows]
        finally:
            conn.close()

    def search(self, lat, lng, radius_m):
        results = []
        for s in self.list_available():
            if geodesic((s["lat"], s["lng"]), (lat, lng)).m <= radius_m:
                results.append({"id": s["id"], "lat": s["lat"], "lng": s["lng"]})
        return results

    # -------------------------------------------------------------- reserve
    def start_reservation(self, scooter_id, emit):
        conn = connect(self.path)
        try:
            with transaction(conn):
                scooter = _scooter(conn, scooter_id)
                if scooter is None:
                    raise AppError(FailureKind.SCOOTER_NOT_FOUND, f"No scooter with id {scooter_id} was found.",
                                   scooter_id=scooter_id)
                if scooter["is_reserved"]:
                    active = _active_ride(conn, scooter_id)
                    raise AppError(FailureKind.SCOOTER_ALREADY_RESERVED,
                                   f"Scooter with id {scooter_id} is already reserved.",
                                   scooter_id=scooter_id, active_ride_id=active and active["id"])
                ride_id = "ride_" + uuid.uuid4().hex[:12]
                conn.execute("UPDATE scooters SET is_reserved = 1, updated_at = ? WHERE id = ?", (now_iso(), scooter_id))
                conn.execute(
                    "INSERT INTO rides (id, scooter_id, status, start_lat, start_lng, started_at, start_request_id) "
                    "VALUES (?, ?, 'active', ?, ?, ?, ?)",
                    (ride_id, scooter_id, scooter["lat"], scooter["lng"], now_iso(), emit.request_id),
                )
            after = _scooter(conn, scooter_id)
        finally:
            conn.close()
        emit("ride.state_changed", scooter_id=scooter_id, ride_id=ride_id, **{"from": None, "to": "active"})
        emit("scooter.state_changed", scooter_id=scooter_id, ride_id=ride_id,
             **{"from": _snapshot(scooter), "to": _snapshot(after)})
        return {"ride_id": ride_id, "scooter_id": scooter_id}

    # ------------------------------------------------------------------ end
    def end_reservation(self, scooter_id, end_lat, end_lng, emit, faults):
        conn = connect(self.path)
        try:
            scooter = _scooter(conn, scooter_id)
            if scooter is None:
                raise AppError(FailureKind.SCOOTER_NOT_FOUND, f"No scooter with id {scooter_id} was found.",
                               scooter_id=scooter_id)
            ride = _active_ride(conn, scooter_id)
            if ride is None:
                last = row_to_dict(conn.execute(
                    "SELECT id, status, txn_id, ended_at FROM rides WHERE scooter_id = ? ORDER BY started_at DESC LIMIT 1",
                    (scooter_id,)).fetchone())
                raise AppError(FailureKind.RIDE_NOT_ACTIVE,
                               f"No reservation for scooter {scooter_id} presently exists.",
                               scooter_id=scooter_id, last_ride=last)

            ride_id = ride["id"]
            distance_m = round(geodesic((ride["start_lat"], ride["start_lng"]), (end_lat, end_lng)).m)
            cost_cents = calculate_cost(distance_m)
            emit("ride.end_requested", scooter_id=scooter_id, ride_id=ride_id,
                 end_lat=end_lat, end_lng=end_lng, distance_m=distance_m, cost_cents=cost_cents,
                 previous_payment_error=ride["last_payment_error"])

            # ---- payment (outside any DB transaction: never hold a lock across a network call)
            payment_fault = faults.get("payment")
            if payment_fault:
                emit("fault.injected", scooter_id=scooter_id, ride_id=ride_id, point="payment", mode=payment_fault)
            emit("payment.attempted", scooter_id=scooter_id, ride_id=ride_id,
                 idempotency_key=ride_id, amount_cents=cost_cents)
            try:
                charge = self.gateway.charge(ride_id, cost_cents, fault=payment_fault, request_id=emit.request_id)
            except PaymentDeclined as e:
                emit("payment.declined", scooter_id=scooter_id, ride_id=ride_id,
                     failure_kind=FailureKind.PAYMENT_DECLINED, error=str(e))
                self._finish_ride(conn, ride, scooter, "payment_failed", end_lat, end_lng,
                                  distance_m, cost_cents, None, emit)
                raise AppError(FailureKind.PAYMENT_DECLINED,
                               f"Payment for scooter {scooter_id} was declined. The ride was ended and no charge was made.",
                               scooter_id=scooter_id, ride_id=ride_id)
            except (PaymentTimeout, GatewayError) as e:
                kind = FailureKind.PAYMENT_TIMEOUT if isinstance(e, PaymentTimeout) else FailureKind.PAYMENT_GATEWAY_ERROR
                emit("payment.ambiguous", scooter_id=scooter_id, ride_id=ride_id, failure_kind=kind, error=str(e))
                with transaction(conn):
                    conn.execute(
                        "UPDATE rides SET last_payment_error = ?, last_payment_at = ?, end_lat = ?, end_lng = ? "
                        "WHERE id = ? AND status = 'active'",
                        (kind.value, now_iso(), end_lat, end_lng, ride_id))
                raise AppError(kind,
                               f"Payment for scooter {scooter_id} could not be confirmed. The ride is still active; "
                               f"retrying this request is safe and will never charge twice.",
                               scooter_id=scooter_id, ride_id=ride_id)

            emit("payment.succeeded", scooter_id=scooter_id, ride_id=ride_id,
                 txn_id=charge.txn_id, amount_cents=charge.amount_cents, replayed=charge.replayed)

            if faults.get("storage") == "crash_after_charge":
                emit("fault.injected", scooter_id=scooter_id, ride_id=ride_id, point="storage", mode="crash_after_charge")
                raise AppError(FailureKind.STORAGE_ERROR,
                               f"Storage write failed after payment for scooter {scooter_id} (simulated). "
                               f"The ride is still active; retrying is safe and will not charge again.",
                               scooter_id=scooter_id, ride_id=ride_id, txn_id=charge.txn_id)

            self._finish_ride(conn, ride, scooter, "completed", end_lat, end_lng,
                              distance_m, charge.amount_cents, charge.txn_id, emit)
        finally:
            conn.close()
        return {"ride_id": ride_id, "txn_id": charge.txn_id, "cost_cents": charge.amount_cents,
                "distance_m": distance_m}

    def _finish_ride(self, conn, ride, scooter_before, status, end_lat, end_lng,
                     distance_m, cost_cents, txn_id, emit):
        """Move an active ride to a terminal status and release its scooter, atomically."""
        scooter_id, ride_id = ride["scooter_id"], ride["id"]
        with transaction(conn):
            updated = conn.execute(
                "UPDATE rides SET status = ?, end_lat = ?, end_lng = ?, distance_m = ?, cost_cents = ?, txn_id = ?, "
                "ended_at = ?, end_request_id = ? WHERE id = ? AND status = 'active'",
                (status, end_lat, end_lng, distance_m, cost_cents, txn_id, now_iso(), emit.request_id, ride_id),
            ).rowcount
            if updated:
                conn.execute(
                    "UPDATE scooters SET is_reserved = 0, lat = ?, lng = ?, updated_at = ? WHERE id = ?",
                    (end_lat, end_lng, now_iso(), scooter_id))
        after = _scooter(conn, scooter_id)
        if not updated:
            # Another request finished this ride first (e.g. a concurrent retry). Nothing to do.
            emit("ride.already_finished", scooter_id=scooter_id, ride_id=ride_id)
            return
        emit("ride.state_changed", scooter_id=scooter_id, ride_id=ride_id,
             **{"from": "active", "to": status, "txn_id": txn_id})
        emit("scooter.state_changed", scooter_id=scooter_id, ride_id=ride_id,
             **{"from": _snapshot(scooter_before), "to": _snapshot(after)})

    # ------------------------------------------------------------ reconcile
    def reconcile(self, emit, older_than_seconds=300):
        """Settle rides left 'active' by an ambiguous payment, using the gateway ledger as truth.

        - charge exists in the ledger -> complete the ride with that txn
        - no charge                   -> mark payment_failed and release the scooter
        Only touches rides whose last ambiguous attempt is older than `older_than_seconds`.
        """
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat(timespec="milliseconds")
        conn = connect(self.path)
        settled = []
        try:
            rides = [row_to_dict(r) for r in conn.execute(
                "SELECT * FROM rides WHERE status = 'active' AND last_payment_error IS NOT NULL "
                "AND last_payment_at <= ?", (cutoff,)).fetchall()]
            for ride in rides:
                scooter = _scooter(conn, ride["scooter_id"])
                charge = self.gateway.lookup(ride["id"])
                distance_m = round(geodesic((ride["start_lat"], ride["start_lng"]), (ride["end_lat"], ride["end_lng"])).m)
                if charge:
                    self._finish_ride(conn, ride, scooter, "completed", ride["end_lat"], ride["end_lng"],
                                      distance_m, charge["amount_cents"], charge["txn_id"], emit)
                    outcome = "completed"
                else:
                    self._finish_ride(conn, ride, scooter, "payment_failed", ride["end_lat"], ride["end_lng"],
                                      distance_m, calculate_cost(distance_m), None, emit)
                    outcome = "payment_failed"
                emit("ride.reconciled", scooter_id=ride["scooter_id"], ride_id=ride["id"], outcome=outcome)
                settled.append({"ride_id": ride["id"], "scooter_id": ride["scooter_id"], "outcome": outcome})
        finally:
            conn.close()
        return settled
