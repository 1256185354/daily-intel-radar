# -*- coding: utf-8 -*-
"""intel/src/run_daily.py — Daily Intel Radar orchestrator.

Pipeline
--------
1. sources_world.collect(date, cfg)      -> world 段（全球要闻/市场/热搜/源状态）
2. sources_ashare.collect(date, wl, cfg) -> ashare 段（指数/宽度/板块/榜/自选股）
3. 组装并规范化 data.json（契约 §4），非法浮点/NaN 清洗为 null
4. build_dashboard.build(...)            -> 单文件离线看板 + 归档页
5. build_report.build(...)               -> 中文每日简报
6. 写 run.log 并打印摘要

任何单个模块/数据源失败都不会中断流水线：缺什么就降级为什么（空数组/null），
并在 data.json 的 errors / sources 中留下痕迹。

Usage
-----
  python src/run_daily.py
  python src/run_daily.py --date 2026-11-18
  python src/run_daily.py --offline                      # 不联网，验证下游渲染健壮性
  python src/run_daily.py --out-root intel/verify/tmp/data
  python src/run_daily.py --stocks 600519,300848
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
INTEL_ROOT = os.path.dirname(HERE)
DEFAULT_DATA_ROOT = os.path.join(INTEL_ROOT, "data")
CONFIG_DIR = os.path.join(INTEL_ROOT, "config")

sys.path.insert(0, HERE)
import common  # noqa: E402

LEVEL_ORDER = {"high": 0, "warn": 1, "info": 2}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _cfg() -> dict:
    return common.load_json(os.path.join(CONFIG_DIR, "sources.json"), {}) or {}


def _watchlist(path: str | None = None, only_codes: str | None = None) -> list[dict]:
    if only_codes:
        base = {s["code"]: s for s in common.load_watchlist(path or os.path.join(CONFIG_DIR, "watchlist.json"))}
        out = []
        for code in [c.strip() for c in only_codes.split(",") if c.strip()]:
            if code in base:
                out.append(base[code])
            else:
                out.append({"code": code, "name": code, "tags": [], "note": "",
                            "secid": common.secid(code), "market": common.market_label(code)})
        return out
    return common.load_watchlist(path or os.path.join(CONFIG_DIR, "watchlist.json"))


def _load_module(name: str):
    """Import an optional sibling module; never raises."""
    try:
        return importlib.import_module(name), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def _sanitize(obj):
    """Make obj strictly JSON-serializable (NaN/Inf -> None, datetimes -> ISO)."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {str(k): _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_sanitize(v) for v in obj]
    if hasattr(obj, "isoformat"):
        try:
            return common.iso(obj)
        except Exception:  # noqa: BLE001
            return str(obj)
    return obj


def _int(v, default=0) -> int:
    f = common.safe_float(v)
    return int(f) if f is not None else default


# --------------------------------------------------------------------------- #
# empty / normalized structures (contract §4)
# --------------------------------------------------------------------------- #
def _empty_world(cfg: dict) -> dict:
    return {"news": [], "by_category": [], "markets": [], "social": [], "sources": []}


def _empty_ashare(watchlist: list[dict]) -> dict:
    cfg = _cfg()
    idx = [{"code": i["secid"].split(".")[-1], "name": i["name"], "price": None,
            "change": None, "change_pct": None, "amount": None}
           for i in ((cfg.get("ashare") or {}).get("indices") or [])]
    return {
        "indices": idx,
        "breadth": {"total": None, "up": None, "down": None, "flat": None,
                    "limit_up": None, "limit_down": None, "up_ratio": None},
        "sectors": {"top": [], "bottom": []},
        "movers": {"gainers": [], "losers": [], "turnover": []},
        "limit_up_pool": [],
        "watchlist": [{
            "code": s["code"], "name": s["name"], "market": s["market"], "tags": s["tags"],
            "note": s["note"], "quote": None, "kline": [], "fund_flow": None,
            "announcements": [], "news": [], "reports": [], "alerts": [],
        } for s in watchlist],
        "watchlist_meta": {"count": len(watchlist), "avg_change_pct": None,
                           "best": None, "worst": None, "total_amount": None},
        "sources": [],
    }


