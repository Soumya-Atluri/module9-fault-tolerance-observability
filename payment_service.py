"""Payment Service (port 8001).

Endpoints
  POST /pay            process a payment (honours the X-Request-ID header)
  POST /admin/delay    set an artificial delay, body: {"seconds": 5}  (0 = remove)
  GET  /metrics        JSON metrics
  GET  /health         liveness check
"""
import json
import logging
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from observability import Metrics, get_logger, log

PORT = int(os.getenv("PAYMENT_PORT", "8001"))
SERVICE = "payment-service"

logger = get_logger(SERVICE)
metrics = Metrics()
_delay_lock = threading.Lock()
_delay_seconds = float(os.getenv("PAYMENT_DELAY", "0"))


def get_delay():
    with _delay_lock:
        return _delay_seconds


def set_delay(seconds):
    global _delay_seconds
    with _delay_lock:
        _delay_seconds = max(0.0, float(seconds))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence default access log; we log JSON instead
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

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        if self.path == "/metrics":
            snap = metrics.snapshot()
            snap["current_delay_seconds"] = get_delay()
            self._send(200, snap)
        elif self.path == "/health":
            self._send(200, {"status": "ok", "service": SERVICE})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/admin/delay":
            body = self._read_json()
            set_delay(body.get("seconds", 0))
            log(logger, logging.WARNING, "delay_changed", delay_seconds=get_delay())
            self._send(200, {"delay_seconds": get_delay()})
            return

        if self.path != "/pay":
            self._send(404, {"error": "not found"})
            return

        request_id = self.headers.get("X-Request-ID") or str(uuid.uuid4())
        start = time.perf_counter()
        body = self._read_json()
        log(logger, logging.INFO, "payment_received", request_id=request_id,
            order_id=body.get("order_id"), amount=body.get("amount"))

        delay = get_delay()
        if delay > 0:
            log(logger, logging.WARNING, "injected_delay", request_id=request_id, delay_seconds=delay)
            time.sleep(delay)

        latency_ms = (time.perf_counter() - start) * 1000
        try:
            self._send(200, {"status": "approved", "request_id": request_id,
                             "transaction_id": str(uuid.uuid4())[:8]}, request_id)
            metrics.record(True, latency_ms)
            log(logger, logging.INFO, "payment_completed", request_id=request_id,
                status=200, latency_ms=round(latency_ms, 1))
        except (BrokenPipeError, ConnectionResetError):
            # Checkout already gave up (timed out) and closed the connection.
            metrics.record(False, latency_ms)
            log(logger, logging.ERROR, "client_disconnected_before_response",
                request_id=request_id, latency_ms=round(latency_ms, 1))


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    log(logger, logging.INFO, "service_started", port=PORT, delay_seconds=get_delay())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log(logger, logging.INFO, "service_stopped")


if __name__ == "__main__":
    main()
