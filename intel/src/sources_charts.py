# -*- coding: utf-8 -*-
"""intel.sources_charts — 图表数据采集（分时 + 日线，owner: ashare）。

契约：``intel/CONTRACT.md`` §12.1（`charts` 段 schema）+ §12.2（实现端点）。
只依赖 Python 标准库 + ``intel/src/common.py``，不得影响主流水线。

入口
----
``collect(date_str, watchlist, cfg, ashare=None) -> {"generated_at","items","sources"}``
    * ``items`` key：自选股 ``stock:<code>`` / A股指数 ``index:<code>`` /
      全球市场 ``world:<f12>``（f12 取自 ``cfg.world_markets[].secid`` 后半段）；
    * 每项含 ``min``（分时）与 ``day``（日线），拿不到为 ``None``（key 必须齐）；
    * 自选股日线**直接复用**调用方传入的 ``ashare`` 段（不重复请求）；
    * 单源失败只记 ``sources[{ok:false}]``，绝不抛异常。

``python intel/src/sources_charts.py``
    读 config + （可选）``intel/out/ashare_preview.json`` 复用个股日线，
    写 ``intel/out/charts_preview.json`` 并打印摘要。

实测端点（2026-11-19 本机验证）
--------------------------------
1. **分时（全部标的）** 东财 trends2（push2his 域名，实测可用，kline 端点被限流）::

       http://push2his.eastmoney.com/api/qt/stock/trends2/get
         ?fields1=f1,f2,f3,f4,f5,f6,f7,f8
         &fields2=f51,f52,f53,f54,f55,f56,f57,f58
         &ndays=1&iscr=0&secid=<secid>

   ``data.trends[]`` 每项 ``"2026-11-18 09:30,开,收,高,低,量,额,均价"``；
   ``data.preClose`` 昨收、``data.decimal`` 小数位、``data.name``/``market``。
   取第 3 项（收）为 price，第 8 项（均价，可能空）为 avg，第 6 项为 volume。
   美股/全球指数/原油同样可用（美股 105.NVDA -> 22:30 起、decimal=3）。
   备选 A股/HK：腾讯 ``/appstock/app/minute/query?code=sh600519``，
   每项 ``"0930 1450.00 781 113245000.00"``（价格=第 2 项，量为累计值需差分）。
2. **日线**：
   * 自选股：复用 ``ashare["watchlist"][code]["kline"]``（30 根，不请求）；
   * A股指数 / 港美指数：腾讯 ``/appstock/app/kline/kline?param=<code>,day,,,60``，
     返回 ``data.<key>.day[]``（A股 key=sh000001、美股指数 key=us.DJI、港股 hkHSI；
     北证50 bj899050 实测只有 1 根，接受）；
   * 原油/天然气：新浪外盘期货
     ``/futures/api/jsonp.php/x/GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=CL``
     -> ``x([...])``，全历史取最后 60 条；
   * 其余市场当前无可靠日线源：先试 1 次东财 push2his kline（快失败），
     失败后 ``day=None``（连续 2 次失败即跳过所有尝试，节省请求）。
3. 体积控制：``min`` 超 ``limits.chart_min_max_points``（默认 500）按等步长下采样
   （保留首尾）；``day`` 最多 ``limits.kline_days``（默认 60）根。
"""
from __future__ import annotations

import json
import math
import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import common as C  # noqa: E402  （common.py 与本文件同目录）

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
_REF = "https://quote.eastmoney.com/"
_REF_TENCENT = "https://gu.qq.com/"
_REF_SINA = "https://finance.sina.com.cn/"

_PUSH2HIS = "http://push2his.eastmoney.com"
_TRENDS_PATH = ("/api/qt/stock/trends2/get"
                "?fields1=f1,f2,f3,f4,f5,f6,f7,f8"
                "&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
                "&ndays=1&iscr=0&secid=%s")