def _normalize_news(items, stage_errors: list, stage: str) -> list[dict]:
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        item = {
            "id": it.get("id") or "",
            "title": common.clean_text(it.get("title")).replace("\n", " ").strip(),
            "summary": common.truncate(it.get("summary"), 300).replace("\n", " ").strip(),
            "url": (it.get("url") or "").strip(),
            "source": it.get("source") or "",
            "source_id": it.get("source_id") or "",
            "published": it.get("published"),
            "category": it.get("category") or common.DEFAULT_CATEGORY,
            "tags": it.get("tags") or [],
            "heat": common.safe_float(it.get("heat"), 0.0) or 0.0,
        }
        if not item["title"]:
            continue
        if not item["id"]:
            item["id"] = _news_id(item["source_id"], item["title"])
        out.append(item)
    return out


def _news_id(source_id: str, title: str) -> str:
    import hashlib
    return hashlib.sha1(f"{source_id}|{title}".encode("utf-8")).hexdigest()[:10]


def _normalize_world(raw: dict) -> dict:
    raw = raw or {}
    news = _normalize_news(raw.get("news"), [], "world")
    news.sort(key=lambda x: x.get("heat", 0), reverse=True)
    by_cat: dict[str, int] = {}
    for n in news:
        by_cat[n["category"]] = by_cat.get(n["category"], 0) + 1
    return {
        "news": news,
        "by_category": sorted([{"name": k, "count": v} for k, v in by_cat.items()],
                              key=lambda x: -x["count"]),
        "markets": [m for m in (raw.get("markets") or []) if isinstance(m, dict)],
        "social": [s for s in (raw.get("social") or []) if isinstance(s, dict)],
        "sources": [s for s in (raw.get("sources") or []) if isinstance(s, dict)],
    }


def _normalize_ashare(raw: dict, watchlist: list[dict]) -> dict:
    raw = raw or {}
    empty = _empty_ashare(watchlist)
    result = dict(empty)
    for key in ("indices", "limit_up_pool", "sources", "watchlist"):
        if isinstance(raw.get(key), list):
            result[key] = raw[key]
    for key in ("breadth", "sectors", "movers", "watchlist_meta"):
        if isinstance(raw.get(key), dict):
            result[key] = {**empty.get(key, {}), **raw[key]}
    result["sectors"] = {"top": (result["sectors"].get("top") or [])[:20],
                         "bottom": (result["sectors"].get("bottom") or [])[:10]}
    for group in ("gainers", "losers", "turnover"):
        result["movers"][group] = result["movers"].get(group) or []
    # fill missing quote/watchlist fields so downstream never KeyErrors
    wl_out = []
    seen = set()
    for stock in result["watchlist"]:
        if not isinstance(stock, dict) or not stock.get("code"):
            continue
        code = str(stock["code"])
        if code in seen:
            continue
        seen.add(code)
        base = next((w for w in watchlist if w["code"] == code), None)
        market = stock.get("market") or (base or {}).get("market") or common.market_label(code)
        currency = (
            stock.get("currency")
            or (base or {}).get("currency")
            or ("USD" if str(market).upper() == "US" else "CNY")
        )
        wl_out.append({
            "code": code,
            "name": stock.get("name") or (base or {}).get("name") or code,
            "market": market,
            "currency": currency,
            "tags": stock.get("tags") or (base or {}).get("tags") or [],
            "note": stock.get("note") or (base or {}).get("note") or "",
            "quote": stock.get("quote") if isinstance(stock.get("quote"), dict) else None,
            "kline": stock.get("kline") or [],
            "fund_flow": stock.get("fund_flow") if isinstance(stock.get("fund_flow"), dict) else None,
            "announcements": stock.get("announcements") or [],
            "news": stock.get("news") or [],
            "reports": stock.get("reports") or [],
            "alerts": stock.get("alerts") or [],
        })
    if not wl_out:  # collector returned nothing at all -> keep configured shells
        result["watchlist"] = empty["watchlist"]
    else:
        result["watchlist"] = wl_out
    return result


