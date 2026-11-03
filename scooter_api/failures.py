"""The fixed set of failure kinds.

Every failed HTTP response and every failure event carries exactly one of these
identifiers in its `kind` / `failure_kind` field. The string values are stable:
never rename one; add a new member instead. `docs/FAILURES.md` mirrors this table.
"""
from enum import Enum
from http import HTTPStatus


class FailureKind(str, Enum):
    # --- request validation ---------------------------------------------------
    MISSING_PARAM = "MISSING_PARAM"            # a required query param is absent
    INVALID_PARAM = "INVALID_PARAM"            # a param is present but malformed/out of range
    INVALID_FAULT_SPEC = "INVALID_FAULT_SPEC"  # X-Fault header could not be parsed
    ROUTE_NOT_FOUND = "ROUTE_NOT_FOUND"        # no such endpoint
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"

    # --- domain rules -----------------------------------------------------------
    SCOOTER_NOT_FOUND = "SCOOTER_NOT_FOUND"
    SCOOTER_ALREADY_RESERVED = "SCOOTER_ALREADY_RESERVED"
    RIDE_NOT_ACTIVE = "RIDE_NOT_ACTIVE"        # tried to end a ride on a scooter with no active ride

    # --- payment ----------------------------------------------------------------
    PAYMENT_DECLINED = "PAYMENT_DECLINED"      # definitive: no money moved
    PAYMENT_TIMEOUT = "PAYMENT_TIMEOUT"        # ambiguous: the charge may or may not exist; retry is safe
    PAYMENT_GATEWAY_ERROR = "PAYMENT_GATEWAY_ERROR"  # gateway 5xx; treated as ambiguous like a timeout

    # --- infrastructure ---------------------------------------------------------
    STORAGE_ERROR = "STORAGE_ERROR"            # a DB write failed (or was made to fail by fault injection)
    INTERNAL_ERROR = "INTERNAL_ERROR"          # an unhandled exception; the event has the traceback


STATUS = {
    FailureKind.MISSING_PARAM: HTTPStatus.UNPROCESSABLE_ENTITY,
    FailureKind.INVALID_PARAM: HTTPStatus.UNPROCESSABLE_ENTITY,
    FailureKind.INVALID_FAULT_SPEC: HTTPStatus.BAD_REQUEST,
    FailureKind.ROUTE_NOT_FOUND: HTTPStatus.NOT_FOUND,
    FailureKind.METHOD_NOT_ALLOWED: HTTPStatus.METHOD_NOT_ALLOWED,
    FailureKind.SCOOTER_NOT_FOUND: HTTPStatus.NOT_FOUND,
    FailureKind.SCOOTER_ALREADY_RESERVED: HTTPStatus.CONFLICT,
    FailureKind.RIDE_NOT_ACTIVE: HTTPStatus.CONFLICT,
    FailureKind.PAYMENT_DECLINED: HTTPStatus.PAYMENT_REQUIRED,
    FailureKind.PAYMENT_TIMEOUT: HTTPStatus.GATEWAY_TIMEOUT,
    FailureKind.PAYMENT_GATEWAY_ERROR: HTTPStatus.BAD_GATEWAY,
    FailureKind.STORAGE_ERROR: HTTPStatus.INTERNAL_SERVER_ERROR,
    FailureKind.INTERNAL_ERROR: HTTPStatus.INTERNAL_SERVER_ERROR,
}

# Failures where the client may safely repeat the exact same request.
RETRYABLE = {
    FailureKind.PAYMENT_TIMEOUT,
    FailureKind.PAYMENT_GATEWAY_ERROR,
    FailureKind.STORAGE_ERROR,
}


class AppError(Exception):
    """Raise this anywhere in request handling; the app turns it into a typed response."""

    def __init__(self, kind: FailureKind, message: str, **details):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.details = details

    @property
    def status(self) -> int:
        return STATUS[self.kind].value

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE
