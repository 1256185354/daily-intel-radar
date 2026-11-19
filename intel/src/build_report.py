# -*- coding: utf-8 -*-
"""intel.build_report — 中文每日简报（Markdown）生成器。

Owner: briefwriter。只依赖 CONTRACT.md §4 的 data.json schema 与 common.py，
不做任何数值推算假设，不做网络请求，不编造数据。

用法（CLI）::

    python intel/src/build_report.py --data intel/data/2026-11-18/data.json \
        --out intel/data/2026-11-18/brief.md [--notes notes.txt]

    from build_report import build
    build(data, "brief.md", notes="编辑备注正文")
"""
from __future__ import annotations

import argparse
import os
import re
import sys

# build_report.py 与 common.py 同目录；直接运行/被 run_daily 调用都能 import。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (  # noqa: E402  (import 顺序依赖上面的 sys.path)
    clean_text,
    fmt_amount,
    fmt_pct,
    load_json,
    load_text,
    parse_dt_any,
    safe_float,
    write_text,
)

MISSING = "数据缺失"
EMPTY = "暂无"
MAX_NEWS_PER_CAT = 6
MOVER_COUNT = 10
LIMIT_UP_TOP = 10
TITLE_LEN = 100

# 禁止出现的裸 Python/JS 空值标记（全篇最后再兜底一次；链接目标会被保护）。
_BAD_TOKEN_RE = re.compile(r"(?i)(?<![A-Za-z0-9_])(none|undefined|nan|null)(?![A-Za-z0-9_])")
_URL = re.compile(r"^https?://[^\s<>\"'()\[\]{}]+$")
_LINK_TARGET_RE = re.compile(r"\]\((https?://[^\s)]+)\)")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# --------------------------------------------------------------------------- #
# 基础格式化 / 取值（全部容错，数据缺失统一成「数据缺失」）
# --------------------------------------------------------------------------- #
def _get(obj, *keys, default=None):
    """安全深取值：任何一层不是 dict / 值为 None 都返回 default。"""
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _dicts(obj, *keys):
    """取出 dict 列表；非 dict 元素丢弃。"""
    value = _get(obj, *keys)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _text(value, limit: int = 0) -> str:
    """任意值 -> 干净单行文本；None -> ''；屏蔽 None/nan/undefined/null 字样。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if not isinstance(value, str):
        if isinstance(value, float):
            if value != value:  # NaN
                return ""
            value = f"{value:g}"
        else:
            value = str(value)
    text = clean_text(value, max_len=limit) if limit else clean_text(value)
    text = _CTRL_RE.sub("", text)
    text = _BAD_TOKEN_RE.sub(MISSING, text)
    # 标题/正文一律单行：新闻源的摘要里常带换行，会把列表项撑成多行。
    text = re.sub(r"\s*\n\s*", " ", text)
    return text.strip()


def _s(value, limit: int = 0, default: str = MISSING) -> str:
    return _text(value, limit) or default


def _num(value, digits: int = 2) -> str:
    val = safe_float(value)
    if val is None:
        return MISSING
    return f"{val:.{digits}f}"


def _num_signed(value, digits: int = 2) -> str:
    """涨跌额等带符号数值：3.25 -> '+3.25'。"""
    val = safe_float(value)
    if val is None:
        return MISSING
    return f"{val:+.{digits}f}"


def _num_asset(value) -> str:
    """市场快照最新价：汇率/商品等绝对值 <10 的资产保留 4 位小数。"""
    val = safe_float(value)
    if val is None:
        return MISSING
    return f"{val:.4f}" if abs(val) < 10 else f"{val:.2f}"


def _amt(value) -> str:
    text = fmt_amount(value)
    return MISSING if text == "—" else text


def _pct(value, digits: int = 2, sign: bool = True) -> str:
    text = fmt_pct(value, digits=digits, sign=sign)
    return MISSING if text == "—" else text


def _pct_plain(value, digits: int = 1) -> str:
    """0.541 -> '54.1%'；若上游已给百分数（>1.5）则原样处理。"""
    val = safe_float(value)
    if val is None:
        return MISSING
    if val <= 1.5:
        val *= 100.0
    return f"{val:.{digits}f}%"


def _int(value) -> str:
    val = safe_float(value)
    if val is None:
        return MISSING
    return str(int(val))


def _sector_counts(row: dict) -> str:
    """涨/跌家数：两列为空时显示 '—'（备用源可能不提供），否则 'a / b'。"""
    up, down = row.get("up_count"), row.get("down_count")
    if up is None and down is None:
        return "—"
    return f"{_int(up)} / {_int(down)}"


def _sector_count_note(row: dict) -> str:
    """用于要点中的括注：两列都缺失时返回空串，避免「数据缺失 涨 / 数据缺失 跌」。"""
    up, down = row.get("up_count"), row.get("down_count")
    if up is None and down is None:
        return ""
    return f"（{_int(up)} 涨 / {_int(down)} 跌）"


def _url(value) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or not _URL.match(text):
        return ""
    return text


def _link(title, url, limit: int = TITLE_LEN) -> str:
    """合法 http(s) 链接才渲染成 Markdown 链接，否则退回纯文本（绝无 []() ）。"""
    text = _text(title, limit)
    if not text:
        text = "（标题缺失）"
    text = text.replace("[", "［").replace("]", "］")
    target = _url(url)
    return f"[{text}]({target})" if target else text


def _cell(value, default: str = MISSING) -> str:
    """Markdown 表格单元格：去换行、竖线转全角，空值给默认。"""
    text = _text(value) if not isinstance(value, str) else _text(value)
    text = text.replace("\n", " ").replace("\r", " ").replace("|", "｜")
    return text.strip() or default


def _row(*cells) -> str:
    return "| " + " | ".join(_cell(c) for c in cells) + " |"


def _table(headers, rows) -> list[str]:
    out = [_row(*headers), "| " + " | ".join("---" for _ in headers) + " |"]
    out.extend(_row(*row) for row in rows)
    return out


def _fmt_time(value, report_date: str = "") -> str:
    dt = parse_dt_any(value)
    if dt is None:
        return "时间未知"
    if report_date and dt.strftime("%Y-%m-%d") == report_date:
        return dt.strftime("%H:%M")
    return dt.strftime("%m-%d %H:%M")


def _fmt_date_any(value) -> str:
    """纯日期原样保留（2026-10-27），其余按时间格式化（22:10 / 11-17 18:48）。"""
    text = _text(value, 20)
    if not text:
        return "时间未知"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    return _fmt_time(value)


def _time_cn(value) -> str:
    dt = parse_dt_any(value)
    if dt is None:
        return MISSING
    return dt.strftime("%Y-%m-%d %H:%M:%S") + "（Asia/Shanghai）"


def _seconds(value) -> str:
    val = safe_float(value)
    if val is None:
        return MISSING
    return f"{val:.1f} 秒"


def _elapsed_ms(value) -> str:
    val = safe_float(value)
    if val is None:
        return "—"
    return f"{int(val)}ms"


def _sanitize_doc(doc: str) -> str:
    """全篇兜底清除裸 None/nan/undefined/null，同时保护链接目标不被改写。"""
    targets: list[str] = []

    def _protect(match: re.Match) -> str:
        targets.append(match.group(1))
        return f"](@@LINK{len(targets) - 1}@@)"

    guarded = _LINK_TARGET_RE.sub(_protect, doc)
    guarded = _BAD_TOKEN_RE.sub(MISSING, guarded)
    for idx, target in enumerate(targets):
        guarded = guarded.replace(f"](@@LINK{idx}@@)", f"]({target})")
    return guarded


def _bullets(lines: list[str]) -> list[str]:
    return lines or [f"- {MISSING}"]


# --------------------------------------------------------------------------- #
# 上下文（一次性解析 data.json，避免各章重复取值）
# --------------------------------------------------------------------------- #
def _context(data: dict) -> dict:
    data = data if isinstance(data, dict) else {}
    world = _get(data, "world", default={}) or {}
    ashare = _get(data, "ashare", default={}) or {}
    if not isinstance(world, dict):
        world = {}
    if not isinstance(ashare, dict):
        ashare = {}

    news = _dicts(world, "news")
    markets = _dicts(world, "markets")
    by_category = _dicts(world, "by_category")
    social = _dicts(world, "social")
    world_sources = _dicts(world, "sources")
    ashare_sources = _dicts(ashare, "sources")
    sources = world_sources + ashare_sources

    breadth = _get(ashare, "breadth")
    breadth = breadth if isinstance(breadth, dict) else None
    movers = _get(ashare, "movers")
    movers = movers if isinstance(movers, dict) else {}

    report_date = _text(_get(data, "date"), 10)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date):
        report_date = ""

    ok_count = sum(1 for src in sources if src.get("ok") is True)
    return {
        "data": data,
        "report_date": report_date,
        "generated_at": _get(data, "generated_at"),
        "run": _get(data, "run") if isinstance(_get(data, "run"), dict) else {},
        "world": world,
        "ashare": ashare,
        "news": news,
        "markets": markets,
        "by_category": by_category,
        "social": social,
        "world_sources": world_sources,
        "ashare_sources": ashare_sources,
        "sources": sources,
        "ok_count": ok_count,
        "fail_count": len(sources) - ok_count,
        "errors": _dicts(data, "errors"),
        "alerts": _visible_alerts(_dicts(data, "alerts")),
        "indices": _dicts(ashare, "indices"),
        "breadth": breadth,
        "sectors_top": _dicts(ashare, "sectors", "top"),
        "sectors_bottom": _dicts(ashare, "sectors", "bottom"),
        "gainers": _dicts(movers, "gainers"),
        "losers": _dicts(movers, "losers"),
        "turnover": _dicts(movers, "turnover"),
        "limit_up_pool": _dicts(ashare, "limit_up_pool"),
        "watchlist": _dicts(ashare, "watchlist"),
        "watchlist_meta": _get(ashare, "watchlist_meta")
        if isinstance(_get(ashare, "watchlist_meta"), dict)
        else {},
    }


# --------------------------------------------------------------------------- #
# 规则化指标
# --------------------------------------------------------------------------- #
def _market_extremes(markets: list[dict]) -> tuple[dict | None, dict | None, int]:
    valid = [m for m in markets if safe_float(m.get("change_pct")) is not None]
    if not valid:
        return None, None, 0
    ordered = sorted(valid, key=lambda m: safe_float(m.get("change_pct")))
    return ordered[-1], ordered[0], len(valid)


def _breadth_ratio(breadth: dict | None) -> float | None:
    if not isinstance(breadth, dict):
        return None
    ratio = safe_float(breadth.get("up_ratio"))
    if ratio is not None:
        return ratio * 100.0 if ratio <= 1.5 else ratio
    up = safe_float(breadth.get("up"))
    total = safe_float(breadth.get("total"))
    if up is not None and total:
        return up / total * 100.0
    return None


def _breadth_tone(ratio: float | None) -> str:
    if ratio is None:
        return MISSING
    if ratio >= 60:
        return "偏强"
    if ratio >= 45:
        return "中性"
    return "偏弱"


def _watchlist_stats(watchlist: list[dict], meta: dict) -> dict:
    known: list[tuple[dict, float]] = []
    for stock in watchlist:
        change = safe_float(_get(stock, "quote", "change_pct"))
        if change is not None:
            known.append((stock, change))
    avg = safe_float(meta.get("avg_change_pct"))
    if avg is None and known:
        avg = sum(change for _, change in known) / len(known)
    best = worst = None
    if known:
        best = max(known, key=lambda pair: pair[1])
        worst = min(known, key=lambda pair: pair[1])
    return {
        "count": len(watchlist),
        "known": known,
        "avg": avg,
        "best": best,
        "worst": worst,
    }


def _stock_name(stock: dict) -> str:
    return _s(stock.get("name"), 20, _s(stock.get("code"), 20, "未命名标的"))


def _index_of_report(indices: list[dict]) -> dict | None:
    for index in indices:
        if "上证" in _text(index.get("name")):
            return index
    return indices[0] if indices else None


def _top_category(news: list[dict], by_category: list[dict]) -> tuple[str, int]:
    """条数最多的类别；优先排除「综合」（信息量低），全为综合时才退回。"""
    counts: dict[str, int] = {}
    for item in news:
        name = _text(item.get("category"), 20) or "综合"
        counts[name] = counts.get(name, 0) + 1
    for row in by_category:
        name = _text(row.get("name"), 20)
        count = safe_float(row.get("count"))
        if name and count is not None:
            counts[name] = max(counts.get(name, 0), int(count))
    if not counts:
        return "", 0
    focused = {name: count for name, count in counts.items() if name and name != "综合"}
    pool = focused or {name: count for name, count in counts.items() if name}
    if not pool:
        return "", 0
    name, count = max(pool.items(), key=lambda pair: pair[1])
    return name, count


# --------------------------------------------------------------------------- #
# 板块 / 涨停池 / 提示 的防御性清洗
# --------------------------------------------------------------------------- #
def _sector_key(row: dict) -> str:
    code = _text(row.get("code"), 16)
    return f"code:{code}" if code else f"name:{_text(row.get('name'), 20)}"


def _sector_view(ctx: dict):
    """(全部有效板块, 上涨板块降序, 下跌板块升序)。

    上游 sectors.top/bottom 偶发同源反转（如 bottom 里全是上涨板块），
    这里按 code/name 去重并按 change_pct 正负严格分流，保证「领跌」不出现正涨幅。
    """
    seen: set[str] = set()
    valid: list[tuple[dict, float]] = []
    for row in list(ctx.get("sectors_top") or []) + list(ctx.get("sectors_bottom") or []):
        key = _sector_key(row)
        if key in seen:
            continue
        change = safe_float(row.get("change_pct"))
        if change is None:
            continue
        seen.add(key)
        valid.append((row, change))
    gainers = sorted(
        (pair for pair in valid if pair[1] > 0), key=lambda pair: pair[1], reverse=True
    )
    losers = sorted((pair for pair in valid if pair[1] < 0), key=lambda pair: pair[1])
    return valid, gainers, losers


def _boards(item: dict):
    """连板数：兼容 lbc / boards / limit_up_days 三种上游写法。"""
    for key in ("lbc", "boards", "limit_up_days", "continuous_boards"):
        value = safe_float(item.get(key))
        if value is not None:
            return value
    return None


def _fmt_seal_time(value) -> str:
    """首封时间：正常 HH:MM:SS 原样输出。

    已知上游 bug：fbt 为 HHMMSS 整数（如 92500）时被当作秒数转成了 25:41:40，
    这里按确定性逆变换还原成 09:25:00；无法安全还原则原样输出，不臆造。
    """
    text = _text(value, 12)
    if not text:
        return "—"
    match = re.fullmatch(r"(\d{1,2}):(\d{2}):(\d{2})", text)
    if match:
        hour, minute, second = (int(part) for part in match.groups())
        if hour < 24 and minute < 60 and second < 60:
            return f"{hour:02d}:{minute:02d}:{second:02d}"
        total = hour * 3600 + minute * 60 + second
        if total > 0:
            hh, mm, ss = total // 10000, (total // 100) % 100, total % 100
            if hh < 24 and mm < 60 and ss < 60:
                return f"{hh:02d}:{mm:02d}:{ss:02d}"
        return text
    if re.fullmatch(r"\d{5,6}", text):
        number = int(text)
        hh, mm, ss = number // 10000, (number // 100) % 100, number % 100
        if hh < 24 and mm < 60 and ss < 60:
            return f"{hh:02d}:{mm:02d}:{ss:02d}"
    return text


def _is_debug_alert(alert: dict) -> bool:
    """ashare 调试类提示（近 N 条新闻中 M 条命中…）不进日报。"""
    text = _text(alert.get("text"), 120)
    return text.startswith("近 ") and "命中" in text


def _visible_alerts(alerts: list[dict]) -> list[dict]:
    """去掉调试提示并按文本去重（上游公告类提示可能重复多条）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for alert in alerts:
        if _is_debug_alert(alert):
            continue
        text = _text(alert.get("text"), 120)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(alert)
    return out


def _header_source_summary(ctx: dict) -> str:
    """页首口径：采集源 ok/total、接口请求数、耗时（53 是源/接口条目数，不是数据源个数）。"""
    sources = ctx["sources"]
    if sources:
        text = f"采集源 {ctx['ok_count']}/{len(sources)} 成功（失败 {ctx['fail_count']}）"
    else:
        text = f"采集源状态{MISSING}"
    run = ctx["run"]
    bits: list[str] = []
    if run.get("requests") is not None:
        bits.append(f"接口请求 {_int(run.get('requests'))} 次")
    if run.get("duration_sec") is not None:
        bits.append(f"耗时 {_seconds(run.get('duration_sec'))}")
    if bits:
        text += " · " + " · ".join(bits)
    return text


# --------------------------------------------------------------------------- #
# 一句话结论
# --------------------------------------------------------------------------- #
def _one_liner(ctx: dict) -> str:
    strong, weak, market_n = _market_extremes(ctx["markets"])
    ratio = _breadth_ratio(ctx["breadth"])
    stats = _watchlist_stats(ctx["watchlist"], ctx["watchlist_meta"])

    parts: list[str] = []
    if strong is not None and weak is not None:
        if market_n > 1 and strong is not weak:
            parts.append(
                f"全球资产中{_s(strong.get('name'), 20)}最强"
                f"（{_pct(strong.get('change_pct'))}）、"
                f"{_s(weak.get('name'), 20)}最弱（{_pct(weak.get('change_pct'))}）"
            )
        else:
            parts.append(f"全球资产中{_s(strong.get('name'), 20)}报 {_pct(strong.get('change_pct'))}")

    category, category_count = _top_category(ctx["news"], ctx["by_category"])
    if category and category_count:
        parts.append(f"全球要闻最热为「{category}」（{category_count} 条）")

    if ratio is not None:
        limit_up = _int(_get(ctx["breadth"], "limit_up", default=None))
        limit_down = _int(_get(ctx["breadth"], "limit_down", default=None))
        parts.append(
            f"A股上涨占比 {_pct_plain(ratio)}（{_breadth_tone(ratio)}，"
            f"涨停 {limit_up} 家 / 跌停 {limit_down} 家）"
        )
    elif ctx["indices"]:
        index = _index_of_report(ctx["indices"])
        parts.append(f"{_s(index.get('name'), 20)} {_pct(index.get('change_pct'))}")

    if stats["count"]:
        if stats["avg"] is not None:
            best_txt = ""
            if stats["best"] is not None:
                stock, change = stats["best"]
                best_txt = f"，最强 {_stock_name(stock)} {_pct(change)}"
            parts.append(f"自选股 {stats['count']} 只平均 {_pct(stats['avg'])}{best_txt}")
        else:
            parts.append(f"自选股 {stats['count']} 只行情{EMPTY}")

    if not parts:
        return "今日核心数据缺失，简报仅保留结构框架，详见第五章数据质量。"
    return "；".join(parts) + "。"


