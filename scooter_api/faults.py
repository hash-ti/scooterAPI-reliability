"""Fault injection: make failures happen on purpose, reproducibly.

A *fault plan* is a dict like {"payment": "decline"}. Each request gets one, from the
first source that applies:

1. `X-Fault` request header, e.g. `X-Fault: payment=timeout_after_charge`
2. `SCOOTER_FAULTS` env var (same syntax), applied to every request
3. Seeded random: `SCOOTER_FAULT_RATE=0.3 SCOOTER_FAULT_SEED=42` gives each request a
   30% chance of a random payment fault. The RNG is drawn once per request in arrival
   order, so restarting with the same seed and replaying the same request sequence
   reproduces the same faults.

The resolved plan is recorded on the `request.received` event, so any failure can be
replayed exactly with `python -m scooter_api.cli replay <request_id>`.

Fault injection is disabled when SCOOTER_ENV=production.
"""
import os
import random
import threading

from .failures import AppError, FailureKind

FAULT_POINTS = {
    "payment": {
        "decline",                # gateway says no; no charge is created
        "timeout_before_charge",  # times out before the gateway records anything
        "timeout_after_charge",   # gateway records the charge, then the response is lost
        "error",                  # gateway 5xx before recording anything
    },
    "storage": {
        "crash_after_charge",     # the charge succeeds, then the DB write that ends the ride dies
    },
}
RANDOM_PAYMENT_FAULTS = sorted(FAULT_POINTS["payment"])


def parse_spec(spec: str) -> dict:
    plan = {}
    for part in filter(None, (p.strip() for p in (spec or "").split(","))):
        point, sep, mode = part.partition("=")
        point, mode = point.strip(), mode.strip()
        if not sep or point not in FAULT_POINTS or mode not in FAULT_POINTS[point]:
            valid = "; ".join(f"{p}={'|'.join(sorted(m))}" for p, m in FAULT_POINTS.items())
            raise AppError(FailureKind.INVALID_FAULT_SPEC,
                           f"Invalid fault spec {part!r}. Valid: {valid}", spec=spec)
        plan[point] = mode
    return plan


def format_spec(plan: dict) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(plan.items()))


class FaultInjector:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.enabled = env.get("SCOOTER_ENV", "development") != "production"
        self.static = parse_spec(env.get("SCOOTER_FAULTS", "")) if self.enabled else {}
        self.rate = float(env.get("SCOOTER_FAULT_RATE", "0") or 0)
        self.seed = env.get("SCOOTER_FAULT_SEED", "0")
        self._rng = random.Random(self.seed)
        self._lock = threading.Lock()

    def plan_for(self, header_value):
        """Returns (plan, source)."""
        if not self.enabled:
            return {}, "disabled"
        if header_value:
            return parse_spec(header_value), "header"
        if self.static:
            return dict(self.static), "env"
        if self.rate > 0:
            with self._lock:
                hit = self._rng.random() < self.rate
                mode = self._rng.choice(RANDOM_PAYMENT_FAULTS)
            if hit:
                return {"payment": mode}, f"random(seed={self.seed},rate={self.rate})"
        return {}, "none"
