# -*- coding: utf-8 -*-
"""build_dashboard.py — 每日情报雷达 · 单文件离线看板生成器.

Owner: **dashboard**.  Reads the authoritative ``data.json`` (CONTRACT.md §4)
and renders ONE self-contained HTML file:

    * inline CSS, inline vanilla JS, ``window.__INTEL__`` inline JSON data
    * zero external requests (no CDN / web fonts / remote images / fetch)
    * opens straight from ``file://`` (double click)

Usage::

    python intel/src/build_dashboard.py --data intel/data/2026-11-18/data.json \
        --out intel/data/2026-11-18/index.html \
        [--archive-index intel/data/index.html \
         --dates 2026-11-16,2026-11-17,2026-11-18 \
         [--archive-items intel/data/archive_items.json]]

Public API::

    build(data, out_dir, dates=None, archive_path=None, archive_items=None,
          title="每日情报雷达", archive_href_prefix="../") -> str

Extra UI added in v1.2 (CONTRACT §12.3):

    * 图表弹层 ``#chart-modal``：纯 canvas 分时 / 日线（无外部库），点击
      自选股卡（data-code）/ A股指数卡（data-idx）/ 全球市场卡（data-chart）打开；
    * 分时 = 价格渐变面积 + 均价虚线 + 昨收虚线 + 成交量柱；日线 = 蜡烛图 +
      MA5/10/20 + 成交量；十字光标浮层、Esc/遮罩关闭、resize 与 devicePixelRatio；
    * 数据来自内联 ``window.__INTEL__.charts.items``（客户端已裁剪数值精度与分时点数）。
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
try:  # reuse Lead-owned helpers when importable (never required)
    import common as _common  # noqa: F401
except Exception:  # pragma: no cover - degraded optional import
    _common = None

VERSION = "1.2.0"

#: 要闻首屏条数（其余按需「加载更多」，控制单文件体积与首屏渲染成本）
NEWS_INITIAL = 80

# --------------------------------------------------------------------------- #
# tiny format / safety helpers
# --------------------------------------------------------------------------- #
def _e(value) -> str:
    """HTML-escape anything (attributes included)."""
    return html.escape("" if value is None else str(value), quote=True)


def _lst(value) -> list:
    return value if isinstance(value, list) else []


def _dct(value) -> dict:
    return value if isinstance(value, dict) else {}


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def _safe_url(value) -> str:
    """Only allow absolute http(s) links inside the page."""
    s = str(value or "").strip()
    if s.startswith(("http://", "https://")) and len(s) <= 2048 and " " not in s:
        return s
    return ""


def _clean(value, max_len: int = 0) -> str:
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    if max_len and len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "…"
    return s


def _link(url, text: str, cls: str = "", extra: str = "") -> str:
    href = _safe_url(url)
    body = _e(text)
    if href:
        return f'<a class="{cls}" href="{_e(href)}" target="_blank" rel="noopener noreferrer"{extra}>{body}</a>'
    return f'<span class="{cls}">{body}</span>'


def _fmt_num(value, digits: int = 2) -> str:
    f = _num(value)
    return "—" if f is None else f"{f:,.{digits}f}"


def _fmt_price(value) -> str:
    f = _num(value)
    if f is None:
        return "—"
    a = abs(f)
    if a and a < 10:
        return f"{f:.4f}"
    if a and a < 1000:
        return f"{f:.2f}"
    return f"{f:,.2f}"


def _fmt_signed(value, digits: int = 2) -> str:
    f = _num(value)
    return "—" if f is None else f"{f:+,.{digits}f}"


def _fmt_pct(value, digits: int = 2) -> str:
    f = _num(value)
    return "—" if f is None else f"{f:+.{digits}f}%"


def _fmt_ratio(value, digits: int = 1) -> str:
    f = _num(value)
    return "—" if f is None else f"{f * 100:.{digits}f}%"


def _amt(value) -> str:
    f = _num(value)
    if f is None:
        return "—"
    a = abs(f)
    sign = "-" if f < 0 else ""
    if a >= 1e12:
        return f"{sign}{a / 1e12:.2f}万亿"
    if a >= 1e8:
        return f"{sign}{a / 1e8:.2f}亿"
    if a >= 1e4:
        return f"{sign}{a / 1e4:.2f}万"
    return f"{sign}{a:.0f}"


def _amt_signed(value) -> str:
    f = _num(value)
    if f is None:
        return "—"
    return ("+" if f > 0 else "") + _amt(f)


def _hot(value) -> str:
    f = _num(value)
    if f is None:
        return "—"
    if abs(f) >= 1e8:
        return f"{f / 1e8:.2f}亿"
    if abs(f) >= 1e4:
        return f"{f / 1e4:.1f}万"
    return f"{f:.0f}"


def _tone(value) -> str:
    f = _num(value)
    if f is None:
        return "flat"
    return "up" if f > 0 else ("down" if f < 0 else "flat")


def _tone_text(value) -> str:
    return {"up": "up", "down": "down", "flat": "flat"}[_tone(value)]


def _dt(value):
    if value is None:
        return None
    if _common is not None:
        try:
            return _common.parse_dt_any(value)
        except Exception:
            pass
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _fmt_time(value, data_date: str = "") -> str:
    d = _dt(value)
    if d is None:
        return "—"
    try:
        if data_date and d.strftime("%Y-%m-%d") == data_date:
            return d.strftime("%H:%M")
        return d.strftime("%m-%d %H:%M")
    except (ValueError, OverflowError):
        return "—"


def _fmt_day(value) -> str:
    d = _dt(value)
    if d is None:
        s = _clean(value, 20)
        return s or "—"
    return d.strftime("%Y-%m-%d")


def _fmt_gen(value) -> str:
    d = _dt(value)
    if d is None:
        return _clean(value, 30) or "—"
    return d.strftime("%Y-%m-%d %H:%M")


_TONES = ("cyan", "mag", "amber", "violet", "green", "blue")


def _cat_tones(names) -> dict:
    out = {}
    for i, name in enumerate(names):
        out[str(name)] = "ct-" + _TONES[i % len(_TONES)]
    return out


def _safe_id(text: str) -> str:
    return re.sub(r"[\s\"'<>&#]+", "-", str(text or "")) or "x"


def _rel_href(target_file: str, from_dir: str) -> str:
    """Relative href from a directory to a file (posix separators)."""
    try:
        rel = os.path.relpath(os.path.abspath(target_file), os.path.abspath(from_dir))
    except (ValueError, OSError):
        return ""
    return rel.replace("\\", "/")


# --------------------------------------------------------------------------- #
# inline SVG charts (no external images)
# --------------------------------------------------------------------------- #
def _svg_kline(code: str, kline, prev_close=None) -> str:
    pts = []
    for row in _lst(kline):
        row = _dct(row)
        close = _num(row.get("close"))
        if close is None:
            continue
        pts.append((_clean(row.get("date"), 16), close))
    if len(pts) < 2:
        return '<div class="empty sm">K线数据缺失</div>'

    closes = [p[1] for p in pts]
    lo, hi = min(closes), max(closes)
    pc = _num(prev_close)
    if pc is not None:
        lo, hi = min(lo, pc), max(hi, pc)
    span = hi - lo
    if span <= 0:
        span = max(abs(hi), 1.0) * 0.02
        lo, hi = lo - span / 2, hi + span / 2
    pad = (hi - lo) * 0.12
    lo, hi = lo - pad, hi + pad
    span = hi - lo
    width, height, pl, pr, pt, pb = 320.0, 92.0, 6.0, 6.0, 8.0, 10.0

    def x(i: int) -> float:
        return pl + (width - pl - pr) * (i / (len(pts) - 1))

    def y(v: float) -> float:
        return pt + (height - pt - pb) * (1.0 - (v - lo) / span)

    up = closes[-1] >= closes[0]
    color = "#ff4d6d" if up else "#2ee6a0"
    gid = ("kg-" + re.sub(r"\W", "", str(code or ""))) or "kg-x"
    line = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, (_d, v) in enumerate(pts))
    area = f"{x(0):.1f},{height - pb:.1f} " + line + f" {x(len(pts) - 1):.1f},{height - pb:.1f}"

    prev_line = ""
    if pc is not None:
        yy = y(pc)
        prev_line = (
            f'<line x1="{pl:.1f}" y1="{yy:.1f}" x2="{width - pr:.1f}" y2="{yy:.1f}" '
            f'stroke="rgba(150,178,215,.32)" stroke-width="1" stroke-dasharray="3 4"/>'
        )
    dots = []
    step = max(1, len(pts) // 6)
    for i, (d, v) in enumerate(pts):
        if i % step and i != len(pts) - 1:
            continue
        dots.append(
            f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="4" fill="transparent">'
            f"<title>{_e(d)} {v:.2f}</title></circle>"
        )
    last = (
        f'<circle cx="{x(len(pts) - 1):.1f}" cy="{y(closes[-1]):.1f}" r="2.8" '
        f'fill="{color}" stroke="#050a12" stroke-width="1"/>'
    )
    svg = (
        f'<svg viewBox="0 0 {width:.0f} {height:.0f}" role="img" '
        f'aria-label="近{len(pts)}日收盘价走势">'
        f'<defs><linearGradient id="{_e(gid)}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{color}" stop-opacity=".30"/>'
        f'<stop offset="1" stop-color="{color}" stop-opacity="0"/>'
        f"</linearGradient></defs>"
        f"{prev_line}"
        f'<polygon points="{area}" fill="url(#{_e(gid)})"/>'
        f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="1.8" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        + "".join(dots)
        + last
        + "</svg>"
    )
    first_c, last_c = closes[0], closes[-1]
    chg = ((last_c - first_c) / first_c * 100.0) if first_c else None
    span_lbl = f"{_e(pts[0][0][5:] or pts[0][0])} → {_e(pts[-1][0][5:] or pts[-1][0])}"
    return (
        f'<div class="kl">{svg}'
        f'<div class="kl-lbl"><span>{span_lbl} · {len(pts)}日</span>'
        f'<span class="{_tone(chg)}">{_fmt_pct(chg)}</span></div></div>'
    )


def _fund_flow(flow, market=None) -> str:
    if str(market or "").upper() == "US":
        return (
            '<div class="flow"><div class="flow-head"><span class="lbl">主力资金</span>'
            '<span class="val flat">美股不采集</span></div></div>'
        )
    flow = _dct(flow)
    today = _num(flow.get("today_main_net"))
    recent = _lst(flow.get("recent"))[-6:]
    rows = []
    for row in recent:
        row = _dct(row)
        val = _num(row.get("main_net"))
        rows.append((_fmt_day(row.get("date")), val))
    bars = []
    if rows:
        top = max([abs(v) for _d, v in rows if v is not None] + [1.0])
        for day, val in rows:
            if val is None:
                bars.append(
                    f'<div class="ff-col"><div class="ff-bar"><i style="height:4%;background:rgba(140,165,200,.35)"></i></div>'
                    f'<span>{_e(day[5:] if len(day) > 5 else day)}</span></div>'
                )
                continue
            h = max(6.0, min(100.0, abs(val) / top * 100.0))
            color = "linear-gradient(180deg,#ff6b7f,#ff3b55)" if val > 0 else "linear-gradient(180deg,#37f0b5,#12c98a)"
            bars.append(
                f'<div class="ff-col"><div class="ff-bar"><i style="height:{h:.0f}%;background:{color}"></i></div>'
                f'<span>{_e(day[5:] if len(day) > 5 else day)}</span></div>'
            )
    body = f'<div class="ff">{"".join(bars)}</div>' if bars else '<span class="dim-note">近5日资金数据缺失</span>'
    return (
        f'<div class="flow"><div class="flow-head"><span class="lbl">主力资金</span>'
        f'<span class="val {_tone(today)}">{_amt_signed(today)}</span></div>{body}</div>'
    )


# --------------------------------------------------------------------------- #
# CSS
# --------------------------------------------------------------------------- #
_CSS = r"""
:root{
  --bg:#070b14;--bg2:#0a1020;--panel:#0c1322;--panel2:#101a2e;
  --line:rgba(120,160,220,.14);--line2:rgba(120,190,255,.28);
  --txt:#e7effc;--muted:#8ea6c6;--dim:#5d708d;
  --cyan:#4df0ff;--magenta:#ff3bd8;--amber:#ffc44d;
  --up:#ff4d6d;--down:#2ee6a0;--violet:#a78bfa;--blue:#6ea8ff;
  --mono:ui-monospace,"Cascadia Mono","Cascadia Code",Consolas,"SFMono-Regular",monospace;
  --sans:"Microsoft YaHei UI","Microsoft YaHei","Segoe UI",system-ui,-apple-system,sans-serif;
  --radius:15px;
}
*,*::before,*::after{box-sizing:border-box}
html{background:var(--bg);-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--sans);font-size:14px;line-height:1.5;min-height:100vh;overflow-x:hidden}
body::before{content:"";position:fixed;inset:0;z-index:0;pointer-events:none;background:
  radial-gradient(880px 460px at 10% -8%,rgba(77,240,255,.10),transparent 62%),
  radial-gradient(760px 420px at 94% -10%,rgba(255,59,216,.09),transparent 60%),
  radial-gradient(900px 620px at 50% 116%,rgba(255,196,77,.05),transparent 66%)}
body::after{content:"";position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.5;background-image:
  linear-gradient(rgba(120,180,255,.045) 1px,transparent 1px),
  linear-gradient(90deg,rgba(120,180,255,.045) 1px,transparent 1px);background-size:64px 64px;
  -webkit-mask-image:linear-gradient(180deg,rgba(0,0,0,.9),rgba(0,0,0,.12));
  mask-image:linear-gradient(180deg,rgba(0,0,0,.9),rgba(0,0,0,.12))}
a{color:inherit}
::selection{background:rgba(77,240,255,.24)}
::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-track{background:rgba(255,255,255,.02)}
::-webkit-scrollbar-thumb{background:rgba(120,170,220,.22);border-radius:8px;border:2px solid var(--bg)}
::-webkit-scrollbar-thumb:hover{background:rgba(120,200,255,.35)}

