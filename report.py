#!/usr/bin/env python3
"""Final PDF report for a finished experiment run.

Reads the CSVs an experiment writes (summary.csv, raw_samples.csv, route_events.csv,
controller_samples.csv) and builds `final_report.pdf` with:

  * the setup and the key findings
  * the summary table (one row per policy x scenario)
  * overview charts (clock error and round-trip delay for every policy / scenario)
  * for every scenario: the NTP-style measurements over time, per policy
        - clock offset  theta = ((t2-t1)+(t3-t4))/2   (what the client measures)
        - round-trip delay  delta = (t4-t1)-(t3-t2)
        - true clock error after the filter (log scale)
    with the periods where Ryu had moved sync traffic to the FAST path shaded
  * the controller's view of the slow link and its route changes

Used automatically at the end of mininet_experiment.py and by the dashboard. Also usable alone:

    python3 report.py results_mininet                 # -> results_mininet/final_report.pdf
    python3 report.py results_sdn --out my_report.pdf
"""
import argparse
import csv
import datetime as dt
import math
import os
import statistics as st

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import cm  # noqa: E402
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,  # noqa: E402
                                Spacer, Table, TableStyle)

POLICY_ORDER = ["static", "dynamic", "static+prio", "dynamic+prio"]
SCENARIO_ORDER = ["baseline", "low", "moderate", "high", "extreme", "burst"]
POLICY_COLORS = {"static": "#7f7f7f", "dynamic": "#1f77b4", "static+prio": "#ff7f0e", "dynamic+prio": "#2ca02c"}
CLIENT_COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd"]
FAST_SHADE = "#1f77b4"
NAN = float("nan")


# ----------------------------------------------------------------------------- loading
def _num(v):
    try:
        return float(v) if v not in ("", None) else NAN
    except ValueError:
        return NAN