# --------------------------------------------------------------------------- #
# 第一章 今日要点
# --------------------------------------------------------------------------- #
def _chapter_points(ctx: dict) -> list[str]:
    points: list[str] = []

    # 1. 全球市场强弱
    strong, weak, market_n = _market_extremes(ctx["markets"])
    if strong is not None and weak is not None:
        text = f"全球市场：{_s(strong.get('name'), 20)} 领涨 {_pct(strong.get('change_pct'))}"
        if weak is not strong:
            text += f"，{_s(weak.get('name'), 20)} 领跌 {_pct(weak.get('change_pct'))}"
        points.append(f"{text}（覆盖 {market_n} 个资产）。")
    else:
        points.append(f"全球市场：市场快照{MISSING}，无法判断强弱资产。")

    # 2. A股宽度 + 代表性指数
    breadth = ctx["breadth"]
    ratio = _breadth_ratio(breadth)
    if isinstance(breadth, dict) and (ratio is not None or breadth.get("total") is not None):
        up = _int(breadth.get("up"))
        down = _int(breadth.get("down"))
        flat = _int(breadth.get("flat"))
        limit_up = _int(breadth.get("limit_up"))
        limit_down = _int(breadth.get("limit_down"))
        total = _int(breadth.get("total"))
        text = (
            f"A股宽度：全市场 {total} 家，上涨 {up} 家、下跌 {down} 家、平盘 {flat} 家，"
            f"涨停 {limit_up} 家、跌停 {limit_down} 家，"
            f"上涨占比 {_pct_plain(ratio)}，整体{_breadth_tone(ratio)}"
        )
        index = _index_of_report(ctx["indices"])
        if index is not None:
            text += f"；{_s(index.get('name'), 20)} {_num(index.get('price'))}（{_pct(index.get('change_pct'))}）"
        points.append(text + "。")
    else:
        points.append(f"A股宽度：市场宽度{MISSING}，无法判断多空结构。")

    # 3. 全球要闻结构 + 头条
    news = ctx["news"]
    if news:
        category, count = _top_category(news, ctx["by_category"])
        top = max(news, key=lambda item: safe_float(item.get("heat"), 0.0) or 0.0)
        points.append(
            f"全球要闻：共 {len(news)} 条，最热类别为「{_s(category, 20)}」（{count} 条）；"
            f"头条《{_s(top.get('title'), 60)}》"
            f"（{_s(top.get('source'), 24, '来源未知')} · {_fmt_time(top.get('published'), ctx['report_date'])}）。"
        )
    else:
        by_category_count = sum(
            int(safe_float(row.get("count"), 0) or 0) for row in ctx["by_category"]
        )
        if ctx["by_category"]:
            points.append(
                f"全球要闻：分类统计 {len(ctx['by_category'])} 类（合计 {by_category_count} 条），"
                f"但新闻明细{MISSING}。"
            )
        else:
            points.append(f"全球要闻：{MISSING}，未获取到今日全球新闻。")

    # 4. 自选股
    stats = _watchlist_stats(ctx["watchlist"], ctx["watchlist_meta"])
    if stats["count"]:
        text = f"自选股：{stats['count']} 只"
        if stats["known"]:
            text += f"，{len(stats['known'])} 只有效行情，平均涨跌 {_pct(stats['avg'])}"
            best = stats["best"]
            worst = stats["worst"]
            if best is not None:
                stock, change = best
                text += f"，最强 {_stock_name(stock)} {_pct(change)}"
            if (
                worst is not None
                and len(stats["known"]) > 1
                and best is not None
                and abs(worst[1] - best[1]) > 1e-9
            ):
                stock, change = worst
                text += f"，最弱 {_stock_name(stock)} {_pct(change)}"
        else:
            text += f"，行情{MISSING}"
        if ctx["alerts"]:
            text += f"；异动提示 {len(ctx['alerts'])} 条"
        points.append(text + "。")
    else:
        points.append(f"自选股：{MISSING}，未获取到自选股数据。")

    # 5. 板块轮动（有数据才写，最多 6 条；涨/跌严格按 change_pct 正负分流）
    valid_sectors, gainers, losers = _sector_view(ctx)
    if valid_sectors or ctx["limit_up_pool"]:
        parts: list[str] = []
        if gainers:
            sector, change = gainers[0]
            parts.append(
                f"领涨板块 {_s(sector.get('name'), 20)} {_pct(change)}"
                f"{_sector_count_note(sector)}"
            )
        elif valid_sectors:
            sector, change = max(valid_sectors, key=lambda pair: pair[1])
            parts.append(f"今日无上涨板块（表现最好 {_s(sector.get('name'), 20)} {_pct(change)}）")
        if losers:
            sector, change = losers[0]
            parts.append(f"领跌 {_s(sector.get('name'), 20)} {_pct(change)}")
        elif valid_sectors:
            parts.append("今日无下跌板块")
        if ctx["limit_up_pool"]:
            boards = [
                (_boards(item), item)
                for item in ctx["limit_up_pool"]
                if _boards(item) is not None
            ]
            if boards:
                board, item = max(boards, key=lambda pair: pair[0])
                if board >= 2:
                    parts.append(f"涨停池最高 {int(board)} 连板 {_s(item.get('name'), 20)}")
        if parts:
            points.append("板块轮动：" + "；".join(parts) + "。")

    # 6. 数据质量（有失败才写）
    if ctx["sources"] and (ctx["fail_count"] or ctx["errors"]):
        text = (
            f"数据质量：采集源 {ctx['ok_count']}/{len(ctx['sources'])} 成功"
            f"（失败 {ctx['fail_count']}）"
        )
        if ctx["errors"]:
            text += f"，采集错误 {len(ctx['errors'])} 条"
        points.append(text + "，详见第五章。")

    return points[:6]


