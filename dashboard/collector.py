"""Samples controller status and synchronization results every few seconds."""
import json
import threading
import time
from collections import deque


class Collector(threading.Thread):
    SYNC_FILES = ["h1-sync.csv", "h2-sync.csv", "h3-sync.csv"]

    def __init__(self, root, interval=2.0):
        super().__init__(daemon=True, name="collector")
        self.interval = interval
        self.log_dir = root / "logs"
        self.status_file = self.log_dir / "controller_status.json"
        self.series = deque(maxlen=900)
        self.callbacks = []
        self.latest = None
        self._pos = {}
        self._first_pass = True

    def read_status(self):
        try:
            return json.loads(self.status_file.read_text())
        except Exception:
            return {}

    def _read_sync(self):
        """Parse only the new complete CSV lines written since the last sample."""
        rtts, errs, timeouts, jitter_diffs = [], [], 0, []
        for name in self.SYNC_FILES:
            path = self.log_dir / name
            try:
                size = path.stat().st_size
            except OSError:
                self._pos.pop(name, None)
                continue
            if name not in self._pos:
                # Ignore history from before the dashboard started; pick up new files from byte 0.
                self._pos[name] = size if self._first_pass else 0
            if size < self._pos[name]:          # file restarted by a new sync run
                self._pos[name] = 0
            if size == self._pos[name]:
                continue
            try:
                with open(path, "rb") as fh:
                    fh.seek(self._pos[name])
                    chunk = fh.read(size - self._pos[name])
            except OSError:
                continue
            end = chunk.rfind(b"\n")
            if end < 0:
                continue
            self._pos[name] += end + 1
            file_rtts = []
            for line in chunk[:end].decode(errors="replace").splitlines():
                parts = line.split(",")
                if len(parts) < 3 or parts[0] == "client_id":
                    continue
                if parts[2] == "TIMEOUT":
                    timeouts += 1
                    continue
                try:
                    file_rtts.append(float(parts[2]))
                    if len(parts) > 6:
                        errs.append(float(parts[6]))
                except ValueError:
                    continue
            rtts += file_rtts
            jitter_diffs += [abs(b - a) for a, b in zip(file_rtts, file_rtts[1:])]
        return rtts, errs, timeouts, jitter_diffs

    def sample_once(self):
        status = self.read_status()
        rtts, errs, timeouts, jit = self._read_sync()
        total = len(rtts) + timeouts
        sample = {
            "ts": time.time(),
            "label": time.strftime("%H:%M:%S"),
            "path": status.get("path", "UNKNOWN"),
            "slow_mbps": float(status.get("slow_link_mbps") or 0.0),
            "rtt_ms": (sum(rtts) / len(rtts)) if rtts else None,
            "jitter_ms": (sum(jit) / len(jit)) if jit else None,
            "loss_pct": (100.0 * timeouts / total) if total else None,
            "sync_err_ms": (sum(errs) / len(errs)) if errs else None,
            "rtt_n": len(rtts),
            "timeouts": timeouts,
        }
        self._first_pass = False
        self.latest = sample
        self.series.append(sample)
        for cb in list(self.callbacks):
            try:
                cb(sample)
            except Exception as exc:  # never let a recorder kill the collector
                print(f"collector callback error: {exc}")
        return sample

    def run(self):
        while True:
            started = time.time()
            try:
                self.sample_once()
            except Exception as exc:
                print(f"collector error: {exc}")
            time.sleep(max(0.2, self.interval - (time.time() - started)))