def _merge_news(world: dict, old_world: dict | None, extra_items: list[dict], cfg: dict) -> dict:
    """合并：当天旧快照里的新闻 + 重点公司关键词回查结果（去重），并重算分类/热度。

    背景：新闻流接口只返回「最新 50 条」，同一天多次运行（早间→收盘→手动）会丢掉早先抓到的重要新闻，
    这里把旧快照与关键词回查结果保留下来，避免「Anthropic 早上抓到了、晚上重跑就没了」。
    """
    import re

    def fix_highlight_spacing(text: str) -> str:
        """修复搜索高亮标签被 strip 后留下的字母间空格：'An thr opi c' -> 'Anthropic'。

        仅当标题里出现 ≥2 处「字母-空格-字母」时才合并（避免误伤 'US CPI' 这类正常空格）。
        """
        s = str(text or "")
        if len(re.findall(r"(?<=[A-Za-z])\s+(?=[A-Za-z])", s)) >= 2:
            s = re.sub(r"(?<=[A-Za-z])\s+(?=[A-Za-z])", "", s)
        return s

    news = list(world.get("news") or [])
    for n in news:
        n["title"] = fix_highlight_spacing(n.get("title"))
    raw_extras = list((old_world or {}).get("news") or []) + list(extra_items or [])
    extras = []
    for n in raw_extras:
        if isinstance(n, dict):
            n = dict(n)
            n["title"] = fix_highlight_spacing(n.get("title"))
            extras.append(n)

    def norm_title(s: str) -> str:
        return re.sub(r"[\s\W_]+", "", str(s or "")).lower()[:60]

    ids = {str(n.get("id") or "") for n in news}
    titles = {norm_title(n.get("title")) for n in news}
    added = 0
    for item in extras:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        iid = str(item.get("id") or "")
        ntitle = norm_title(item.get("title"))
        if (iid and iid in ids) or (ntitle and ntitle in titles):
            continue
        item = dict(item)
        if not item.get("id"):
            item["id"] = _news_id(item.get("source_id") or "merged", item.get("title") or "")
        news.append(item)
        ids.add(str(item["id"]))
        titles.add(ntitle)
        added += 1
    if added:
        common.log(f"merge: 合并历史/回查新闻 {added} 条")
    # 重新统一打分（分类/热度），保证新旧条目同一口径
    common.score_items(news, cfg, ref_time=common.now_cn())
    news.sort(key=lambda x: common.safe_float(x.get("heat"), 0.0) or 0.0, reverse=True)
    world["news"] = news
    counts: dict[str, int] = {}
    for n in news:
        cat = n.get("category") or common.DEFAULT_CATEGORY
        counts[cat] = counts.get(cat, 0) + 1
    world["by_category"] = sorted(
        [{"name": k, "count": v} for k, v in counts.items()], key=lambda x: -x["count"])
    return world


