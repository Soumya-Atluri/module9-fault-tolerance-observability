"""Checkout Service (port 8000).

Endpoints
  POST /checkout   create an order and call the Payment Service
  GET  /metrics    JSON metrics (success/failure counts, latency, error rate)
  GET  /health     liveness check

Every request gets a request/trace ID (or reuses the caller's X-Request-ID).
The ID is sent to the Payment Service in the X-Request-ID header and appears
in both services' logs, so one request can be followed end to end.
"""
import json
import logging
import os
import socket
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from observability import Metrics, get_logger, log

PORT = int(os.getenv("CHECKOUT_PORT", "8000"))
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://127.0.0.1:8001/pay")
PAYMENT_TIMEOUT = float(os.getenv("PAYMENT_TIMEOUT", "2.0"))  # seconds
SERVICE = "checkout-service"

logger = get_logger(SERVICE)
metrics = Metrics()


class PaymentTimeout(Exception):
    pass


def call_payment(request_id, order):
    req = urllib.request.Request(
        PAYMENT_URL,
        data=json.dumps(order).encode(),
        headers={"Content-Type": "application/json", "X-Request-ID": request_id},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=PAYMENT_TIMEOUT) as resp:
            return resp.status, json.loads(resp.read())
    except (TimeoutError, socket.timeout) as exc:
        raise PaymentTimeout() from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise PaymentTimeout() from exc
        raise


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body, request_id=None):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if request_id:
            self.send_header("X-Request-ID", request_id)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/metrics":
            snap = metrics.snapshot()
            snap["payment_timeout_seconds"] = PAYMENT_TIMEOUT
            self._send(200, snap)
        elif self.path == "/health":
            self._send(200, {"status": "ok", "service": SERVICE})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/checkout":
            self._send(404, {"error": "not found"})
            return

        request_id = self.headers.get("X-Request-ID") or str(uuid.uuid4())
        start = time.perf_counter()
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            body = {}
        order = {"order_id": body.get("order_id") or f"ord-{uuid.uuid4().hex[:6]}",
                 "amount": body.get("amount", 49.99)}
        log(logger, logging.INFO, "checkout_received", request_id=request_id, **order)

        try:
            log(logger, logging.INFO, "calling_payment", request_id=request_id,
                url=PAYMENT_URL, timeout_s=PAYMENT_TIMEOUT)
            status, payment = call_payment(request_id, order)
            latency_ms = (time.perf_counter() - start) * 1000
            metrics.record(True, latency_ms)
            log(logger, logging.INFO, "checkout_succeeded", request_id=request_id,
                status=200, payment_status=payment.get("status"), latency_ms=round(latency_ms, 1))
            self._send(200, {"status": "order_confirmed", "request_id": request_id,
                             "order": order, "payment": payment,
                             "latency_ms": round(latency_ms, 1)}, request_id)
        except PaymentTimeout:
            latency_ms = (time.perf_counter() - start) * 1000
            metrics.record(False, latency_ms, timeout=True)
            log(logger, logging.ERROR, "payment_timeout", request_id=request_id, status=504,
                timeout_s=PAYMENT_TIMEOUT, latency_ms=round(latency_ms, 1))
            self._send(504, {"status": "failed", "error": "payment service timed out",
                             "request_id": request_id, "latency_ms": round(latency_ms, 1)},
                       request_id)
        except Exception as exc:  # payment down, bad response, etc.
            latency_ms = (time.perf_counter() - start) * 1000
            metrics.record(False, latency_ms)
            log(logger, logging.ERROR, "payment_error", request_id=request_id, status=502,
                error=repr(exc), latency_ms=round(latency_ms, 1))
            self._send(502, {"status": "failed", "error": str(exc),
                             "request_id": request_id}, request_id)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    log(logger, logging.INFO, "service_started", port=PORT,
        payment_url=PAYMENT_URL, payment_timeout_s=PAYMENT_TIMEOUT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log(logger, logging.INFO, "service_stopped")


if __name__ == "__main__":
    main()
