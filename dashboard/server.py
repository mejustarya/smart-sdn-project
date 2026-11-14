#!/usr/bin/env python3
"""Dashboard server: serves the web UI and streams live events from the lab.

    sudo /usr/bin/python3 dashboard/server.py            # then open http://127.0.0.1:8000
    sudo /usr/bin/python3 dashboard/server.py --autostart

Uses only the Python standard library (no Flask needed). It listens on 127.0.0.1 only:
it controls Mininet as root, so do not expose it to a network.
"""
import argparse
import csv
import io
import json
import mimetypes
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from lab import Bus, Lab, clean  # noqa: E402

STATIC = os.path.join(HERE, "static")
LAB = None
BUS = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TimeSyncLab"

    def log_message(self, *args):  # quiet
        pass

    # ---------------------------------------------------------- helpers
    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(clean(body)).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            return {}

    # ------------------------------------------------------------- GET
    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        try:
            if path == "/":
                return self._file(os.path.join(STATIC, "index.html"))
            if path.startswith("/static/"):
                rel = os.path.normpath(path[len("/static/"):])
                if rel.startswith("..") or os.path.isabs(rel):
                    return self._send(404, {"error": "not found"})
                return self._file(os.path.join(STATIC, rel))
            if path == "/api/state":
                return self._send(200, LAB.status())
            if path == "/api/results":
                return self._send(200, LAB.results())
            if path == "/api/events":
                return self._sse(int(parse_qs(url.query).get("since", ["0"])[0]))
            if path in ("/api/report/run.pdf", "/api/report/matrix.pdf"):
                fp = LAB.report_path("matrix" if "matrix" in path else "run")
                if not fp:
                    return self._send(404, {"error": "no report yet: run an experiment first"})
                with open(fp, "rb") as f:
                    body = f.read()
                name = "final_report_comparison.pdf" if "matrix" in path else "final_report_run.pdf"
                return self._send(200, body, "application/pdf",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            if path == "/api/export/last_run.csv":
                return self._csv(LAB.last_rows, "last_run.csv")
            if path == "/api/export/history.csv":
                return self._csv(LAB.history, "history.csv")
            if path == "/api/export/matrix.csv":
                m = LAB.results()["matrix"]
                return self._csv(m["rows"] if m else [], "comparison.csv")
            return self._send(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _file(self, fp):
        if not os.path.isfile(fp):
            return self._send(404, {"error": "not found"})
        with open(fp, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        if fp.endswith(".js"):
            ctype = "text/javascript"
        extra = {"Cache-Control": "public, max-age=86400"} if fp.endswith(".woff2") else None
        self._send(200, data, ctype + ("; charset=utf-8" if ctype.startswith("text") else ""), extra)

    def _csv(self, rows, name):
        buf = io.StringIO()
        if rows:
            keys = []
            for r in rows:
                for k in r:
                    if k not in keys and not isinstance(r[k], (list, dict)):
                        keys.append(k)
            w = csv.DictWriter(buf, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        self._send(200, buf.getvalue(), "text/csv", {"Content-Disposition": f'attachment; filename="{name}"'})

    def _sse(self, since):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last = since
        try:
            while True:
                evs = BUS.since(last, timeout=10)
                if not evs:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                out = []
                for e in evs:
                    out.append(f"id: {e['id']}\ndata: {json.dumps(e)}\n\n")
                    last = e["id"]
                self.wfile.write("".join(out).encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    # ------------------------------------------------------------ POST
    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()
        try:
            if path == "/api/lab/start":
                ok, msg = LAB.submit("start")
            elif path == "/api/lab/stop":
                LAB.request_abort()
                ok, msg = LAB.submit("stop")
            elif path == "/api/run":
                ok, msg = LAB.submit("run", body)
            elif path == "/api/matrix":
                ok, msg = LAB.submit("matrix", body)
            elif path == "/api/abort":
                LAB.request_abort()
                ok, msg = True, "ok"
            else:
                return self._send(404, {"error": "not found"})
            self._send(200 if ok else 409, {"ok": ok, "message": msg})
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    global LAB, BUS
    ap = argparse.ArgumentParser(description="Time Sync Lab dashboard")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--autostart", action="store_true", help="start the lab as soon as the server is up")
    ap.add_argument("--datapath", choices=["auto", "kernel", "user"], default="auto")
    ap.add_argument("--netem", choices=["auto", "on", "off"], default="auto")
    ap.add_argument("--queue-pkts", type=int, default=None)
    a = ap.parse_args()

    import sdn_config as C
    BUS = Bus()
    LAB = Lab(BUS, datapath=a.datapath,
              netem={"auto": "auto", "on": True, "off": False}[a.netem],
              queue_pkts=a.queue_pkts or C.QUEUE_PKTS)

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print(f"Time Sync Lab dashboard: http://{a.host}:{a.port}")
    if os.geteuid() != 0:
        print("WARNING: not running as root. The page will load, but the lab (Mininet) cannot start.\n"
              "         Use: sudo /usr/bin/python3 dashboard/server.py")
    if a.autostart:
        LAB.submit("start")

    def stop(*_):
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("\nStopping the lab (Mininet and Ryu) ...")
        LAB.shutdown()
        srv.server_close()


if __name__ == "__main__":
    main()