_KLINE_PATH = ("/api/qt/stock/kline/get?secid=%s&fields1=f1,f2,f3,f4,f5,f6"
               "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
               "&klt=101&fqt=1&lmt=60&end=20500101")
_TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param=%s,day,,,%d"
_TENCENT_MINUTE = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=%s"
_SINA_FUT = ("https://stock.finance.sina.com.cn/futures/api/jsonp.php/x/"
             "GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=%s")

# world_markets f12 -> (market 标签, currency)
_WORLD_META = {
    "DJIA": ("US", "USD"), "NDX": ("US", "USD"), "SPX": ("US", "USD"),
    "N225": ("JP", "JPY"), "HSI": ("HK", "HKD"), "HSCEI": ("HK", "HKD"),
    "GDAXI": ("DE", "EUR"), "FTSE": ("GB", "GBP"), "KS11": ("KR", "KRW"),
    "AS51": ("AU", "AUD"), "SENSEX": ("IN", "INR"), "TSX": ("CA", "CAD"),
    "TWII": ("TW", "TWD"), "STI": ("SG", "SGD"), "SET": ("TH", "THB"),
    "JKSE": ("ID", "IDR"), "KLSE": ("MY", "MYR"), "VNINDEX": ("VN", "VND"),
    "UDI": ("FX", "USD"),
    "CL00Y": ("FUT", "USD"), "NG00Y": ("FUT", "USD"), "GC00Y": ("FUT", "USD"),
    "SI00Y": ("FUT", "USD"), "HG00Y": ("FUT", "USD"),
}

# push2his kline 连续失败计数（>2 次就跳过剩余兜底尝试，省请求）
_P2H_LOCK = threading.Lock()
_P2H_STATE = {"fails": 0}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _f(value):
    return C.safe_float(value)


def _num(value):
    """转 int（整数值）或 float（保留小数），失败 None。"""
    f = C.safe_float(value)
    if f is None:
        return None
    return int(f) if abs(f - round(f)) < 1e-9 else f


def _norm_arrays(rows: list[dict], limit: int) -> dict | None:
    """[{date,open,close,high,low,volume}] -> {times,open,close,high,low,volume}。"""
    rows = [r for r in (rows or []) if isinstance(r, dict) and r.get("date")]
    if not rows:
        return None
    rows = rows[-limit:] if limit and len(rows) > limit else rows
    return {
        "times": [str(r["date"]) for r in rows],
        "open": [_f(r.get("open")) for r in rows],
        "close": [_f(r.get("close")) for r in rows],
        "high": [_f(r.get("high")) for r in rows],
        "low": [_f(r.get("low")) for r in rows],
        "volume": [_num(r.get("volume")) for r in rows],
    }


def downsample_indices(n: int, max_points: int) -> list[int]:
    """等步长下采样索引（保留首尾）。n<=max_points 时返回全部。"""
    if n <= 0:
        return []
    if max_points <= 1:
        return [0]
    if n <= max_points:
        return list(range(n))
    step = (n - 1) / float(max_points - 1)
    idx = sorted({int(round(i * step)) for i in range(max_points)})
    if idx[0] != 0:
        idx.insert(0, 0)
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return idx


# --------------------------------------------------------------------------- #
# parsers（可离线单测）
# --------------------------------------------------------------------------- #
def parse_trends2(jobj: dict, max_points: int = 500) -> dict | None:
    """东财 trends2 分时解析 -> {date,prev_close,times,price,avg,volume,_decimals}。"""
    data = (jobj or {}).get("data")
    if not isinstance(data, dict):
        return None
    trends = data.get("trends")
    if not isinstance(trends, list) or not trends:
        return None
    rows = []
    for line in trends:
        parts = str(line).split(",")
        if len(parts) < 8:
            continue
        raw_t = parts[0].strip()
        bits = raw_t.split(" ")
        date = bits[0] if len(bits) > 1 else None
        hhmm = bits[-1]
        rows.append(type("Row", (), {
            "date": date, "time": hhmm,
            "price": _f(parts[2]), "volume": _num(parts[5]), "avg": _f(parts[7]),
        }))
    if not rows:
        return None
    idx = downsample_indices(len(rows), max_points)
    picked = [rows[i] for i in idx]
    dates = [r.date for r in picked if r.date]
    avg_list = [r.avg for r in picked]
    return {
        "date": dates[0] if dates else None,
        "prev_close": _f(data.get("preClose")),
        "times": [r.time for r in picked],
        "price": [r.price for r in picked],
        "avg": avg_list if any(a is not None for a in avg_list) else None,
        "volume": [r.volume for r in picked],
        "_decimals": int(_f(data.get("decimal")) or 2),
        "_name": C.clean_text(data.get("name") or ""),
    }


