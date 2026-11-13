"""The controller's decision logic, kept free of Ryu so it can be unit-tested.

dynamic: sync traffic starts on SLOW. If the measured SLOW-link rate reaches
         `high_mbps` it moves to FAST; if the rate falls to `low_mbps` or less it
         returns to SLOW. Two thresholds give hysteresis. `hold_down` is the
         minimum time between two route changes.
static:  sync traffic always stays on SLOW.
"""


class RoutePolicy:
    def __init__(self, high_mbps=2.5, low_mbps=0.8, hold_down=4.0, policy="dynamic"):
        assert policy in ("dynamic", "static")
        self.high, self.low, self.hold_down = high_mbps, low_mbps, hold_down
        self.policy = policy
        self.path = "SLOW"
        self.last_change = float("-inf")
        self.changes = 0

    def reset(self, policy=None):
        """Back to the default route, e.g. when a new experiment run starts."""
        if policy is not None:
            assert policy in ("dynamic", "static")
            self.policy = policy
        self.path = "SLOW"
        self.last_change = float("-inf")
        self.changes = 0

    def update(self, rate_mbps, now):
        """Feed one SLOW-link rate measurement. Returns the new path name if the
        route must change, otherwise None."""
        if self.policy != "dynamic":
            return None
        if now - self.last_change < self.hold_down:
            return None
        new = None
        if self.path == "SLOW" and rate_mbps >= self.high:
            new = "FAST"
        elif self.path == "FAST" and rate_mbps <= self.low:
            new = "SLOW"
        if new:
            self.path, self.last_change = new, now
            self.changes += 1
        return new
