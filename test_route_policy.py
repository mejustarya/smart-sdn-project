"""Unit tests for the controller's decision logic (no Ryu or Mininet needed).

Run:  python3 test_route_policy.py
"""
from route_policy import RoutePolicy


def test_stays_slow_below_high_threshold():
    p = RoutePolicy(2.5, 0.8, 4.0)
    assert p.update(2.4, now=10) is None and p.path == "SLOW"


def test_moves_to_fast_at_high_threshold():
    p = RoutePolicy(2.5, 0.8, 4.0)
    assert p.update(2.5, now=10) == "FAST" and p.path == "FAST"      # >= is inclusive


def test_hold_down_blocks_quick_return():
    p = RoutePolicy(2.5, 0.8, 4.0)
    p.update(5.0, now=10)                                            # -> FAST
    assert p.update(0.0, now=12) is None and p.path == "FAST"        # only 2 s later
    assert p.update(0.0, now=14) == "SLOW"                           # 4 s later


def test_hysteresis_band_keeps_route():
    p = RoutePolicy(2.5, 0.8, 4.0)
    p.update(5.0, now=0)
    assert p.update(1.5, now=20) is None and p.path == "FAST"        # between 0.8 and 2.5


def test_return_threshold_is_inclusive():
    p = RoutePolicy(2.5, 0.8, 4.0)
    p.update(5.0, now=0)
    assert p.update(0.8, now=10) == "SLOW"


def test_static_never_changes():
    p = RoutePolicy(2.5, 0.8, 4.0, policy="static")
    assert p.update(9.0, now=100) is None and p.path == "SLOW"


def test_reset_restores_defaults():
    p = RoutePolicy(2.5, 0.8, 4.0)
    p.update(5.0, now=0)
    p.reset("static")
    assert p.path == "SLOW" and p.policy == "static" and p.changes == 0
    p.reset("dynamic")
    assert p.update(5.0, now=0.1) == "FAST"                          # hold-down also cleared


def test_counts_route_changes():
    p = RoutePolicy(2.5, 0.8, 4.0)
    p.update(5.0, now=0); p.update(0.0, now=5); p.update(5.0, now=10)
    assert p.changes == 3


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok  ", t.__name__)
    print(f"{len(tests)} tests passed")