def _focus_lookup(cfg: dict, existing: list[dict] | None = None) -> list[dict]:
    """按「重点公司」关键词回查东财新闻（近 N 天），补齐新闻流可能漏掉/被挤掉的重点报道。"""
    focus_cfg = (cfg or {}).get("focus") or {}
    keywords = [str(k).strip() for k in (focus_cfg.get("lookup_keywords") or []) if str(k).strip()]
    days = int(common.safe_float(focus_cfg.get("lookup_days"), 3) or 3)
    if not keywords:
        return []
    import urllib.parse
    deadline = common.now_cn().timestamp() - days * 86400
    seen = set()
    for n in existing or []:
        seen.add((n.get("title") or "").strip())
    out: list[dict] = []
    for kw in keywords:
        try:
            param = {
                "uid": "", "keyword": kw, "type": ["cmsArticleWebOld"],
                "client": "web", "clientType": "web", "clientVersion": "curr",
                "param": {"cmsArticleWebOld": {
                    "searchScope": "default", "sort": "time", "pageIndex": 1, "pageSize": 8,
                    "preTag": "<em>", "postTag": "</em>"}},
            }
            url = ("https://search-api-web.eastmoney.com/search/jsonp?cb=cb&param="
                   + urllib.parse.quote(json.dumps(param, ensure_ascii=False)))
            data = common.http_get_json(url, retries=1, timeout=10, backoff=1.5)
            arts = (((data.get("result") or {}).get("cmsArticleWebOld")) or [])
            for art in arts:
                raw_title = str(art.get("title") or "")
                for _tag in ("<em>", "</em>", "<b>", "</b>", "<strong>", "</strong>"):
                    raw_title = raw_title.replace(_tag, "")
                title = common.clean_text(raw_title).replace("\n", " ").strip()
                if not title or title in seen:
                    continue
                published = common.parse_dt_any(art.get("date"))
                if published is None or published.timestamp() < deadline:
                    continue
                seen.add(title)
                out.append({
                    "id": _news_id("focus_lookup", title),
                    "title": title,
                    "summary": common.truncate(art.get("content"), 200),
                    "url": (art.get("url") or "").strip(),
                    "source": common.clean_text(art.get("mediaName")) or "东方财富",
                    "source_id": "focus_lookup",
                    "published": common.iso(published),
                    "category": common.DEFAULT_CATEGORY,
                    "tags": ["焦点"],
                    "heat": 0.0,
                })
        except Exception as exc:  # noqa: BLE001
            common.log(f"focus lookup「{kw}」失败: {type(exc).__name__}: {exc}")
        continue
    if out:
        common.log(f"focus lookup: 回查补入 {len(out)} 条（关键词 {len(keywords)} 个）")
    return out


def _apply_focus(world: dict, cfg: dict) -> dict:
    """焦点事件识别：重点公司（强）+ 重大事件词（次强）加权并打「焦点」标签。

    news[].focus = true / focus_company = true / focus_score / focus_hits，
    heat 按 company/event 命中数加权后重排；dashboard 与简报按 focus_score 优先展示。
    """
    focus_cfg = (cfg or {}).get("focus") or {}
    events = [str(k) for k in (focus_cfg.get("event_keywords") or []) if str(k).strip()]
    companies = [str(k) for k in (focus_cfg.get("companies") or []) if str(k).strip()]
    if not events and not companies:
        return world
    c_boost = common.safe_float(focus_cfg.get("company_heat_boost"), 1.9) or 1.9
    e_boost = common.safe_float(focus_cfg.get("event_heat_boost"), 0.45) or 0.45
    news = world.get("news") or []
    for item in news:
        blob = f"{item.get('title', '')} {item.get('summary', '')}"
        low = blob.lower()
        c_hits = [c for c in companies if c.lower() in low]
        e_hits = [e for e in events if e.lower() in low and e.lower() not in {h.lower() for h in c_hits}]
        if not c_hits and not e_hits:
            continue
        item["focus"] = True
        item["focus_company"] = bool(c_hits)
        item["focus_hits"] = (c_hits + e_hits)[:5]
        item["focus_score"] = 3 * min(len(c_hits), 2) + 1 * min(len(e_hits), 2)
        heat = common.safe_float(item.get("heat"), 0.0) or 0.0
        item["heat"] = round(heat * (1 + c_boost * min(len(c_hits), 2) + e_boost * min(len(e_hits), 2)), 1)
        tags = [t for t in (item.get("tags") or []) if t != "焦点"]
        item["tags"] = ["焦点"] + tags[:2]
    news.sort(key=lambda x: common.safe_float(x.get("heat"), 0.0) or 0.0, reverse=True)
    world["news"] = news
    focus_news = [n for n in news if n.get("focus")]
    world["focus_count"] = len(focus_news)
    world["focus_company_count"] = sum(1 for n in focus_news if n.get("focus_company"))
    common.log(f"focus: 焦点新闻 {world['focus_count']} 条（其中重点公司 {world['focus_company_count']} 条）")
    return world