def parse_tencent_minute(jobj: dict, code: str, max_points: int = 500) -> dict | None:
    """腾讯分时备选解析（A股/HK）。

    结构：``data[code].data.data = ["0930 1450.00 781 113245000.00", ...]``
    （时间 HHMM、价格、**累计**成交量、累计成交额）。累计量差分转每分量。
    """
    data = (jobj or {}).get("data") or {}
    node = data.get(code)
    if not isinstance(node, dict):
        # 兜底：取第一个带 data 的节点
        for val in data.values():
            if isinstance(val, dict) and isinstance(val.get("data"), dict):
                node = val
                break
    if not isinstance(node, dict):
        return None
    inner = node.get("data")
    lines = inner.get("data") if isinstance(inner, dict) else node.get("data")
    if not isinstance(lines, list) or not lines:
        return None
    day = (inner or {}).get("date") if isinstance(inner, dict) else None
    rows = []
    prev_cum = None
    for line in lines:
        parts = str(line).split()
        if len(parts) < 3:
            continue
        hm = str(parts[0]).zfill(4)
        price = _f(parts[1])
        cum = _f(parts[2])
        vol = None
        if cum is not None:
            vol = cum if prev_cum is None else max(0.0, cum - prev_cum)
            prev_cum = cum
        rows.append((":".join((hm[:2], hm[2:4])), price, _num(vol)))
    if not rows:
        return None
    idx = downsample_indices(len(rows), max_points)
    picked = [rows[i] for i in idx]
    date = str(day) if day else None
    if date and len(date) == 8 and date.isdigit():
        date = "%s-%s-%s" % (date[:4], date[4:6], date[6:])
    return {
        "date": date,
        "prev_close": None,                 # 腾讯分时不带昨收
        "times": [r[0] for r in picked],
        "price": [r[1] for r in picked],
        "avg": None,
        "volume": [r[2] for r in picked],
        "_decimals": 2,
        "_name": "",
    }


def parse_tencent_daily(jobj: dict, symbol: str | None = None,
                        kline_days: int = 60) -> dict | None:
    """腾讯日K解析：``data.<key>.day[]``，key 可能是 sh000001/us.DJI/hkHSI。

    每行 ``[date, open, close, high, low, volume]``；按 data 下第一个含
    ``day``/``qfqday`` 的节点取（不硬依赖 symbol key）。
    """
    data = (jobj or {}).get("data")
    if not isinstance(data, dict):
        return None
    node = None
    if symbol and isinstance(data.get(symbol), dict):
        node = data[symbol]
    else:
        for val in data.values():
            if isinstance(val, dict) and any(
                    isinstance(val.get(k), list) and val.get(k)
                    for k in ("day", "qfqday")):
                node = val
                break
    if not isinstance(node, dict):
        return None
    raw = node.get("qfqday") or node.get("day") or []
    rows = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, (list, tuple)) or len(item) < 6:
            continue
        rows.append({
            "date": str(item[0]),
            "open": _f(item[1]), "close": _f(item[2]),
            "high": _f(item[3]), "low": _f(item[4]),
            "volume": _num(item[5]),
        })
    return _norm_arrays(rows, kline_days)


