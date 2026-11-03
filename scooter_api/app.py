"""Flask app: HTTP routes plus the request lifecycle that records every request.

Every request gets a request id (honours an incoming X-Request-ID, else generates one,
always echoed back in the X-Request-ID response header) and produces at least two events:
`request.received` and `request.completed`. Failures add a `request.failed` event that
carries the failure kind, and unhandled exceptions record their full traceback.
"""
import os
import time
import traceback
import uuid

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException

from .db import db_path, init_db
from .events import EventLog, RequestEvents
from .failures import AppError, FailureKind
from .faults import FaultInjector
from .payments import PaymentGateway
from .service import ScooterService


def create_app(path=None, env=None) -> Flask:
    path = init_db(path or db_path())
    app = Flask(__name__)
    app.config["SCOOTER_DB"] = str(path)
    events = EventLog(path)
    service = ScooterService(path, PaymentGateway(path))
    faults = FaultInjector(env if env is not None else os.environ)
    app.extensions["scooter"] = {"events": events, "service": service, "faults": faults}

    # ------------------------------------------------------------ lifecycle
    @app.before_request
    def start_request():
        g.started = time.perf_counter()
        g.request_id = request.headers.get("X-Request-ID") or "req_" + uuid.uuid4().hex[:12]
        g.emit = RequestEvents(events, g.request_id)
        g.failure_kind = None
        g.fault_plan, g.fault_source = {}, "none"
        fault_error = None
        try:
            g.fault_plan, g.fault_source = faults.plan_for(request.headers.get("X-Fault"))
        except AppError as e:
            fault_error = e
        g.emit("request.received", scooter_id=request.args.get("id"),
               method=request.method, path=request.path, args=request.args.to_dict(),
               fault_plan=g.fault_plan, fault_source=g.fault_source,
               x_fault_header=request.headers.get("X-Fault"))
        if fault_error:
            return error_response(fault_error)

    @app.after_request
    def finish_request(response):
        response.headers["X-Request-ID"] = g.request_id
        g.emit("request.completed", scooter_id=request.args.get("id"),
               failure_kind=g.failure_kind, status=response.status_code,
               duration_ms=round((time.perf_counter() - g.started) * 1000, 2),
               method=request.method, path=request.path)
        return response

    def error_response(err: AppError):
        g.failure_kind = err.kind
        g.emit("request.failed", scooter_id=request.args.get("id"), ride_id=err.details.get("ride_id"),
               failure_kind=err.kind, message=err.message, status=err.status,
               retryable=err.retryable, details=err.details)
        body = {"ok": False, "msg": f"Error {err.status} - {err.message}", "request_id": g.request_id,
                "error": {"kind": err.kind.value, "message": err.message, "retryable": err.retryable,
                          "details": err.details}}
        return jsonify(body), err.status

    @app.errorhandler(AppError)
    def handle_app_error(err):
        return error_response(err)

    @app.errorhandler(HTTPException)
    def handle_http_error(err):
        kind = {404: FailureKind.ROUTE_NOT_FOUND, 405: FailureKind.METHOD_NOT_ALLOWED}.get(err.code)
        if kind is None:
            kind = FailureKind.INVALID_PARAM if 400 <= err.code < 500 else FailureKind.INTERNAL_ERROR
        return error_response(AppError(kind, err.description or err.name, http_code=err.code))

    @app.errorhandler(Exception)
    def handle_unexpected(exc):
        g.emit("exception", scooter_id=request.args.get("id"), failure_kind=FailureKind.INTERNAL_ERROR,
               exc_type=type(exc).__name__, exc_message=str(exc), traceback=traceback.format_exc())
        return error_response(AppError(FailureKind.INTERNAL_ERROR,
                                       f"Unexpected {type(exc).__name__}: {exc}",
                                       exc_type=type(exc).__name__))

    # ---------------------------------------------------------------- params
    def required(name):
        value = request.args.get(name)
        if value is None or value == "":
            raise AppError(FailureKind.MISSING_PARAM, f"Missing required parameter '{name}'.", param=name)
        return value

    def number(name, lo=None, hi=None):
        raw = required(name)
        try:
            value = float(raw)
        except ValueError:
            raise AppError(FailureKind.INVALID_PARAM, f"Parameter '{name}' must be a number, got {raw!r}.",
                           param=name, value=raw)
        if value != value or (lo is not None and not lo <= value <= hi):  # NaN or out of range
            raise AppError(FailureKind.INVALID_PARAM, f"Parameter '{name}' must be in the [{lo}, {hi}] range.",
                           param=name, value=raw)
        return value

    # ---------------------------------------------------------------- routes
    @app.get("/")
    @app.get("/view_all_available")
    def view_all_available():
        return jsonify(service.list_available())

    @app.get("/search")
    def search():
        lat, lng = number("lat", -90, 90), number("lng", -180, 180)
        radius = number("radius", 0, 40_075_000)
        return jsonify(service.search(lat, lng, radius))

    @app.route("/reservation/start", methods=["GET", "POST"])
    def start_reservation():
        scooter_id = required("id")
        result = service.start_reservation(scooter_id, g.emit)
        return jsonify({"ok": True, "msg": f"Scooter {scooter_id} was reserved successfully.",
                        "request_id": g.request_id, **result})

    @app.route("/reservation/end", methods=["GET", "POST"])
    def end_reservation():
        scooter_id = required("id")
        lat, lng = number("lat", -90, 90), number("lng", -180, 180)
        result = service.end_reservation(scooter_id, lat, lng, g.emit, g.fault_plan)
        return jsonify({"ok": True,
                        "msg": f"Payment for scooter {scooter_id} was made successfully and the reservation was ended.",
                        "request_id": g.request_id, **result})

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "db": app.config["SCOOTER_DB"], "faults_enabled": faults.enabled})

    return app
