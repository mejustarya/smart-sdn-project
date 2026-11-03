"""Automatic experiment runner: baseline -> congestion levels -> recovery, recorded in SQLite."""
import math
import threading
import time

LEVELS = ["low", "moderate", "high", "extreme"]
MAX_TOTAL_SECONDS = 480          # sync clients are limited to 1000 rounds (~500 s)


class ExperimentRunner:
    def __init__(self, store, collector, control):
        self.store, self.collector, self.control = store, collector, control
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._state = {"active": False}
        self._prev_path = None
        collector.callbacks.append(self._on_sample)

    # ---- planning
    @staticmethod
    def build_plan(cfg):
        stage = int(cfg.get("stage_seconds", 40))
        if not 5 <= stage <= 120:
            raise ValueError("Seconds per stage must be between 5 and 120")
        base = int(cfg.get("baseline_seconds", 20))
        rec = int(cfg.get("recovery_seconds", 30))
        if cfg.get("mode") == "single":
            level = cfg.get("level", "high")
            if level not in LEVELS:
                raise ValueError("Unknown congestion level")
            plan = [("Baseline", "baseline", 10), (level.title(), "udp:" + level, stage), ("Recovery", "recovery", rec)]
        else:
            plan = [("Baseline", "baseline", base)]
            plan += [(l.title(), "udp:" + l, stage) for l in LEVELS]
            if cfg.get("include_tcp"):
                plan.append(("TCP flows", "tcp", stage))
            if cfg.get("include_burst"):
                plan.append(("Burst", "burst", stage))
            plan.append(("Recovery", "recovery", rec))
        total = sum(p[2] for p in plan)
        if total > MAX_TOTAL_SECONDS:
            raise ValueError(f"Experiment would take {total}s; the maximum is {MAX_TOTAL_SECONDS}s. Reduce the stage length.")
        return [{"name": n, "kind": k, "seconds": s} for n, k, s in plan]

    # ---- public API
    def start(self, cfg):
        with self._lock:
            if self._state.get("active"):
                return {"ok": False, "message": "An experiment is already running"}
            try:
                plan = self.build_plan(cfg)
            except (ValueError, TypeError) as exc:
                return {"ok": False, "message": str(exc)}
            name = (cfg.get("name") or "").strip()[:60] or time.strftime("Experiment %Y-%m-%d %H:%M")
            run_id = self.store.create_run(name, cfg.get("mode", "auto"), {**cfg, "plan": plan})
            self._stop.clear()
            self._prev_path = None
            self._state = {"active": True, "run_id": run_id, "name": name, "mode": cfg.get("mode", "auto"),
                           "plan": plan, "phase": None, "phase_index": -1, "started": time.time(),
                           "phase_started": time.time(), "total": sum(p["seconds"] for p in plan),
                           "message": "Starting…", "status": "running"}
        threading.Thread(target=self._run, args=(run_id, plan), daemon=True, name="experiment").start()
        return {"ok": True, "message": f"Experiment '{name}' started", "run_id": run_id}

    def stop(self):
        if not self._state.get("active"):
            return {"ok": False, "message": "No experiment is running"}
        self._stop.set()
        return {"ok": True, "message": "Stopping experiment…"}

    def get_state(self):
        st = dict(self._state)
        if st.get("active"):
            now = time.time()
            st["elapsed"] = round(now - st["started"], 1)
            st["phase_elapsed"] = round(now - st["phase_started"], 1)
        return st

    # ---- internals
    def _on_sample(self, s):
        st = self._state
        if not st.get("active") or not st.get("phase"):
            return
        run_id, phase = st["run_id"], st["phase"]
        self.store.add_sample(run_id, s, phase)
        path = s["path"]
        if path in ("SLOW", "FAST"):
            if self._prev_path and path != self._prev_path:
                self.store.add_event(run_id, s["ts"], phase, "ROUTE_CHANGE",
                                     f"{self._prev_path} -> {path} (slow link {s['slow_mbps']:.2f} Mbps)")
            self._prev_path = path

    def _apply(self, phase):
        kind, secs = phase["kind"], phase["seconds"] + 5
        if kind in ("baseline", "recovery"):
            return self.control({"action": "stop"})
        if kind.startswith("udp:"):
            return self.control({"action": "scenario", "level": kind[4:], "seconds": secs})
        if kind == "tcp":
            return self.control({"action": "tcp", "seconds": secs, "flows": 2})
        if kind == "burst":
            return self.control({"action": "burst", "seconds": secs, "rate_mbps": 3, "on_seconds": 4, "off_seconds": 3})
        return {"ok": False, "message": f"Unknown phase {kind}"}

    def _run(self, run_id, plan):
        status, message = "completed", "Experiment completed"
        try:
            first = self.control({"action": "stop"})
            if not first.get("ok"):
                raise RuntimeError(first.get("message", "Mininet control unavailable"))
            total = sum(p["seconds"] for p in plan)
            rounds = min(1000, int(math.ceil(total / 0.5)) + 10)   # each round takes >= 0.5 s
            res = self.control({"action": "sync", "rounds": rounds})
            if not res.get("ok"):
                raise RuntimeError(res.get("message", "Could not start synchronization"))
            for i, phase in enumerate(plan):
                if self._stop.is_set():
                    break
                self._state.update(phase=phase["name"], phase_index=i, phase_started=time.time(),
                                   message=f"Phase {i + 1}/{len(plan)}: {phase['name']}")
                res = self._apply(phase)
                if not res.get("ok"):
                    raise RuntimeError(res.get("message", "Phase failed"))
                deadline = time.time() + phase["seconds"]
                while time.time() < deadline and not self._stop.is_set():
                    time.sleep(0.5)
            if self._stop.is_set():
                status, message = "stopped", "Experiment stopped by user"
        except Exception as exc:
            status, message = "failed", str(exc)
        finally:
            self.control({"action": "stop"})
            self.control({"action": "stopsync"})
            self.store.finish_run(run_id, status)
            self._state.update(active=False, phase=None, status=status, message=message, finished=time.time())
