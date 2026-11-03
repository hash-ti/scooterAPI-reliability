"""A simulated payment gateway with a real ledger and injectable failures.

The ledger (`gateway_charges` table) stands in for the bank: it is what was actually
charged, independent of what our app believes. Charges are idempotent on the key we
pass (the ride id), like Stripe's Idempotency-Key: charging the same key twice returns
the original transaction and never creates a second charge.
"""
import uuid
from dataclasses import dataclass

from .db import connect, now_iso, row_to_dict


class PaymentDeclined(Exception):
    """Definitive: nothing was charged."""


class PaymentTimeout(Exception):
    """Ambiguous: the charge may or may not exist. Retrying with the same key is safe."""


class GatewayError(Exception):
    """Gateway 5xx. Treated as ambiguous, same as a timeout."""


@dataclass
class Charge:
    txn_id: str
    amount_cents: int
    idempotency_key: str
    replayed: bool  # True if this key had already been charged


class PaymentGateway:
    def __init__(self, path):
        self.path = path

    def charge(self, idempotency_key: str, amount_cents: int, *, fault=None, request_id=None) -> Charge:
        conn = connect(self.path)
        try:
            existing = self.lookup(idempotency_key, conn=conn)
            if existing:
                # A real gateway replays the stored result for a repeated key.
                return Charge(existing["txn_id"], existing["amount_cents"], idempotency_key, replayed=True)

            if fault == "decline":
                raise PaymentDeclined(f"card declined (simulated) for {idempotency_key}")
            if fault == "timeout_before_charge":
                raise PaymentTimeout(f"gateway timed out before charging {idempotency_key} (simulated)")
            if fault == "error":
                raise GatewayError(f"gateway returned 503 for {idempotency_key} (simulated)")

            txn_id = "txn_" + uuid.uuid4().hex[:16]
            conn.execute(
                "INSERT OR IGNORE INTO gateway_charges (idempotency_key, txn_id, amount_cents, created_at, request_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (idempotency_key, txn_id, amount_cents, now_iso(), request_id),
            )
            # If a concurrent call won the race, return its charge, not ours.
            stored = self.lookup(idempotency_key, conn=conn)
            replayed = stored["txn_id"] != txn_id

            if fault == "timeout_after_charge":
                raise PaymentTimeout(f"gateway charged {idempotency_key} but the response was lost (simulated)")
            return Charge(stored["txn_id"], stored["amount_cents"], idempotency_key, replayed=replayed)
        finally:
            conn.close()

    def lookup(self, idempotency_key: str, conn=None):
        own = conn is None
        conn = conn or connect(self.path)
        try:
            return row_to_dict(conn.execute(
                "SELECT * FROM gateway_charges WHERE idempotency_key = ?", (idempotency_key,)).fetchone())
        finally:
            if own:
                conn.close()