def _collect_errors(world: dict, ashare: dict) -> list[dict]:
    errs = []
    for stage, seg in (("world", world), ("ashare", ashare)):
        for src in seg.get("sources") or []:
            if isinstance(src, dict) and src.get("ok") is False:
                errs.append({"source": src.get("id") or src.get("name") or "unknown",
                             "stage": stage,
                             "error": str(src.get("error") or "unknown error")[:300]})
    return errs


# --------------------------------------------------------------------------- #
# collection stages
# --------------------------------------------------------------------------- #
def _run_world(date_str: str, cfg: dict, offline: bool) -> tuple[dict, list[dict]]:
    if offline:
        return _empty_world(cfg), []
    mod, err = _load_module("sources_world")
    if mod is None:
        common.log(f"sources_world 不可用: {err}")
        return _empty_world(cfg), [{"source": "sources_world", "stage": "world", "error": err}]
    t0 = time.time()
    try:
        raw = mod.collect(date_str, cfg) or {}
        common.log(f"world 采集完成 {time.time() - t0:.1f}s")
        return _normalize_world(raw), []
    except Exception as exc:  # noqa: BLE001
        common.log(f"sources_world 异常: {exc}")
        return _empty_world(cfg), [{"source": "sources_world", "stage": "world",
                                    "error": f"{type(exc).__name__}: {exc}"}]


def _run_ashare(date_str: str, watchlist: list[dict], cfg: dict, offline: bool) -> tuple[dict, list[dict]]:
    if offline:
        return _empty_ashare(watchlist), []
    mod, err = _load_module("sources_ashare")
    if mod is None:
        common.log(f"sources_ashare 不可用: {err}")
        return _empty_ashare(watchlist), [{"source": "sources_ashare", "stage": "ashare", "error": err}]
    t0 = time.time()
    try:
        raw = mod.collect(date_str, watchlist, cfg) or {}
        common.log(f"ashare 采集完成 {time.time() - t0:.1f}s")
        return _normalize_ashare(raw, watchlist), []
    except Exception as exc:  # noqa: BLE001
        common.log(f"sources_ashare 异常: {exc}")
        return _empty_ashare(watchlist), [{"source": "sources_ashare", "stage": "ashare",
                                           "error": f"{type(exc).__name__}: {exc}"}]