# --------------------------------------------------------------------------- #
# 第二章 全球信息
# --------------------------------------------------------------------------- #
def _chapter_world(ctx: dict) -> list[str]:
    lines = ["## 二、全球信息", ""]

    lines += ["### 2.1 全球市场快照", ""]
    if ctx["markets"]:
        rows = [
            (
                _s(row.get("name"), 24),
                _s(row.get("group"), 12, "未分组"),
                _num_asset(row.get("price")),
                _pct(row.get("change_pct")),
            )
            for row in ctx["markets"]
        ]
        lines += _table(["资产", "分组", "最新", "涨跌幅"], rows)
        lines.append("")
    else:
        lines += ["> 数据缺失：今日未获取到全球市场快照。", ""]

    lines += ["### 2.2 分类要闻", ""]
    news = ctx["news"]
    if not news and not ctx["by_category"]:
        lines += ["> 数据缺失：今日未获取到全球要闻。", ""]
        return lines

    counts: dict[str, int] = {}
    for item in news:
        name = _text(item.get("category"), 20) or "综合"
        counts[name] = counts.get(name, 0) + 1

    order: list[str] = []
    total_by_cat: dict[str, int] = {}
    for row in ctx["by_category"]:
        name = _text(row.get("name"), 20)
        if not name or name in total_by_cat:
            continue
        declared = safe_float(row.get("count"))
        total_by_cat[name] = max(int(declared) if declared is not None else 0, counts.get(name, 0))
        order.append(name)
    for name, count in counts.items():
        if name not in total_by_cat:
            total_by_cat[name] = count
            order.append(name)

    for category in order:
        items = [item for item in news if (_text(item.get("category"), 20) or "综合") == category]
        items.sort(key=lambda item: safe_float(item.get("heat"), 0.0) or 0.0, reverse=True)
        lines.append(f"#### {category}（{total_by_cat.get(category, len(items))} 条）")
        lines.append("")
        if not items:
            lines += [f"{EMPTY}。", ""]
            continue
        for item in items[:MAX_NEWS_PER_CAT]:
            title = _link(item.get("title"), item.get("url"), 90)
            source = _s(item.get("source"), 24, "来源未知")
            when = _fmt_time(item.get("published"), ctx["report_date"])
            lines.append(f"- {title} — {source} · {when}")
        lines.append("")
    return lines


