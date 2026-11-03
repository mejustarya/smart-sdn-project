"""CSV and PDF report generation for experiment runs."""
import csv
import io
import time

from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _ts(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""


def _f(v, d=2, unit=""):
    return "n/a" if v is None else f"{v:.{d}f}{unit}"


def run_csv(store, run_id):
    run = store.get_run(run_id)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["time", "seconds_from_start", "phase", "path", "slow_link_mbps", "rtt_ms",
                "jitter_ms", "packet_loss_pct", "sync_error_ms"])
    for r in store.samples(run_id):
        w.writerow([_ts(r["ts"]), round(r["ts"] - run["started"], 1), r["phase"], r["path"],
                    r["slow_mbps"], r["rtt_ms"], r["jitter_ms"], r["loss_pct"], r["sync_err_ms"]])
    return out.getvalue()


def runs_csv(store):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["id", "name", "mode", "started", "duration_s", "status", "route_changes",
                "avg_slow_mbps", "max_slow_mbps", "rtt_slow_ms", "rtt_fast_ms",
                "rtt_improvement_pct", "loss_pct"])
    for run in reversed(store.list_runs()):
        s = store.summary(run["id"])
        w.writerow([s["id"], s["name"], s["mode"], _ts(s["started"]), s["duration_s"], s["status"],
                    s["route_changes"], s["avg_slow_mbps"], s["max_slow_mbps"], s["rtt_slow_ms"],
                    s["rtt_fast_ms"], s["rtt_improvement_pct"], s["loss_pct"]])
    return out.getvalue()


def _chart(title, points, ylabel, color, hline=None, width=470, height=170):
    d = Drawing(width, height + 22)
    d.add(String(0, height + 8, title, fontSize=10, fillColor=colors.HexColor("#222222")))
    if len(points) < 2:
        d.add(String(10, height / 2, "No data recorded", fontSize=9, fillColor=colors.grey))
        return d
    lp = LinePlot()
    lp.x, lp.y, lp.width, lp.height = 42, 22, width - 60, height - 18
    series = [points]
    if hline is not None:
        series.append([(points[0][0], hline), (points[-1][0], hline)])
    lp.data = series
    lp.lines[0].strokeColor = color
    lp.lines[0].strokeWidth = 1.6
    if hline is not None:
        lp.lines[1].strokeColor = colors.HexColor("#cc3344")
        lp.lines[1].strokeDashArray = (4, 3)
        lp.lines[1].strokeWidth = 1
    ys = [p[1] for p in points] + ([hline] if hline is not None else [])
    lp.yValueAxis.valueMin = 0
    lp.yValueAxis.valueMax = max(ys) * 1.15 or 1
    lp.xValueAxis.valueMin = points[0][0]
    lp.xValueAxis.valueMax = max(points[-1][0], points[0][0] + 1)
    lp.xValueAxis.labels.fontSize = lp.yValueAxis.labels.fontSize = 7
    lp.yValueAxis.labelTextFormat = "%.1f"
    lp.xValueAxis.labelTextFormat = "%d"
    d.add(lp)
    d.add(String(2, height - 6, ylabel, fontSize=7, fillColor=colors.grey))
    d.add(String(width / 2 - 25, 2, "seconds from start", fontSize=7, fillColor=colors.grey))
    return d


def _table(rows, col_widths=None, header=True):
    t = Table(rows, colWidths=col_widths, hAlign="LEFT")
    style = [("FONTSIZE", (0, 0), (-1, -1), 8), ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#bbbbbb")),
             ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f3a5f")),
                  ("TEXTCOLOR", (0, 0), (-1, 0), colors.white)]
    t.setStyle(TableStyle(style))
    return t


def run_pdf(store, run_id, high_threshold=2.5):
    s = store.summary(run_id)
    run = store.get_run(run_id)
    samples = store.samples(run_id)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm, title=f"Experiment report #{run_id}")
    st = getSampleStyleSheet()
    el = [Paragraph("Smart SDN – Experiment Report", st["Title"]),
          Paragraph(f"<b>{s['name']}</b> (run #{s['id']}, {s['mode']} mode)", st["Normal"]),
          Paragraph(f"Started {_ts(s['started'])} · duration {s['duration_s']} s · status {s['status']}", st["Normal"]),
          Spacer(1, 8)]

    el.append(Paragraph("Summary", st["Heading3"]))
    el.append(_table([
        ["Metric", "Value"],
        ["Automatic route changes", str(s["route_changes"])],
        ["Average / peak slow-link throughput", f"{s['avg_slow_mbps']:.2f} / {s['max_slow_mbps']:.2f} Mbps"],
        ["Sync RTT while on SLOW path", _f(s["rtt_slow_ms"], 1, " ms")],
        ["Sync RTT while on FAST path (after reroute)", _f(s["rtt_fast_ms"], 1, " ms")],
        ["RTT improvement after reroute", _f(s["rtt_improvement_pct"], 1, " %")],
        ["Synchronization packet loss (timeouts)", _f(s["loss_pct"], 1, " %")],
    ], [95 * mm, 70 * mm]))
    el.append(Spacer(1, 8))

    if s["phases"]:
        el.append(Paragraph("Per-phase results", st["Heading3"]))
        rows = [["Phase", "Start (s)", "Length (s)", "Avg slow Mbps", "Peak Mbps", "Avg RTT ms", "Loss %", "SLOW/FAST samples"]]
        for p in s["phases"]:
            rows.append([p["phase"], p["start_offset_s"], p["seconds"], f"{p['avg_slow_mbps']:.2f}",
                         f"{p['max_slow_mbps']:.2f}", _f(p["avg_rtt_ms"], 1), _f(p["loss_pct"], 1),
                         f"{p['slow_samples']}/{p['fast_samples']}"])
        el.append(_table(rows))
        el.append(Spacer(1, 8))

    changes = [e for e in s["events"] if e["kind"] == "ROUTE_CHANGE"]
    el.append(Paragraph("Controller routing events", st["Heading3"]))
    if changes:
        rows = [["Time", "Phase", "Event"]]
        rows += [[_ts(e["ts"]), e["phase"], e["detail"]] for e in changes]
        el.append(_table(rows, [38 * mm, 28 * mm, 99 * mm]))
    else:
        el.append(Paragraph("No route changes were recorded during this run.", st["Normal"]))
    el.append(Spacer(1, 10))

    t0 = run["started"]
    thr = [(r["ts"] - t0, r["slow_mbps"] or 0) for r in samples]
    rtt = [(r["ts"] - t0, r["rtt_ms"]) for r in samples if r["rtt_ms"] is not None]
    el.append(_chart("Slow-link throughput (red dashed = reroute threshold)", thr, "Mbps", colors.HexColor("#1f77b4"), hline=high_threshold))
    el.append(Spacer(1, 6))
    el.append(_chart("Synchronization RTT", rtt, "ms", colors.HexColor("#e07b00")))
    el.append(Spacer(1, 6))
    el.append(Paragraph(f"Generated {_ts(time.time())} by the Smart SDN dashboard. "
                        "RTT values come from the UDP synchronization clients (h1–h3); throughput is measured by the "
                        "Ryu controller on the slow link (s1 port 5).", st["Italic"]))
    doc.build(el)
    return buf.getvalue()