def parse_sina_futures(text, kline_days: int = 60) -> dict | None:
    """新浪外盘期货日线：``x([{date,open,high,low,close,volume,...},...])``。"""
    if not text:
        return None
    body = text.decode("utf-8", "replace") if isinstance(text, bytes) else str(text)
    try:
        data = json.loads(C.strip_jsonp(body))
    except ValueError:
        return None
    if isinstance(data, dict):
        data = data.get("result") or data.get("data") or []
    if not isinstance(data, list):
        return None
    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        rows.append({
            "date": str(item.get("date") or ""),
            "open": _f(item.get("open")), "close": _f(item.get("close")),
            "high": _f(item.get("high")), "low": _f(item.get("low")),
            "volume": _num(item.get("volume")),
        })
    return _norm_arrays(rows, kline_days)


# --------------------------------------------------------------------------- #
# HTTP layer（失败快切；全部返回 dict/None，不抛异常）
# --------------------------------------------------------------------------- #
def _get_json(url: str, referer: str = _REF, timeout: float = 8) -> dict:
    return C.http_get_json(url, referer=referer, timeout=timeout, retries=1)


def _get_text(url: str, referer: str = _REF, timeout: float = 8) -> str:
    return C.http_get_text(url, referer=referer, timeout=timeout, retries=1)


def _trends_secid(spec: dict) -> str:
    return str(spec.get("secid") or "")


def _p2h_available() -> bool:
    with _P2H_LOCK:
        return _P2H_STATE["fails"] < 2


def _p2h_fail() -> None:
    with _P2H_LOCK:
        _P2H_STATE["fails"] += 1


def _p2h_ok() -> None:
    with _P2H_LOCK:
        _P2H_STATE["fails"] = 0


def _p2h_reset() -> None:
    with _P2H_LOCK:
        _P2H_STATE["fails"] = 0


