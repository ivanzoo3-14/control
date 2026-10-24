"""control_spc.py -- On-chain Statistical Process Control engine (corrected).

Applies manufacturing SPC to real Solana kill-test data. Robust (median/MAD)
statistics because memecoin returns are fat-tailed and mean-based Cpk assumes
normality these data violate.

Ingestion rules (from the project handoff):
  * filter to the definitive config fingerprint (63b8561732a5c6bc = v3.2)
  * friction capped at < 20%/side (out-of-range values are a known instrument bug)
  * forward fills: last-write-wins per (signal, horizon), mirroring the rig fold()

Verdict is rendered directly: how many robust-sigmas the median sits below
breakeven, and whether the process is in-control-but-negative vs out-of-control.

Dependencies: standard library only. Output: control_dashboard.html (self-contained).
"""
import json
import sys
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SIGNALS = r"C:\Users\ivanz\killtest\logs\signals.jsonl"
OUT = r"C:\Users\ivanz\OneDrive\Documents\deepseek-harness\default-workspace\control_dashboard.html"
FP = "63b8561732a5c6bc"   # definitive v3.2 fingerprint
RPC_CALLS = r"C:\Users\ivanz\killtest\logs\rpc_calls.jsonl"

# ---------------------------------------------------------------------------
# 0. Live RPC probe + usage accounting
# ---------------------------------------------------------------------------
import os
import urllib.request

def rpc_probe(url, method, params=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params or []}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def live_probe():
    url = os.environ.get("KILLTEST_RPC_URL") or "https://api.mainnet-beta.solana.com"
    try:
        slot = rpc_probe(url, "getSlot").get("result")
        ver = rpc_probe(url, "getVersion").get("result")
        return {"ok": True, "url": url, "slot": slot, "version": ver}
    except Exception as e:
        return {"ok": False, "url": url, "error": str(e)}

