"""Client for the localhost control socket exposed by the Mininet CLI process."""
import json
import socket

HOST, PORT = "127.0.0.1", 8765
ALLOWED_KEYS = {"action", "level", "seconds", "rounds", "flows", "rate_mbps",
                "on_seconds", "off_seconds", "delay_ms", "jitter_ms", "loss_pct"}


def control(payload, timeout=20):
    payload = {k: v for k, v in payload.items() if k in ALLOWED_KEYS}
    try:
        with socket.create_connection((HOST, PORT), timeout=3) as sock:
            sock.sendall((json.dumps(payload) + "\n").encode())
            sock.settimeout(timeout)
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        return json.loads(data.decode().strip())
    except Exception as exc:
        return {"ok": False,
                "message": f"Mininet control unavailable: {exc}. Keep the Mininet CLI running."}