# --------------------------------------------------------------------------- #
# 第三章 A股市场
# --------------------------------------------------------------------------- #
def _chapter_ashare(ctx: dict) -> list[str]:
    lines = ["## 三、A股市场", ""]

    # 3.1 指数与市场宽度
    lines += ["### 3.1 指数与市场宽度", ""]
    if ctx["indices"]:
        rows = [
            (
                _s(index.get("name"), 20),
                _num(index.get("price")),
                _num_signed(index.get("change")),
                _pct(index.get("change_pct")),
                _amt(index.get("amount")),
            )
            for index in ctx["indices"]
        ]
        lines += _table(["指数", "最新", "涨跌", "涨跌幅", "成交额"], rows)
        lines.append("")
    else:
        lines += [f"> {MISSING}：今日未获取到指数行情。", ""]

    breadth = ctx["breadth"]
    if isinstance(breadth, dict) and any(
        breadth.get(key) is not None
        for key in ("total", "up", "down", "flat", "limit_up", "limit_down", "up_ratio")
    ):
        ratio = _breadth_ratio(breadth)
        lines.append(
            f"**市场宽度**：全市场 {_int(breadth.get('total'))} 家，"
            f"上涨 {_int(breadth.get('up'))} 家、下跌 {_int(breadth.get('down'))} 家、"
            f"平盘 {_int(breadth.get('flat'))} 家，涨停 {_int(breadth.get('limit_up'))} 家、"
            f"跌停 {_int(breadth.get('limit_down'))} 家，上涨占比 {_pct_plain(ratio)}"
            f"（{_breadth_tone(ratio)}）。"
        )
        lines.append("")
    else:
        lines += [f"> {MISSING}：今日未获取到市场宽度统计。", ""]

    # 3.2 板块轮动（领涨只列 >0、领跌只列 <0；上游 top/bottom 反转时给一致文案）
    lines += ["### 3.2 板块轮动", ""]
    valid_sectors, gainers, losers = _sector_view(ctx)
    if not valid_sectors:
        lines += [f"> {MISSING}：今日未获取到板块行情。", ""]
    else:
        if gainers:
            lines.append("**领涨板块**")
            lines.append("")
            rows = [
                (
                    _s(row.get("name"), 20),
                    _pct(change),
                    _sector_counts(row),
                    _s(row.get("leader_name"), 20, "—"),
                    _pct(row.get("leader_change_pct")),
                )
                for row, change in gainers[:5]
            ]
            lines += _table(["板块", "涨跌幅", "涨/跌家数", "领涨股", "领涨股涨跌幅"], rows)
            lines.append("")
        else:
            row, change = max(valid_sectors, key=lambda pair: pair[1])
            lines += [
                f"**领涨板块**：今日无上涨板块（表现最好：{_s(row.get('name'), 20)} {_pct(change)}）。",
                "",
            ]
        if losers:
            lines.append("**领跌板块**")
            lines.append("")
            rows = [
                (
                    _s(row.get("name"), 20),
                    _pct(change),
                    _sector_counts(row),
                    _s(row.get("leader_name"), 20, "—"),
                    _pct(row.get("leader_change_pct")),
                )
                for row, change in losers[:5]
            ]
            lines += _table(["板块", "涨跌幅", "涨/跌家数", "最强成分股", "其涨跌幅"], rows)
            lines.append("")
        else:
            row, change = min(valid_sectors, key=lambda pair: pair[1])
            lines += [
                f"**领跌板块**：今日无下跌板块（涨幅最小：{_s(row.get('name'), 20)} {_pct(change)}）。",
                "",
            ]
        feature: list[str] = []
        if gainers:
            feature.append(
                "资金偏好集中于 " + "、".join(_s(row.get("name"), 16) for row, _ in gainers[:3])
            )
        if losers:
            feature.append(
                "调整压力集中于 " + "、".join(_s(row.get("name"), 16) for row, _ in losers[:3])
            )
        else:
            feature.append("今日无下跌板块")
        lines += [f"**轮动特征**：{'；'.join(feature)}。", ""]

    # 3.3 个股榜单
    lines += ["### 3.3 涨幅榜 / 跌幅榜 / 成交额榜", ""]
    boards = [
        ("涨幅榜", ctx["gainers"]),
        ("跌幅榜", ctx["losers"]),
        ("成交额榜", ctx["turnover"]),
    ]
    for label, items in boards:
        lines.append(f"**{label}（前 {MOVER_COUNT}）**")
        lines.append("")
        if not items:
            lines += [f"> {MISSING}：今日未获取到{label}数据。", ""]
            continue
        rows = [
            (
                _s(item.get("code"), 12, "—"),
                _s(item.get("name"), 16),
                _num(item.get("price")),
                _pct(item.get("change_pct")),
                _amt(item.get("amount")),
                _pct(item.get("turnover"), sign=False),
            )
            for item in items[:MOVER_COUNT]
        ]
        lines += _table(["代码", "名称", "现价", "涨跌幅", "成交额", "换手率"], rows)
        lines.append("")

    # 3.4 涨停池
    lines += ["### 3.4 涨停池", ""]
    pool = ctx["limit_up_pool"]
    if pool:
        limit_up = _int(_get(breadth, "limit_up", default=None)) if isinstance(breadth, dict) else MISSING
        names = ""
        if limit_up not in (MISSING, "0"):
            names = f"（宽度统计显示涨停 {limit_up} 家）"
        lines.append(
            f"涨停池共 {len(pool)} 只{names}，下表为前 {min(len(pool), LIMIT_UP_TOP)} 只。"
        )
        lines.append("")
        rows = [
            (
                _s(item.get("code"), 12, "—"),
                _s(item.get("name"), 16),
                _pct(item.get("change_pct")),
                _fmt_seal_time(item.get("first_seal_time")),
                _int(_boards(item)) if _boards(item) is not None else "—",
                _s(
                    item.get("reason")
                    or item.get("hybk")
                    or item.get("sector")
                    or item.get("industry"),
                    20,
                    "—",
                ),
            )
            for item in pool[:LIMIT_UP_TOP]
        ]
        lines += _table(["代码", "名称", "涨跌幅", "首封时间", "连板", "行业/原因"], rows)
        lines.append("")
    else:
        lines += [f"> {MISSING}：今日涨停池为空或未获取到。", ""]
    return lines