# --------------------------------------------------------------------------- #
# archive
# --------------------------------------------------------------------------- #
def _scan_archive(data_root: str) -> list[dict]:
    items = []
    try:
        names = sorted(os.listdir(data_root), reverse=True)
    except OSError:
        return items
    for name in names:
        if not (len(name) == 10 and name[4] == "-" and name[7] == "-"):
            continue
        path = os.path.join(data_root, name, "data.json")
        d = common.load_json(path, {}) or {}
        world = d.get("world") or {}
        ash = d.get("ashare") or {}
        cats = world.get("by_category") or []
        top = max(cats, key=lambda c: c.get("count", 0))["name"] if cats else "—"
        items.append({
            "date": name,
            "news_count": len(world.get("news") or []),
            "watchlist_avg": (ash.get("watchlist_meta") or {}).get("avg_change_pct"),
            "top_category": top,
        })
    return items


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="每日情报雷达 — 采集/看板/简报 一键流水线")
    ap.add_argument("--date", default=None, help="数据归属日 YYYY-MM-DD，默认今天（Asia/Shanghai）")
    ap.add_argument("--out-root", default=None, help="数据根目录，默认 intel/data")
    ap.add_argument("--offline", action="store_true", help="不联网，产出空结构（用于下游健壮性测试）")
    ap.add_argument("--stocks", default=None, help="临时覆盖自选股，如 600519,300848")
    ap.add_argument("--watchlist", default=None, help="自选股配置路径")
    ap.add_argument("--skip-dashboard", action="store_true")
    ap.add_argument("--skip-report", action="store_true")
    ap.add_argument("--skip-share", action="store_true", help="不生成手机包/不同步 OneDrive")
    ap.add_argument("--skip-push", action="store_true", help="不发送邮件推送")
    ap.add_argument("--skip-charts", action="store_true", help="不采集分时/日线图表数据")
    ap.add_argument("--skip-publish", action="store_true", help="不发布到 GitHub Pages")
    args = ap.parse_args(argv)

    t_start = time.time()
    date_str = args.date or common.today_cn()
    try:
        common.parse_dt_any(date_str)
    except Exception:  # noqa: BLE001
        common.log(f"非法 --date: {date_str}")
        return 1
    data_root = os.path.abspath(args.out_root or DEFAULT_DATA_ROOT)
    day_dir = os.path.join(data_root, date_str)
    common.ensure_dir(os.path.join(day_dir, "raw"))
    cfg = _cfg()
    watchlist = _watchlist(args.watchlist, args.stocks)
    old_data = common.load_json(os.path.join(day_dir, "data.json"), {}) or {}
    common.log(f"=== 每日情报雷达 {date_str} | 自选股 {len(watchlist)} 只 | offline={args.offline} ===")

    errors: list[dict] = []
    world, e1 = _run_world(date_str, cfg, args.offline)
    if not args.offline:
        old_world = old_data.get("world") if isinstance(old_data.get("world"), dict) else None
        lookup = _focus_lookup(cfg, world.get("news"))
        world = _merge_news(world, old_world, lookup, cfg)
    world = _apply_focus(world, cfg)
    ashare, e2 = _run_ashare(date_str, watchlist, cfg, args.offline)
    errors.extend(e1)
    errors.extend(e2)
    errors.extend(_collect_errors(world, ashare))

    # ---- 图表数据（分时 / 日线，可选独立模块） ------------------------------- #
    charts = {"generated_at": common.iso(common.now_cn()), "items": {}, "sources": []}
    if not args.skip_charts and not args.offline:
        mod, err = _load_module("sources_charts")
        if mod is None:
            errors.append({"source": "sources_charts", "stage": "charts", "error": err})
        else:
            try:
                got = mod.collect(date_str, watchlist, cfg, ashare) or {}
                if isinstance(got, dict) and isinstance(got.get("items"), dict):
                    charts = got
                common.log(f"charts 采集完成：items={len(charts.get('items') or {})} "
                           f"sources={len(charts.get('sources') or [])}")
            except Exception as exc:  # noqa: BLE001
                errors.append({"source": "sources_charts", "stage": "charts",
                               "error": f"{type(exc).__name__}: {exc}"})
                common.log("图表数据采集失败:\n" + traceback.format_exc(limit=3))

    # top-level alerts: merge stock alerts, sort by severity, cap 20
    alerts = []
    for stock in ashare.get("watchlist") or []:
        for a in stock.get("alerts") or []:
            if isinstance(a, dict):
                alerts.append({
                    "level": a.get("level") or "info",
                    "kind": a.get("kind") or "stock",
                    "text": a.get("text") or "",
                    "ref": stock.get("code"),
                    "when": a.get("when") or common.iso(common.now_cn()),
                })
    alerts.sort(key=lambda a: (LEVEL_ORDER.get(a["level"], 3), a.get("ref") or ""))
    alerts = alerts[:20]

    data = {
        "schema_version": 1,
        "date": date_str,
        "generated_at": common.iso(common.now_cn()),
        "timezone": "Asia/Shanghai",
        "run": {
            "duration_sec": None,
            "requests": len(world.get("sources") or []) + len(ashare.get("sources") or []),
            "errors": len(errors),
            "offline": bool(args.offline),
        },
        "world": world,
        "ashare": ashare,
        "charts": _sanitize(charts),
        "alerts": alerts,
        "errors": errors,
    }
    data = _sanitize(data)

    data_path = os.path.join(day_dir, "data.json")
    try:
        common.save_json(data_path, data)
    except OSError as exc:
        common.log(f"写 data.json 失败: {exc}")
        return 1
    if not args.offline:
        try:
            common.save_json(os.path.join(day_dir, "raw", "world.json"), _sanitize(world))
            common.save_json(os.path.join(day_dir, "raw", "ashare.json"), _sanitize(ashare))
        except OSError:
            pass

    archive_items = _scan_archive(data_root)
    dates = [it["date"] for it in archive_items]

    dashboard_path, report_path = None, None
    if not args.skip_dashboard:
        mod, err = _load_module("build_dashboard")
        if mod is None:
            errors.append({"source": "build_dashboard", "stage": "render", "error": err})
        else:
            archive_path = os.path.join(data_root, "index.html")
            try:
                # archive index lives at data_root/index.html, day pages at data_root/<date>/index.html
                # -> relative href from archive page to a day page is "<date>/index.html".
                try:
                    dashboard_path = mod.build(
                        data, day_dir, dates=dates, archive_path=archive_path,
                        archive_items=archive_items, archive_href_prefix="")
                except TypeError:
                    # older build_dashboard without archive_href_prefix support
                    dashboard_path = mod.build(
                        data, day_dir, dates=dates, archive_path=archive_path,
                        archive_items=archive_items)
            except Exception as exc:  # noqa: BLE001
                errors.append({"source": "build_dashboard", "stage": "render",
                               "error": f"{type(exc).__name__}: {exc}"})
                common.log("看板生成失败:\n" + traceback.format_exc(limit=3))
    if not args.skip_report:
        mod, err = _load_module("build_report")
        if mod is None:
            errors.append({"source": "build_report", "stage": "render", "error": err})
        else:
            try:
                report_path = mod.build(data, os.path.join(day_dir, "brief.md"))
            except Exception as exc:  # noqa: BLE001
                errors.append({"source": "build_report", "stage": "render",
                               "error": f"{type(exc).__name__}: {exc}"})
                common.log("简报生成失败:\n" + traceback.format_exc(limit=3))

    # ---- 手机可用包（长图 + 单文件 HTML，按需同步 OneDrive） ------------------ #
    share_path = None
    if not args.skip_share:
        mod, err = _load_module("share_phone")
        if mod is None:
            errors.append({"source": "share_phone", "stage": "share", "error": err})
        else:
            try:
                m = mod.share(date_str, data_root=data_root)
                for t in (m.get("targets") or []):
                    if t.get("type") == "onedrive":
                        share_path = t.get("path")
                        break
                if not share_path and m.get("targets"):
                    share_path = m["targets"][-1].get("path")
            except Exception as exc:  # noqa: BLE001
                errors.append({"source": "share_phone", "stage": "share",
                               "error": f"{type(exc).__name__}: {exc}"})
                common.log("手机分享包生成失败:\n" + traceback.format_exc(limit=3))

    # ---- 邮件推送（可选，需 config/push.json） -------------------------------- #
    push_info = None
    if not args.skip_push:
        mod, err = _load_module("push_email")
        if mod is None:
            errors.append({"source": "push_email", "stage": "push", "error": err})
        else:
            try:
                res = mod.send(date_str)
                if res.get("ok"):
                    push_info = f"邮件已发送 -> {res.get('to')}"
                elif res.get("skipped"):
                    common.log(f"push: 跳过（{res.get('reason')}）")
                else:
                    errors.append({"source": "push_email", "stage": "push",
                                   "error": str(res.get("error") or "unknown")})
            except Exception as exc:  # noqa: BLE001
                errors.append({"source": "push_email", "stage": "push",
                               "error": f"{type(exc).__name__}: {exc}"})
                common.log("邮件推送失败:\n" + traceback.format_exc(limit=3))

    # ---- 发布到 GitHub Pages（可选，需 config/publish.json 或 GITHUB_TOKEN） ------ #
    publish_info = None
    if not args.skip_publish:
        pub_cfg_path = os.path.join(CONFIG_DIR, "publish.json")
        if os.path.isfile(pub_cfg_path) or os.environ.get("GITHUB_TOKEN"):
            mod, err = _load_module("publish_pages")
            if mod is None:
                errors.append({"source": "publish_pages", "stage": "publish", "error": err})
            else:
                try:
                    pcfg = mod.load_config()
                    if not pcfg.get("enabled") or not pcfg.get("token"):
                        common.log("publish: 跳过（config/publish.json 未启用或缺 token）")
                    else:
                        res = mod.publish(pcfg["token"], pcfg["repo"], pcfg["owner"],
                                          pcfg["private"], pcfg["branch"],
                                          max_days=pcfg["max_days"])
                        if res.get("ok"):
                            publish_info = res.get("site_url")
                            common.log(f"publish: 已发布 -> {res.get('site_url')}")
                        else:
                            errors.append({"source": "publish_pages", "stage": "publish",
                                           "error": str(res.get("error") or "unknown")})
                except Exception as exc:  # noqa: BLE001
                    errors.append({"source": "publish_pages", "stage": "publish",
                                   "error": f"{type(exc).__name__}: {exc}"})
                    common.log("发布失败:\n" + traceback.format_exc(limit=3))

    elapsed = round(time.time() - t_start, 2)
    data["run"]["duration_sec"] = elapsed
    data["run"]["errors"] = len(errors)
    data["errors"] = _sanitize(errors)
    try:
        common.save_json(data_path, data)
    except OSError:
        pass

    # ---- summary + run log -------------------------------------------------- #
    breadth = (ashare.get("breadth") or {})
    lines = [
        f"=== 每日情报雷达 {date_str} ===",
        f"全球：要闻 {len(world.get('news') or [])} 条 / 市场 {len(world.get('markets') or [])} 项 / "
        f"热搜 {len(world.get('social') or [])} 条 / 源 {len(world.get('sources') or [])} 个",
        f"A股：指数 {len(ashare.get('indices') or [])} / 上涨 {breadth.get('up')} 下跌 {breadth.get('down')} "
        f"涨停 {breadth.get('limit_up')} 跌停 {breadth.get('limit_down')} / "
        f"自选股 {len(ashare.get('watchlist') or [])} 只 / 板块 {len((ashare.get('sectors') or {}).get('top') or [])}",
        f"异动 {len(alerts)} 条 / 错误 {len(errors)} 条 / 耗时 {elapsed}s",
        f"图表：{len((charts.get('items') or {}))} 个标的（分时/日线，详情见看板点击卡片）",
        f"data      : {data_path}",
    ]
    if dashboard_path:
        lines.append(f"dashboard : {dashboard_path}")
    if report_path:
        lines.append(f"brief     : {report_path}")
    if share_path:
        lines.append(f"手机包    : {share_path}（长图 + 单文件 HTML，可同步到手机）")
    if push_info:
        lines.append(f"邮件推送  : {push_info}")
    if publish_info:
        lines.append(f"公网地址  : {publish_info}")
    if archive_items:
        lines.append(f"archive   : {os.path.join(data_root, 'index.html')} ({len(archive_items)} 天)")
    summary = "\n".join(lines)
    print(summary)
    try:
        common.write_text(os.path.join(day_dir, "run.log"), summary + "\n")
    except OSError:
        pass
    if errors:
        print("\n[降级/错误]")
        for e in errors:
            print(f"  - {e.get('stage')}/{e.get('source')}: {e.get('error')}")
    common.log("完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