def _load(path):
    try:
        with open(path, newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def load_results(d):
    """Read all CSVs of a results directory into plain Python structures."""
    summary = _load(os.path.join(d, "summary.csv"))
    for s in summary:
        for k, v in list(s.items()):
            if k not in ("policy", "scenario"):
                s[k] = _num(v)
    raw = _load(os.path.join(d, "raw_samples.csv"))
    for r in raw:
        for k in ("seq", "lost", "t", "delay_ms", "theta_ms", "raw_error_ms", "synced_error_ms",
                  "initial_skew_ms"):
            if k in r:
                r[k] = _num(r[k])
    events = _load(os.path.join(d, "route_events.csv"))
    for e in events:
        e["t"], e["rate_mbps"] = _num(e["t"]), _num(e["rate_mbps"])
    ctrl = _load(os.path.join(d, "controller_samples.csv"))
    for c in ctrl:
        c["t"], c["slow_mbps"] = _num(c["t"]), _num(c["slow_mbps"])
    return summary, raw, events, ctrl


def _order(items, ref):
    return sorted(items, key=lambda x: ref.index(x) if x in ref else len(ref))


def _fast_intervals(events, end):
    out, start = [], None
    for e in sorted(events, key=lambda e: e["t"]):
        if e["path"] == "FAST" and start is None:
            start = e["t"]
        elif e["path"] == "SLOW" and start is not None:
            out.append((start, e["t"]))
            start = None
    if start is not None:
        out.append((start, end))
    return out


def _fmt(v, nd=2):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "-"
    return f"{v:.{nd}f}"


# ----------------------------------------------------------------------------- charts
def _clean_axes(ax):
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(alpha=0.25, lw=0.5)


def chart_overview(summary, path):
    pols = _order({s["policy"] for s in summary}, POLICY_ORDER)
    scens = _order({s["scenario"] for s in summary}, SCENARIO_ORDER)
    get = lambda p, s, k: next((x[k] for x in summary if x["policy"] == p and x["scenario"] == s), NAN)
    fig, ax = plt.subplots(1, 3, figsize=(15, 4.2))
    w = 0.8 / max(len(pols), 1)
    specs = [("steady_synced_err_ms", "Clock error after sync (steady state)", "error (ms, log scale)", True),
             ("mean_rtt_ms", "Mean round-trip delay", "delay (ms)", False),
             ("loss_%", "Probe loss", "% of probes lost", False)]
    for a, (key, title, yl, log) in zip(ax, specs):
        for i, p in enumerate(pols):
            vals = [get(p, s, key) for s in scens]
            vals = [max(v, 0.01) if log and v == v else v for v in vals]
            a.bar([j + i * w for j in range(len(scens))], vals, w, label=p, color=POLICY_COLORS.get(p))
        a.set_xticks([j + w * (len(pols) - 1) / 2 for j in range(len(scens))])
        a.set_xticklabels(scens, fontsize=8)
        a.set_title(title, fontsize=10)
        a.set_ylabel(yl, fontsize=8)
        if log:
            a.set_yscale("log")
        _clean_axes(a)
    ax[0].legend(fontsize=7, frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def chart_scenario(scen, pols, raw, events, duration, path):
    """Rows: NTP offset, round-trip delay, clock error. Columns: one per policy."""
    n = len(pols)
    fig, ax = plt.subplots(3, n, figsize=(4.8 * n, 4.5), squeeze=False, sharex=True)
    rows_by = {p: [r for r in raw if r["policy"] == p and r["scenario"] == scen] for p in pols}
    # common y ranges so policies are directly comparable
    all_ok = [r for p in pols for r in rows_by[p] if not r["lost"]]
    th = sorted(abs(r["theta_ms"]) for r in all_ok if r["theta_ms"] == r["theta_ms"] and r["seq"] >= 1)
    for j, p in enumerate(pols):
        rows = rows_by[p]
        ev = [e for e in events if e["policy"] == p and e["scenario"] == scen]
        clients = sorted({r["client"] for r in rows})
        for i, c in enumerate(clients):
            cr = sorted((r for r in rows if r["client"] == c), key=lambda r: r["t"])
            ok = [r for r in cr if not r["lost"]]
            col = CLIENT_COLORS[i % len(CLIENT_COLORS)]
            # the first probe of each client measures the initial seconds-sized skew: show from probe 1 on
            ok_th = [r for r in ok if r["seq"] >= 1]
            ax[0][j].plot([r["t"] for r in ok_th], [r["theta_ms"] for r in ok_th], ".", ms=3, alpha=0.7,
                          color=col, label=c)
            ax[1][j].plot([r["t"] for r in ok], [r["delay_ms"] for r in ok], ".", ms=3, alpha=0.7, color=col)
            ax[2][j].plot([r["t"] for r in cr], [max(r["synced_error_ms"], 0.005) for r in cr], "-", lw=1,
                          color=col)
            lost = [r for r in cr if r["lost"]]
            if lost:
                ax[1][j].plot([r["t"] for r in lost], [0] * len(lost), "x", ms=4, color="#d62728",
                              label="lost probe" if i == 0 else None)
        for a in ax[:, j]:
            for k, (x0, x1) in enumerate(_fast_intervals(ev, duration)):
                a.axvspan(x0, x1, color=FAST_SHADE, alpha=0.10,
                          label="sync on FAST path" if (k == 0 and a is ax[0][j]) else None)
        ax[0][j].axhline(0, color="k", lw=0.6)
        ax[0][j].set_title(p, fontsize=11, color=POLICY_COLORS.get(p), fontweight="bold")
        ax[2][j].set_yscale("log")
        ax[2][j].set_xlabel("time (s)", fontsize=8)
        for a in ax[:, j]:
            _clean_axes(a)
            a.tick_params(labelsize=7)
        if j:
            pass
    ax[0][0].set_ylabel("NTP offset θ (ms)", fontsize=8)
    ax[1][0].set_ylabel("round-trip delay δ (ms)", fontsize=8)
    ax[2][0].set_ylabel("true clock error (ms, log)", fontsize=8)
    h, l = ax[0][0].get_legend_handles_labels()
    ax[0][-1].legend(h, l, fontsize=7, frameon=False, loc="upper right")
    if th:
        lim = max(2.0, th[int(0.99 * (len(th) - 1))] * 1.2)
        for j in range(n):
            ax[0][j].set_ylim(-lim, lim)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def chart_offset_convergence(raw, scen, pols, path):
    """Offset θ of every client, first seconds only: shows the initial skew being removed."""
    fig, ax = plt.subplots(1, 2, figsize=(11, 3.6))
    rows = [r for r in raw if r["scenario"] == scen and r["policy"] == pols[0] and not r["lost"]]
    clients = sorted({r["client"] for r in rows})
    for i, c in enumerate(clients):
        cr = sorted((r for r in rows if r["client"] == c), key=lambda r: r["t"])[:8]
        ax[0].plot([r["seq"] for r in cr], [r["theta_ms"] for r in cr], "o-", ms=4,
                   color=CLIENT_COLORS[i % 4], label=f"{c} (start error {cr[0]['initial_skew_ms']:+.0f} ms)")
        ax[1].semilogy([r["seq"] for r in cr], [max(r["synced_error_ms"], 0.005) for r in cr], "o-", ms=4,
                       color=CLIENT_COLORS[i % 4])
    ax[0].set_title("Measured offset θ, first probes", fontsize=10)
    ax[0].set_ylabel("ms")
    ax[1].set_title("True clock error, first probes (log)", fontsize=10)
    ax[1].set_ylabel("ms")
    for a in ax:
        a.set_xlabel("probe number")
        _clean_axes(a)
    ax[0].legend(fontsize=7, frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def chart_controller(scen, pols, ctrl, events, duration, high, low, path):
    pols = [p for p in pols if p.startswith("dynamic")]
    if not pols:
        return False
    fig, ax = plt.subplots(1, len(pols), figsize=(5.4 * len(pols), 2.5), squeeze=False)
    for j, p in enumerate(pols):
        a = ax[0][j]
        smp = sorted((c for c in ctrl if c["policy"] == p and c["scenario"] == scen), key=lambda c: c["t"])
        ev = [e for e in events if e["policy"] == p and e["scenario"] == scen]
        if smp:
            a.step([0] + [c["t"] for c in smp], [smp[0]["slow_mbps"]] + [c["slow_mbps"] for c in smp],
                   where="pre", color="#1f77b4", label="slow-link rate")
        a.axhline(high, color="#d62728", ls="--", lw=1, label=f"high threshold {high:g}")
        a.axhline(low, color="#2ca02c", ls="--", lw=1, label=f"low threshold {low:g}")
        for k, (x0, x1) in enumerate(_fast_intervals(ev, duration)):
            a.axvspan(x0, x1, color=FAST_SHADE, alpha=0.12, label="sync on FAST path" if k == 0 else None)
        a.set_title(f"{p}: controller view", fontsize=10)
        a.set_xlabel("time (s)")
        a.set_ylabel("Mbps")
        a.legend(fontsize=7, frameon=False, loc="upper right")
        _clean_axes(a)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return True


# ----------------------------------------------------------------------------- findings
def findings(summary):
    """A few plain-language sentences computed from the numbers (never hard-coded results)."""
    out = []
    get = lambda p, s: next((x for x in summary if x["policy"] == p and x["scenario"] == s), None)
    scens = _order({s["scenario"] for s in summary}, SCENARIO_ORDER)
    worst = None
    for s in scens:
        r = get("static", s)
        if r and r["steady_synced_err_ms"] == r["steady_synced_err_ms"]:
            if worst is None or r["steady_synced_err_ms"] > worst["steady_synced_err_ms"]:
                worst = r
    if worst and worst["steady_synced_err_ms"] > 1:
        line = (f"Without protection (static), the worst case was the <b>{worst['scenario']}</b> scenario: "
                f"mean round-trip delay {_fmt(worst['mean_rtt_ms'], 1)} ms, "
                f"{_fmt(worst['loss_%'], 1)}% of probes lost and a steady clock error of "
                f"{_fmt(worst['steady_synced_err_ms'], 1)} ms.")
        out.append(line)
        sc = worst["scenario"]
        for p in ("dynamic", "static+prio", "dynamic+prio"):
            r = get(p, sc)
            if r:
                out.append(f"<b>{p}</b> in the same scenario: delay {_fmt(r['mean_rtt_ms'], 1)} ms, "
                           f"loss {_fmt(r['loss_%'], 1)}%, steady error {_fmt(r['steady_synced_err_ms'], 2)} ms"
                           + (f", first reroute at {_fmt(r['first_reroute_s'], 1)} s" if r["first_reroute_s"] == r["first_reroute_s"] else "")
                           + ".")
    elif summary:
        out.append("No scenario produced a significant clock error for the policies that were run.")
    base = [x for x in summary if x["scenario"] == "baseline"]
    if base:
        out.append(f"On an idle network (baseline) the clock error stays near "
                   f"{_fmt(st.mean(b['steady_synced_err_ms'] for b in base), 2)} ms: this is the measurement floor.")
    return out


# ----------------------------------------------------------------------------- PDF
def build_report(results_dir, out_path=None, title="Time Synchronization under Congestion",
                 info=None, high=2.5, low=0.8):
    """Build the PDF. `info` is an optional dict of setup facts shown on the first page."""
    summary, raw, events, ctrl = load_results(results_dir)
    if not summary or not raw:
        raise RuntimeError(f"no results in {results_dir} (need summary.csv and raw_samples.csv)")
    out_path = out_path or os.path.join(results_dir, "final_report.pdf")
    tmp = os.path.join(results_dir, "_report_tmp")
    os.makedirs(tmp, exist_ok=True)

    ss = getSampleStyleSheet()
    H1 = ParagraphStyle("H1", parent=ss["Heading1"], fontSize=20, spaceAfter=6, textColor=colors.HexColor("#1b2a41"))
    H2 = ParagraphStyle("H2", parent=ss["Heading2"], fontSize=13, spaceBefore=10, spaceAfter=4,
                        textColor=colors.HexColor("#1b2a41"))
    BODY = ParagraphStyle("B", parent=ss["BodyText"], fontSize=9.5, leading=13)
    SMALL = ParagraphStyle("S", parent=BODY, fontSize=8, leading=10.5, textColor=colors.HexColor("#555555"))
    page = A4

    def on_page(c, d):
        c.saveState()
        c.setFont("Helvetica", 7.5)
        c.setFillColor(colors.HexColor("#777777"))
        c.drawString(1.5 * cm, 1 * cm, title)
        c.drawRightString(page[0] - 1.5 * cm, 1 * cm, f"page {d.page}")
        c.restoreState()

    doc = SimpleDocTemplate(out_path, pagesize=page, leftMargin=1.5 * cm, rightMargin=1.5 * cm,
                            topMargin=1.5 * cm, bottomMargin=1.6 * cm, title=title + " - final report")
    width = page[0] - 3 * cm
    story = []

    def img(path, w=width):
        from PIL import Image as PI
        iw, ih = PI.open(path).size
        return Image(path, width=w, height=w * ih / iw)

    pols = _order({s["policy"] for s in summary}, POLICY_ORDER)
    scens = _order({s["scenario"] for s in summary}, SCENARIO_ORDER)
    duration = max((r["t"] for r in raw if r["t"] == r["t"]), default=16) + 0.5
    n_clients = len({r["client"] for r in raw})
    n_probes = len(raw)

    # ---- page 1: title, setup, findings, summary table
    story += [Paragraph(title, H1),
              Paragraph("Final experiment report - UDP time synchronization over Mininet, Open vSwitch and Ryu", BODY),
              Paragraph(dt.datetime.now().strftime("Generated %d %b %Y, %H:%M"), SMALL), Spacer(1, 8)]
    setup = [["Policies compared", ", ".join(pols)],
             ["Scenarios", ", ".join(scens)],
             ["Runs / clients / probes", f"{len(summary)} runs, {n_clients} clients, {n_probes} probes in total"],
             ["Controller thresholds", f"reroute above {high:g} Mbps, back below {low:g} Mbps"]]
    for k, v in (info or {}).items():
        setup.append([k, str(v)])
    t = Table(setup, colWidths=[4.5 * cm, width - 4.5 * cm])
    t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 8.5), ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#555555")),
                           ("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                           ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#dddddd"))]))
    story += [t, Paragraph("Key findings", H2)]
    for f in findings(summary):
        story.append(Paragraph("&bull; " + f, BODY))
    story.append(Paragraph("How to read the graphs", H2))
    story.append(Paragraph(
        "Each probe gives four timestamps: t1 (client send), t2 (server receive), t3 (server send), t4 (client receive). "
        "The <b>offset</b> θ = ((t2-t1)+(t3-t4))/2 is the client's estimate of how wrong its clock is, and the "
        "<b>round-trip delay</b> δ = (t4-t1)-(t3-t2). θ is exact only when the forward and return delays are equal: "
        "when congestion delays one direction more than the other, θ is wrong by (d<sub>forward</sub> - d<sub>return</sub>)/2. "
        "The client steps its clock using the lowest-delay of its last samples; the server holds true time, so the "
        "<b>true clock error</b> is exactly the client's remaining offset.", BODY))
    story += [Paragraph("Summary table", H2)]

    cols = [("scenario", "Scenario"), ("policy", "Policy"), ("mean_rtt_ms", "RTT ms"), ("p95_rtt_ms", "p95 ms"),
            ("jitter_ms", "Jitter ms"), ("loss_%", "Loss %"), ("steady_synced_err_ms", "Error ms"),
            ("max_synced_err_ms", "Max err"), ("route_changes", "Reroutes"), ("first_reroute_s", "1st reroute s")]
    data = [[c[1] for c in cols]]
    for s in sorted(summary, key=lambda x: (SCENARIO_ORDER.index(x["scenario"]) if x["scenario"] in SCENARIO_ORDER else 99,
                                            POLICY_ORDER.index(x["policy"]) if x["policy"] in POLICY_ORDER else 99)):
        row = []
        for k, _ in cols:
            v = s[k]
            row.append(v if isinstance(v, str) else (_fmt(v, 0) if k == "route_changes" else _fmt(v, 2)))
        data.append(row)
    t = Table(data, repeatRows=1, colWidths=[2.0 * cm, 2.5 * cm] + [(width - 4.5 * cm) / 8] * 8)
    style = [("FONTSIZE", (0, 0), (-1, -1), 7.5), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1b2a41")),
             ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
             ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f5f8")]),
             ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]
    # highlight steady error above 5 ms
    ei = [c[0] for c in cols].index("steady_synced_err_ms")
    for ri, s in enumerate(sorted(summary, key=lambda x: (SCENARIO_ORDER.index(x["scenario"]) if x["scenario"] in SCENARIO_ORDER else 99,
                                                          POLICY_ORDER.index(x["policy"]) if x["policy"] in POLICY_ORDER else 99)), 1):
        if s["steady_synced_err_ms"] > 5:
            style.append(("TEXTCOLOR", (ei, ri), (ei, ri), colors.HexColor("#b00020")))
            style.append(("FONTNAME", (ei, ri), (ei, ri), "Helvetica-Bold"))
    t.setStyle(TableStyle(style))
    story.append(t)
    story.append(Paragraph("Error = true clock error after filtering, averaged after the start-up transient. "
                           "Values above 5 ms are in red.", SMALL))

    # ---- overview charts
    story.append(PageBreak())
    story.append(Paragraph("Overview across all policies and scenarios", H2))
    p = os.path.join(tmp, "overview.png")
    chart_overview(summary, p)
    story.append(img(p))

    # offset convergence (first probes) for the baseline or first scenario
    conv_scen = "baseline" if "baseline" in scens else scens[0]
    story.append(Paragraph(f"NTP offset convergence at start-up ({conv_scen}, policy {pols[0]})", H2))
    p = os.path.join(tmp, "converge.png")
    chart_offset_convergence(raw, conv_scen, pols, p)
    story.append(img(p))
    story.append(Paragraph("Every client starts with a different wrong clock. The first probe measures that error as "
                           "the offset θ and the clock is stepped to the lowest-delay sample; the true error then "
                           "drops to the measurement floor.", SMALL))

    # ---- one section per scenario
    for s in scens:
        sp = [p for p in pols if any(r["policy"] == p and r["scenario"] == s for r in raw)]
        if not sp:
            continue
        story.append(PageBreak())
        story.append(Paragraph(f"Scenario: {s} - NTP offset, delay and clock error", H2))
        for k in range(0, len(sp), 2):
            p = os.path.join(tmp, f"scen_{s}_{k}.png")
            chart_scenario(s, sp[k:k + 2], raw, events, duration, p)
            story.append(img(p, width if len(sp[k:k + 2]) == 2 else width * 0.55))
        story.append(Paragraph("Dots are individual probes, one colour per client. Blue shading = Ryu had moved sync "
                               "traffic to the FAST path. Red crosses = probes lost (no reply within the timeout). "
                               "The offset panels share one scale so policies can be compared directly.", SMALL))
        p2 = os.path.join(tmp, f"ctrl_{s}.png")
        if chart_controller(s, sp, ctrl, events, duration, high, low, p2):
            nd = len([x for x in sp if x.startswith("dynamic")])
            story.append(KeepTogether([Paragraph("Controller view", H2), img(p2, width * min(1.0, 0.55 * nd))]))

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    for f in os.listdir(tmp):
        try:
            os.remove(os.path.join(tmp, f))
        except OSError:
            pass
    try:
        os.rmdir(tmp)
    except OSError:
        pass
    return out_path


def main():
    ap = argparse.ArgumentParser(description="Build final_report.pdf from a results directory")
    ap.add_argument("results_dir", nargs="?", default="results_mininet")
    ap.add_argument("--out", default=None, help="output PDF (default: <results_dir>/final_report.pdf)")
    ap.add_argument("--title", default="Time Synchronization under Congestion")
    a = ap.parse_args()
    try:
        import sdn_config as C
        high, low = C.HIGH_MBPS, C.LOW_MBPS
    except Exception:
        high, low = 2.5, 0.8
    print("report ->", build_report(a.results_dir, a.out, a.title, high=high, low=low))


if __name__ == "__main__":
    main()
