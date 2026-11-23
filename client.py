"""Small cross-platform client (works the same on Windows, macOS, Linux).

  python client.py checkout [N]     send N checkout requests (default 1)
  python client.py delay SECONDS    set Payment Service delay (0 removes it)
  python client.py metrics          print metrics from both services
  python client.py trace REQUEST_ID show every log line for one request ID
"""
import json
import os
import sys
import urllib.error
import urllib.request

CHECKOUT = "http://127.0.0.1:8000"
PAYMENT = "http://127.0.0.1:8001"
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


def http(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def checkout(n=1):
    results = []
    for i in range(n):
        status, body = http("POST", f"{CHECKOUT}/checkout", {"amount": 49.99})
        print(f"[{i + 1}/{n}] HTTP {status}  request_id={body.get('request_id')}  "
              f"status={body.get('status')}  latency_ms={body.get('latency_ms')}"
              + (f"  error={body['error']}" if "error" in body else ""))
        results.append((status, body))
    return results


def metrics():
    for name, base in (("checkout-service", CHECKOUT), ("payment-service", PAYMENT)):
        _, m = http("GET", f"{base}/metrics")
        print(f"--- {name} /metrics ---")
        print(json.dumps(m, indent=2))


def trace(request_id):
    """Merge both services' logs for one request ID, ordered by time."""
    lines = []
    for svc in ("checkout-service", "payment-service"):
        path = os.path.join(LOG_DIR, f"{svc}.log")
        if not os.path.exists(path):
            continue
        with open(path) as f:
            for line in f:
                if request_id in line:
                    lines.append(json.loads(line))
    for entry in sorted(lines, key=lambda e: e["ts"]):
        print(json.dumps(entry))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "checkout":
        checkout(int(sys.argv[2]) if len(sys.argv) > 2 else 1)
    elif cmd == "delay":
        print(http("POST", f"{PAYMENT}/admin/delay", {"seconds": float(sys.argv[2])})[1])
    elif cmd == "metrics":
        metrics()
    elif cmd == "trace":
        trace(sys.argv[2])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