def _fetch_task(task: dict, max_points: int, kline_days: int) -> dict:
    """执行一个采集任务，返回 {min/day: 数据|null, source, error, elapsed_ms, requests}。"""
    t0 = time.time()
    kind = task["kind"]
    spec = task.get("spec") or {}
    out = {"data": None, "source": None, "error": None, "requests": 0}
    try:
        if kind == "min":
            out["requests"] += 1
            try:
                parsed = parse_trends2(
                    _get_json(_PUSH2HIS + (_TRENDS_PATH % spec["secid"])),
                    max_points=max_points)
                out["source"] = "em_trends2"
            except Exception as exc:  # noqa: BLE001
                parsed = None
                out["error"] = "trends2: %s" % str(exc)[:80]
            if parsed is None and spec.get("tencent_minute"):
                out["requests"] += 1
                try:
                    parsed = parse_tencent_minute(
                        _get_json(_TENCENT_MINUTE % spec["tencent_minute"],
                                  referer=_REF_TENCENT, timeout=8),
                        spec["tencent_minute"], max_points=max_points)
                    if parsed is not None:
                        out["source"] = "tencent_minute"
                        out["error"] = None
                except Exception as exc:  # noqa: BLE001
                    if not out["error"]:
                        out["error"] = "tencent_minute: %s" % str(exc)[:80]
            out["data"] = parsed
            if parsed is None and not out["error"]:
                out["error"] = "空数据"
        elif kind == "tencent_daily":
            out["requests"] += 1
            symbol = spec["symbol"]
            parsed = parse_tencent_daily(
                _get_json(_TENCENT_KLINE % (symbol, kline_days),
                          referer=_REF_TENCENT, timeout=8),
                symbol, kline_days=kline_days)
            out["data"] = parsed
            out["source"] = "tencent_kline"
            if parsed is None:
                out["error"] = "空数据"
        elif kind == "sina_fut":
            out["requests"] += 1
            parsed = parse_sina_futures(
                _get_text(_SINA_FUT % spec["symbol"], referer=_REF_SINA,
                          timeout=10),
                kline_days=kline_days)
            out["data"] = parsed
            out["source"] = "sina_futures"
            if parsed is None:
                out["error"] = "空数据"
        elif kind == "p2h_daily":
            if not _p2h_available():
                out["error"] = "push2his kline 已限流，跳过"
            else:
                out["requests"] += 1
                try:
                    jobj = _get_json(_PUSH2HIS + (_KLINE_PATH % spec["secid"]),
                                     timeout=8)
                    parsed = _day_from_em_kline(jobj, kline_days)
                    out["data"] = parsed
                    out["source"] = "em_push2his"
                    if parsed is None:
                        out["error"] = "空数据"
                        _p2h_fail()
                    else:
                        _p2h_ok()
                except Exception as exc:  # noqa: BLE001
                    _p2h_fail()
                    out["error"] = "push2his: %s" % str(exc)[:80]
        else:
            out["error"] = "未知任务类型 %s" % kind
    except Exception as exc:  # noqa: BLE001 - 绝不让单源失败冒泡
        out["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:100])
        out["data"] = None
    out["elapsed_ms"] = int((time.time() - t0) * 1000)
    return out


def _day_from_em_kline(jobj: dict, kline_days: int) -> dict | None:
    """东财 kline klines 逗号串 -> day 结构（push2his 恢复后的兜底）。"""
    data = (jobj or {}).get("data")
    if not isinstance(data, dict):
        return None
    rows = []
    for line in data.get("klines") or []:
        parts = str(line).split(",")
        if len(parts) < 7:
            continue
        rows.append({
            "date": parts[0], "open": _f(parts[1]), "close": _f(parts[2]),
            "high": _f(parts[3]), "low": _f(parts[4]), "volume": _num(parts[5]),
        })
    return _norm_arrays(rows, kline_days)


# --------------------------------------------------------------------------- #
# 标的构建
# --------------------------------------------------------------------------- #
def _index_market(code: str, secid: str = "") -> str:
    """A股指数市场：优先看东财 secid 前缀（1.=SH，0.=SZ），899xxx 归 BJ。"""
    c = str(code or "")
    if c.startswith(("899", "4", "8", "92")):
        return "BJ"
    if str(secid).startswith("1."):
        return "SH"
    if str(secid).startswith("0."):
        return "SZ"
    if c.startswith("399"):
        return "SZ"
    return "SH"


def _index_tencent_symbol(code: str, secid: str = "") -> str:
    """"000001"/1.000001 -> "sh000001"；899050 -> bj899050。"""
    c = str(code)
    market = _index_market(c, secid)
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(market, "sh") + c


def _ashare_kline_map(ashare) -> dict:
    """ashare 段 -> {code: kline rows}，兼容 list 或 dict 形式的 watchlist。"""
    if not isinstance(ashare, dict):
        return {}
    wl = ashare.get("watchlist")
    if isinstance(wl, dict):
        wl = list(wl.values())
    out = {}
    for st in wl if isinstance(wl, list) else []:
        if isinstance(st, dict) and st.get("code"):
            out[str(st["code"])] = st.get("kline") or []
    return out


def _day_from_ashare(kline_rows: list, kline_days: int) -> dict | None:
    rows = [r for r in (kline_rows or []) if isinstance(r, dict) and r.get("date")]
    rows = rows[-kline_days:]
    if not rows:
        return None
    return _norm_arrays(rows, kline_days)


def _build_items(cfg: dict, watchlist: list[dict], ashare) -> dict:
    """构建 items 骨架 + 采集任务列表。"""
    cfg = cfg or {}
    charts_cfg = cfg.get("charts") or {}
    ashare_cfg = cfg.get("ashare") or {}
    kline_map = _ashare_kline_map(ashare)
    items: dict[str, dict] = {}
    tasks: list[dict] = []
    day_reuse: list[tuple] = []       # (key, ok, count, error) 复用日线的 sources 记录

    # ---- 1) 自选股 ----
    for stock in watchlist or []:
        if not isinstance(stock, dict):
            continue
        code = str(stock.get("code") or "").strip()
        if not code:
            continue
        market = str(stock.get("market") or "").upper() or "SZ"
        currency = stock.get("currency") or ("USD" if market == "US" else "CNY")
        secid = stock.get("secid")
        if not secid:
            secid = C.secid(code) if market in ("SH", "SZ", "BJ") else ""
        code, secid = str(code), str(secid)
        key = "stock:%s" % code
        items[key] = {
            "name": stock.get("name") or code, "kind": "stock",
            "market": market, "currency": currency, "decimals": 2,
            "min": None, "day": None,
        }
        # 分时：A股/HK 有腾讯分时备选
        minute_code = ""
        if market in ("SH", "SZ", "BJ"):
            minute_code = {"SH": "sh", "SZ": "sz", "BJ": "bj"}[market] + code
        tasks.append({"key": key, "kind": "min",
                      "spec": {"secid": secid, "tencent_minute": minute_code}})
        # 日线：复用 ashare kline（不请求；股票默认 30 根）
        day = _day_from_ashare(kline_map.get(code) or [], 30)
        items[key]["day"] = day
        day_reuse.append((key, bool(day), len(day["times"]) if day else 0,
                          None if day else "ashare.kline 缺失"))

    # ---- 2) A股指数 ----
    idx_daily_codes = {str(s)[-6:]: str(s)
                       for s in (charts_cfg.get("ashare_index_daily_codes") or [])}
    for spec in ashare_cfg.get("indices") or []:
        secid = str(spec.get("secid") or "")
        code = secid.split(".")[-1] if "." in secid else str(spec.get("code") or "")
        if not code:
            continue
        key = "index:%s" % code
        market = _index_market(code, secid)
        items[key] = {
            "name": spec.get("name") or code, "kind": "index",
            "market": market,
            "currency": "CNY", "decimals": 2, "min": None, "day": None,
        }
        tasks.append({"key": key, "kind": "min",
                      "spec": {"secid": secid,
                               "tencent_minute": _index_tencent_symbol(code, secid)}})
        symbol = idx_daily_codes.get(code) or (
            "bj899050" if code == "899050" else "")
        if symbol:
            tasks.append({"key": key, "kind": "tencent_daily",
                          "spec": {"symbol": symbol}})
        else:
            day_reuse.append((key, False, 0, "无日线源配置"))

    # ---- 3) 全球市场/商品 ----
    markets = {str(m.get("secid")): m for m in (cfg.get("world_markets") or [])}
    world_daily = charts_cfg.get("world_daily") or {}
    futures_daily = charts_cfg.get("futures_daily") or {}
    for secid in charts_cfg.get("world_chart_secids") or []:
        secid = str(secid)
        f12 = secid.split(".")[-1]
        meta = markets.get(secid) or {}
        market, currency = _WORLD_META.get(f12, ("US", "USD"))
        key = "world:%s" % f12
        items[key] = {
            "name": meta.get("name") or f12, "kind": "world",
            "group": meta.get("group") or "", "market": market,
            "currency": currency, "decimals": 2, "min": None, "day": None,
        }
        minute_code = {"HSI": "hkHSI", "HSCEI": "hkHSCEI"}.get(f12, "")
        tasks.append({"key": key, "kind": "min",
                      "spec": {"secid": secid, "tencent_minute": minute_code}})
        if f12 in futures_daily:
            tasks.append({"key": key, "kind": "sina_fut",
                          "spec": {"symbol": futures_daily[f12]}})
        elif f12 in world_daily:
            tasks.append({"key": key, "kind": "tencent_daily",
                          "spec": {"symbol": world_daily[f12]}})
        else:
            tasks.append({"key": key, "kind": "p2h_daily",
                          "spec": {"secid": secid}})
    return {"items": items, "tasks": tasks, "day_reuse": day_reuse}


def _source_id(prefix: str, key: str) -> str:
    return "%s_%s" % (prefix, key.replace(":", "_"))


def _source_name(kind_cn: str, key: str, items: dict) -> str:
    name = (items.get(key) or {}).get("name") or key
    return "%s %s" % (kind_cn, name)


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def collect(date_str: str, watchlist: list[dict], cfg: dict,
            ashare: dict | None = None) -> dict:
    """采集图表数据（分时 + 日线），返回契约 §12.1 的 charts 段。

    Parameters
    ----------
    date_str : str
        数据归属日 ``YYYY-MM-DD``。
    watchlist : list[dict]
        ``common.load_watchlist`` 输出（13 只，含 A股/美股）。
    cfg : dict
        ``config/sources.json``。
    ashare : dict | None
        已归一化的 ``data.json`` ashare 段；自选股日线直接复用它。
    """
    t_start = time.time()
    date_str = str(date_str or C.today_cn())
    cfg = cfg or {}
    limits = cfg.get("limits") or {}
    max_points = int(limits.get("chart_min_max_points") or 500)
    kline_days = int(limits.get("kline_days") or 60)
    _p2h_reset()
    built = _build_items(cfg, watchlist or [], ashare)
    items, tasks, day_reuse = built["items"], built["tasks"], built["day_reuse"]
    sources: list[dict] = []
    request_count = 0

    # ---- 日线复用记录（自选股，无请求）----
    for key, ok, count, error in day_reuse:
        sources.append({
            "id": _source_id("day", key),
            "name": _source_name("日线(复用ashare)", key, items),
            "ok": bool(ok), "count": int(count),
            "error": error, "elapsed_ms": 0,
        })

    # ---- 预探测：push2his kline 若被限流，一次性跳过所有 p2h 日线兜底 ----
    # （并发=4 时若直接发任务会同时打出 4 个失败请求，预探测只需 1 个）
    p2h_tasks = [t for t in tasks if t.get("kind") == "p2h_daily"]
    if p2h_tasks:
        request_count += 1
        try:
            _get_json(_PUSH2HIS + (_KLINE_PATH % p2h_tasks[0]["spec"]["secid"]),
                      timeout=8)
            _p2h_ok()
        except Exception:  # noqa: BLE001 - 限流：后续 p2h 任务直接跳过
            with _P2H_LOCK:
                _P2H_STATE["fails"] = 2

    # ---- 并发采集（≤4）----
    def _runner(task):
        return _fetch_task(task, max_points, kline_days)

    pairs = C.run_parallel(_runner, tasks, workers=4) if tasks else []

    for task, res, err in pairs:
        key = task["key"]
        kind = task["kind"]
        item = items.get(key)
        if item is None:
            continue
        if err is not None or res is None:
            res = {"data": None, "source": None, "requests": 0,
                   "error": str(err)[:100], "elapsed_ms": 0}
        request_count += int(res.get("requests") or 0)
        data = res.get("data")
        if kind == "min":
            item["min"] = data
            if data and data.get("_decimals"):
                item["decimals"] = int(data["_decimals"])
            prefix, cn = "trends", "分时"
        else:
            item["day"] = data
            prefix, cn = "day", "日线"
        sources.append({
            "id": _source_id(prefix, key),
            "name": _source_name(cn, key, items),
            "ok": data is not None,
            "count": len(data.get("times") or []) if data else 0,
            "error": None if data is not None else (res.get("error") or "空数据"),
            "elapsed_ms": int(res.get("elapsed_ms") or 0),
        })

    # ---- 清理内部字段 + 兜底统计 ----
    items_out = {}
    for key, item in items.items():
        clean = {k: v for k, v in item.items() if not k.startswith("_")}
        if clean.get("min"):
            clean["min"] = {k: v for k, v in clean["min"].items()
                            if not k.startswith("_")}
        items_out[key] = clean

    sources = sources[:80]
    min_ok = sum(1 for it in items_out.values() if it.get("min"))
    day_ok = sum(1 for it in items_out.values() if it.get("day"))
    return {
        "generated_at": C.iso(C.now_cn()),
        "items": items_out,
        "sources": sources,
        # 额外统计（run_daily 可忽略；--main 打印/自测用）
        "stats": {
            "date": date_str,
            "items": len(items_out),
            "min_ok": min_ok,
            "day_ok": day_ok,
            "min_total": sum(1 for it in items_out.values() if it.get("kind")),
            "requests": request_count,
            "elapsed_sec": round(time.time() - t_start, 2),
        },
    }


# --------------------------------------------------------------------------- #
# CLI / preview
# --------------------------------------------------------------------------- #
def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_configs():
    root = _project_root()
    cfg = C.load_json(os.path.join(root, "config", "sources.json"), {}) or {}
    watchlist = C.load_watchlist(os.path.join(root, "config", "watchlist.json"))
    return cfg, watchlist


def load_ashare_preview():
    """CLI 用：若有 ashare_preview.json 就复用自选股日线（run_daily 会显式传入）。"""
    path = os.path.join(_project_root(), "out", "ashare_preview.json")
    data = C.load_json(path, None)
    return data if isinstance(data, dict) else None


def print_summary(data: dict) -> None:
    items = data.get("items") or {}
    stats = data.get("stats") or {}
    sources = data.get("sources") or []
    min_ok = sum(1 for it in items.values() if it.get("min"))
    day_ok = sum(1 for it in items.values() if it.get("day"))
    failed = [s for s in sources if not s.get("ok")]
    print("=" * 78)
    print("图表 items：%d 个（stock %d / index %d / world %d）｜分时成功 %d｜日线成功 %d"
          % (len(items),
             sum(1 for it in items.values() if it.get("kind") == "stock"),
             sum(1 for it in items.values() if it.get("kind") == "index"),
             sum(1 for it in items.values() if it.get("kind") == "world"),
             min_ok, day_ok))
    print("请求数：%s｜耗时 %.2fs｜sources %d 条（失败 %d）"
          % (stats.get("requests"), stats.get("elapsed_sec", 0),
             len(sources), len(failed)))
    # 分时覆盖
    print("-" * 78)
    print("分时：")
    for key, it in items.items():
        m = it.get("min") or {}
        print("  %-16s %-10s %s  points=%s price=%s"
              % (key, it.get("name"),
                 "OK" if it.get("min") else "NULL",
                 len(m.get("times") or []),
                 ("%.4g" % m["price"][-1]) if m.get("price") else "—"))
    # 日线覆盖
    print("-" * 78)
    print("日线：")
    for key, it in items.items():
        d = it.get("day") or {}
        print("  %-16s %-10s %s  bars=%s"
              % (key, it.get("name"),
                 "OK" if it.get("day") else "NULL",
                 len(d.get("times") or [])))
    # 失败源（aggregate by reason to avoid noise）
    print("-" * 78)
    reasons: dict[str, int] = {}
    for s in failed:
        reason = (s.get("error") or "")[:60]
        reasons[reason] = reasons.get(reason, 0) + 1
    print("失败源：%d 条" % len(failed))
    for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print("  x%-2d %s" % (n, reason or "空数据"))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    date_str = C.today_cn()
    if "--date" in argv:
        idx = argv.index("--date")
        if idx + 1 < len(argv):
            date_str = argv[idx + 1]
    cfg, watchlist = load_configs()
    ashare = load_ashare_preview()
    print("[charts] date=%s watchlist=%d 只，开始采集（live）…"
          % (date_str, len(watchlist)))
    data = collect(date_str, watchlist, cfg, ashare)
    out_path = os.path.join(_project_root(), "out", "charts_preview.json")
    try:
        C.save_json(out_path, data)
        print("[charts] 预览已写入 %s" % out_path)
    except Exception as exc:  # noqa: BLE001
        print("[charts] 写预览失败: %s" % exc)
    print_summary(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