# --------------------------------------------------------------------------- #
# 第四章 自选股日报
# --------------------------------------------------------------------------- #
def _alert_level(level) -> str:
    text = _text(level, 12).lower()
    return {"high": "高", "warn": "警示", "warning": "警示", "info": "提示"}.get(text, _s(level, 12))


def _stock_quote_line(stock: dict) -> str:
    quote = _get(stock, "quote")
    quote = quote if isinstance(quote, dict) else {}
    if not any(
        safe_float(quote.get(key)) is not None
        for key in ("price", "change", "amount", "turnover", "pe", "market_cap")
    ):
        return f"- 行情：{MISSING}（该标的行情未获取）"
    currency = _text(stock.get("currency") or "CNY", 8).upper()
    price_unit = " USD" if currency == "USD" else ""
    parts = [
        f"现价 {_num(quote.get('price'))}{price_unit}",
        f"涨跌 {_num_signed(quote.get('change'))}",
        f"最高 {_num(quote.get('high'))}",
        f"最低 {_num(quote.get('low'))}",
    ]
    # A股专属字段（美股等备用源可能没有）：有值才展示，避免「数据缺失」刷屏
    if safe_float(quote.get("amount")) is not None:
        parts.append(f"成交额 {_amt(quote.get('amount'))}")
    if safe_float(quote.get("turnover")) is not None:
        parts.append(f"换手 {_pct(quote.get('turnover'), sign=False)}")
    if safe_float(quote.get("volume_ratio")) is not None:
        parts.append(f"量比 {_num(quote.get('volume_ratio'))}")
    if safe_float(quote.get("pe")) is not None:
        parts.append(f"PE(动) {_num(quote.get('pe'))}")
    if safe_float(quote.get("market_cap")) is not None:
        cap = safe_float(quote.get("market_cap"))
        if currency == "USD":
            cap_text = f"{cap / 1e12:.2f}万亿USD" if abs(cap) >= 1e12 else f"{cap / 1e8:.2f}亿USD"
        else:
            cap_text = _amt(quote.get("market_cap"))
        parts.append(f"总市值 {cap_text}")
    return "- 行情：" + " ｜ ".join(parts)


