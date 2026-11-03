"""SQLite storage for experiment runs, samples and events."""
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, mode TEXT,
  started REAL, ended REAL, status TEXT, config TEXT);
CREATE TABLE IF NOT EXISTS samples(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, ts REAL, phase TEXT, path TEXT,
  slow_mbps REAL, rtt_ms REAL, jitter_ms REAL, loss_pct REAL, sync_err_ms REAL,
  rtt_n INTEGER, timeouts INTEGER);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, ts REAL, phase TEXT, kind TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS idx_samples_run ON samples(run_id);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
"""


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- writes
    def create_run(self, name, mode, config):
        with self._conn() as c:
            cur = c.execute("INSERT INTO runs(name,mode,started,status,config) VALUES(?,?,?,?,?)",
                            (name, mode, time.time(), "running", json.dumps(config)))
            return cur.lastrowid

    def finish_run(self, run_id, status):
        with self._conn() as c:
            c.execute("UPDATE runs SET ended=?, status=? WHERE id=?", (time.time(), status, run_id))

    def add_sample(self, run_id, s, phase):
        with self._conn() as c:
            c.execute("INSERT INTO samples(run_id,ts,phase,path,slow_mbps,rtt_ms,jitter_ms,loss_pct,"
                      "sync_err_ms,rtt_n,timeouts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (run_id, s["ts"], phase, s["path"], s["slow_mbps"], s["rtt_ms"], s["jitter_ms"],
                       s["loss_pct"], s["sync_err_ms"], s["rtt_n"], s["timeouts"]))

    def add_event(self, run_id, ts, phase, kind, detail):
        with self._conn() as c:
            c.execute("INSERT INTO events(run_id,ts,phase,kind,detail) VALUES(?,?,?,?,?)",
                      (run_id, ts, phase, kind, detail))

    def delete_run(self, run_id):
        with self._conn() as c:
            for table, col in (("samples", "run_id"), ("events", "run_id"), ("runs", "id")):
                c.execute(f"DELETE FROM {table} WHERE {col}=?", (run_id,))

    # ---- reads
    def get_run(self, run_id):
        with self._conn() as c:
            row = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self):
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM runs ORDER BY id DESC")]

    def samples(self, run_id):
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM samples WHERE run_id=? ORDER BY ts", (run_id,))]

    def events(self, run_id):
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM events WHERE run_id=? ORDER BY ts", (run_id,))]

    # ---- analysis
    @staticmethod
    def _rtt_avg(rows):
        n = sum(r["rtt_n"] or 0 for r in rows)
        if not n:
            return None
        return sum((r["rtt_ms"] or 0) * (r["rtt_n"] or 0) for r in rows) / n

    @staticmethod
    def _loss(rows):
        to = sum(r["timeouts"] or 0 for r in rows)
        n = sum(r["rtt_n"] or 0 for r in rows)
        return 100.0 * to / (to + n) if (to + n) else None

    def summary(self, run_id, interval=2.0):
        run = self.get_run(run_id)
        if not run:
            return None
        rows = self.samples(run_id)
        events = self.events(run_id)
        route_changes = [e for e in events if e["kind"] == "ROUTE_CHANGE"]
        end = run["ended"] or (rows[-1]["ts"] if rows else run["started"])
        slow_rows = [r for r in rows if r["path"] == "SLOW"]
        fast_rows = [r for r in rows if r["path"] == "FAST"]
        rtt_slow, rtt_fast = self._rtt_avg(slow_rows), self._rtt_avg(fast_rows)
        improvement = ((rtt_slow - rtt_fast) / rtt_slow * 100.0) if (rtt_slow and rtt_fast) else None

        phases, order = {}, []
        for r in rows:
            if r["phase"] not in phases:
                phases[r["phase"]] = []
                order.append(r["phase"])
            phases[r["phase"]].append(r)
        phase_rows = []
        for idx, name in enumerate(order):
            pr = phases[name]
            mbps = [r["slow_mbps"] or 0 for r in pr]
            nxt = phases[order[idx + 1]][0]["ts"] if idx + 1 < len(order) else None
            span = (nxt - pr[0]["ts"]) if nxt else (pr[-1]["ts"] - pr[0]["ts"] + (
                (pr[-1]["ts"] - pr[0]["ts"]) / (len(pr) - 1) if len(pr) > 1 else interval))
            phase_rows.append({
                "phase": name,
                "start_offset_s": round(pr[0]["ts"] - run["started"], 1),
                "seconds": round(span, 1),
                "avg_slow_mbps": round(sum(mbps) / len(mbps), 3),
                "max_slow_mbps": round(max(mbps), 3),
                "avg_rtt_ms": self._rtt_avg(pr),
                "loss_pct": self._loss(pr),
                "slow_samples": sum(1 for r in pr if r["path"] == "SLOW"),
                "fast_samples": sum(1 for r in pr if r["path"] == "FAST"),
            })
        mbps_all = [r["slow_mbps"] or 0 for r in rows]
        return {
            "id": run["id"], "name": run["name"], "mode": run["mode"], "status": run["status"],
            "started": run["started"], "ended": run["ended"],
            "duration_s": round(end - run["started"], 1),
            "samples": len(rows),
            "route_changes": len(route_changes),
            "avg_slow_mbps": round(sum(mbps_all) / len(mbps_all), 3) if mbps_all else 0.0,
            "max_slow_mbps": round(max(mbps_all), 3) if mbps_all else 0.0,
            "rtt_slow_ms": rtt_slow, "rtt_fast_ms": rtt_fast, "rtt_improvement_pct": improvement,
            "loss_pct": self._loss(rows),
            "phases": phase_rows,
            "events": events,
            "config": json.loads(run["config"] or "{}"),
        }
