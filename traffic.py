"""Congestion scenarios: Low / Moderate / High / Extreme and burst traffic.

Three background generators (like h5, h6, h7 in the SDN project) each send
constant-rate UDP to the network. Offered load against the 5 Mbps SLOW link:

    low 1.5 Mbps | moderate 3 Mbps | high 6 Mbps | extreme 9 Mbps

Burst mode repeats "send for `on` seconds, idle for `off` seconds".
"""
import threading

from bg_traffic import run_bg

LEVELS = {"low": 0.5, "moderate": 1.0, "high": 2.0, "extreme": 3.0}  # Mbps per generator
N_GENERATORS = 3
PAYLOAD = 1000  # bytes


def pps(mbps, size=PAYLOAD):
    return mbps * 1e6 / 8 / size


def _burst(target, rate_pps, size, stop, on, off):
    while not stop.is_set():
        phase_stop = threading.Event()
        t = threading.Thread(target=run_bg, args=(target, rate_pps, size, phase_stop), daemon=True)
        t.start()
        stop.wait(on)
        phase_stop.set()
        t.join()
        if stop.wait(off):
            break


def start_generators(level, target, stop, burst=False, on=4.0, off=3.0, size=PAYLOAD):
    rate = pps(LEVELS[level], size)
    threads = []
    for _ in range(N_GENERATORS):
        if burst:
            t = threading.Thread(target=_burst, args=(target, rate, size, stop, on, off), daemon=True)
        else:
            t = threading.Thread(target=run_bg, args=(target, rate, size, stop), daemon=True)
        t.start()
        threads.append(t)
    return threads