def _stock_fund_line(stock: dict) -> str:
    currency = _text(stock.get("currency") or "CNY", 8).upper()
    market = _text(stock.get("market") or "", 8).upper()
    if currency == "USD" or market == "US":
        return "- 资金流：美股暂不采集主力资金流（接口不支持日频资金流）"
    flow = _get(stock, "fund_flow")
    flow = flow if isinstance(flow, dict) else {}
    today_net = _amt(flow.get("today_main_net"))
    recent = flow.get("recent") if isinstance(flow.get("recent"), list) else []
    recent = [row for row in recent if isinstance(row, dict)]
    if not recent:
        return f"- 资金流：主力净流入 {today_net} ｜ 近期明细{MISSING}"
    values = [safe_float(row.get("main_net")) for row in recent]
    known = [value for value in values if value is not None]
    if not known:
        return f"- 资金流：主力净流入 {today_net} ｜ 近期明细{MISSING}"
    total = sum(known)
    inflow_days = sum(1 for value in known if value > 0)
    outflow_days = sum(1 for value in known if value < 0)
    return (
        f"- 资金流：主力净流入 {today_net} ｜ "
        f"近 {len(known)} 个交易日合计 {_amt(total)}"
        f"（{inflow_days} 天净流入 / {outflow_days} 天净流出）"
    )


def _link_list(items: list[dict], title_key: str, url_key: str = "url",
               limit: int = 3, extra_keys: tuple[str, ...] = ()) -> str:
    lines: list[str] = []
    for item in items[:limit]:
        title = _link(item.get(title_key), item.get(url_key), 60)
        extras: list[str] = []
        for key in extra_keys:
            raw = item.get(key)
            if not _text(raw):
                continue
            extras.append(_fmt_date_any(raw) if key in ("date", "published", "time")
                          else _s(raw, 20))
        lines.append(title + (f"（{' · '.join(extras)}）" if extras else ""))
    return "；".join(lines)


def _chapter_watchlist(ctx: dict) -> list[str]:
    lines = ["## 四、自选股日报", ""]
    watchlist = ctx["watchlist"]
    if not watchlist:
        lines += [f"> {MISSING}：今日未获取到自选股数据。", ""]
        return lines

    stats = _watchlist_stats(watchlist, ctx["watchlist_meta"])
    if ctx["watchlist_meta"] or watchlist:
        lines.append(
            f"> 自选股共 {stats['count']} 只，"
            f"平均涨跌 {_pct(stats['avg'])} ｜ 有效行情 {len(stats['known'])} 只"
            + (f" ｜ 异动提示 {len(ctx['alerts'])} 条" if ctx["alerts"] else "")
        )
        lines.append("")

    for stock in watchlist:
        code = _s(stock.get("code"), 12, "代码缺失")
        name = _s(stock.get("name"), 20, "名称缺失")
        tags = stock.get("tags")
        tag_text = " · ".join(
            _text(tag, 12) for tag in tags if _text(tag, 12)
        ) if isinstance(tags, list) else ""
        change = safe_float(_get(stock, "quote", "change_pct"))
        heading = f"### {code} {name}"
        if tag_text:
            heading += f" ｜ {tag_text}"
        heading += f" ｜ {_pct(change) if change is not None else MISSING}"
        lines += [heading, ""]
        lines.append(_stock_quote_line(stock))
        lines.append(_stock_fund_line(stock))

        announcements = _dicts(stock, "announcements")
        if announcements:
            lines.append(f"- 公告：{_link_list(announcements, 'title', extra_keys=('date',))}")
        else:
            lines.append(f"- 公告：{EMPTY}")

        news = _dicts(stock, "news")
        if news:
            lines.append(
                f"- 新闻：{_link_list(news, 'title', extra_keys=('source', 'date'))}"
            )
        else:
            lines.append(f"- 新闻：{EMPTY}")

        reports = _dicts(stock, "reports")
        if reports:
            lines.append(
                f"- 研报：{_link_list(reports, 'title', extra_keys=('org', 'date'))}"
            )
        else:
            lines.append(f"- 研报：{EMPTY}")

        alerts = _visible_alerts(_dicts(stock, "alerts"))
        if alerts:
            alert_text = "；".join(
                f"{_s(alert.get('text'), 80)}（{_alert_level(alert.get('level'))}）"
                for alert in alerts[:4]
            )
            lines.append(f"- 提示：{alert_text}")
        else:
            lines.append("- 提示：无异常")
        lines.append("")
    return lines