def usage_total():
    """Correct RPC usage: n_calls is a CUMULATIVE daily counter; take max per day."""
    daily_max = {}
    with open(RPC_CALLS, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            n = r.get("n_calls")
            if not isinstance(n, (int, float)):
                continue
            d = r.get("ts", "")[:10]
            daily_max[d] = max(daily_max.get(d, 0), n)
    return int(sum(daily_max.values())), daily_max

live = live_probe()
usage_total_calls, usage_daily = usage_total()
peak_day = max(usage_daily.values()) if usage_daily else 0

print(f"live: {live}")
print(f"usage: total={usage_total_calls} peak_day={peak_day}")

# ---------------------------------------------------------------------------
# 1. Ingest real data (fingerprint-filtered)
# ---------------------------------------------------------------------------
print("reading signals.jsonl ...")
signals = {}      # sid -> {ts, friction}
order = []        # time-ordered sids
with open(SIGNALS, "r", encoding="utf-8", errors="replace") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("kind") == "signal" and r.get("config_fp") == FP:
            sid = r["id"]
            if r.get("usable") and r.get("friction_pct") is not None and r["friction_pct"] < 20.0:
                signals[sid] = {"ts": str(r.get("ts") or r.get("detected_at") or 0),
                                "friction": float(r["friction_pct"])}
                order.append(sid)

fwd = defaultdict(dict)  # (sid) -> {h: net_pct}  last-write-wins
with open(SIGNALS, "r", encoding="utf-8", errors="replace") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("kind") == "forward" and r.get("net_pct") is not None:
            sid = r["id"]
            if sid in signals:
                fwd[sid][int(r["h"])] = float(r["net_pct"])

# time-order and de-duplicate
ordered = []
seen = set()
for sid in sorted(order, key=lambda s: signals[s]["ts"]):
    if sid not in seen:
        seen.add(sid)
        ordered.append(sid)

n_signals = len(ordered)
print(f"fingerprint {FP}: {n_signals} usable signals (friction < 20%)")

# ---------------------------------------------------------------------------
# 2. Robust SPC math (median / MAD)
# ---------------------------------------------------------------------------
def med(xs):
    s = sorted(xs)
    return s[len(s) // 2]

def mad(xs, center):
    return med([abs(x - center) for x in xs])

def robust_stats(values):
    n = len(values)
    m = med(values)
    mads = mad(values, m)
    sigma = 1.4826 * mads          # MAD -> sigma equivalent for normal data
    ucl = m + 3.0 * sigma
    lcl = m - 3.0 * sigma
    mean = sum(values) / n
    return {"n": n, "median": m, "mad": mads, "sigma": sigma,
            "ucl": ucl, "lcl": lcl, "mean": mean}

def nelson_robust(values, center, sigma):
    """Flag points beyond +-3 robust sigma (special cause)."""
    return [i for i, v in enumerate(values) if abs(v - center) > 3.0 * sigma]

# ---------------------------------------------------------------------------
# 3. Build the series
# ---------------------------------------------------------------------------
friction_series = [signals[sid]["friction"] for sid in ordered]
f_s = robust_stats(friction_series)
f_flag = nelson_robust(friction_series, f_s["median"], f_s["sigma"])

horizons = sorted({h for d in fwd.values() for h in d})
horizon_series = {}
for h in horizons:
    vals = [fwd[sid].get(h) for sid in ordered if h in fwd.get(sid, {})]
    horizon_series[h] = [v for v in vals if v is not None]

primary_h = 300 if 300 in horizon_series else (horizons[0] if horizons else None)
net_series = horizon_series.get(primary_h, [])
net_s = robust_stats(net_series) if net_series else None
net_flag = nelson_robust(net_series, net_s["median"], net_s["sigma"]) if net_series else []

import math
def sign_test_le_zero(values):
    """One-sided sign test H0: median >= 0 vs H1: median < 0. Returns (p, pct_pos)."""
    n = len(values)
    if n == 0:
        return None, 0.0
    pos = sum(1 for v in values if v > 0)
    p_hat = pos / n
    if n > 1:
        se = (0.25 / n) ** 0.5
        z = (p_hat - 0.5) / se
        p = 0.5 * (1.0 + math.erf(z / (2.0 ** 0.5)))
    else:
        p = p_hat
    return p, p_hat * 100.0

net_p, net_pct_pos = sign_test_le_zero(net_series) if net_series else (None, 0.0)
rugs = sum(1 for v in net_series if v < -90.0) if net_series else 0
moons = sum(1 for v in net_series if v > 100.0) if net_series else 0
fric_budget_z = ((1.5 - f_s["median"]) / f_s["sigma"]) if f_s["sigma"] > 0 else None

print(f"friction n={f_s['n']} median={f_s['median']:.3f}% sigma={f_s['sigma']:.3f}")
print(f"return h={primary_h} n={net_s['n']} median={net_s['median']:.3f}% "
      f"sigma={net_s['sigma']:.3f} sign_test_p={net_p} pct_pos={net_pct_pos:.1f} rugs={rugs} moons={moons}")

# ---------------------------------------------------------------------------
# 4. SVG renderers (dependency-free)
# ---------------------------------------------------------------------------
def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def svg_chart(values, title, center, ucl, lcl, flagged, unit, w=980, h=340,
              ref_line=None, ref_label=None, clip=None, note=""):
    n = len(values)
    if n == 0:
        return ""
    left, right, top, bottom = 78, 24, 44, 52
    pw, ph = w - left - right, h - top - bottom
    disp = values if clip is None else [max(-clip, min(clip, v)) for v in values]
    ymin = min(min(disp), lcl if lcl > -clip else -clip)
    ymax = max(max(disp), ucl if ucl < clip else clip)
    pad = (ymax - ymin) * 0.08 or 1.0
    ymin, ymax = ymin - pad, ymax + pad

    def Y(v):
        return top + (ymax - v) / (ymax - ymin) * ph
    def X(i):
        return left + (i / (n - 1)) * pw if n > 1 else left + pw / 2

    fset = set(flagged)
    pts = " ".join(f"{X(i):.1f},{Y(disp[i]):.1f}" for i in range(n))
    circles = "".join(
        f'<circle cx="{X(i):.1f}" cy="{Y(disp[i]):.1f}" r="3.2" fill="#d63031" stroke="#fff" stroke-width="0.6"/>'
        for i in fset)
    ticks = ""
    for t in range(5):
        v = ymin + (ymax - ymin) * t / 4
        ticks += (f'<line x1="{left}" y1="{Y(v):.1f}" x2="{left+6}" y2="{Y(v):.1f}" stroke="#888"/>'
                  f'<text x="{left-6}" y="{Y(v)+4:.1f}" text-anchor="end" font-size="11" fill="#888">{v:.1f}</text>')
    cy = Y(center)
    lines = (f'<line x1="{left}" y1="{cy:.1f}" x2="{w-right}" y2="{cy:.1f}" stroke="#2d3436" stroke-width="1.4"/>'
             f'<text x="{w-right+6}" y="{cy+4:.1f}" font-size="11" fill="#2d3436" font-weight="700">CL {center:.2f}</text>')
    if ucl < clip:
        uy = Y(ucl)
        lines += (f'<line x1="{left}" y1="{uy:.1f}" x2="{w-right}" y2="{uy:.1f}" stroke="#e17055" stroke-width="1.2" stroke-dasharray="6 4"/>'
                  f'<text x="{w-right+6}" y="{uy+4:.1f}" font-size="11" fill="#e17055">UCL</text>')
    if lcl > -clip:
        ly = Y(lcl)
        lines += (f'<line x1="{left}" y1="{ly:.1f}" x2="{w-right}" y2="{ly:.1f}" stroke="#e17055" stroke-width="1.2" stroke-dasharray="6 4"/>'
                  f'<text x="{w-right+6}" y="{ly+4:.1f}" font-size="11" fill="#e17055">LCL</text>')
    if ref_line is not None:
        ry = Y(ref_line)
        lines += (f'<line x1="{left}" y1="{ry:.1f}" x2="{w-right}" y2="{ry:.1f}" stroke="#d63031" stroke-width="2"/>'
                  f'<text x="{left+4}" y="{ry-4:.1f}" font-size="12" fill="#d63031" font-weight="700">{ref_label}</text>')
    hnote = f'<text x="{w-right-4}" y="{top+6}" text-anchor="end" font-size="11" fill="#888">{note}</text>' if note else ""
    return f'''
  <svg viewBox="0 0 {w} {h}" width="100%" xmlns="http://www.w3.org/2000/svg" style="background:#fff;border:1px solid #e6e6e6;border-radius:8px">
    <text x="{left}" y="20" font-size="15" font-weight="700" fill="#1f3b63">{esc(title)}</text>
    <text x="{left}" y="38" font-size="12" fill="#666">n={n} · median={center:.3f}{unit} · robust σ={ucl-center:.3f} · UCL={ucl:.3f} · LCL={lcl:.3f}</text>
    {hnote}
    {ticks}
    {lines}
    <polyline points="{pts}" fill="none" stroke="#0984e3" stroke-width="1.1" opacity="0.85"/>
    {circles}
    <text x="{left}" y="{h-8}" font-size="11" fill="#888">signal index (time order) →</text>
  </svg>'''

def svg_hist(values, title, breakeven, w=980, h=320, clip=None):
    n = len(values)
    if n == 0:
        return ""
    disp = values if clip is None else [max(-clip, min(clip, v)) for v in values]
    left, right, top, bottom = 78, 24, 44, 40
    pw, ph = w - left - right, h - top - bottom
    nb = 60
    vmin, vmax = min(disp), max(disp)
    if vmax - vmin < 1e-9:
        vmax = vmin + 1
    bw = (vmax - vmin) / nb
    counts = [0] * nb
    for v in disp:
        i = int((v - vmin) / bw)
        i = min(i, nb - 1)
        counts[i] += 1
    cmax = max(counts) or 1
    bars = ""
    for i, c in enumerate(counts):
        if c == 0:
            continue
        x0 = left + i / nb * pw
        x1 = left + (i + 0.85) / nb * pw
        y1 = top + ph
        y0 = y1 - (c / cmax) * ph
        bv = vmin + (i + 0.5) * bw
        col = "#d63031" if bv < breakeven else "#00b894"
        bars += f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{max(x1-x0,0.8):.1f}" height="{y1-y0:.1f}" fill="{col}" opacity="0.82"/>'
    bx = left + (breakeven - vmin) / (vmax - vmin) * pw
    bx = max(left, min(left + pw, bx))
    m = med(disp)
    mx = left + (m - vmin) / (vmax - vmin) * pw
    return f'''
  <svg viewBox="0 0 {w} {h}" width="100%" xmlns="http://www.w3.org/2000/svg" style="background:#fff;border:1px solid #e6e6e6;border-radius:8px">
    <text x="{left}" y="20" font-size="15" font-weight="700" fill="#1f3b63">{esc(title)}</text>
    <text x="{left}" y="38" font-size="12" fill="#666">n={n} · median={m:.3f}</text>
    {bars}
    <line x1="{bx:.1f}" y1="{top}" x2="{bx:.1f}" y2="{top+ph}" stroke="#d63031" stroke-width="2"/>
    <text x="{bx+4:.1f}" y="{top+12}" font-size="12" fill="#d63031" font-weight="700">breakeven 0</text>
    <line x1="{mx:.1f}" y1="{top}" x2="{mx:.1f}" y2="{top+ph}" stroke="#2d3436" stroke-width="1.4" stroke-dasharray="5 3"/>
    <text x="{mx+4:.1f}" y="{top+26}" font-size="12" fill="#2d3436" font-weight="700">median {m:.2f}</text>
  </svg>'''

# ---------------------------------------------------------------------------
# 5. Verdict + HTML
# ---------------------------------------------------------------------------
if net_s and net_p is not None:
    if net_s["median"] < 0 and net_p < 0.01:
        badge = "<div class='badge red'>MEDIAN BELOW BREAKEVEN — STATISTICALLY SIGNIFICANT, NOT LUCK</div>"
        verdict = (f"The median forward return at {primary_h}s is <b>{net_s['median']:.2f}%</b> and a one-sided sign test "
                   f"rejects 'median ≥ 0' at <b>p = {net_p:.2g}</b>. {net_pct_pos:.1f}% of signals were positive. "
                   f"That is the statistical statement that this strategy is dead: the loss is the center of the "
                   f"distribution, not a run of bad luck. <b>{rugs}</b> signals were rugs (&lt;−90%) and <b>{moons}</b> "
                   f"were moonshots (&gt;+100%) — the fat tails that every take-profit rule truncates.")
    elif net_s["median"] < 0:
        badge = "<div class='badge amber'>NEGATIVE MEDIAN, NOT YET SIGNIFICANT</div>"
        verdict = (f"The median is <b>{net_s['median']:.2f}%</b> ({net_pct_pos:.1f}% positive, p = {net_p:.2g}). "
                   f"Negative but not decisively — more data would settle it. Do not trade this.")
    else:
        badge = "<div class='badge green'>MEDIAN ABOVE BREAKEVEN</div>"
        verdict = f"The median is {net_s['median']:.2f}% ({net_pct_pos:.1f}% positive). Investigate, then pre-register before believing it."
else:
    badge = "<div class='badge gray'>NO DATA AT PRIMARY HORIZON</div>"
    verdict = ""

# live status card
if live["ok"]:
    live_badge = "<div class='badge green'>LIVE — connected to Solana mainnet</div>"
    live_slot = f"{live['slot']:,}"
    live_url = live["url"].split("//")[-1].split("/")[0]
else:
    live_badge = "<div class='badge amber'>OFFLINE — RPC probe failed (set KILLTEST_RPC_URL for GetBlock)</div>"
    live_slot = "—"
    live_url = "—"

horizon_rows = ""
for h in sorted(horizon_series):
    vals = horizon_series[h]
    if not vals:
        continue
    s = robust_stats(vals)
    p, pct_pos = sign_test_le_zero(vals)
    p_str = f"{p:.2g}" if p is not None else "—"
    horizon_rows += (f"<tr><td>{h}s</td><td>{len(vals)}</td><td>{s['median']:.3f}%</td>"
                     f"<td>{s['sigma']:.3f}</td><td>{s['ucl']:.2f}</td><td>{s['lcl']:.2f}</td>"
                     f"<td>{pct_pos:.1f}%</td><td>{p_str}</td></tr>")

CLIP = 200.0
net_chart = svg_chart(net_series, f"Control chart — forward return at {primary_h}s (%)",
                      net_s["median"], net_s["ucl"], net_s["lcl"], net_flag, "%",
                      ref_line=0.0, ref_label="breakeven", clip=CLIP,
                      note=f"clipped ±{CLIP}% for display")
net_hist = svg_hist(net_series, "Return distribution vs breakeven (0%)", 0.0, clip=CLIP)

fric_chart = svg_chart(friction_series, "Control chart — friction per side (%/side)",
                       f_s["median"], f_s["ucl"], f_s["lcl"], f_flag, "%",
                       ref_line=1.5, ref_label="1.5% budget", clip=20.0,
                       note="clipped 20% (out-of-range is a known bug)")
fric_hist = svg_hist(friction_series, "Friction distribution vs round-trip budget", 1.5, clip=20.0)

html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CONTROL — On-chain SPC for Solana</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin:0; background:#f5f6fa; color:#1a1a1a; }}
  .wrap {{ max-width: 1040px; margin: 0 auto; padding: 28px 20px 60px; }}
  h1 {{ font-size: 28px; margin: 0 0 2px; color:#1f3b63; }}
  .sub {{ color:#666; font-size:14px; margin: 0 0 20px; }}
  .card {{ background:#fff; border-radius:10px; padding:20px 22px; margin-bottom:18px; border:1px solid #e6e6e6; box-shadow:0 1px 2px rgba(0,0,0,.04); }}
  .badge {{ display:inline-block; padding:10px 16px; border-radius:8px; font-weight:700; font-size:15px; margin-bottom:10px; }}
  .red {{ background:#fdecea; color:#b00020; border:1px solid #f5b7b1; }}
  .amber {{ background:#fef9e7; color:#9a7d0a; border:1px solid #f7dc6f; }}
  .green {{ background:#eafaf1; color:#1e8449; border:1px solid #a9dfbf; }}
  .gray {{ background:#f0f0f0; color:#666; border:1px solid #ddd; }}
  .verdict {{ font-size:15px; line-height:1.5; color:#333; }}
  table {{ border-collapse:collapse; width:100%; font-size:13px; }}
  th,td {{ padding:7px 9px; text-align:right; border-bottom:1px solid #eee; }}
  th {{ color:#1f3b63; }}
  td:first-child, th:first-child {{ text-align:left; }}
  h2 {{ font-size:17px; color:#1f3b63; margin: 4px 0 12px; }}
  .muted {{ color:#999; }}
  .kpis {{ display:flex; gap:14px; flex-wrap:wrap; }}
  .kpi {{ flex:1 1 150px; background:#fafbfc; border:1px solid #e6e6e6; border-radius:8px; padding:12px 14px; }}
  .kpi .v {{ font-size:22px; font-weight:700; color:#1f3b63; }}
  .kpi .l {{ font-size:12px; color:#777; }}
</style></head>
<body><div class="wrap">

<h1>CONTROL</h1>
<p class="sub">On-chain Statistical Process Control for Solana · fingerprint {FP[:8]}… · {n_signals} usable signals · data through 2026-10-17</p>

<div class="card">
  <h2>Live status + RPC usage</h2>
  {live_badge}
  <div class="kpis">
    <div class="kpi"><div class="v">{live_slot}</div><div class="l">live slot</div></div>
    <div class="kpi"><div class="v">{usage_total_calls:,}</div><div class="l">total RPC calls (rig lifetime)</div></div>
    <div class="kpi"><div class="v">{peak_day:,}</div><div class="l">peak calls / day</div></div>
    <div class="kpi"><div class="v">{live_url}</div><div class="l">RPC endpoint</div></div>
  </div>
</div>

<div class="card">
  <h2>Verdict — forward return at {primary_h}s</h2>
  {badge}
  <p class="verdict">{verdict}</p>
</div>

<div class="card">
  <h2>Key indicators</h2>
  <div class="kpis">
    <div class="kpi"><div class="v">{n_signals}</div><div class="l">usable signals</div></div>
    <div class="kpi"><div class="v">{f_s['median']:.2f}%</div><div class="l">median friction / side</div></div>
    <div class="kpi"><div class="v">{f_s['sigma']:.3f}</div><div class="l">friction robust σ</div></div>
    <div class="kpi"><div class="v">{net_s['median']:.2f}%</div><div class="l">median return @ {primary_h}s</div></div>
    <div class="kpi"><div class="v">p={net_p:.2g}</div><div class="l">sign test (median ≥ 0)</div></div>
    <div class="kpi"><div class="v">{rugs}</div><div class="l">rugs (&lt;−90%)</div></div>
    <div class="kpi"><div class="v">{moons}</div><div class="l">moonshots (&gt;+100%)</div></div>
  </div>
</div>

<div class="card"><h2>Forward return — control chart</h2>{net_chart}</div>
<div class="card"><h2>Forward return — distribution vs breakeven</h2>{net_hist}</div>

<div class="card"><h2>All horizons</h2>
<table><thead><tr><th>horizon</th><th>n</th><th>median</th><th>robust σ</th><th>UCL</th><th>LCL</th><th>% positive</th><th>sign-test p</th></tr></thead>
<tbody>{horizon_rows}</tbody></table></div>

<div class="card"><h2>Execution — friction control chart</h2>{fric_chart}</div>
<div class="card"><h2>Execution — friction distribution vs budget</h2>{fric_hist}</div>

<div class="card">
  <h2>Method</h2>
  <p class="verdict">Robust Individuals chart: center line = median, σ = 1.4826 × MAD (median absolute deviation),
  control limits = median ± 3σ. Significance is a one-sided sign test (H0: median ≥ 0), which is non-parametric and
  makes no normality assumption — appropriate because memecoin returns are fat-tailed. Data: pre-registered kill-test
  rig (<code>signals.jsonl</code>), definitive fingerprint {FP}, paper-only, no keys, friction capped &lt;20% (documented
  instrument bug above that).</p>
</div>

</div></body></html>"""

with open(OUT, "w", encoding="utf-8") as f:
    f.write(html)

print(f"written: {OUT}")
print(f"horizons: {horizons}")
print("DONE")