/* ---------- top bar ---------- */
.topbar{position:sticky;top:0;z-index:50;background:linear-gradient(180deg,rgba(7,11,20,.96),rgba(7,11,20,.82));border-bottom:1px solid var(--line);-webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px)}
.topbar-in{max-width:1720px;margin:0 auto;padding:13px 22px;display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:12px;min-width:0}
.brand .glyph{width:34px;height:34px;border-radius:9px;display:grid;place-items:center;font-family:var(--mono);font-size:16px;color:#04141b;background:linear-gradient(135deg,var(--cyan),#9df7ff);box-shadow:0 0 22px rgba(77,240,255,.35);flex:0 0 auto}
.brand h1{margin:0;font-size:18px;font-weight:650;letter-spacing:.14em;white-space:nowrap}
.brand .en{font-family:var(--mono);font-size:9.5px;letter-spacing:.3em;color:var(--dim);text-transform:uppercase;white-space:nowrap}
.meta{margin-left:auto;display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.pill{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);font-size:11px;color:var(--muted);background:rgba(255,255,255,.03);border:1px solid var(--line);border-radius:20px;padding:4px 10px;white-space:nowrap}
.pill b{color:var(--txt);font-weight:600}
.pill.date{color:#c9f9ff;border-color:rgba(77,240,255,.32);background:rgba(77,240,255,.07)}
.pill.offline{color:#ffd98a;border-color:rgba(255,196,77,.35);background:rgba(255,196,77,.08)}
.pill.bad{color:#ff9aa8;border-color:rgba(255,77,109,.4);background:rgba(255,77,109,.09)}
.pill.bad b{color:#ff8a9a}
.pill .dot-live{width:6px;height:6px;border-radius:50%;background:var(--cyan);box-shadow:0 0 9px var(--cyan)}
.arch{position:relative;margin:0}
.arch summary{list-style:none;cursor:pointer;display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);font-size:11px;color:#c9f9ff;background:rgba(77,240,255,.07);border:1px solid rgba(77,240,255,.3);border-radius:20px;padding:4px 11px;white-space:nowrap;transition:.16s}
.arch summary::-webkit-details-marker{display:none}
.arch summary:hover{border-color:rgba(77,240,255,.55);box-shadow:0 0 16px rgba(77,240,255,.16)}
.arch[open] summary{border-color:rgba(77,240,255,.6)}
.arch .menu{position:absolute;right:0;top:calc(100% + 9px);min-width:230px;max-height:330px;overflow:auto;display:flex;flex-direction:column;gap:2px;padding:7px;background:#0b1221;border:1px solid var(--line2);border-radius:12px;box-shadow:0 26px 60px -18px #000,0 0 40px -22px rgba(77,240,255,.5);z-index:60}
.arch .menu a{display:flex;justify-content:space-between;gap:10px;font-family:var(--mono);font-size:12px;color:#bacbe3;text-decoration:none;padding:7px 10px;border-radius:8px;white-space:nowrap}
.arch .menu a:hover{background:rgba(77,240,255,.09);color:#eafcff}
.arch .menu a.cur{color:var(--cyan);background:rgba(77,240,255,.1)}
.arch .menu a.all{margin-top:4px;border-top:1px solid var(--line);border-radius:0 0 8px 8px;color:var(--muted);padding-top:9px}

/* ---------- layout shell ---------- */
.wrap{position:relative;z-index:1;max-width:1720px;margin:0 auto;padding:0 22px 44px}
.layout{display:grid;gap:16px;grid-template-columns:repeat(3,minmax(0,1fr));grid-template-areas:
  "markets markets markets"
  "news news radar"
  "ashare ashare alerts"
  "watch watch watch"
  "sources sources sources"}
.sec{position:relative;min-width:0;background:linear-gradient(178deg,rgba(15,24,40,.93),rgba(9,14,25,.96));border:1px solid var(--line);border-radius:var(--radius);padding:16px 18px 18px;box-shadow:0 18px 44px -34px #000,inset 0 1px 0 rgba(255,255,255,.035)}
.sec::before{content:"";position:absolute;left:14px;right:14px;top:0;height:1px;background:linear-gradient(90deg,transparent,rgba(77,240,255,.42),rgba(255,59,216,.3),transparent);opacity:.85}
.sec-head{display:flex;align-items:center;gap:10px;margin-bottom:13px;flex-wrap:wrap;min-width:0}
.sec-head h2{margin:0;font-size:13.5px;font-weight:650;letter-spacing:.1em;display:flex;align-items:center;gap:9px;white-space:nowrap}
.sec-head h2 .dot{width:6px;height:6px;border-radius:2px;background:var(--cyan);box-shadow:0 0 11px var(--cyan);flex:0 0 auto}
.sec-head .count{font-family:var(--mono);font-size:10.5px;color:var(--cyan);background:rgba(77,240,255,.08);border:1px solid rgba(77,240,255,.22);padding:1px 7px;border-radius:20px;white-space:nowrap}
.sec-head .hint{font-size:11px;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sec-head .spacer{margin-left:auto}
.subhead{display:flex;align-items:center;gap:8px;font-size:11px;letter-spacing:.14em;color:var(--muted);text-transform:uppercase;margin:14px 0 8px}
.subhead .ln{flex:1;height:1px;background:linear-gradient(90deg,rgba(120,160,220,.2),transparent)}

/* ---------- KPI ---------- */
.kpi-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin:20px 0 18px}
.kpi{position:relative;overflow:hidden;min-width:0;border-radius:14px;padding:15px 17px 14px;background:linear-gradient(158deg,rgba(17,27,46,.96),rgba(9,14,25,.97));border:1px solid var(--line);box-shadow:0 16px 40px -32px #000,inset 0 1px 0 rgba(255,255,255,.04);transition:transform .18s ease,border-color .18s ease,box-shadow .18s ease}
.kpi:hover{transform:translateY(-2px);border-color:rgba(77,240,255,.32);box-shadow:0 22px 46px -30px #000,0 0 34px -20px rgba(77,240,255,.45)}
.kpi::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:linear-gradient(180deg,var(--a),transparent 82%)}
.kpi .k-glyph{position:absolute;right:9px;bottom:2px;font-family:var(--mono);font-size:44px;line-height:1;opacity:.055;pointer-events:none}
.kpi .lbl{display:flex;align-items:center;gap:7px;font-size:10.5px;letter-spacing:.16em;color:var(--muted);text-transform:uppercase}
.kpi .lbl i{width:5px;height:5px;border-radius:50%;background:var(--a);box-shadow:0 0 9px var(--a);flex:0 0 auto}
.kpi .val{font-family:var(--mono);font-size:33px;font-weight:700;line-height:1.16;margin:7px 0 3px;color:var(--a);letter-spacing:.005em;text-shadow:0 0 26px color-mix(in srgb,var(--a) 26%,transparent);word-break:break-all}
.kpi .val small{font-size:12px;font-weight:500;color:var(--muted);margin-left:6px;letter-spacing:.03em}
.kpi .val.up,.kpi .val.down{color:inherit}
.kpi .sub{display:flex;gap:12px;flex-wrap:wrap;font-size:11.5px;color:var(--dim);min-height:16px}
.kpi .sub b{font-family:var(--mono);font-weight:600;color:#c6d8f2}
.kpi .sub .up{color:var(--up)}.kpi .sub .down{color:var(--down)}

/* ---------- shared ---------- */
.up{color:var(--up)!important}.down{color:var(--down)!important}.flat{color:var(--muted)!important}
.mono{font-family:var(--mono)}
.dim-note{font-size:11px;color:var(--dim)}
.empty{padding:18px 12px;text-align:center;color:var(--dim);font-size:12.5px;border:1px dashed rgba(120,160,220,.18);border-radius:10px;background:rgba(255,255,255,.012)}
.empty.sm{padding:12px 8px;font-size:11.5px}
.tscroll{overflow-x:auto;-webkit-overflow-scrolling:touch;max-width:100%}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 10px;min-width:0}
.chip{cursor:pointer;font:inherit;font-size:11.5px;line-height:1.5;color:var(--muted);background:rgba(255,255,255,.03);border:1px solid var(--line);padding:3px 10px;border-radius:20px;display:inline-flex;gap:6px;align-items:center;white-space:nowrap;transition:.15s;max-width:100%}
.chip:hover{color:var(--txt);border-color:rgba(77,240,255,.32)}
.chip.on{color:#04141b;background:linear-gradient(135deg,var(--cyan),#9bf6ff);border-color:transparent;font-weight:650;box-shadow:0 0 17px rgba(77,240,255,.28)}
.chip .n{font-family:var(--mono);font-size:10px;opacity:.72}
.news-tools{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.search{position:relative;flex:1 1 210px;min-width:0}
.search input{width:100%;background:rgba(255,255,255,.03);border:1px solid var(--line);border-radius:9px;color:var(--txt);font:inherit;font-size:12.5px;padding:7px 11px 7px 31px;outline:none;transition:.15s}
.search input::placeholder{color:var(--dim)}
.search input:focus{border-color:rgba(77,240,255,.5);box-shadow:0 0 0 3px rgba(77,240,255,.08);background:rgba(77,240,255,.04)}
.search i{position:absolute;left:10px;top:50%;transform:translateY(-50%);font-style:normal;color:var(--dim);font-size:14px;pointer-events:none}
.sel{font:inherit;font-size:12px;color:var(--muted);background:#0d1626;border:1px solid var(--line);border-radius:9px;padding:7px 9px;outline:none;cursor:pointer;max-width:100%}
.sel:focus{border-color:rgba(77,240,255,.45)}
.cnt-live{font-family:var(--mono);font-size:11px;color:var(--dim);white-space:nowrap}
.cnt-live b{color:var(--cyan);font-weight:600}

/* ---------- markets ---------- */
.mkt-groups{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;min-width:0}
.mkt-group{min-width:0;border:1px solid rgba(120,160,220,.11);border-radius:12px;padding:10px 12px 12px;background:rgba(255,255,255,.018)}
.mkt-group h3{margin:0 0 9px;font-size:10.5px;letter-spacing:.2em;color:var(--muted);display:flex;align-items:center;gap:8px;text-transform:uppercase}
.mkt-group h3 .ln{flex:1;height:1px;background:linear-gradient(90deg,rgba(120,160,220,.22),transparent)}
.mkt-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(126px,1fr));gap:8px;min-width:0}
.mkt{min-width:0;border-radius:9px;padding:8px 10px;background:rgba(255,255,255,.022);border:1px solid rgba(120,160,220,.08);transition:.15s}
.mkt:hover{border-color:rgba(77,240,255,.26);background:rgba(77,240,255,.04)}
.mkt .nm{font-size:11.5px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mkt .px{font-family:var(--mono);font-size:15.5px;font-weight:600;margin:1px 0;letter-spacing:.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mkt .ch{font-family:var(--mono);font-size:10.5px;display:flex;gap:6px;white-space:nowrap;overflow:hidden}
.mkt .sym{font-family:var(--mono);font-size:9.5px;color:var(--dim);float:right;margin-left:4px}

/* ---------- news ---------- */
.sec-news{display:flex;flex-direction:column;min-height:0}
.news-list{display:flex;flex-direction:column;gap:8px;flex:1 1 auto;min-height:320px;max-height:none;overflow-y:auto;overflow-x:hidden;padding-right:5px;min-width:0}
.news-more-wrap{display:flex;justify-content:center;flex:0 0 auto;padding:10px 0 2px}
.news-more{cursor:pointer;font:inherit;font-family:var(--mono);font-size:11.5px;letter-spacing:.04em;color:#04141b;background:linear-gradient(135deg,var(--cyan),#9bf6ff);border:1px solid transparent;border-radius:20px;padding:6px 18px;font-weight:650;box-shadow:0 0 18px rgba(77,240,255,.22);transition:.16s;max-width:100%;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.news-more:hover{box-shadow:0 0 26px rgba(77,240,255,.45);transform:translateY(-1px)}
.news-more:active{transform:translateY(0)}
.news-item{display:grid;grid-template-columns:minmax(0,1fr) 70px;gap:12px;min-width:0;padding:10px 12px;border-radius:10px;background:rgba(255,255,255,.022);border:1px solid rgba(120,160,220,.085);transition:.15s}
.news-item:hover{background:rgba(77,240,255,.045);border-color:rgba(77,240,255,.22)}
.news-item .t{font-size:13.5px;line-height:1.45;color:#dce8fb;text-decoration:none;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere;word-break:break-word}
a.t:hover{color:var(--cyan)}
.news-item .sum{font-size:11.5px;color:var(--dim);margin-top:4px;display:-webkit-box;-webkit-line-clamp:1;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}
.news-item .m{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px;font-size:10.5px;color:var(--dim);min-width:0}
.news-item .m .src{color:var(--muted)}
.news-item .m .tm{font-family:var(--mono)}
.cat{font-size:10px;line-height:1.6;padding:0 7px;border-radius:20px;border:1px solid rgba(120,160,220,.3);color:var(--muted);white-space:nowrap}
.ct-cyan{color:var(--cyan);border-color:rgba(77,240,255,.35);background:rgba(77,240,255,.08)}
.ct-mag{color:#ff8ae8;border-color:rgba(255,59,216,.35);background:rgba(255,59,216,.08)}
.ct-amber{color:var(--amber);border-color:rgba(255,196,77,.35);background:rgba(255,196,77,.08)}
.ct-violet{color:#c4b5fd;border-color:rgba(167,139,250,.35);background:rgba(167,139,250,.09)}
.ct-green{color:#5cf0c0;border-color:rgba(46,230,160,.32);background:rgba(46,230,160,.08)}
.ct-blue{color:#9cc4ff;border-color:rgba(110,168,255,.35);background:rgba(110,168,255,.09)}
.tg{font-size:10px;color:var(--dim);border:1px dashed rgba(120,160,220,.22);border-radius:20px;padding:0 6px}
.heat{display:flex;flex-direction:column;align-items:flex-end;justify-content:center;gap:5px;min-width:0}
.heat .bar{width:100%;height:4px;border-radius:3px;background:rgba(255,255,255,.07);overflow:hidden}
.heat .bar i{display:block;height:100%;border-radius:3px;background:linear-gradient(90deg,var(--cyan),var(--magenta));box-shadow:0 0 8px rgba(77,240,255,.4)}
.heat .v{font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:.02em}

/* ---------- radar ---------- */
.radar{display:flex;flex-direction:column;gap:4px;min-width:0}
.rank-list{display:flex;flex-direction:column;gap:1px;min-width:0}
.rk{display:grid;grid-template-columns:22px minmax(0,1fr) auto;gap:9px;align-items:center;padding:5px 6px;border-radius:8px;font-size:12.5px;min-width:0;transition:.14s}
.rk:hover{background:rgba(77,240,255,.05)}
.rk .i{font-family:var(--mono);font-size:11px;color:var(--dim);text-align:right;font-variant-numeric:tabular-nums}
.rk .i.top{color:var(--amber);font-weight:700;text-shadow:0 0 12px rgba(255,196,77,.4)}
.rk a,.rk .tt{color:#c9d9f0;text-decoration:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;display:block}
.rk a:hover{color:var(--cyan)}
.rk .h{font-family:var(--mono);font-size:10px;color:var(--dim);white-space:nowrap}
.rk .tag{font-family:var(--mono);font-size:9.5px;color:var(--muted);border:1px solid var(--line);border-radius:4px;padding:0 4px;white-space:nowrap;max-width:88px;overflow:hidden;text-overflow:ellipsis}
.tech-list{display:flex;flex-direction:column;gap:1px;min-width:0}
.tech-list .rk .tag{color:#9cc4ff;border-color:rgba(110,168,255,.3)}

/* ---------- A share ---------- */
.idx-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(138px,1fr));gap:9px;min-width:0}
.idx{min-width:0;border-radius:10px;padding:9px 11px;background:rgba(255,255,255,.022);border:1px solid rgba(120,160,220,.09);transition:.15s}
.idx:hover{border-color:rgba(77,240,255,.28);background:rgba(77,240,255,.04)}
.idx .nm{font-size:11.5px;color:var(--muted);display:flex;justify-content:space-between;gap:6px;white-space:nowrap;overflow:hidden}
.idx .nm .cd{font-family:var(--mono);font-size:9.5px;color:var(--dim)}
.idx .px{font-family:var(--mono);font-size:18px;font-weight:650;margin:2px 0 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.idx .ch{font-family:var(--mono);font-size:11px;display:flex;gap:7px;white-space:nowrap;overflow:hidden}
.idx .amt{font-family:var(--mono);font-size:9.5px;color:var(--dim);margin-top:2px}
.bw{margin:16px 0 4px}
.bw-bar{display:flex;height:13px;border-radius:7px;overflow:hidden;background:rgba(255,255,255,.055);border:1px solid rgba(120,160,220,.14)}
.bw-bar i{display:block;height:100%;min-width:0}
.bw-bar .u{background:linear-gradient(180deg,#ff7588,#ff3b55)}
.bw-bar .d{background:linear-gradient(180deg,#37f0b5,#0fbf82)}
.bw-bar .f{background:rgba(140,162,192,.42)}
.bw-legend{display:flex;gap:16px;flex-wrap:wrap;margin-top:9px;font-size:11.5px;color:var(--muted);align-items:center}
.bw-legend b{font-family:var(--mono);font-weight:650;color:#d4e4f8}
.bw-legend .u b{color:#ff7b8d}.bw-legend .d b{color:#4ef0bd}.bw-legend .f b{color:#aebfd8}
.bw-pills{display:flex;gap:8px;flex-wrap:wrap;margin-top:9px}
.bw-pill{font-family:var(--mono);font-size:10.5px;padding:2px 8px;border-radius:20px;border:1px solid var(--line);color:var(--muted);background:rgba(255,255,255,.025);white-space:nowrap}
.bw-pill.zt{color:#ff7b8d;border-color:rgba(255,77,109,.32);background:rgba(255,77,109,.08)}
.bw-pill.dt{color:#4ef0bd;border-color:rgba(46,230,160,.3);background:rgba(46,230,160,.07)}
.sector-cols{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:6px;min-width:0}
.sect{min-width:0;border:1px solid rgba(120,160,220,.1);border-radius:11px;padding:9px 11px 6px;background:rgba(255,255,255,.016)}
.sect h4{margin:0 0 4px;font-size:11px;letter-spacing:.12em;color:var(--muted);display:flex;align-items:center;gap:7px;font-weight:600}
.sect h4 .ln{flex:1;height:1px;background:linear-gradient(90deg,rgba(120,160,220,.2),transparent)}
.sect .row{display:grid;grid-template-columns:17px minmax(0,1fr) auto;gap:8px;align-items:center;padding:5px 2px;border-bottom:1px dashed rgba(120,160,220,.08);font-size:12px;min-width:0}
.sect .row:last-child{border-bottom:0}
.sect .row .i{font-family:var(--mono);font-size:10px;color:var(--dim);text-align:right}
.sect .row .nm{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sect .row .nm .ld{display:block;font-size:10px;color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sect .row .ch{font-family:var(--mono);font-size:11.5px;white-space:nowrap}
.pool{display:flex;gap:7px;flex-wrap:wrap;margin-top:12px;align-items:center;min-width:0}
.pool .tt{font-size:10.5px;letter-spacing:.14em;color:var(--muted);white-space:nowrap}
.pool .pi{font-size:11px;color:#cfdff6;border:1px solid var(--line);border-radius:20px;padding:1px 9px;background:rgba(255,255,255,.02);white-space:nowrap}
.pool .pi b{font-family:var(--mono);color:#ff8a9a;font-weight:600;margin-left:4px}
.pool .pi .rs{color:var(--dim);font-size:10px;margin-left:4px}

/* ---------- watchlist ---------- */
.wl-tools{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.wl-sort{cursor:pointer;font:inherit;font-family:var(--mono);font-size:10.5px;color:var(--muted);background:rgba(255,255,255,.03);border:1px solid var(--line);border-radius:7px;padding:3px 9px;transition:.15s;white-space:nowrap}
.wl-sort:hover{color:var(--txt);border-color:rgba(77,240,255,.3)}
.wl-sort.on{color:#04141b;background:linear-gradient(135deg,var(--cyan),#9bf6ff);border-color:transparent;font-weight:650}
.wl-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px;min-width:0}
.wl{position:relative;min-width:0;overflow:hidden;display:flex;flex-direction:column;gap:10px;border:1px solid var(--line);border-radius:13px;padding:13px 14px 12px;background:linear-gradient(172deg,rgba(16,26,44,.94),rgba(9,14,24,.97));box-shadow:0 14px 34px -30px #000}
.wl::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:linear-gradient(180deg,var(--wc,var(--cyan)),transparent 80%);opacity:.9}
.wl.up{--wc:var(--up)}.wl.down{--wc:var(--down)}
.wl-head{display:flex;align-items:flex-start;gap:9px;flex-wrap:wrap;min-width:0}
.wl-head .who{min-width:0;flex:1 1 130px}
.wl-head .nm{font-size:15px;font-weight:650;letter-spacing:.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.wl-head .cd{font-family:var(--mono);font-size:10.5px;color:var(--dim);display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:1px}
.wl-head .mk{font-size:9px;border:1px solid rgba(120,160,220,.28);border-radius:4px;padding:0 4px;color:var(--muted)}
.wl-head .tg{font-size:9.5px}
.wl-head .px{margin-left:auto;text-align:right;white-space:nowrap}
.wl-head .px .p{font-family:var(--mono);font-size:21px;font-weight:700;letter-spacing:.01em;line-height:1.1}
.wl-head .px .c{font-family:var(--mono);font-size:11.5px;margin-top:1px}
.wl-stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:3px 10px;font-size:10.5px;color:var(--dim);min-width:0}
.wl-stats .st{display:flex;justify-content:space-between;gap:4px;min-width:0;padding:2px 0;border-bottom:1px dotted rgba(120,160,220,.09)}
.wl-stats .st b{font-family:var(--mono);font-weight:600;color:#c9d9f0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kl{position:relative;min-width:0;border:1px solid rgba(120,160,220,.1);border-radius:10px;background:rgba(4,8,16,.55);padding:6px 8px 4px;overflow:hidden}
.kl svg{display:block;width:100%;height:auto}
.kl-lbl{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;font-family:var(--mono);font-size:9.5px;color:var(--dim);margin-top:3px;padding:0 1px}
.flow{display:flex;flex-direction:column;gap:6px;min-width:0}
.flow-head{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:10.5px;color:var(--dim);min-width:0}
.flow-head .val{font-family:var(--mono);font-size:13px;font-weight:650}
.ff{display:flex;align-items:flex-end;gap:5px;min-width:0}
.ff-col{flex:1 1 0;min-width:0;display:flex;flex-direction:column;align-items:center;gap:3px}
.ff-bar{width:100%;max-width:30px;height:32px;display:flex;align-items:flex-end;border-radius:3px;background:rgba(255,255,255,.025)}
.ff-bar i{display:block;width:100%;border-radius:3px 3px 0 0;opacity:.85}
.ff-col span{font-family:var(--mono);font-size:8.5px;color:var(--dim);white-space:nowrap}
.acc-list{display:flex;flex-direction:column;gap:5px;min-width:0}
details.acc{border:1px solid rgba(120,160,220,.1);border-radius:9px;background:rgba(255,255,255,.015);overflow:hidden;min-width:0}
details.acc summary{cursor:pointer;list-style:none;padding:6px 10px;font-size:11.5px;color:var(--muted);display:flex;align-items:center;gap:6px;flex-wrap:wrap}
details.acc summary::-webkit-details-marker{display:none}
details.acc summary:hover{color:var(--txt)}
details.acc summary .arw{font-family:var(--mono);font-size:9px;color:var(--dim);transition:.15s}
details.acc[open] summary{color:var(--cyan);border-bottom:1px solid rgba(120,160,220,.1);background:rgba(77,240,255,.04)}
details.acc[open] summary .arw{transform:rotate(90deg)}
details.acc summary .cnt2{font-family:var(--mono);font-size:9.5px;color:var(--dim);margin-left:auto}
details.acc .bd{padding:6px 10px 8px;display:flex;flex-direction:column;gap:5px;max-height:190px;overflow-y:auto;overflow-x:hidden;min-width:0}
.acc .it{display:block;font-size:11.5px;line-height:1.42;color:#b7c8e2;text-decoration:none;overflow-wrap:anywhere;min-width:0}
.acc .it:hover{color:var(--cyan)}
.acc .it .d{font-family:var(--mono);font-size:9.5px;color:var(--dim);margin-right:5px}
.acc .it .meta2{color:var(--dim);font-size:10px;margin-left:4px}
.acc .none{font-size:11px;color:var(--dim)}
.wl-alerts{display:flex;flex-direction:column;gap:4px;min-width:0}
.wl-alert{display:flex;gap:7px;align-items:center;font-size:11px;color:#e8c6f4;min-width:0}
.wl-alert .bd2{width:5px;height:5px;border-radius:50%;background:var(--magenta);box-shadow:0 0 8px var(--magenta);flex:0 0 auto}
.wl-alert.warn{color:#f4dcb0}.wl-alert.warn .bd2{background:var(--amber);box-shadow:0 0 8px var(--amber)}
.wl-alert.info{color:#b6ecf7}.wl-alert.info .bd2{background:var(--cyan);box-shadow:0 0 8px var(--cyan)}

/* ---------- alerts / sources ---------- */
.alert-list{display:flex;flex-direction:column;gap:7px;max-height:460px;overflow-y:auto;overflow-x:hidden;min-width:0}
.alert{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:9px;align-items:center;padding:7px 9px;border-radius:8px;border:1px solid rgba(120,160,220,.1);background:rgba(255,255,255,.018);font-size:12px;min-width:0}
.alert .tx{min-width:0;overflow-wrap:anywhere;color:#cfdef5}
.alert .tm{font-family:var(--mono);font-size:9.5px;color:var(--dim);white-space:nowrap}
.alert .lv{font-family:var(--mono);font-size:9.5px;padding:1px 6px;border-radius:4px;white-space:nowrap;border:1px solid transparent}
.alert.high{border-color:rgba(255,59,216,.3);background:rgba(255,59,216,.055)}
.alert.high .lv{color:#ff9ae8;background:rgba(255,59,216,.15);border-color:rgba(255,59,216,.3)}
.alert.warn{border-color:rgba(255,196,77,.26);background:rgba(255,196,77,.045)}
.alert.warn .lv{color:var(--amber);background:rgba(255,196,77,.12);border-color:rgba(255,196,77,.26)}
.alert.info .lv{color:var(--cyan);background:rgba(77,240,255,.1);border-color:rgba(77,240,255,.24)}
.src-summary{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:11px}
.src-pill{font-family:var(--mono);font-size:10.5px;padding:2px 9px;border-radius:20px;border:1px solid var(--line);color:var(--muted);background:rgba(255,255,255,.025);white-space:nowrap}
.src-pill.ok{color:#4ef0bd;border-color:rgba(46,230,160,.3);background:rgba(46,230,160,.07)}
.src-pill.bad{color:#ff7b8d;border-color:rgba(255,77,109,.32);background:rgba(255,77,109,.08)}
table.src{width:100%;border-collapse:collapse;font-size:11.5px;min-width:560px}
table.src th{font-weight:500;color:var(--muted);font-size:10.5px;letter-spacing:.08em;text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);white-space:nowrap;text-transform:uppercase}
table.src td{padding:6px 8px;border-bottom:1px solid rgba(120,160,220,.055);color:#c3d2e8;vertical-align:top;overflow-wrap:anywhere}
table.src tr:hover td{background:rgba(77,240,255,.03)}
table.src tr.bad td{color:#ff9aa8}
table.src tr.bad td:first-child{box-shadow:inset 2px 0 0 rgba(255,77,109,.6)}
.st{font-family:var(--mono);font-size:9.5px;padding:1px 6px;border-radius:4px;white-space:nowrap;display:inline-block}
.st.ok{color:#4ef0bd;background:rgba(46,230,160,.1)}
.st.no{color:#ff7b8d;background:rgba(255,77,109,.13)}
.err-box{margin-top:12px;border:1px solid rgba(255,77,109,.22);border-radius:10px;background:rgba(255,77,109,.04);padding:9px 12px;min-width:0}
.err-box h4{margin:0 0 6px;font-size:11px;letter-spacing:.1em;color:#ff9aa8;font-weight:600}
.err-box .ei{font-size:11px;color:#e6b7bf;line-height:1.5;overflow-wrap:anywhere;font-family:var(--mono)}
.err-box .ei b{color:#ff8a9a;font-weight:600}

/* ---------- footer ---------- */
.foot{margin-top:26px;padding-top:14px;border-top:1px solid var(--line);display:flex;gap:14px;flex-wrap:wrap;justify-content:space-between;font-family:var(--mono);font-size:10.5px;color:var(--dim)}
.foot .r{display:flex;gap:14px;flex-wrap:wrap}

/* ---------- archive page ---------- */
.arch-wrap{max-width:1180px;margin:0 auto;padding:26px 22px 48px;position:relative;z-index:1}
.arch-title{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-bottom:6px}
.arch-title h1{margin:0;font-size:24px;letter-spacing:.12em;font-weight:650}
.arch-title .en{font-family:var(--mono);font-size:10px;letter-spacing:.3em;color:var(--dim);text-transform:uppercase}
.arch-sub{font-family:var(--mono);font-size:11.5px;color:var(--dim);margin-bottom:20px}
.arch-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(255px,1fr));gap:14px}
.arch-card{display:flex;flex-direction:column;gap:10px;text-decoration:none;min-width:0;border:1px solid var(--line);border-radius:13px;padding:15px 16px;background:linear-gradient(168deg,rgba(16,26,44,.94),rgba(9,14,24,.97));box-shadow:0 16px 38px -32px #000;transition:.18s}
.arch-card:hover{border-color:rgba(77,240,255,.36);transform:translateY(-2px);box-shadow:0 22px 44px -28px #000,0 0 36px -20px rgba(77,240,255,.5)}
.arch-card .d{font-family:var(--mono);font-size:19px;font-weight:700;letter-spacing:.03em;color:#e8f4ff}
.arch-card .m{display:flex;flex-direction:column;gap:5px;font-size:11.5px;color:var(--muted)}
.arch-card .m .row{display:flex;justify-content:space-between;gap:10px;border-bottom:1px dotted rgba(120,160,220,.1);padding-bottom:3px;min-width:0}
.arch-card .m b{font-family:var(--mono);font-weight:600;color:#d3e4fa;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.arch-card .m b.up{color:var(--up)}.arch-card .m b.down{color:var(--down)}
.arch-card .go{font-family:var(--mono);font-size:11px;color:var(--cyan);margin-top:auto}
.arch-card .go::after{content:" →"}
.arch-empty{padding:30px;text-align:center;color:var(--dim);border:1px dashed rgba(120,160,220,.2);border-radius:12px}

/* ---------- responsive ---------- */
@media (max-width:1279px){
  .layout{grid-template-columns:repeat(2,minmax(0,1fr));grid-template-areas:
    "markets markets"
    "news news"
    "radar alerts"
    "ashare ashare"
    "watch watch"
    "sources sources"}
  .kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media (max-width:900px){
  .wl-grid{grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}
  .wl-stats{grid-template-columns:repeat(3,minmax(0,1fr))}
}
@media (max-width:767px){
  .topbar-in{padding:11px 14px;gap:10px}
  .brand h1{font-size:16px;letter-spacing:.1em}
  .brand .en{display:none}
  .meta{gap:6px;margin-left:0;width:100%}
  .wrap{padding:0 13px 34px}
  .kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:14px 0}
  .kpi{padding:12px 13px}
  .kpi .val{font-size:26px}
  .kpi .k-glyph{font-size:34px}
  .layout{grid-template-columns:minmax(0,1fr);grid-template-areas:
    "markets"
    "news"
    "radar"
    "alerts"
    "ashare"
    "watch"
    "sources"}
  .sec{padding:13px 13px 15px;border-radius:13px}
  .sec-news .news-list{max-height:none;min-height:0;overflow:visible;padding-right:0}
  .mkt-groups{grid-template-columns:minmax(0,1fr)}
  .mkt-cards{grid-template-columns:repeat(auto-fill,minmax(108px,1fr))}
  .news-item{grid-template-columns:minmax(0,1fr);gap:6px}
  .heat{display:none}
  .sector-cols{grid-template-columns:minmax(0,1fr)}
  .idx-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
  .wl-grid{grid-template-columns:minmax(0,1fr)}
  .wl-stats{grid-template-columns:repeat(2,minmax(0,1fr))}
  .wl-head .px .p{font-size:19px}
  .arch .menu{min-width:200px}
  .foot{font-size:10px;gap:8px}
  .arch-wrap{padding:20px 14px 36px}
  .arch-title h1{font-size:20px}
}
@media (max-width:430px){
  .kpi .val{font-size:23px}
  .brand .glyph{width:30px;height:30px;font-size:14px}
  .pill{font-size:10px;padding:3px 8px}
  .kpi-grid{gap:9px}
}
@media (prefers-reduced-motion:reduce){
  *{transition:none!important}
}
"""


# --------------------------------------------------------------------------- #
# inline JS (vanilla, no external calls, no "undefined" literal)
# --------------------------------------------------------------------------- #
_JS = r"""
(function () {
  "use strict";

  var DATA = (window.__INTEL__ && typeof window.__INTEL__ === "object") ? window.__INTEL__ : {};
  var WORLD = (DATA.world && typeof DATA.world === "object") ? DATA.world : {};
  var NEWS = (WORLD.news && typeof WORLD.news.length === "number") ? WORLD.news : [];
  var DATA_DATE = typeof DATA.date === "string" ? DATA.date : "";
  var PAGE = 80;
  var TONES = ["cyan", "mag", "amber", "violet", "green", "blue"];
  var STATE = { cat: "", q: "", sort: "heat", shown: PAGE };
  var CAT_TONE = {};
  var MAX_HEAT = 1;
  var NEWS_TARGET = 0;

  function each(nodes, fn) { for (var i = 0; i < nodes.length; i++) { fn(nodes[i], i); } }
  function isArr(v) { return Object.prototype.toString.call(v) === "[object Array]"; }
  function isObj(v) { return v !== null && typeof v === "object" && !isArr(v); }
  function num(v) { var f = parseFloat(v); return isFinite(f) ? f : null; }
  function str(v) { return v == null ? "" : String(v); }

  function clean(v, max) {
    var s = str(v).replace(/\s+/g, " ").trim();
    if (max && s.length > max) { s = s.slice(0, max - 1).replace(/\s+$/, "") + "…"; }
    return s;
  }

  function esc(v) {
    return str(v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function safeUrl(v) {
    var s = clean(v, 2048);
    if (!/^https?:\/\//i.test(s)) { return ""; }
    if (/\s/.test(s)) { return ""; }
    return s;
  }

  function fmtTime(pub) {
    var m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(str(pub));
    if (!m) { return "—"; }
    if (DATA_DATE && (m[1] + "-" + m[2] + "-" + m[3]) === DATA_DATE) { return m[4] + ":" + m[5]; }
    return m[2] + "-" + m[3] + " " + m[4] + ":" + m[5];
  }

  function newsList() { return document.getElementById("news-list"); }
  function newsSection() { return document.getElementById("world-news"); }
  function attr(el, name) { var v = el.getAttribute(name); return v === null ? "" : v; }

  function buildCategories() {
    var map = {};
    var order = [];
    var i;
    var it;
    var name;
    var bc = isArr(WORLD.by_category) ? WORLD.by_category : [];
    for (i = 0; i < bc.length; i++) {
      it = bc[i];
      if (!isObj(it)) { continue; }
      name = clean(it.name, 24);
      if (!name) { continue; }
      if (!(name in map)) { map[name] = 0; order.push(name); }
      map[name] = map[name] + (num(it.count) || 0);
    }
    if (!order.length) {
      for (i = 0; i < NEWS.length; i++) {
        name = clean(NEWS[i] && NEWS[i].category, 24) || "综合";
        if (!(name in map)) { map[name] = 0; order.push(name); }
        map[name] = map[name] + 1;
      }
    }
    var normal = [];
    var zong = [];
    for (i = 0; i < order.length; i++) {
      if (order[i] === "综合") { zong.push(order[i]); } else { normal.push(order[i]); }
    }
    normal.sort(function (a, b) { return map[b] - map[a]; });
    var chips = normal.concat(zong);
    for (i = 0; i < chips.length; i++) { CAT_TONE[chips[i]] = "ct-" + TONES[i % TONES.length]; }
  }

  function toneOf(cat) { return CAT_TONE[cat] || "ct-cyan"; }

  function computeMaxHeat() {
    var m = 0;
    var i;
    var h;
    for (i = 0; i < NEWS.length; i++) {
      h = num(NEWS[i] && NEWS[i].heat);
      if (h !== null && h > m) { m = h; }
    }
    MAX_HEAT = m > 0 ? m : 1;
  }

  function newsItemHTML(n) {
    n = isObj(n) ? n : {};
    var title = clean(n.title, 160) || "（无标题）";
    var summary = clean(n.summary, 180);
    var src = clean(n.source, 40) || clean(n.source_id, 40) || "未知来源";
    var cat = clean(n.category, 24) || "综合";
    var url = safeUrl(n.url);
    var heat = num(n.heat);
    var tone = toneOf(cat);
    var tags = [];
    var raw = isArr(n.tags) ? n.tags : [];
    var k;
    for (k = 0; k < raw.length && tags.length < 2; k++) {
      var tg = clean(raw[k], 12);
      if (tg) { tags.push(tg); }
    }
    var tagHTML = "";
    for (k = 0; k < tags.length; k++) { tagHTML += '<span class="tg">' + esc(tags[k]) + "</span>"; }
    var titleHTML;
    if (url) {
      titleHTML = '<a class="t" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + esc(title) + "</a>";
    } else {
      titleHTML = '<span class="t">' + esc(title) + "</span>";
    }
    var sumHTML = summary ? '<div class="sum">' + esc(summary) + "</div>" : "";
    var meta = '<span class="cat ' + tone + '">' + esc(cat) + "</span>" +
      '<span class="src">' + esc(src) + "</span>" +
      '<span class="tm">' + esc(fmtTime(n.published)) + "</span>" + tagHTML;
    var heatHTML;
    if (heat !== null) {
      var hpct = Math.max(6, Math.min(100, heat / MAX_HEAT * 100));
      heatHTML = '<div class="heat"><span class="v">' + Math.round(heat) +
        '</span><span class="bar"><i style="width:' + Math.round(hpct) + '%"></i></span></div>';
    } else {
      heatHTML = '<div class="heat"><span class="v">—</span></div>';
    }
    return '<article class="news-item" data-news-item="1"><div class="nb">' + titleHTML + sumHTML +
      '<div class="m">' + meta + "</div></div>" + heatHTML + "</article>";
  }

  function filteredNews() {
    var out = [];
    var i;
    var n;
    var blob;
    var q = STATE.q;
    var cat = STATE.cat;
    for (i = 0; i < NEWS.length; i++) {
      n = isObj(NEWS[i]) ? NEWS[i] : {};
      if (cat && (clean(n.category, 24) || "综合") !== cat) { continue; }
      if (q) {
        blob = (str(n.title) + " " + str(n.summary) + " " + str(n.source) + " " +
          str(n.category) + " " + (isArr(n.tags) ? n.tags.join(" ") : "")).toLowerCase();
        if (blob.indexOf(q) < 0) { continue; }
      }
      out.push(n);
    }
    if (STATE.sort === "time") {
      out.sort(function (a, b) { return str(b.published).localeCompare(str(a.published)); });
    } else {
      out.sort(function (a, b) { return (num(b.heat) || 0) - (num(a.heat) || 0); });
    }
    return out;
  }

  function renderNews() {
    var list = newsList();
    if (!list) { return; }
    var arr = filteredNews();
    var shown = Math.min(STATE.shown, arr.length);
    var html = [];
    var i;
    for (i = 0; i < shown; i++) { html.push(newsItemHTML(arr[i])); }
    list.innerHTML = html.join("");
    list.scrollTop = 0;
    var visible = document.getElementById("news-visible-count");
    if (visible) { visible.textContent = String(shown); }
    var totalEl = document.getElementById("news-total");
    if (totalEl) { totalEl.textContent = String(NEWS.length); }
    var more = document.getElementById("news-load-more");
    var remain = arr.length - shown;
    if (more) {
      if (remain > 0) {
        more.style.display = "";
        more.textContent = "加载更多（剩余 " + remain + " 条）";
      } else {
        more.style.display = "none";
      }
    }
    var empty = document.getElementById("news-empty");
    if (empty) { empty.style.display = (arr.length === 0 && NEWS.length > 0) ? "block" : "none"; }
    var sec = newsSection();
    if (sec) { sec.setAttribute("data-visible", String(shown)); }
    var hint = document.getElementById("news-hint");
    if (hint) {
      hint.textContent = STATE.cat
        ? ("筛选：" + STATE.cat + " · 匹配 " + arr.length + " 条")
        : "按热度排序 · 支持搜索与分类筛选";
    }
    fitNewsPanel();
  }

  function radarNaturalHeight(sec) {
    var radar = document.querySelector('[data-testid="hot-radar"]');
    if (!radar) { return 0; }
    var prev = sec.style.height;
    sec.style.height = "0px";
    var h = radar.getBoundingClientRect().height;
    sec.style.height = prev ? prev : "";
    return h;
  }

  function fitNewsPanel() {
    var sec = newsSection();
    var list = newsList();
    if (!sec || !list) { return; }
    var vw = window.innerWidth;
    if (vw < 768) {
      sec.style.height = "";
      list.style.maxHeight = "";
      NEWS_TARGET = 0;
      return;
    }
    var radar = document.querySelector('[data-testid="hot-radar"]');
    var target;
    if (vw >= 1280) {
      target = radarNaturalHeight(sec);
      if (!target || target < 420) { target = 620; }
      if (NEWS_TARGET && Math.abs(target - NEWS_TARGET) < 2 && sec.style.height) { return; }
      NEWS_TARGET = target;
      sec.style.height = Math.round(target) + "px";
      list.style.maxHeight = "";
    } else {
      target = radar ? radar.getBoundingClientRect().height : 0;
      if (!target || target < 420) { target = 620; }
      NEWS_TARGET = 0;
      sec.style.height = "";
      list.style.maxHeight = Math.round(Math.min(Math.max(target, 500), 780)) + "px";
    }
  }

  function initNews() {
    buildCategories();
    computeMaxHeat();
    var chips = document.querySelectorAll("[id^='filter-chip-']");
    each(chips, function (chip) {
      chip.addEventListener("click", function () {
        each(chips, function (c) { c.classList.remove("on"); });
        chip.classList.add("on");
        STATE.cat = attr(chip, "data-cat");
        STATE.shown = PAGE;
        renderNews();
      });
    });
    var input = document.getElementById("news-search");
    if (input) {
      input.addEventListener("input", function () {
        STATE.q = clean(input.value, 80).toLowerCase();
        STATE.shown = PAGE;
        renderNews();
      });
    }
    var sortSel = document.getElementById("news-sort");
    if (sortSel) {
      sortSel.addEventListener("change", function () {
        STATE.sort = sortSel.value === "time" ? "time" : "heat";
        renderNews();
      });
    }
    var more = document.getElementById("news-load-more");
    if (more) {
      more.addEventListener("click", function () {
        STATE.shown = STATE.shown + PAGE;
        renderNews();
      });
    }
    document.addEventListener("keydown", function (ev) {
      if (!input) { return; }
      if (ev.key === "/" && document.activeElement !== input) {
        ev.preventDefault();
        input.focus();
      }
    });
    renderNews();
  }

  function initWatchlist() {
    var grid = document.getElementById("wl-grid");
    if (!grid) { return; }
    var btns = document.querySelectorAll("[data-wl-sort]");
    if (!btns.length) { return; }
    var cards = Array.prototype.slice.call(grid.children);
    each(btns, function (btn) {
      btn.addEventListener("click", function () {
        each(btns, function (b) { b.classList.remove("on"); });
        btn.classList.add("on");
        var key = attr(btn, "data-wl-sort");
        cards.sort(function (a, b) {
          if (key === "code") { return attr(a, "data-code").localeCompare(attr(b, "data-code")); }
          var va = parseFloat(attr(a, "data-" + key)) || 0;
          var vb = parseFloat(attr(b, "data-" + key)) || 0;
          return vb - va;
        });
        for (var i = 0; i < cards.length; i++) { grid.appendChild(cards[i]); }
      });
    });
  }

  function initArchive() {
    var dd = document.getElementById("archive-dd");
    if (!dd) { return; }
    document.addEventListener("click", function (ev) {
      if (dd.open && ev.target && !dd.contains(ev.target)) { dd.open = false; }
    });
  }

  function initFit() {
    fitNewsPanel();
    window.addEventListener("resize", fitNewsPanel);
    window.addEventListener("load", fitNewsPanel);
    if (typeof ResizeObserver !== "function") { return; }
    var radar = document.querySelector('[data-testid="hot-radar"]');
    if (!radar) { return; }
    try {
      new ResizeObserver(function () { fitNewsPanel(); }).observe(radar);
    } catch (err) { /* layout already fitted once */ }
  }

  try {
    initNews();
    initWatchlist();
    initArchive();
    initFit();
  } catch (err) {
    /* static content is server-rendered; interactions are progressive enhancement */
  }
})();
"""


# --------------------------------------------------------------------------- #
# top bar + KPI
# --------------------------------------------------------------------------- #
def _render_topbar(data, title, dates, archive_href) -> str:
    d = _dct(data)
    world, ash = _dct(d.get("world")), _dct(d.get("ashare"))
    run = _dct(d.get("run"))
    date_s = str(d.get("date") or "—")
    gen_s = _fmt_gen(d.get("generated_at"))

    sources = _lst(world.get("sources")) + _lst(ash.get("sources"))
    ok = bad = 0
    for s in sources:
        s = _dct(s)
        if s.get("ok") is True:
            ok += 1
        elif s.get("ok") is False:
            bad += 1
    bits = [f'<span class="pill"><span class="dot-live"></span><b>{_e(date_s)}</b></span>']
    if gen_s and gen_s != "—":
        bits.append(f'<span class="pill">生成 <b>{_e(gen_s)}</b></span>')
    dur = _num(run.get("duration_sec"))
    if dur is not None:
        bits.append(f'<span class="pill">采集 <b>{dur:.1f}s</b></span>')
    req = _num(run.get("requests"))
    if req is not None:
        bits.append(f'<span class="pill">请求 <b>{int(req)}</b></span>')
    if sources:
        src_total = len(sources)
        if bad:
            bits.append(
                f'<span class="pill bad">数据源 <b>{ok}</b>/<b>{src_total}</b>'
                f" · 失败 <b>{bad}</b></span>"
            )
        else:
            bits.append(f'<span class="pill">数据源 <b>{ok}</b>/<b>{src_total}</b></span>')
    if run.get("offline") is True:
        bits.append('<span class="pill offline">离线样本</span>')

    menu = []
    for dd in _lst(dates):
        dd = str(dd)
        cur = dd == date_s
        cls = "cur" if cur else ""
        suffix = " · 当前" if cur else ""
        menu.append(f'<a class="{cls}" href="../{_e(dd)}/index.html">{_e(dd)}{suffix}</a>')
    if archive_href:
        menu.append(f'<a class="all" href="{_e(archive_href)}">全部归档 ↗</a>')
    arch = ""
    if menu:
        arch = (
            '<details class="arch" id="archive-dd"><summary>归档 ▾</summary>'
            f'<div class="menu">{"".join(menu)}</div></details>'
        )

    return (
        '<header class="topbar"><div class="topbar-in">'
        '<div class="brand"><span class="glyph">◈</span>'
        f'<h1>{_e(title)}</h1><span class="en">daily intel radar</span></div>'
        f'<div class="meta">{"".join(bits)}{arch}</div>'
        "</div></header>"
    )


def _kpi_card(label, value_html, sub_html, accent, glyph, value_cls="") -> str:
    return (
        '<div class="kpi" style="--a:var(' + accent + ')">'
        f'<div class="k-glyph">{glyph}</div>'
        f'<div class="lbl"><i></i>{_e(label)}</div>'
        f'<div class="val {value_cls}">{value_html}</div>'
        f'<div class="sub">{sub_html}</div>'
        "</div>"
    )


def _render_kpis(data) -> str:
    d = _dct(data)
    world, ash = _dct(d.get("world")), _dct(d.get("ashare"))
    news = [_dct(x) for x in _lst(world.get("news"))]
    news_count = len(news)

    by_cat = [_dct(x) for x in _lst(world.get("by_category"))]
    if not by_cat:
        counts = {}
        for it in news:
            name = str(it.get("category") or "综合")
            counts[name] = counts.get(name, 0) + 1
        by_cat = [{"name": k, "count": v} for k, v in counts.items()]
    by_cat = [c for c in by_cat if str(c.get("name") or "")]
    by_cat.sort(key=lambda c: -(int(_num(c.get("count")) or 0)))
    # 「综合」是兜底类别（真实数据里占比常超 30%），做「最热类别」没有信息量：
    # 优先取非「综合」类别；没有其他类别时才回退到「综合」。
    _named = [c for c in by_cat if str(c.get("name")) != "综合"]
    _pool = _named or by_cat
    top = _pool[0] if _pool else None

    # --- KPI 1: world news count ---
    cat_num = len(by_cat)
    sub1 = f'<span>覆盖 <b>{cat_num}</b> 个类别</span><span>来源 <b>{len(_lst(world.get("sources")))}</b> 个</span>'
    if not news_count:
        sub1 = '<span class="dim-note">今日数据缺失</span>'

    # --- KPI 2: A-share up ratio ---
    breadth = _dct(ash.get("breadth"))
    total = _num(breadth.get("total"))
    up = _num(breadth.get("up"))
    down = _num(breadth.get("down"))
    flat = _num(breadth.get("flat"))
    ratio = _num(breadth.get("up_ratio"))
    if ratio is None and total and up is not None:
        ratio = up / total if total else None
    if ratio is not None:
        kpi2_val = f"{ratio * 100:.1f}" + '<small>%</small>'
        kpi2_cls = "up" if ratio >= 0.5 else "down"
        u_txt = f"{int(up)}" if up is not None else "—"
        d_txt = f"{int(down)}" if down is not None else "—"
        t_txt = f"{int(total)}" if total is not None else "—"
        f_txt = f"{int(flat)}" if flat is not None else "—"
        sub2 = (
            f'<span class="u">涨 <b class="up">{u_txt}</b></span>'
            f'<span class="d">跌 <b class="down">{d_txt}</b></span>'
            f'<span>平 <b>{f_txt}</b></span><span>共 <b>{t_txt}</b></span>'
        )
    else:
        kpi2_val, kpi2_cls = "—", ""
        sub2 = '<span class="dim-note">今日数据缺失</span>'

    # --- KPI 3: watchlist average change ---
    wl = [_dct(x) for x in _lst(ash.get("watchlist"))]
    meta = _dct(ash.get("watchlist_meta"))
    avg = _num(meta.get("avg_change_pct"))
    if avg is None and wl:
        vals = [_num(_dct(w.get("quote")).get("change_pct")) for w in wl]
        vals = [v for v in vals if v is not None]
        avg = sum(vals) / len(vals) if vals else None
    if avg is not None:
        kpi3_val = f"{avg:+.2f}" + '<small>%</small>'
        kpi3_cls = _tone(avg)
    else:
        kpi3_val, kpi3_cls = "—", ""
    best = _dct(meta.get("best"))
    worst = _dct(meta.get("worst"))
    if not best and wl:
        ranked = sorted(wl, key=lambda w: (_num(_dct(w.get("quote")).get("change_pct")) or 0), reverse=True)
        best = {"name": ranked[0].get("name"), "change_pct": _dct(ranked[0].get("quote")).get("change_pct")}
        worst = {"name": ranked[-1].get("name"), "change_pct": _dct(ranked[-1].get("quote")).get("change_pct")}
    sub3 = (
        f'<span>最强 <b class="up">{_e(best.get("name") or "—")} {_fmt_pct(best.get("change_pct"))}</b></span>'
        f'<span>最弱 <b class="down">{_e(worst.get("name") or "—")} {_fmt_pct(worst.get("change_pct"))}</b></span>'
        if (best or worst) else f'<span>自选股 <b>{len(wl)}</b> 只</span>'
    )

    # --- KPI 4: hottest category ---
    if top:
        top_name = str(top.get("name") or "—")
        top_count = int(_num(top.get("count")) or 0)
        share = (top_count / news_count * 100.0) if news_count else None
        kpi4_val = _e(top_name)
        kpi4_cls = ""
        sub4 = f'<span><b>{top_count}</b> 条</span><span>占全部 <b>{share:.1f}%</b></span>' if share is not None else f'<span><b>{top_count}</b> 条</span>'
    else:
        kpi4_val, kpi4_cls = "—", ""
        sub4 = '<span class="dim-note">今日数据缺失</span>'

    cards = [
        _kpi_card("全球要闻", f"{news_count}", sub1, "--cyan", "◍"),
        _kpi_card("A股上涨家数占比", kpi2_val, sub2, "--magenta", "▤", kpi2_cls),
        _kpi_card("自选股平均涨跌", kpi3_val, sub3, "--amber", "◈", kpi3_cls),
        _kpi_card("最热类别", kpi4_val, sub4, "--violet", "✦", kpi4_cls),
    ]
    return '<section class="kpi-grid" data-testid="kpi-cards">' + "".join(cards) + "</section>"


# --------------------------------------------------------------------------- #
# section: global markets
# --------------------------------------------------------------------------- #
def _render_markets(world) -> str:
    markets = [_dct(m) for m in _lst(_dct(world).get("markets"))]
    groups = {}
    order = []
    for m in markets:
        g = str(m.get("group") or "其他")
        if g not in groups:
            groups[g] = []
            order.append(g)
        groups[g].append(m)

    if not markets:
        body = '<div class="empty">今日数据缺失 · 全球市场快照不可用</div>'
    else:
        blocks = []
        for g in order:
            cards = []
            for m in groups[g]:
                name = str(m.get("name") or m.get("symbol") or "—")
                sym = str(m.get("symbol") or "")
                pct = _num(m.get("change_pct"))
                chg = _num(m.get("change"))
                tone = _tone(pct)
                chg_html = ""
                if chg is not None:
                    chg_html = f'<span class="{tone}">{_fmt_signed(chg)}</span>'
                chart_key = f"world:{sym}" if sym else ""
                chart_attr = f' data-chart="{_e(chart_key)}"' if chart_key else ""
                cards.append(
                    f'<div class="mkt chartable"{chart_attr} data-label="{_e(name)}" title="点击查看分时 / 日线">'
                    f'<div class="nm"><span class="sym">{_e(sym)}</span>{_e(name)}</div>'
                    f'<div class="px">{_fmt_price(m.get("price"))}</div>'
                    f'<div class="ch {tone}">{_fmt_pct(pct)}{chg_html}</div>'
                    "</div>"
                )
            blocks.append(
                '<div class="mkt-group">'
                f'<h3>{_e(g)}<span class="ln"></span></h3>'
                f'<div class="mkt-cards">{"".join(cards)}</div></div>'
            )
        body = f'<div class="mkt-groups">{"".join(blocks)}</div>'

    head = (
        '<div class="sec-head"><h2><span class="dot"></span>全球市场快照</h2>'
        f'<span class="count">{len(markets)} 项</span><span class="spacer"></span>'
        '<span class="hint">红涨绿跌 · 按分组 · 点击卡片看分时 / 日线</span></div>'
    )
    return f'<section class="sec" style="grid-area:markets" data-testid="markets-snapshot">{head}{body}</section>'


# --------------------------------------------------------------------------- #
# section: world news (chips + search + heat bars)
# --------------------------------------------------------------------------- #
def _news_item(it, tones, max_heat, data_date) -> str:
    it = _dct(it)
    title = _clean(it.get("title"), 160) or "（无标题）"
    summary = _clean(it.get("summary"), 180)
    src = _clean(it.get("source") or it.get("source_id"), 40) or "未知来源"
    cat = str(it.get("category") or "综合")
    url = _safe_url(it.get("url"))
    heat = _num(it.get("heat"))
    tone = tones.get(cat, "ct-cyan")
    hpct = 0
    if heat is not None and max_heat > 0:
        hpct = max(6.0, min(100.0, heat / max_heat * 100.0))
    ts = _fmt_time(it.get("published"), data_date)
    tags = [_clean(t, 12) for t in _lst(it.get("tags"))[:2] if _clean(t, 12)]
    title_html = _link(url, title, "t")
    sum_html = f'<div class="sum">{_e(summary)}</div>' if summary else ""
    tag_html = "".join(f'<span class="tg">{_e(t)}</span>' for t in tags)
    meta = (
        f'<span class="cat {tone}">{_e(cat)}</span>'
        f'<span class="src">{_e(src)}</span>'
        f'<span class="tm">{_e(ts)}</span>{tag_html}'
    )
    heat_html = (
        '<div class="heat">'
        f'<span class="v">{heat:.0f}</span>'
        f'<span class="bar"><i style="width:{hpct:.0f}%"></i></span>'
        "</div>"
    ) if heat is not None else '<div class="heat"><span class="v">—</span></div>'

    return (
        '<article class="news-item" data-news-item="1">'
        f'<div class="nb">{title_html}{sum_html}<div class="m">{meta}</div></div>'
        f"{heat_html}</article>"
    )


def _ordered_category_names(counts: dict) -> list:
    """按条数降序；「综合」没有信息量，固定排最后。"""
    names = [name for name in counts.keys() if name != "综合"]
    names.sort(key=lambda name: -counts.get(name, 0))
    if "综合" in counts:
        names.append("综合")
    return names


def _render_news(world, data_date: str) -> str:
    world = _dct(world)
    news = [_dct(x) for x in _lst(world.get("news"))]
    total = len(news)

    counts = {}
    for item in _lst(world.get("by_category")):
        item = _dct(item)
        name = _clean(item.get("name"), 20)
        if name:
            counts[name] = int(_num(item.get("count")) or 0)
    if not counts:
        for it in news:
            name = _clean(it.get("category"), 20) or "综合"
            counts[name] = counts.get(name, 0) + 1

    names = _ordered_category_names(counts)
    tones = _cat_tones(names)

    chips = [
        '<button type="button" class="chip on" id="filter-chip-全部" data-cat="">全部'
        f'<span class="n">{total}</span></button>'
    ]
    for name in names:
        chips.append(
            f'<button type="button" class="chip" id="filter-chip-{_e(_safe_id(name))}" '
            f'data-cat="{_e(name)}">{_e(name)}<span class="n">{counts[name]}</span></button>'
        )

    # 首屏仅渲染 NEWS_INITIAL 条，其余由「加载更多」按钮按需渲染（控制单文件体积）
    max_heat = max([_num(it.get("heat")) or 0.0 for it in news] + [1.0])
    initial = news[:NEWS_INITIAL]
    remaining = max(0, total - len(initial))
    items_html = "".join(_news_item(it, tones, max_heat, data_date) for it in initial)
    nodata = "" if news else '<div class="empty">今日数据缺失 · 全球要闻暂未采集到</div>'
    more_text = f"加载更多（剩余 {remaining} 条）" if remaining else "加载更多"
    more_hidden = "" if remaining else ' style="display:none"'
    more_html = (
        f'<div class="news-more-wrap" id="news-more-wrap"{more_hidden}>'
        '<button type="button" class="news-more" id="news-load-more" '
        f'data-testid="news-load-more">{more_text}</button></div>'
    )

    head = (
        '<div class="sec-head"><h2><span class="dot"></span>全球要闻</h2>'
        f'<span class="count" id="news-count">{total} 条</span>'
        '<span class="spacer"></span>'
        '<span class="hint" id="news-hint">按热度排序 · 支持搜索与分类筛选</span></div>'
    )
    tools = (
        '<div class="news-tools">'
        '<label class="search"><i>⌕</i>'
        '<input id="news-search" type="search" placeholder="搜索标题 / 摘要 / 来源…" '
        'autocomplete="off" spellcheck="false" aria-label="搜索全球要闻"></label>'
        '<select class="sel" id="news-sort" aria-label="排序方式">'
        '<option value="heat">按热度</option><option value="time">按时间</option></select>'
        f'<span class="cnt-live">显示 <b id="news-visible-count">{len(initial)}</b> / '
        f'<span id="news-total">{total}</span></span></div>'
    )
    body = (
        f'<div class="chips">{"".join(chips)}</div>'
        f"{tools}{nodata}"
        '<div class="news-list" id="news-list">'
        f"{items_html}"
        "</div>"
        '<div class="empty" id="news-empty" style="display:none">无匹配结果 · 调整关键词或选择「全部」</div>'
        f"{more_html}"
    )
    return (
        '<section class="sec sec-news" style="grid-area:news" id="world-news" '
        f'data-testid="world-news" data-total="{total}" data-visible="{len(initial)}">'
        f"{head}{body}</section>"
    )


# --------------------------------------------------------------------------- #
# section: hotspot radar (social + tech)
# --------------------------------------------------------------------------- #
def _render_radar(world) -> str:
    world = _dct(world)
    social = [_dct(x) for x in _lst(world.get("social"))]
    news = [_dct(x) for x in _lst(world.get("news"))]
    tech_ids = {"hackernews", "arxiv_cs_ai"}
    tech = [it for it in news if str(it.get("source_id") or "") in tech_ids]
    if not tech:
        tech = [it for it in news if str(it.get("category") or "") == "科技AI"][:8]
    tech = tech[:10]

    def rank_rows(items, with_heat=True, tag_key="source"):
        rows = []
        for i, it in enumerate(items, 1):
            it = _dct(it)
            title = _clean(it.get("title"), 150) or "（无标题）"
            url = _safe_url(it.get("url"))
            tag = _clean(it.get("source") or it.get("source_id"), 20)
            rank_cls = "i top" if i <= 3 else "i"
            right = ""
            if with_heat:
                hot = _num(it.get("heat"))
                if hot is not None:
                    right = f'<span class="h">{_hot(hot)}</span>'
                elif it.get("rank") is not None:
                    right = f'<span class="h">#{_e(it.get("rank"))}</span>'
            else:
                right = f'<span class="tag">{_e(tag)}</span>'
            link = _link(url, title, "tt") if url else f'<span class="tt">{_e(title)}</span>'
            rows.append(
                f'<div class="rk"><span class="{rank_cls}">{i:02d}</span>'
                f"{link}{right}</div>"
            )
        return "".join(rows)

    social_html = (
        f'<div class="rank-list">{rank_rows(social)}</div>'
        if social else '<div class="empty sm">热搜数据缺失</div>'
    )
    tech_html = (
        f'<div class="tech-list">{rank_rows(tech, with_heat=False)}</div>'
        if tech else '<div class="empty sm">科技源数据缺失</div>'
    )
    head = (
        '<div class="sec-head"><h2><span class="dot"></span>热点雷达</h2>'
        f'<span class="count">{len(social) + len(tech)}</span></div>'
    )
    body = (
        '<div class="radar">'
        '<div class="subhead"><span>热搜榜</span><span class="ln"></span>'
        f'<span class="dim-note">百度热搜</span></div>{social_html}'
        '<div class="subhead"><span>科技前沿</span><span class="ln"></span>'
        f'<span class="dim-note">HN / arXiv</span></div>{tech_html}'
        "</div>"
    )
    return f'<section class="sec" style="grid-area:radar" data-testid="hot-radar">{head}{body}</section>'


# --------------------------------------------------------------------------- #
# section: A-share
# --------------------------------------------------------------------------- #
def _render_ashare(ash) -> str:
    ash = _dct(ash)
    indices = [_dct(x) for x in _lst(ash.get("indices"))]
    breadth = _dct(ash.get("breadth"))
    sectors = _dct(ash.get("sectors"))

    # indices
    idx_cards = []
    for it in indices:
        pct = _num(it.get("change_pct"))
        tone = _tone(pct)
        chg = _num(it.get("change"))
        idx_cards.append(
            f'<div class="idx chartable" data-idx="{_e(it.get("code") or "")}" '
            f'data-label="{_e(it.get("name") or "")}" title="点击查看分时 / 日线">'
            f'<div class="nm"><span>{_e(it.get("name") or "—")}</span>'
            f'<span class="cd">{_e(it.get("code") or "")}</span></div>'
            f'<div class="px {tone}" data-f="price">{_fmt_price(it.get("price"))}</div>'
            f'<div class="ch {tone}"><span data-f="change_pct">{_fmt_pct(pct)}</span>'
            f'<span data-f="change">{_fmt_signed(chg)}</span></div>'
            f'<div class="amt">成交 <span data-f="amount">{_amt(it.get("amount"))}</span></div>'
            "</div>"
        )
    idx_html = (
        f'<div class="idx-grid">{"".join(idx_cards)}</div>'
        if idx_cards else '<div class="empty sm">指数数据缺失</div>'
    )

    # breadth
    total = _num(breadth.get("total"))
    up = _num(breadth.get("up"))
    down = _num(breadth.get("down"))
    flat = _num(breadth.get("flat"))
    lim_up = _num(breadth.get("limit_up"))
    lim_dn = _num(breadth.get("limit_down"))
    ratio = _num(breadth.get("up_ratio"))
    if ratio is None and total and up is not None:
        ratio = up / total if total else None
    if total is None and None not in (up, down, flat):
        total = (up or 0) + (down or 0) + (flat or 0)
    if total and total > 0:
        u_pct = (up or 0) / total * 100.0
        d_pct = (down or 0) / total * 100.0
        f_pct = max(0.0, 100.0 - u_pct - d_pct)
        bar = (
            '<div class="bw"><div class="bw-bar">'
            f'<i class="u" style="width:{u_pct:.2f}%"></i>'
            f'<i class="d" style="width:{d_pct:.2f}%"></i>'
            f'<i class="f" style="width:{f_pct:.2f}%"></i></div>'
            '<div class="bw-legend">'
            f'<span class="u">上涨 <b>{int(up or 0)}</b></span>'
            f'<span class="d">下跌 <b>{int(down or 0)}</b></span>'
            f'<span class="f">平盘 <b>{int(flat or 0)}</b></span>'
            f'<span>上涨占比 <b class="{_tone((ratio or 0) - 0.5)}">'
            f'{_fmt_ratio(ratio)}</b></span>'
            f'<span>合计 <b>{int(total)}</b></span>'
            "</div>"
            '<div class="bw-pills">'
            f'<span class="bw-pill zt">涨停 {int(lim_up) if lim_up is not None else "—"}</span>'
            f'<span class="bw-pill dt">跌停 {int(lim_dn) if lim_dn is not None else "—"}</span>'
            "</div></div>"
        )
    else:
        bar = '<div class="bw"><div class="empty sm">今日数据缺失 · 涨跌宽度不可用</div></div>'

    # sectors
    def sector_block(title, rows, tone_force=None):
        rows = [_dct(r) for r in rows]
        if not rows:
            return f'<div class="sect"><h4>{_e(title)}<span class="ln"></span></h4><div class="empty sm">板块数据缺失</div></div>'
        lines = []
        for i, row in enumerate(rows[:8], 1):
            pct = _num(row.get("change_pct"))
            tone = tone_force or _tone(pct)
            leader = _clean(row.get("leader_name"), 16)
            leader_pct = _fmt_pct(row.get("leader_change_pct"))
            ld = f'<span class="ld">{_e(leader)} {_e(leader_pct)}</span>' if leader else ""
            lines.append(
                f'<div class="row"><span class="i">{i}</span>'
                f'<span class="nm">{_e(row.get("name") or "—")}{ld}</span>'
                f'<span class="ch {tone}">{_fmt_pct(pct)}</span></div>'
            )
        return f'<div class="sect"><h4>{_e(title)}<span class="ln"></span></h4>{"".join(lines)}</div>'

    sectors_html = (
        '<div class="sector-cols">'
        + sector_block("板块涨幅榜", _lst(sectors.get("top")))
        + sector_block("板块跌幅榜", _lst(sectors.get("bottom")))
        + "</div>"
    )

    # limit-up pool (bonus, only when data present)
    pool = [_dct(x) for x in _lst(ash.get("limit_up_pool"))]
    pool_html = ""
    if pool:
        chips = []
        for row in pool[:10]:
            reason = _clean(row.get("reason"), 12)
            chips.append(
                f'<span class="pi">{_e(row.get("name") or row.get("code") or "—")}'
                f'<b>{_fmt_pct(row.get("change_pct"))}</b>'
                f'<span class="rs">{_e(reason)}</span></span>'
            )
        pool_html = f'<div class="pool"><span class="tt">涨停池</span>{"".join(chips)}</div>'

    head = (
        '<div class="sec-head"><h2><span class="dot"></span>A股大盘</h2>'
        f'<span class="count">{len(indices)} 指数</span><span class="spacer"></span>'
        '<span class="hint">红涨绿跌 · 市场宽度与板块轮动 · 点击指数卡看图</span></div>'
    )
    body = idx_html + bar + sectors_html + pool_html
    return f'<section class="sec" style="grid-area:ashare" data-testid="ashare-index">{head}{body}</section>'


# --------------------------------------------------------------------------- #
# section: watchlist
# --------------------------------------------------------------------------- #
def _stat_pair(label, value_html, field=None) -> str:
    attr = f' data-f="{_e(field)}"' if field else ""
    return f'<div class="st"><span>{_e(label)}</span><b{attr}>{value_html}</b></div>'


def _render_watchlist(ash) -> str:
    ash = _dct(ash)
    wl = [_dct(x) for x in _lst(ash.get("watchlist"))]
    meta = _dct(ash.get("watchlist_meta"))

    cards = []
    for idx, stock in enumerate(wl):
        code = str(stock.get("code") or "")
        name = str(stock.get("name") or code or "—")
        quote = _dct(stock.get("quote"))
        pct = _num(quote.get("change_pct"))
        price = _num(quote.get("price"))
        tone = _tone(pct)
        tags = "".join(f'<span class="tg">{_e(_clean(t, 12))}</span>' for t in _lst(stock.get("tags"))[:3] if _clean(t, 12))

        head = (
            '<div class="wl-head">'
            '<div class="who">'
            f'<div class="nm">{_e(name)}</div>'
            f'<div class="cd">{_e(code)}<span class="mk">{_e(stock.get("market") or "")}</span>{tags}</div>'
            "</div>"
            '<div class="px">'
            f'<div class="p {tone}" data-f="price">{_fmt_price(price)}</div>'
            f'<div class="c {tone}"><span data-f="change_pct">{_fmt_pct(pct)}</span> '
            f'<span data-f="change">{_fmt_signed(quote.get("change"))}</span></div>'
            "</div></div>"
        )

        cur_suffix = " USD" if str(stock.get("currency") or "").upper() == "USD" else ""
        stats = [
            ("今开", _fmt_price(quote.get("open")), "open"),
            ("最高", _fmt_price(quote.get("high")), "high"),
            ("最低", _fmt_price(quote.get("low")), "low"),
            ("昨收", _fmt_price(quote.get("prev_close")), "prev_close"),
            ("换手", f'{_fmt_num(quote.get("turnover"), 2)}%' if _num(quote.get("turnover")) is not None else "—", "turnover"),
            ("量比", _fmt_num(quote.get("volume_ratio"), 2), "volume_ratio"),
            ("PE", _fmt_num(quote.get("pe"), 2), "pe"),
            ("PB", _fmt_num(quote.get("pb"), 2), "pb"),
            ("成交额", _amt(quote.get("amount")) + (cur_suffix if _num(quote.get("amount")) is not None else ""), "amount"),
            ("总市值", _amt(quote.get("market_cap")) + (cur_suffix if _num(quote.get("market_cap")) is not None else ""), "market_cap"),
            ("流通市值", _amt(quote.get("float_cap")) + (cur_suffix if _num(quote.get("float_cap")) is not None else ""), "float_cap"),
            ("成交量", _amt(quote.get("volume")), "volume"),
        ]
        stats_html = '<div class="wl-stats">' + "".join(_stat_pair(k, v, f) for k, v, f in stats) + "</div>"

        kline_html = _svg_kline(code, stock.get("kline"), quote.get("prev_close"))
        flow_html = _fund_flow(stock.get("fund_flow"), stock.get("market"))

        def acc(title, items, kind):
            items = [_dct(x) for x in items]
            body = []
            for it in items:
                url = _safe_url(it.get("url"))
                if kind == "ann":
                    t = _clean(it.get("title"), 80) or "（无标题）"
                    meta_bits = [_fmt_day(it.get("date")), _clean(it.get("type"), 16)]
                elif kind == "news":
                    t = _clean(it.get("title"), 80) or "（无标题）"
                    meta_bits = [_fmt_time(it.get("date")), _clean(it.get("source"), 16)]
                else:
                    t = _clean(it.get("title"), 80) or "（无标题）"
                    meta_bits = [_clean(it.get("org"), 16), _fmt_day(it.get("date"))]
                meta_txt = " · ".join([m for m in meta_bits if m])
                body.append(
                    f'<a class="it" href="{_e(url)}" target="_blank" rel="noopener noreferrer">' 
                    f'<span class="d">{_e(meta_txt)}</span>{_e(t)}</a>'
                    if url else
                    f'<span class="it"><span class="d">{_e(meta_txt)}</span>{_e(t)}</span>'
                )
            inner = "".join(body) if body else '<span class="none">暂无数据</span>'
            return (
                f'<details class="acc"><summary><span class="arw">▸</span>{_e(title)}'
                f'<span class="cnt2">{len(items)}</span></summary>'
                f'<div class="bd">{inner}</div></details>'
            )

        accs = (
            '<div class="acc-list">'
            + acc("公告", stock.get("announcements"), "ann")
            + acc("新闻", stock.get("news"), "news")
            + acc("研报", stock.get("reports"), "report")
            + "</div>"
        )

        wl_alerts = [_dct(x) for x in _lst(stock.get("alerts"))]
        alerts_html = ""
        if wl_alerts:
            rows = []
            for a in wl_alerts[:3]:
                level = str(a.get("level") or "info")
                cls = "high" if level == "high" else ("warn" if level in ("warn", "warning") else "info")
                rows.append(
                    f'<div class="wl-alert {cls}"><span class="bd2"></span>'
                    f'<span>{_e(_clean(a.get("text"), 90))}</span></div>'
                )
            alerts_html = '<div class="wl-alerts">' + "".join(rows) + "</div>"

        amount = _num(quote.get("amount"))
        cards.append(
            f'<article class="wl {tone} chartable" data-code="{_e(code)}" '
            f'data-label="{_e(name)}" title="点击查看分时 / 日线" '
            f'data-pct="{"" if pct is None else f"{pct:.4f}"}" '
            f'data-amount="{"" if amount is None else f"{amount:.0f}"}">'
            f"{head}{stats_html}{kline_html}{flow_html}{alerts_html}{accs}</article>"
        )

    if not cards:
        cards_html = '<div class="empty">今日数据缺失 · 自选股行情未采集到</div>'
    else:
        cards_html = f'<div class="wl-grid" id="wl-grid">{"".join(cards)}</div>'

    meta_bits = [
        f'<span class="count">{int(_num(meta.get("count")) or len(wl))} 只</span>',
    ]
    avg = _num(meta.get("avg_change_pct"))
    if avg is not None:
        meta_bits.append(f'<span class="src-pill {_tone(avg)}">平均 {_fmt_pct(avg)}</span>')
    total_amt = _num(meta.get("total_amount"))
    if total_amt is not None:
        meta_bits.append(f'<span class="src-pill">成交 {_amt(total_amt)}</span>')

    head = (
        '<div class="sec-head"><h2><span class="dot"></span>自选股</h2>'
        + "".join(meta_bits)
        + '<span class="spacer"></span>'
        '<div class="wl-tools"><span class="dim-note">点击卡片看分时 / 日线</span>'
        '<span class="dim-note">排序</span>'
        '<button type="button" class="wl-sort on" data-wl-sort="pct">涨跌幅</button>'
        '<button type="button" class="wl-sort" data-wl-sort="amount">成交额</button>'
        '<button type="button" class="wl-sort" data-wl-sort="code">代码</button>'
        "</div></div>"
    )
    return (
        '<section class="sec" style="grid-area:watch" data-testid="watchlist">'
        f"{head}{cards_html}</section>"
    )


# --------------------------------------------------------------------------- #
# section: alerts
# --------------------------------------------------------------------------- #
def _render_alerts(data) -> str:
    data = _dct(data)
    alerts = [_dct(x) for x in _lst(data.get("alerts"))]
    if not alerts:
        body = '<div class="empty">今日无异动提醒</div>'
    else:
        order = {"high": 0, "warn": 1, "warning": 1, "info": 2}
        alerts.sort(key=lambda a: order.get(str(a.get("level") or "info"), 3))
        rows = []
        for a in alerts[:24]:
            level = str(a.get("level") or "info")
            cls = "high" if level == "high" else ("warn" if level in ("warn", "warning") else "info")
            label = {"high": "高", "warn": "警", "warning": "警", "info": "讯"}.get(level, "讯")
            when = _fmt_time(a.get("when"), str(data.get("date") or ""))
            ref = _clean(a.get("ref"), 12)
            text = _clean(a.get("text"), 120) or "—"
            body_text = f"{text}"
            if ref:
                body_text += f' <span class="dim-note mono">[{_e(ref)}]</span>'
            rows.append(
                f'<div class="alert {cls}"><span class="lv">{label}</span>'
                f'<span class="tx">{body_text}</span>'
                f'<span class="tm">{_e(when)}</span></div>'
            )
        body = f'<div class="alert-list">{"".join(rows)}</div>'
    head = (
        '<div class="sec-head"><h2><span class="dot"></span>异动提醒</h2>'
        f'<span class="count">{len(alerts)}</span></div>'
    )
    return f'<section class="sec" style="grid-area:alerts" data-testid="alerts">{head}{body}</section>'


# --------------------------------------------------------------------------- #
# section: data source status
# --------------------------------------------------------------------------- #
def _render_sources(data) -> str:
    data = _dct(data)
    world, ash = _dct(data.get("world")), _dct(data.get("ashare"))
    rows = []
    for s in _lst(world.get("sources")) + _lst(ash.get("sources")):
        s = _dct(s)
        rows.append({
            "name": _clean(s.get("name") or s.get("id"), 40) or "—",
            "ok": s.get("ok"),
            "count": s.get("count"),
            "elapsed": s.get("elapsed_ms"),
            "error": _clean(s.get("error"), 120),
        })
    errors = [_dct(x) for x in _lst(data.get("errors"))]
    ok_n = sum(1 for r in rows if r["ok"] is True)
    bad_n = sum(1 for r in rows if r["ok"] is False)
    rows.sort(key=lambda r: (0 if r["ok"] is False else 1))

    if rows:
        trs = []
        for r in rows:
            ok = r["ok"] is True
            bad = r["ok"] is False
            cls = "bad" if bad else ""
            status = '<span class="st ok">成功</span>' if ok else ('<span class="st no">失败</span>' if bad else '<span class="st">—</span>')
            count = r["count"]
            count_s = str(int(_num(count))) if _num(count) is not None else "—"
            elapsed = _num(r["elapsed"])
            elapsed_s = f"{elapsed:.0f}ms" if elapsed is not None else "—"
            err = r["error"] or ("—" if not bad else "未知错误")
            trs.append(
                f'<tr class="{cls}"><td>{_e(r["name"])}</td><td>{status}</td>'
                f'<td class="mono">{_e(count_s)}</td><td class="mono">{_e(elapsed_s)}</td>'
                f'<td class="err">{_e(err)}</td></tr>'
            )
        table = (
            '<div class="tscroll"><table class="src"><thead><tr>'
            "<th>数据源</th><th>状态</th><th>条数</th><th>耗时</th><th>备注</th>"
            f'</tr></thead><tbody>{"".join(trs)}</tbody></table></div>'
        )
        summary = (
            '<div class="src-summary">'
            f'<span class="src-pill ok">成功 {ok_n}</span>'
            f'<span class="src-pill bad">失败 {bad_n}</span>'
            f'<span class="src-pill">共 {len(rows)} 个源</span></div>'
        )
    else:
        summary = '<div class="empty sm">数据源状态缺失</div>'
        table = ""

    err_html = ""
    if errors:
        lines = []
        for e in errors[:12]:
            src = _clean(e.get("source"), 30) or "—"
            stage = _clean(e.get("stage"), 16)
            msg = _clean(e.get("error"), 160) or "—"
            lines.append(
                f'<div class="ei"><b>{_e(src)}</b>'
                + (f" · {_e(stage)}" if stage else "")
                + f" · {_e(msg)}</div>"
            )
        err_html = (
            f'<div class="err-box"><h4>采集错误 · {len(errors)} 条</h4>{"".join(lines)}</div>'
        )

    head = (
        '<div class="sec-head"><h2><span class="dot"></span>数据源状态</h2>'
        f'<span class="count">{len(rows)} 源</span><span class="spacer"></span>'
        f'<span class="hint">{ok_n} 成功 · <span class="up">{bad_n} 失败</span></span></div>'
    )
    return (
        '<section class="sec" style="grid-area:sources" data-testid="sources-status">'
        f"{head}{summary}{table}{err_html}</section>"
    )


# --------------------------------------------------------------------------- #
# page assembly
# --------------------------------------------------------------------------- #
_CLIENT_NEWS_KEYS = ("title", "summary", "url", "source", "published", "category", "tags", "heat")


def _round_list(values, digits: int, as_int: bool = False) -> list:
    out = []
    for v in _lst(values):
        f = _num(v)
        if f is None:
            out.append(None)
        elif as_int:
            out.append(int(round(f)))
        else:
            out.append(round(f, digits))
    return out


#: 客户端分时点数上限（数据侧已下采样到 ≤500；这里再限制一次，
#: 36 个标的 × 分时数组是单文件体积的最大变量，320 点足够 1080px 画布绘制）
_CLIENT_MIN_POINTS = 320


def _client_charts(charts) -> dict:
    """图表段（CONTRACT §12.1）内联给浏览器做 canvas 绘图。

    数值按 decimals 取整（小数位 + 2 位冗余，最多 4 位）控制体积；分时点数超过
    ``_CLIENT_MIN_POINTS`` 时按等步长二次下采样（保留首尾）。其余字段原样透传。
    key 规则：stock:<code> / index:<code> / world:<f12>。
    """
    charts = _dct(charts)
    items = {}
    for key, raw in _dct(charts.get("items")).items():
        raw = _dct(raw)
        if not raw:
            continue
        dec = _num(raw.get("decimals"))
        dec = 2 if dec is None else max(0, min(6, int(dec)))
        digits = max(2, min(4, dec + 2))

        min_raw = _dct(raw.get("min"))
        min_obj = None
        if min_raw:
            times = [str(t) for t in _lst(min_raw.get("times"))]
            price = _round_list(min_raw.get("price"), digits)
            avg_raw = min_raw.get("avg")
            avg = None if avg_raw is None else _round_list(avg_raw, digits)
            vol = _round_list(min_raw.get("volume"), 0, as_int=True)
            n = min(len(times), len(price))
            if n > _CLIENT_MIN_POINTS:
                idx = sorted({int(round(i * (n - 1) / (_CLIENT_MIN_POINTS - 1)))
                              for i in range(_CLIENT_MIN_POINTS)})
                times = [times[i] for i in idx]
                price = [price[i] for i in idx]
                if avg is not None:
                    avg = [avg[i] if i < len(avg) else None for i in idx]
                vol = [vol[i] if i < len(vol) else None for i in idx]
            prev = _num(min_raw.get("prev_close"))
            min_obj = {
                "date": min_raw.get("date"),
                "prev_close": None if prev is None else round(prev, digits),
                "times": times,
                "price": price,
                "avg": avg,
                "volume": vol,
            }

        day_raw = _dct(raw.get("day"))
        day_obj = None
        if day_raw:
            day_obj = {
                "times": [str(t) for t in _lst(day_raw.get("times"))],
                "open": _round_list(day_raw.get("open"), digits),
                "close": _round_list(day_raw.get("close"), digits),
                "high": _round_list(day_raw.get("high"), digits),
                "low": _round_list(day_raw.get("low"), digits),
                "volume": _round_list(day_raw.get("volume"), 0, as_int=True),
            }

        items[str(key)] = {
            "name": raw.get("name"),
            "kind": raw.get("kind"),
            "market": raw.get("market"),
            "currency": raw.get("currency"),
            "decimals": dec,
            "min": min_obj,
            "day": day_obj,
        }
    return {"generated_at": charts.get("generated_at"), "items": items}


def _client_payload(data) -> dict:
    """内联给浏览器的数据子集（结构完整，剔除只用于服务端渲染的重字段）。

    客户端只需要全量 ``world.news`` 做搜索/筛选/加载更多；自选股的 K 线、公告、
    新闻、研报已在服务端渲染成 DOM，内联 JSON 再放一份只会让单文件无谓变大，
    因此这里裁剪成 行情/标签/异动 摘要。归档页与 report 不依赖本 payload。
    """
    d = _dct(data)
    world = _dct(d.get("world"))
    ash = _dct(d.get("ashare"))

    news = []
    for it in _lst(world.get("news")):
        it = _dct(it)
        news.append({key: it.get(key) for key in _CLIENT_NEWS_KEYS})

    watchlist = []
    for stock in _lst(ash.get("watchlist")):
        stock = _dct(stock)
        watchlist.append({
            "code": stock.get("code"),
            "name": stock.get("name"),
            "market": stock.get("market"),
            "tags": _lst(stock.get("tags")),
            "note": stock.get("note"),
            "quote": _dct(stock.get("quote")),
            "alerts": _lst(stock.get("alerts")),
        })

    return {
        "schema_version": d.get("schema_version"),
        "date": d.get("date"),
        "generated_at": d.get("generated_at"),
        "timezone": d.get("timezone"),
        "run": _dct(d.get("run")),
        "world": {
            "news": news,
            "by_category": _lst(world.get("by_category")),
            "markets": _lst(world.get("markets")),
            "social": _lst(world.get("social")),
            "sources": _lst(world.get("sources")),
        },
        "ashare": {
            "indices": _lst(ash.get("indices")),
            "breadth": _dct(ash.get("breadth")),
            "sectors": _dct(ash.get("sectors")),
            "watchlist": watchlist,
            "watchlist_meta": _dct(ash.get("watchlist_meta")),
            "sources": _lst(ash.get("sources")),
        },
        "alerts": _lst(d.get("alerts")),
        "errors": _lst(d.get("errors")),
        "charts": _client_charts(d.get("charts")),
    }


def _data_script(data, title: str, dates, archive_href: str) -> str:
    payload_obj = _client_payload(data)
    payload = json.dumps(payload_obj, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    meta = {
        "title": title,
        "dates": [str(x) for x in _lst(dates)],
        "archive": archive_href or "",
        "version": VERSION,
        "newsInitial": NEWS_INITIAL,
        "newsTotal": len(_lst(_dct(_dct(data).get("world")).get("news"))),
    }
    meta_json = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
    return (
        "<script>window.__INTEL__ = " + payload + ";\n"
        "window.__INTEL_META__ = " + meta_json + ";</script>"
    )


def _render_footer(data) -> str:
    d = _dct(data)
    date_s = _e(d.get("date") or "—")
    gen_s = _e(_fmt_gen(d.get("generated_at")))
    return (
        '<footer class="foot">'
        f'<div>每日情报雷达 · 单文件离线看板 <span class="mono">v{VERSION}</span> · '
        "零外部依赖 · 双击即可打开</div>"
        f'<div class="r"><span>数据日期 <b>{date_s}</b></span><span>生成于 {gen_s}</span>'
        f'<span>schema v{_e(d.get("schema_version") if d.get("schema_version") is not None else 1)}</span>'
        "</div></footer>"
    )


# --------------------------------------------------------------------------- #
# live quotes overlay (optional): polls /intel/data/live.json written by
# src/live_service.py.  Disabled automatically on file:// pages.
# --------------------------------------------------------------------------- #
_LIVE_CSS = r"""
.live-badge{position:fixed;right:14px;bottom:14px;z-index:90;font-family:var(--mono);font-size:11px;
  letter-spacing:.3px;padding:6px 11px;border-radius:999px;border:1px solid var(--line2);
  background:rgba(8,14,26,.94);color:var(--muted);box-shadow:0 8px 26px -12px #000;cursor:default;
  transition:color .2s,background .2s}
.live-badge[data-state="live"]{color:#04141b;background:linear-gradient(135deg,var(--cyan),#9bf6ff);
  border-color:transparent;font-weight:650}
.live-badge[data-state="warn"]{color:#2a1a05;background:linear-gradient(135deg,var(--amber),#ffe0a1);
  border-color:transparent;font-weight:650}
.live-badge[data-state="off"]{opacity:.85}
@media (max-width:820px){.live-badge{right:10px;bottom:10px;font-size:10px;padding:5px 9px}}
"""

_LIVE_JS = r"""
/* 实时行情：轮询 /intel/data/live.json，只更新自选股/指数卡片（需通过服务模式打开） */
(function () {
  var LIVE_URL = "/intel/data/live.json";
  var badge = document.createElement("div");
  badge.className = "live-badge";
  badge.setAttribute("data-testid", "live-badge");
  badge.textContent = "○ 静态快照（实时需本机服务）";
  document.body.appendChild(badge);

  function setBadge(state, text, title) {
    badge.setAttribute("data-state", state);
    badge.textContent = text;
    if (title) badge.title = title;
  }
  if (location.protocol === "file:") {
    setBadge("off", "○ 离线快照 · 实时需手机版/服务模式");
    return;
  }
  function toneOf(v) { return v > 0 ? "up" : (v < 0 ? "down" : "flat"); }
  function fillFields(root, display) {
    var n = 0;
    Object.keys(display || {}).forEach(function (f) {
      var els = root.querySelectorAll('[data-f="' + f + '"]');
      for (var i = 0; i < els.length; i++) { els[i].textContent = display[f]; n++; }
    });
    return n;
  }
  function applyCard(code, info) {
    var card = document.querySelector('[data-code="' + code + '"]');
    if (!card || !info) return 0;
    var tone = info.tone || "flat";
    var n = fillFields(card, info.display);
    card.className = "wl " + tone;
    var p = card.querySelector(".p"); if (p) p.className = "p " + tone;
    var c = card.querySelector(".c"); if (c) c.className = "c " + tone;
    return n;
  }
  function applyIdx(code, info) {
    var el = document.querySelector('[data-idx="' + code + '"]');
    if (!el || !info) return 0;
    var tone = info.tone || "flat";
    var n = fillFields(el, info.display);
    var px = el.querySelector(".px"); if (px) px.className = "px " + tone;
    var ch = el.querySelector(".ch"); if (ch) ch.className = "ch " + tone;
    return n;
  }
  function hhmmss(iso) {
    try {
      var d = new Date(iso);
      return ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2) + ":" + ("0" + d.getSeconds()).slice(-2);
    } catch (e) { return ""; }
  }
  function poll() {
    var req = new XMLHttpRequest();
    req.open("GET", LIVE_URL + "?t=" + Date.now(), true);
    req.timeout = 12000;
    req.onload = function () {
      if (req.status !== 200) { setBadge("off", "○ 静态快照（实时需本机服务）"); return; }
      var payload;
      try { payload = JSON.parse(req.responseText); } catch (e) { setBadge("off", "○ 实时数据解析失败"); return; }
      var n = 0, q = payload.quotes || {};
      Object.keys(q).forEach(function (code) { n += applyCard(code, q[code]); });
      (payload.indices || []).forEach(function (it) { n += applyIdx(it.code, it); });
      var errs = (payload.errors || []).length;
      setBadge(errs > 0 ? "warn" : "live",
        "● 实时 " + hhmmss(payload.generated_at) + (errs > 0 ? " · " + errs + " 项延迟" : " · 已更新"),
        "每 30 秒轮询 · 本次刷新 " + n + " 个字段");
    };
    req.onerror = req.ontimeout = function () { setBadge("off", "○ 静态快照（实时需本机服务）"); };
    req.send();
  }
  poll();
  setInterval(poll, 30000);
})();
"""


# --------------------------------------------------------------------------- #
# chart modal (CONTRACT §12.3): pure canvas intraday / daily charts
# --------------------------------------------------------------------------- #
_CHART_CSS = r"""
/* ---------- chart modal ---------- */
.chartable{cursor:pointer}
.mkt.chartable:hover,.idx.chartable:hover,.wl.chartable:hover{border-color:rgba(77,240,255,.4);box-shadow:0 0 0 1px rgba(77,240,255,.13) inset,0 0 26px -14px rgba(77,240,255,.55)}
.chart-modal{position:fixed;inset:0;z-index:200;display:flex;align-items:center;justify-content:center;padding:22px}
.chart-modal.hidden{display:none}
.chart-backdrop{position:absolute;inset:0;background:rgba(3,6,12,.8);-webkit-backdrop-filter:blur(7px);backdrop-filter:blur(7px)}
.chart-panel{position:relative;z-index:1;width:min(1080px,100%);max-height:92vh;display:flex;flex-direction:column;gap:9px;padding:15px 17px 13px;background:linear-gradient(178deg,rgba(16,26,44,.99),rgba(8,13,23,.995));border:1px solid var(--line2);border-radius:16px;box-shadow:0 42px 90px -30px #000,0 0 70px -28px rgba(77,240,255,.4);overflow:hidden}
.chart-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap;min-width:0}
.chart-title-wrap{min-width:0;display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.chart-title{font-size:16px;font-weight:650;letter-spacing:.02em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}
.chart-meta{font-family:var(--mono);font-size:10.5px;color:var(--dim);white-space:nowrap}
.chart-tabs{margin-left:auto;display:flex;gap:2px;padding:2px;background:rgba(255,255,255,.03);border:1px solid var(--line);border-radius:9px}
.chart-tab{cursor:pointer;font:inherit;font-family:var(--mono);font-size:11px;color:var(--muted);background:transparent;border:0;border-radius:7px;padding:5px 13px;transition:.15s;white-space:nowrap}
.chart-tab:hover{color:var(--txt)}
.chart-tab.on{color:#04141b;background:linear-gradient(135deg,var(--cyan),#9bf6ff);font-weight:650;box-shadow:0 0 14px rgba(77,240,255,.25)}
.chart-tab:disabled{opacity:.34;cursor:not-allowed;box-shadow:none;background:transparent;color:var(--dim);font-weight:500}
.chart-close{cursor:pointer;font:inherit;font-size:13px;line-height:1;color:var(--muted);background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:8px;padding:6px 9px;transition:.15s}
.chart-close:hover{color:#fff;border-color:rgba(255,77,109,.5);background:rgba(255,77,109,.12)}
.chart-hint{display:none;font-family:var(--mono);font-size:11px;color:var(--amber);line-height:1.4}
.chart-hint.show{display:block}
.chart-canvas-wrap{position:relative;flex:0 0 auto;height:clamp(280px,52vh,460px);min-height:240px;background:rgba(4,8,16,.55);border:1px solid rgba(120,160,220,.12);border-radius:10px;overflow:hidden}
#chart-canvas{display:block;width:100%;height:100%;touch-action:none;cursor:crosshair}
.chart-tip{position:absolute;left:0;top:0;z-index:2;pointer-events:none;opacity:0;transition:opacity .1s;max-width:min(340px,94%);background:rgba(8,14,24,.96);border:1px solid rgba(120,190,255,.26);border-radius:8px;padding:6px 9px;font-family:var(--mono);font-size:10.5px;line-height:1.55;color:#cfe0f6;white-space:nowrap;box-shadow:0 10px 24px -10px #000}
.chart-tip.show{opacity:1}
.chart-tip b{color:#fff;font-weight:650}
.chart-tip .up{color:var(--up)}
.chart-tip .down{color:var(--down)}
.chart-tip .tt{color:var(--dim)}
.chart-legend{display:flex;gap:13px;flex-wrap:wrap;font-family:var(--mono);font-size:10.5px;color:var(--muted);min-height:14px}
.chart-legend .k{display:inline-flex;align-items:center;gap:5px;white-space:nowrap}
.chart-legend .k i{width:11px;height:2px;border-radius:2px;display:inline-block;flex:0 0 auto}
body.chart-open{overflow:hidden}
@media (max-width:767px){
  .chart-modal{padding:8px}
  .chart-panel{padding:11px 11px 9px;gap:7px;max-height:96vh;border-radius:13px}
  .chart-title{font-size:14px}
  .chart-meta{display:none}
  .chart-tabs{order:4;margin-left:0;width:100%;justify-content:center}
  .chart-close{margin-left:auto}
  .chart-tab{flex:1 1 auto;text-align:center}
  .chart-canvas-wrap{height:clamp(230px,40vh,330px);min-height:220px}
  .chart-legend{gap:9px;font-size:10px}
}
"""


_CHART_JS = r"""
/* 行情图表弹层：纯 canvas 分时 / 日线（CONTRACT §12.3） */
(function () {
  "use strict";

  var DATA = (window.__INTEL__ && typeof window.__INTEL__ === "object") ? window.__INTEL__ : {};
  var CHARTS = (DATA.charts && DATA.charts.items && typeof DATA.charts.items === "object") ? DATA.charts.items : {};
  var modal = document.getElementById("chart-modal");
  var canvas = document.getElementById("chart-canvas");
  if (!modal || !canvas || !canvas.getContext) { return; }
  var ctx = canvas.getContext("2d");
  var wrap = document.getElementById("chart-canvas-wrap");
  var titleEl = document.getElementById("chart-title");
  var metaEl = document.getElementById("chart-meta");
  var hintEl = document.getElementById("chart-hint");
  var legendEl = document.getElementById("chart-legend");
  var tipEl = document.getElementById("chart-tip");
  var tabMin = document.getElementById("chart-tab-min");
  var tabDay = document.getElementById("chart-tab-day");
  var closeBtn = document.getElementById("chart-close");
  var backdrop = document.getElementById("chart-backdrop");

  var MONO = 'ui-monospace,"Cascadia Mono","Cascadia Code",Consolas,monospace';
  var C = {
    up: "#ff4d6d", down: "#2ee6a0", price: "#4df0ff", avg: "#ffc44d",
    grid: "rgba(120,160,220,.12)", grid2: "rgba(120,160,220,.23)",
    axis: "#5d708d", prev: "rgba(150,178,215,.55)",
    cross: "rgba(165,195,230,.6)", crossSoft: "rgba(165,195,230,.26)",
    ma: ["#ffc44d", "#4df0ff", "#ff3bd8"]
  };
  var S = { key: "", item: null, tab: "min", open: false, hover: null, dpr: 1, w: 0, h: 0, focus: null, raf: 0, map: null };

  function arrOf(v) { return Object.prototype.toString.call(v) === "[object Array]" ? v : []; }
  function numOf(v) { var f = parseFloat(v); return isFinite(f) ? f : null; }
  function fix(v, d) { return v === null ? "—" : v.toFixed(d); }
  function strOf(v) { return v == null ? "" : String(v); }
  function esc(v) {
    return strOf(v).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function volTxt(v) {
    var f = numOf(v);
    if (f === null) { return "—"; }
    var a = Math.abs(f);
    if (a >= 1e8) { return (f / 1e8).toFixed(2) + "亿"; }
    if (a >= 1e4) { return (f / 1e4).toFixed(1) + "万"; }
    return String(Math.round(f));
  }
  function decOf(item) {
    var d = numOf(item && item.decimals);
    return d === null ? 2 : Math.max(0, Math.min(6, Math.round(d)));
  }
  function hasMin(item) { return !!(item && item.min && arrOf(item.min.price).length); }
  function hasDay(item) { return !!(item && item.day && arrOf(item.day.close).length); }

  function layout() {
    var w = S.w, h = S.h;
    var L = { l: 10, r: 58, t: 12, b: 26 };
    var bottom = h - L.b;
    var volH = Math.max(28, Math.round((bottom - L.t) * 0.20));
    var gap = 12;
    var priceBot = bottom - volH - gap;
    L.plotW = Math.max(40, w - L.l - L.r);
    L.priceTop = L.t;
    L.priceBot = priceBot;
    L.volTop = priceBot + gap;
    L.volBot = bottom;
    L.right = L.l + L.plotW;
    L.width = w;
    return L;
  }

  function resizeCanvas() {
    var rect = wrap.getBoundingClientRect();
    var w = Math.max(240, Math.round(rect.width));
    var h = Math.max(200, Math.round(rect.height));
    var dpr = window.devicePixelRatio || 1;
    S.w = w; S.h = h; S.dpr = dpr;
    var pw = Math.round(w * dpr);
    var ph = Math.round(h * dpr);
    if (canvas.width !== pw) { canvas.width = pw; }
    if (canvas.height !== ph) { canvas.height = ph; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function strokeLine(x1, y1, x2, y2, color, width, dash) {
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = width || 1;
    ctx.setLineDash(dash || []);
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
    ctx.restore();
  }

  function roundRect(x, y, w, h, r) {
    var rr = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + rr, y);
    ctx.lineTo(x + w - rr, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + rr);
    ctx.lineTo(x + w, y + h - rr);
    ctx.quadraticCurveTo(x + w, y + h, x + w - rr, y + h);
    ctx.lineTo(x + rr, y + h);
    ctx.quadraticCurveTo(x, y + h, x, y + h - rr);
    ctx.lineTo(x, y + rr);
    ctx.quadraticCurveTo(x, y, x + rr, y);
    ctx.closePath();
  }

  function drawTag(text, x, y, align) {
    ctx.save();
    ctx.font = "10px " + MONO;
    var tw = ctx.measureText(text).width;
    var padX = 5;
    var hgt = 16;
    var bx;
    if (align === "left") { bx = x; }
    else if (align === "right") { bx = x - tw - padX * 2; }
    else { bx = x - (tw + padX * 2) / 2; }
    ctx.fillStyle = "rgba(22,35,58,.96)";
    ctx.strokeStyle = "rgba(120,190,255,.42)";
    ctx.lineWidth = 1;
    roundRect(bx, y - hgt / 2, tw + padX * 2, hgt, 3);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = "#dbe8fb";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(text, bx + padX, y);
    ctx.restore();
  }

  function drawGrid(L, lo, hi, dec) {
    var k, f, y, x;
    ctx.save();
    ctx.font = "10px " + MONO;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    for (k = 0; k <= 4; k++) {
      f = k / 4;
      y = L.priceBot - (L.priceBot - L.priceTop) * f;
      strokeLine(L.l, y, L.right, y, (k === 0 || k === 4) ? C.grid2 : C.grid, 1);
      ctx.fillStyle = C.axis;
      ctx.fillText(fix(lo + (hi - lo) * f, dec), L.right + 7, y);
    }
    for (k = 0; k <= 4; k++) {
      x = L.l + L.plotW * (k / 4);
      strokeLine(x, L.priceTop, x, L.volBot, C.grid, 1);
    }
    ctx.restore();
  }

  function labelPositions(L, n, timeAt, count) {
    var out = [], k, idx, x;
    var c = Math.max(2, count);
    for (k = 0; k < c; k++) {
      idx = n <= 1 ? 0 : Math.round((n - 1) * k / (c - 1));
      x = L.l + (n <= 1 ? L.plotW / 2 : L.plotW * idx / (n - 1));
      x = Math.max(L.l + 16, Math.min(L.right - 16, x));
      out.push([x, timeAt(idx)]);
    }
    return out;
  }

  function drawXLabels(L, labels) {
    for (var i = 0; i < labels.length; i++) {
      ctx.save();
      ctx.font = "10px " + MONO;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.fillStyle = C.axis;
      ctx.fillText(strOf(labels[i][1]), labels[i][0], L.volBot + 7);
      ctx.restore();
    }
  }

  function drawPolyline(points, color, width, dash) {
    var i, started = false, p;
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = width || 1.5;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.setLineDash(dash || []);
    ctx.beginPath();
    for (i = 0; i < points.length; i++) {
      p = points[i];
      if (!p) { started = false; continue; }
      if (!started) { ctx.moveTo(p[0], p[1]); started = true; }
      else { ctx.lineTo(p[0], p[1]); }
    }
    ctx.stroke();
    ctx.restore();
  }

  function drawArea(points, topY, botY) {
    var i, j, seg = [];
    function flush() {
      if (seg.length >= 2) {
        ctx.beginPath();
        ctx.moveTo(seg[0][0], botY);
        for (j = 0; j < seg.length; j++) { ctx.lineTo(seg[j][0], seg[j][1]); }
        ctx.lineTo(seg[seg.length - 1][0], botY);
        ctx.closePath();
        var grad = ctx.createLinearGradient(0, topY, 0, botY);
        grad.addColorStop(0, "rgba(77,240,255,.30)");
        grad.addColorStop(0.65, "rgba(77,240,255,.07)");
        grad.addColorStop(1, "rgba(77,240,255,0)");
        ctx.fillStyle = grad;
        ctx.fill();
      }
      seg = [];
    }
    for (i = 0; i < points.length; i++) {
      if (!points[i]) { flush(); continue; }
      seg.push(points[i]);
    }
    flush();
  }

  function drawVolumes(L, bars, maxVol) {
    var n = bars.length;
    var band = L.plotW / Math.max(1, n);
    var bw = Math.max(1, Math.min(7, band * 0.62));
    for (var i = 0; i < n; i++) {
      var b = bars[i];
      if (!b) { continue; }
      var v = numOf(b[1]);
      if (v === null || maxVol <= 0) { continue; }
      var hh = Math.max(1, (v / maxVol) * (L.volBot - L.volTop));
      var x = L.l + band * (i + 0.5);
      ctx.save();
      ctx.fillStyle = b[0];
      ctx.globalAlpha = 0.82;
      ctx.fillRect(x - bw / 2, L.volBot - hh, bw, hh);
      ctx.restore();
    }
  }

  function maSeries(close, period) {
    var out = [], sum = 0, count = 0, i, v, old;
    for (i = 0; i < close.length; i++) {
      v = numOf(close[i]);
      if (v === null) { out.push(null); continue; }
      sum += v;
      count += 1;
      if (count > period) {
        old = numOf(close[i - period]);
        if (old !== null) { sum -= old; }
        count = period;
      }
      out.push(count >= period ? sum / period : null);
    }
    return out;
  }

  function drawEmpty(text) {
    S.map = null;
    ctx.save();
    ctx.fillStyle = C.axis;
    ctx.font = "12px " + MONO;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(text, S.w / 2, S.h / 2);
    ctx.restore();
  }

  function toneCls(v) { return v > 0 ? "up" : (v < 0 ? "down" : ""); }

  function minTip(m, idx, dec) {
    var times = arrOf(m.times), price = arrOf(m.price), avg = arrOf(m.avg), vol = arrOf(m.volume);
    var p = numOf(price[idx]);
    var prev = numOf(m.prev_close);
    var chg = (p !== null && prev !== null && prev !== 0) ? (p - prev) / prev * 100 : null;
    var html = '<span class="tt">' + esc(times[idx]) + "</span>  <b>" + fix(p, dec) + "</b>";
    if (chg !== null) {
      html += '  <span class="' + toneCls(chg) + '">' + (chg >= 0 ? "+" : "") + chg.toFixed(2) + "%</span>";
    }
    var av = numOf(avg[idx]);
    html += '<br><span class="tt">均价</span> ' + (av === null ? "—" : fix(av, dec)) +
      '  <span class="tt">量</span> ' + volTxt(vol[idx]);
    if (prev !== null) { html += '<br><span class="tt">昨收</span> ' + fix(prev, dec); }
    return html;
  }

  function dayTip(d, idx, dec) {
    var times = arrOf(d.times), opens = arrOf(d.open), closes = arrOf(d.close);
    var highs = arrOf(d.high), lows = arrOf(d.low), vols = arrOf(d.volume);
    var o = numOf(opens[idx]), c = numOf(closes[idx]);
    var h = numOf(highs[idx]), l = numOf(lows[idx]);
    var prevC = idx > 0 ? numOf(closes[idx - 1]) : null;
    var chg = (c !== null && prevC !== null && prevC !== 0) ? (c - prevC) / prevC * 100 : null;
    var html = '<span class="tt">' + esc(times[idx]) + "</span>";
    if (chg !== null) {
      html += '  <span class="' + toneCls(chg) + '">' + (chg >= 0 ? "+" : "") + chg.toFixed(2) + "%</span>";
    }
    html += '<br><span class="tt">开</span> ' + fix(o, dec) + '  <span class="tt">高</span> ' + fix(h, dec) +
      '  <span class="tt">低</span> ' + fix(l, dec) + '  <span class="tt">收</span> ' + fix(c, dec);
    html += '<br><span class="tt">量</span> ' + volTxt(vols[idx]);
    return html;
  }

  function drawMin(item) {
    var m = item.min || {};
    var dec = decOf(item);
    var times = arrOf(m.times), price = arrOf(m.price), avg = arrOf(m.avg), vol = arrOf(m.volume);
    var n = Math.min(times.length, price.length);
    if (!n) { drawEmpty("暂无分时数据"); return; }
    var prev = numOf(m.prev_close);
    var lo = null, hi = null, i, v;
    for (i = 0; i < n; i++) {
      v = numOf(price[i]);
      if (v === null) { continue; }
      lo = (lo === null || v < lo) ? v : lo;
      hi = (hi === null || v > hi) ? v : hi;
    }
    var hasAvg = false;
    for (i = 0; i < n; i++) {
      v = numOf(avg[i]);
      if (v === null) { continue; }
      hasAvg = true;
      lo = (lo === null || v < lo) ? v : lo;
      hi = (hi === null || v > hi) ? v : hi;
    }
    if (lo === null) { drawEmpty("暂无分时数据"); return; }
    if (prev !== null) { lo = Math.min(lo, prev); hi = Math.max(hi, prev); }
    var pad = (hi - lo) * 0.07;
    if (!(pad > 0)) { pad = Math.max(Math.abs(hi) * 0.002, 0.01); }
    lo -= pad;
    hi += pad;
    var L = layout();
    var xOf = function (idx) { return L.l + (n <= 1 ? L.plotW / 2 : L.plotW * idx / (n - 1)); };
    var yOf = function (val) { return L.priceBot - (val - lo) / (hi - lo) * (L.priceBot - L.priceTop); };
    drawGrid(L, lo, hi, dec);
    var pPts = [], aPts = [], pv, av;
    for (i = 0; i < n; i++) {
      pv = numOf(price[i]);
      pPts.push(pv === null ? null : [xOf(i), yOf(pv)]);
      av = hasAvg ? numOf(avg[i]) : null;
      aPts.push(av === null ? null : [xOf(i), yOf(av)]);
    }
    drawArea(pPts, L.priceTop, L.priceBot);
    drawPolyline(pPts, C.price, 1.7);
    if (hasAvg) { drawPolyline(aPts, C.avg, 1.2, [4, 3]); }
    if (prev !== null) { strokeLine(L.l, yOf(prev), L.right, yOf(prev), C.prev, 1, [5, 4]); }
    var maxVol = 0, bars = [], vv, base, cur, up;
    for (i = 0; i < n; i++) {
      vv = numOf(vol[i]);
      if (vv !== null && vv > maxVol) { maxVol = vv; }
      base = i > 0 ? numOf(price[i - 1]) : prev;
      cur = numOf(price[i]);
      up = cur !== null && (base === null || cur >= base);
      bars.push([up ? C.up : C.down, vv]);
    }
    drawVolumes(L, bars, maxVol);
    drawXLabels(L, labelPositions(L, n, function (idx) { return String(times[idx] || ""); }, Math.min(5, n)));
    var lastP = null, lastA = null;
    for (i = n - 1; i >= 0; i--) {
      if (lastP === null) { lastP = numOf(price[i]); }
      if (lastA === null && hasAvg) { lastA = numOf(avg[i]); }
      if (lastP !== null && (!hasAvg || lastA !== null)) { break; }
    }
    var legend = [legendItem("价格 " + fix(lastP, dec), C.price),
      legendItem("均价 " + (hasAvg ? fix(lastA, dec) : "—"), C.avg)];
    if (prev !== null) { legend.push(legendItem("昨收 " + fix(prev, dec), C.prev)); }
    setLegend(legend);
    S.map = {
      n: n, left: L.l, right: L.right, top: L.priceTop, bottom: L.volBot,
      priceTop: L.priceTop, priceBot: L.priceBot, dec: dec, xOf: xOf,
      yToVal: function (y) {
        var t = (L.priceBot - y) / (L.priceBot - L.priceTop);
        return lo + t * (hi - lo);
      },
      tip: function (idx) { return minTip(m, idx, dec); }
    };
  }

  function drawDay(item) {
    var d = item.day || {};
    var dec = decOf(item);
    var times = arrOf(d.times), opens = arrOf(d.open), closes = arrOf(d.close);
    var highs = arrOf(d.high), lows = arrOf(d.low), vols = arrOf(d.volume);
    var n = times.length;
    if (!n) { drawEmpty("该市场暂无日线数据"); return; }
    var lo = null, hi = null, i, h, l;
    for (i = 0; i < n; i++) {
      h = numOf(highs[i]);
      l = numOf(lows[i]);
      if (h !== null) { hi = (hi === null || h > hi) ? h : hi; }
      if (l !== null) { lo = (lo === null || l < lo) ? l : lo; }
    }
    if (lo === null || hi === null) { drawEmpty("该市场暂无日线数据"); return; }
    var ma5 = maSeries(closes, 5);
    var ma10 = maSeries(closes, 10);
    var ma20 = maSeries(closes, 20);
    var mas = [ma5, ma10, ma20], mi, mv, j;
    for (mi = 0; mi < mas.length; mi++) {
      for (j = 0; j < n; j++) {
        mv = numOf(mas[mi][j]);
        if (mv === null) { continue; }
        hi = Math.max(hi, mv);
        lo = Math.min(lo, mv);
      }
    }
    var pad = (hi - lo) * 0.06;
    if (!(pad > 0)) { pad = Math.max(Math.abs(hi) * 0.002, 0.01); }
    lo -= pad;
    hi += pad;
    var L = layout();
    var band = L.plotW / n;
    var bw = Math.max(1.2, Math.min(9, band * 0.6));
    var xOf = function (idx) { return L.l + band * (idx + 0.5); };
    var yOf = function (val) { return L.priceBot - (val - lo) / (hi - lo) * (L.priceBot - L.priceTop); };
    drawGrid(L, lo, hi, dec);
    var maxVol = 0, bars = [], i2, o, c, hh, ll, vv, up, col, x, y1, y2;
    for (i2 = 0; i2 < n; i2++) {
      o = numOf(opens[i2]);
      c = numOf(closes[i2]);
      hh = numOf(highs[i2]);
      ll = numOf(lows[i2]);
      vv = numOf(vols[i2]);
      if (vv !== null && vv > maxVol) { maxVol = vv; }
      up = (c !== null && o !== null) ? c >= o : true;
      col = up ? C.up : C.down;
      bars.push([col, vv]);
      if (o === null || c === null || hh === null || ll === null) { continue; }
      x = xOf(i2);
      strokeLine(x, yOf(hh), x, yOf(ll), col, 1);
      y1 = yOf(Math.max(o, c));
      y2 = yOf(Math.min(o, c));
      ctx.save();
      ctx.fillStyle = col;
      ctx.globalAlpha = 0.9;
      ctx.fillRect(x - bw / 2, y1, bw, Math.max(1, y2 - y1));
      ctx.restore();
    }
    drawVolumes(L, bars, maxVol);
    var maPts, maVal;
    for (mi = 0; mi < mas.length; mi++) {
      maPts = [];
      for (i2 = 0; i2 < n; i2++) {
        maVal = numOf(mas[mi][i2]);
        maPts.push(maVal === null ? null : [xOf(i2), yOf(maVal)]);
      }
      drawPolyline(maPts, C.ma[mi], 1.15);
    }
    drawXLabels(L, labelPositions(L, n, function (idx) {
      var t = String(times[idx] || "");
      return t.length > 5 ? t.slice(5) : t;
    }, Math.min(6, n)));
    setLegend([legendItem("MA5", C.ma[0]), legendItem("MA10", C.ma[1]),
      legendItem("MA20", C.ma[2]), legendItem("涨", C.up), legendItem("跌", C.down)]);
    S.map = {
      n: n, left: L.l, right: L.right, top: L.priceTop, bottom: L.volBot,
      priceTop: L.priceTop, priceBot: L.priceBot, dec: dec, xOf: xOf,
      yToVal: function (y) {
        var t = (L.priceBot - y) / (L.priceBot - L.priceTop);
        return lo + t * (hi - lo);
      },
      tip: function (idx) { return dayTip(d, idx, dec); }
    };
  }

  function drawCrosshair() {
    var m = S.map;
    if (!m || !S.hover) { return; }
    var idx = S.hover.idx;
    if (idx < 0 || idx >= m.n) { return; }
    var x = m.xOf(idx);
    strokeLine(x, m.top, m.bottom, C.cross, 1, [4, 4]);
    var hy = Math.max(m.top, Math.min(m.bottom, S.hover.y));
    strokeLine(m.left, hy, m.right, hy, C.crossSoft, 1, [3, 4]);
    if (hy >= m.priceTop && hy <= m.priceBot) {
      drawTag(fix(m.yToVal(hy), m.dec), m.right + 2, hy, "left");
    }
    ctx.save();
    ctx.fillStyle = "#eaf6ff";
    ctx.strokeStyle = C.price;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(x, hy, 2.4, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    ctx.restore();
  }

  function scheduleDraw() {
    if (S.raf) { return; }
    S.raf = requestAnimationFrame(function () { S.raf = 0; draw(); });
  }

  function draw() {
    if (!S.open) { return; }
    resizeCanvas();
    ctx.clearRect(0, 0, S.w, S.h);
    S.map = null;
    if (!S.item) { drawEmpty("暂无图表数据"); return; }
    if (S.tab === "day") { drawDay(S.item); } else { drawMin(S.item); }
    drawCrosshair();
  }

  function legendItem(label, color) {
    return '<span class="k"><i style="background:' + color + '"></i>' + esc(label) + "</span>";
  }
  function setLegend(parts) { if (legendEl) { legendEl.innerHTML = parts.join(""); } }

  function setHint(text) {
    if (!hintEl) { return; }
    if (!text) { hintEl.textContent = ""; hintEl.classList.remove("show"); return; }
    hintEl.textContent = text;
    hintEl.classList.add("show");
  }

  function metaText(item, key, tab) {
    if (!item) { return strOf(key); }
    var bits = [];
    if (item.market) { bits.push(strOf(item.market)); }
    if (item.currency) { bits.push(strOf(item.currency)); }
    if (tab === "day") {
      var dates = arrOf((item.day || {}).times);
      if (dates.length) { bits.push("日线 " + strOf(dates[0]) + " ~ " + strOf(dates[dates.length - 1])); }
    } else {
      var minItem = item.min || {};
      if (minItem.date) { bits.push("分时 " + strOf(minItem.date)); }
    }
    return bits.join(" · ");
  }

  function setTab(tab) {
    var item = S.item;
    if (tab === "day" && !hasDay(item)) { return; }
    if (tab === "min" && !hasMin(item)) { return; }
    S.tab = tab;
    S.hover = null;
    hideTip();
    if (metaEl && S.item) { metaEl.textContent = metaText(S.item, S.key, tab); }
    if (tabMin) {
      tabMin.classList.toggle("on", tab === "min");
      tabMin.setAttribute("aria-selected", tab === "min" ? "true" : "false");
    }
    if (tabDay) {
      tabDay.classList.toggle("on", tab === "day");
      tabDay.setAttribute("aria-selected", tab === "day" ? "true" : "false");
    }
    draw();
  }

  function openChart(key, label) {
    var item = CHARTS[key] || null;
    S.key = key;
    S.item = item;
    S.open = true;
    S.hover = null;
    S.focus = document.activeElement;
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    document.body.classList.add("chart-open");
    if (titleEl) { titleEl.textContent = (item && item.name) ? String(item.name) : (label || String(key)); }
    if (metaEl) { metaEl.textContent = metaText(item, key, "min"); }
    var canMin = hasMin(item);
    var canDay = hasDay(item);
    if (tabMin) { tabMin.disabled = !canMin; tabMin.classList.toggle("disabled", !canMin); }
    if (tabDay) { tabDay.disabled = !canDay; tabDay.classList.toggle("disabled", !canDay); }
    setHint("");
    if (!item || (!canMin && !canDay)) {
      S.tab = "min";
      if (tabMin) { tabMin.classList.add("on"); }
      if (tabDay) { tabDay.classList.remove("on"); }
      setHint(item ? "暂无分时 / 日线数据（数据采集中）" : "暂无该标的图表数据（数据采集中）");
      draw();
    } else {
      setTab(canMin ? "min" : "day");
      if (!canDay) { setHint("该市场暂无日线数据"); }
      else if (!canMin) { setHint("暂无分时数据，已显示日线"); }
      else { setHint(""); }
    }
    if (closeBtn && closeBtn.focus) { try { closeBtn.focus(); } catch (err) { } }
  }

  function closeChart() {
    if (!S.open) { return; }
    S.open = false;
    S.item = null;
    S.hover = null;
    S.map = null;
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    document.body.classList.remove("chart-open");
    hideTip();
    var f = S.focus;
    S.focus = null;
    if (f && f.focus) { try { f.focus(); } catch (err2) { } }
  }

  function hideTip() { if (tipEl) { tipEl.classList.remove("show"); } }

  function positionTip(x, y) {
    if (!tipEl || !S.map || !S.hover) { return; }
    tipEl.innerHTML = S.map.tip(S.hover.idx);
    tipEl.classList.add("show");
    var ww = wrap.clientWidth;
    var wh = wrap.clientHeight;
    var tw = tipEl.offsetWidth || 170;
    var th = tipEl.offsetHeight || 44;
    var left = x + 16;
    var top = y + 16;
    if (left + tw > ww - 8) { left = x - tw - 16; }
    if (top + th > wh - 8) { top = y - th - 16; }
    left = Math.max(6, Math.min(left, ww - tw - 6));
    top = Math.max(6, Math.min(top, wh - th - 6));
    tipEl.style.left = Math.round(left) + "px";
    tipEl.style.top = Math.round(top) + "px";
  }

  function setHover(x, y) {
    var m = S.map;
    if (!m || m.n <= 0) { return; }
    var t = (x - m.left) / ((m.right - m.left) || 1);
    t = Math.max(0, Math.min(1, t));
    var idx = Math.round(t * (m.n - 1));
    if (idx < 0) { idx = 0; }
    if (idx > m.n - 1) { idx = m.n - 1; }
    S.hover = { x: x, y: y, idx: idx };
    positionTip(x, y);
    scheduleDraw();
  }

  function onMove(ev) {
    var rect = canvas.getBoundingClientRect();
    setHover(ev.clientX - rect.left, ev.clientY - rect.top);
  }
  function onLeave() {
    S.hover = null;
    hideTip();
    scheduleDraw();
  }
  function onTouch(ev) {
    if (!ev.touches || !ev.touches.length) { return; }
    ev.preventDefault();
    var rect = canvas.getBoundingClientRect();
    var t = ev.touches[0];
    setHover(t.clientX - rect.left, t.clientY - rect.top);
  }

  function bindChart() {
    document.addEventListener("click", function (ev) {
      if (S.open) { return; }
      var t = ev.target;
      if (!t || !t.closest) { return; }
      if (t.closest("a,button,input,select,textarea,summary,details,label")) { return; }
      var trigger = t.closest("[data-chart],[data-code],[data-idx]");
      if (!trigger) { return; }
      var key = "";
      if (trigger.hasAttribute("data-chart")) {
        key = trigger.getAttribute("data-chart") || "";
      } else if (trigger.hasAttribute("data-code")) {
        var code = trigger.getAttribute("data-code") || "";
        if (code) { key = "stock:" + code; }
      } else if (trigger.hasAttribute("data-idx")) {
        var idx = trigger.getAttribute("data-idx") || "";
        if (idx) { key = "index:" + idx; }
      }
      if (!key) { return; }
      ev.preventDefault();
      openChart(key, trigger.getAttribute("data-label") || "");
    });
    if (backdrop) { backdrop.addEventListener("click", closeChart); }
    if (closeBtn) { closeBtn.addEventListener("click", closeChart); }
    if (tabMin) { tabMin.addEventListener("click", function () { setTab("min"); }); }
    if (tabDay) { tabDay.addEventListener("click", function () { setTab("day"); }); }
    document.addEventListener("keydown", function (ev) {
      if (!S.open) { return; }
      if (ev.key === "Escape" || ev.keyCode === 27) { ev.preventDefault(); closeChart(); return; }
      if (ev.key === "ArrowLeft") { ev.preventDefault(); setTab("min"); }
      if (ev.key === "ArrowRight") { ev.preventDefault(); setTab("day"); }
    });
    canvas.addEventListener("mousemove", onMove);
    canvas.addEventListener("mouseleave", onLeave);
    canvas.addEventListener("touchstart", onTouch, { passive: false });
    canvas.addEventListener("touchmove", onTouch, { passive: false });
    canvas.addEventListener("touchend", onLeave);
    window.addEventListener("resize", function () { if (S.open) { scheduleDraw(); } });
    if (typeof ResizeObserver === "function" && wrap) {
      try {
        new ResizeObserver(function () { if (S.open) { scheduleDraw(); } }).observe(wrap);
      } catch (err3) { /* first draw already sized */ }
    }
  }

  bindChart();
})();
"""


def _render_chart_modal() -> str:
    """图表弹层骨架（数据由客户端 _CHART_JS 从 window.__INTEL__.charts 读取）。"""
    return (
        '<div id="chart-modal" class="chart-modal hidden" data-testid="chart-modal" aria-hidden="true">'
        '<div class="chart-backdrop" id="chart-backdrop"></div>'
        '<div class="chart-panel" role="dialog" aria-modal="true" aria-label="行情图表">'
        '<div class="chart-head">'
        '<div class="chart-title-wrap"><span class="chart-title" id="chart-title">—</span>'
        '<span class="chart-meta" id="chart-meta"></span></div>'
        '<div class="chart-tabs" role="tablist" aria-label="图表类型">'
        '<button type="button" class="chart-tab on" id="chart-tab-min" data-chart-tab="min" '
        'role="tab" aria-selected="true">分时</button>'
        '<button type="button" class="chart-tab" id="chart-tab-day" data-chart-tab="day" '
        'role="tab" aria-selected="false">日线</button>'
        "</div>"
        '<button type="button" class="chart-close" id="chart-close" aria-label="关闭图表">✕</button>'
        "</div>"
        '<div class="chart-hint" id="chart-hint"></div>'
        '<div class="chart-canvas-wrap" id="chart-canvas-wrap">'
        '<canvas id="chart-canvas"></canvas>'
        '<div class="chart-tip" id="chart-tip"></div>'
        "</div>"
        '<div class="chart-legend" id="chart-legend"></div>'
        "</div></div>"
    )


def _render_page(data, title, dates, archive_href, data_date) -> str:
    d = _dct(data)
    world, ash = _dct(d.get("world")), _dct(d.get("ashare"))
    date_s = str(d.get("date") or "")
    body = "".join([
        _render_topbar(d, title, dates, archive_href),
        '<div class="wrap">',
        _render_kpis(d),
        '<div class="layout">',
        _render_markets(world),
        _render_news(world, date_s),
        _render_radar(world),
        _render_ashare(ash),
        _render_alerts(d),
        _render_watchlist(ash),
        _render_sources(d),
        "</div>",
        _render_footer(d),
        "</div>",
        _render_chart_modal(),
        _data_script(d, title, dates, archive_href),
        "<script>" + _JS + "</script>",
        "<script>" + _LIVE_JS + "</script>",
        "<script>" + _CHART_JS + "</script>",
    ])
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_e(title)} · {_e(data_date or '看板')}</title>\n"
        '<meta name="color-scheme" content="dark">\n'
        f"<style>{_CSS}{_LIVE_CSS}{_CHART_CSS}</style>\n</head>\n<body>\n{body}\n</body>\n</html>\n"
    )


# --------------------------------------------------------------------------- #
# archive page
# --------------------------------------------------------------------------- #
def _render_archive_page(dates, archive_items, title, archive_href_prefix) -> str:
    # 注意：空前缀 "" 是合法值（归档页与日期目录同级时用 `<date>/index.html`），
    # 不能写成 `or "../"`，否则 run_daily 传 archive_href_prefix="" 会被错误回退。
    prefix = "../" if archive_href_prefix is None else str(archive_href_prefix)
    items = []
    seen = set()
    for it in _lst(archive_items):
        it = _dct(it)
        day = str(it.get("date") or "").strip()
        if not day or day in seen:
            continue
        seen.add(day)
        items.append({
            "date": day,
            "news_count": it.get("news_count"),
            "watchlist_avg": it.get("watchlist_avg"),
            "top_category": it.get("top_category"),
            "href": str(it.get("href") or ""),
        })
    for dd in _lst(dates):
        dd = str(dd).strip()
        if dd and dd not in seen:
            seen.add(dd)
            items.append({"date": dd, "news_count": None, "watchlist_avg": None,
                          "top_category": None, "href": ""})
    items.sort(key=lambda it: it["date"], reverse=True)

    cards = []
    for it in items:
        day = it["date"]
        news_n = _num(it.get("news_count"))
        avg = _num(it.get("watchlist_avg"))
        top_cat = _clean(it.get("top_category"), 24)
        href = it.get("href") or (prefix + day + "/index.html")
        rows = []
        rows.append(
            '<div class="row"><span>要闻</span><b>'
            + (f"{int(news_n)} 条" if news_n is not None else "—")
            + "</b></div>"
        )
        rows.append(
            '<div class="row"><span>自选股平均</span><b class="'
            + _tone(avg)
            + '">'
            + (_fmt_pct(avg) if avg is not None else "—")
            + "</b></div>"
        )
        rows.append(
            '<div class="row"><span>最热类别</span><b>'
            + (f"{_e(top_cat)}" if top_cat else "—")
            + "</b></div>"
        )
        cards.append(
            f'<a class="arch-card" href="{_e(href)}">'
            f'<div class="d">{_e(day)}</div>'
            f'<div class="m">{"".join(rows)}</div>'
            '<div class="go">查看当日看板</div></a>'
        )

    if cards:
        body = f'<div class="arch-grid" data-testid="archive-list">{"".join(cards)}</div>'
    else:
        body = '<div class="arch-empty" data-testid="archive-list">暂无历史归档数据</div>'

    now = _fmt_gen(datetime.now().strftime("%Y-%m-%dT%H:%M:%S+08:00"))
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_e(title)} · 历史归档</title>\n"
        '<meta name="color-scheme" content="dark">\n'
        f"<style>{_CSS}</style>\n</head>\n<body>\n"
        '<div class="arch-wrap">'
        '<div class="arch-title"><h1>历史归档</h1>'
        f'<span class="en">{_e(title)} · archive</span></div>'
        f'<div class="arch-sub">共 {len(cards)} 个交易日 · 生成于 {_e(now)} · 单文件离线页面</div>'
        f"{body}"
        f'<div class="foot"><div>每日情报雷达 · 归档索引</div>'
        f'<div class="r"><span><a href="../index.html">返回最新</a></span></div></div>'
        "</div>\n</body>\n</html>\n"
    )


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def _resolve_out_file(out_dir: str) -> str:
    out_dir = str(out_dir or "").strip()
    if not out_dir:
        raise ValueError("out_dir is required")
    low = out_dir.lower()
    if low.endswith((".html", ".htm")):
        return out_dir
    return os.path.join(out_dir, "index.html")


def build(data: dict, out_dir: str, dates: list[str] | None = None,
          archive_path: str | None = None, archive_items: list[dict] | None = None,
          title: str = "每日情报雷达", archive_href_prefix: str = "../") -> str:
    """Render the single-file dashboard (and optionally the archive page).

    ``out_dir`` may be a directory (``index.html`` is created inside it) or an
    explicit ``*.html`` output path.  When ``archive_path`` is given an archive
    index page is written as well.  Returns the path of the dashboard HTML.
    """
    data = data if isinstance(data, dict) else {}
    out_file = _resolve_out_file(out_dir)
    out_parent = os.path.dirname(os.path.abspath(out_file))
    os.makedirs(out_parent, exist_ok=True)

    title = str(title or "每日情报雷达")
    dates = [str(x) for x in _lst(dates)]
    items = [x for x in _lst(archive_items) if isinstance(x, dict)]
    data_date = str(data.get("date") or "")

    archive_file = ""
    if archive_path:
        archive_file = str(archive_path)
        low = archive_file.lower()
        if not low.endswith((".html", ".htm")):
            archive_file = os.path.join(archive_file, "index.html")
        if os.path.abspath(archive_file) != os.path.abspath(out_file):
            os.makedirs(os.path.dirname(os.path.abspath(archive_file)), exist_ok=True)
            archive_html = _render_archive_page(dates, items, title, archive_href_prefix)
            _write(archive_file, archive_html)
        else:  # collision guard: never overwrite the dashboard with the archive
            archive_file = ""

    archive_href = ""
    if archive_file:
        archive_href = _rel_href(archive_file, out_parent)

    page = _render_page(data, title, dates, archive_href, data_date)
    _write(out_file, page)
    return out_file


def _write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成每日情报雷达单文件离线看板")
    ap.add_argument("--data", required=True, help="data.json 路径（契约 §4）")
    ap.add_argument("--out", required=True, help="输出 index.html 路径")
    ap.add_argument("--title", default="每日情报雷达", help="看板标题")
    ap.add_argument("--archive-index", default=None, help="归档页输出路径（可选）")
    ap.add_argument("--dates", default=None, help="逗号分隔的日期列表，用于归档下拉")
    ap.add_argument("--archive-items", default=None, help="归档卡片 JSON 文件路径（可选）")
    ap.add_argument("--archive-href-prefix", default="../",
                    help="归档卡片相对链接前缀（默认 ../）")
    args = ap.parse_args(argv)

    data_path = os.path.abspath(args.data)
    try:
        with open(data_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"[build_dashboard] 读取 data.json 失败: {exc}\n")
        return 2

    dates = None
    if args.dates:
        dates = [s.strip() for s in str(args.dates).split(",") if s.strip()]

    archive_items = None
    if args.archive_items:
        try:
            with open(os.path.abspath(args.archive_items), "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            archive_items = loaded if isinstance(loaded, list) else []
        except (OSError, ValueError) as exc:
            sys.stderr.write(f"[build_dashboard] 读取 archive-items 失败: {exc}\n")
            return 2

    out = build(
        data,
        args.out,
        dates=dates,
        archive_path=args.archive_index,
        archive_items=archive_items,
        title=args.title,
        archive_href_prefix=args.archive_href_prefix,
    )
    size = os.path.getsize(out) if os.path.exists(out) else 0
    sys.stdout.write(f"{out} ({size / 1024.0:.1f} KB)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