# --------------------------------------------------------------------------- #
# 第五章 数据质量
# --------------------------------------------------------------------------- #
def _chapter_quality(ctx: dict) -> list[str]:
    lines = ["## 五、数据质量", ""]
    run = ctx["run"]
    run_bits: list[str] = []
    if run.get("duration_sec") is not None:
        run_bits.append(f"耗时 {_seconds(run.get('duration_sec'))}")
    if run.get("requests") is not None:
        run_bits.append(f"请求 {_int(run.get('requests'))} 次")
    if run.get("errors") is not None:
        run_bits.append(f"错误 {_int(run.get('errors'))} 次")
    offline = run.get("offline")
    if offline is not None:
        run_bits.append(f"离线模式：{'是' if offline is True else '否'}")
    if run_bits:
        lines += [f"**本次采集**：{' ｜ '.join(run_bits)}", ""]

    lines.append(
        f"**数据源状态**：共 {len(ctx['sources'])} 个，"
        f"成功 {ctx['ok_count']} 个 / 失败 {ctx['fail_count']} 个。"
    )
    lines.append("")
    if ctx["sources"]:
        rows = []
        for source in ctx["sources"]:
            status = source.get("ok")
            status_text = "正常" if status is True else ("失败" if status is False else "未知")
            count = source.get("count")
            rows.append(
                (
                    _s(source.get("name") or source.get("id"), 28, "未命名源"),
                    status_text,
                    _int(count) if count is not None else "—",
                    _elapsed_ms(source.get("elapsed_ms")),
                    _s(source.get("error"), 40, "—"),
                )
            )
        lines += _table(["数据源", "状态", "条数", "耗时", "备注"], rows)
        lines.append("")
    else:
        lines += [f"> {MISSING}：未获取到数据源状态。", ""]

    lines += ["**错误列表**", ""]
    if ctx["errors"]:
        for error in ctx["errors"]:
            stage = _s(error.get("stage"), 12, "未知阶段")
            source = _s(error.get("source"), 24, "未知来源")
            message = _s(error.get("error"), 100, MISSING)
            lines.append(f"- [{stage}] {source}：{message}")
    else:
        lines.append("无采集错误。")
    lines.append("")

    lines += ["**缺失说明**", ""]
    missing: list[str] = []
    if not ctx["news"]:
        missing.append("全球要闻（0 条）")
    if not ctx["markets"]:
        missing.append("全球市场快照（0 个）")
    if not ctx["indices"]:
        missing.append("A股指数（0 个）")
    if ctx["breadth"] is None:
        missing.append("市场宽度")
    if not ctx["sectors_top"] and not ctx["sectors_bottom"]:
        missing.append("板块行情")
    if not (ctx["gainers"] or ctx["losers"] or ctx["turnover"]):
        missing.append("个股榜单")
    if not ctx["limit_up_pool"]:
        missing.append("涨停池")
    if not ctx["watchlist"]:
        missing.append("自选股")
    if not ctx["sources"]:
        missing.append("数据源状态")
    if missing:
        for item in missing:
            lines.append(f"- {item}：{MISSING}")
    else:
        lines.append("- 各主要区块均有数据。")
    lines.append("")
    return lines


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def build(data: dict, out_path: str, notes: str = "") -> str:
    """生成中文 Markdown 日报，写入 out_path，返回绝对路径。"""
    ctx = _context(data)
    report_date = ctx["report_date"] or "日期未知"

    header = [
        f"# 每日情报雷达 · {report_date}",
        "",
        f"> 生成时间：{_time_cn(ctx['generated_at'])} ｜ "
        f"数据窗口：{report_date} ｜ "
        f"{_header_source_summary(ctx)}",
        ">",
        f"> 一句话结论：{_one_liner(ctx)}",
        ">",
        "> 说明：本简报由程序依据当日采集数据自动生成，仅为信息整理，不构成投资建议。",
        "",
    ]

    points = _chapter_points(ctx)
    body = ["## 一、今日要点", ""]
    for idx, point in enumerate(points, 1):
        body.append(f"{idx}. {point}")
    body.append("")

    chapters = header + body
    chapters += _chapter_world(ctx)
    chapters += _chapter_ashare(ctx)
    chapters += _chapter_watchlist(ctx)
    chapters += _chapter_quality(ctx)

    note_text = (notes or "").strip().lstrip("\ufeff").strip()
    if note_text:
        chapters += ["## 编辑备注", ""]
        chapters += [line.rstrip() for line in note_text.splitlines()]
        chapters.append("")

    document = _sanitize_doc("\n".join(chapters).rstrip() + "\n")
    write_text(out_path, document)
    return os.path.abspath(out_path)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 - console encoding is best-effort
        pass
    parser = argparse.ArgumentParser(description="每日情报雷达 · 中文 Markdown 日报生成")
    parser.add_argument("--data", required=True, help="data.json 路径")
    parser.add_argument("--out", required=True, help="输出 brief.md 路径")
    parser.add_argument("--notes", default="", help="编辑备注文本（若为已存在文件则读取其内容）")
    args = parser.parse_args(argv)

    data = load_json(args.data)
    if not isinstance(data, dict):
        sys.stderr.write(f"[brief] 无法读取或解析 data.json：{args.data}\n")
        return 2
    notes = args.notes or ""
    if notes and os.path.isfile(notes):
        notes = load_text(notes, "")
    out = build(data, args.out, notes=notes)
    size_kb = os.path.getsize(out) / 1024.0
    print(f"[brief] 已生成 {out}（{size_kb:.1f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
