# -*- coding: utf-8 -*-
"""intel.sources_world — 全球信息采集模块（owner: worldwatch）。

采集「全球要闻 + 全球市场快照 + 社交热搜 + 科技前沿」，产出对齐
`intel/CONTRACT.md` §4 `world` 段的数据结构：

    {
      "news":        [ {id,title,summary,url,source,source_id,published,category,tags,heat} ],
      "by_category": [ {"name": ..., "count": ...} ],
      "markets":     [ {name,symbol,group,price,change,change_pct} ],
      "social":      [ {title,url,source,rank,heat} ],
      "sources":     [ {id,name,ok,count,error,elapsed_ms} ],
    }

设计要点
--------
* 纯标准库；HTTP / RSS / 分类 / 热度 / 去重全部复用 ``common.py``。
* 每个源独立 try/except：失败只写 ``sources[].ok=false`` + ``error``，绝不抛异常。
* 各源用 ``run_parallel`` 并发（workers=8），单源耗时用 ``time.time()`` 计。
* **新鲜度过滤**：``published`` 可解析且早于 ``now - 3 天`` 的新闻丢弃（避免人民网/
  新浪等陈旧 RSS 污染"今日"简报）；``published`` 缺失/不可解析的保留。过滤在单源
  汇总处执行，因此 ``sources[].count`` 是过滤后的条数（陈旧源 ok=True,count=0）。
* 纯解析逻辑拆成可单测的小函数（``_parse_em_7x24`` / ``_parse_wscn`` /
  ``_parse_baidu_hot`` / ``_parse_hn_items`` / ``_parse_markets`` / ``_parse_rss_raw``）。
* Hacker News 逐条拉 item 是主要耗时：硬上限 20 条、item 全部并发 + 2s 超时砍长尾
  （实测 HN 段 ~4s，全流程目标 < 5s）。

CLI::

    python intel/src/sources_world.py [YYYY-MM-DD]
    -> 打印各源条数/耗时/错误、分类分布、Top10，并写 intel/out/world_preview.json
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.parse
from collections import Counter
from datetime import timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import common as C  # noqa: E402  (must follow sys.path fix)

SCHEMA_VERSION = 1

# --------------------------------------------------------------------------- #
# 端点常量（CONTRACT.md §5）
# --------------------------------------------------------------------------- #
EM_7X24_URL = "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html"
WSCN_URL = "https://api-one-wscn.awtmt.com/apiv1/content/lives?channel=global-channel&limit=40"
BAIDU_URL = "https://top.baidu.com/api/board?platform=pc&tab=realtime"
HN_TOP_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
HN_ITEM_PAGE = "https://news.ycombinator.com/item?id={id}"
ARXIV_URL = (
    "http://export.arxiv.org/api/query?search_query=cat:cs.AI"
    "&sortBy=submittedDate&sortOrder=descending&max_results=8"
)
# 东财 push2：实测 https 会被服务端直接断开(RemoteDisconnected)，http 正常。
PUSH2_HOSTS = ("http://push2.eastmoney.com", "https://push2.eastmoney.com")
PUSH2_PATH = "/api/qt/ulist.np/get?fltt=2&invt=2&fields=f1,f2,f3,f4,f6,f12,f13,f14&secids={secids}"

# 源定义：顺序即输出 sources / 合并顺序（与 CONTRACT §5 表格一致）
SOURCE_DEFS = [
    {"id": "eastmoney_7x24", "name": "东方财富7×24", "kind": "news"},
    {"id": "wallstreetcn", "name": "华尔街见闻", "kind": "news"},
    {"id": "chinanews_world", "name": "中新网国际", "kind": "rss",
     "url": "https://www.chinanews.com.cn/rss/world.xml"},
    {"id": "people_world", "name": "人民网国际", "kind": "rss",
     "url": "http://www.people.com.cn/rss/world.xml"},
    {"id": "sina_world", "name": "新浪国际", "kind": "rss",
     "url": "https://rss.sina.com.cn/news/world/focus15.xml"},
    {"id": "hackernews", "name": "Hacker News", "kind": "news"},
    {"id": "arxiv_cs_ai", "name": "arXiv cs.AI", "kind": "rss", "url": ARXIV_URL},
    {"id": "baidu_hot", "name": "百度热搜", "kind": "hot"},
    {"id": "world_markets", "name": "全球市场快照", "kind": "markets"},
]

# 配置缺失时的兜底（与 config/sources.json 保持一致）
DEFAULT_WEIGHTS = {
    "eastmoney_7x24": 1.0,
    "wallstreetcn": 1.02,
    "chinanews_world": 1.0,
    "people_world": 0.95,
    "sina_world": 0.9,
    "hackernews": 0.85,
    "arxiv_cs_ai": 0.7,
    "baidu_hot": 0.75,
}
DEFAULT_LIMITS = {
    "eastmoney_7x24": 50,
    "wallstreetcn": 40,
    "hackernews": 20,
    "arxiv_cs_ai": 8,
    "baidu_hot": 50,
}
DEFAULT_WORLD_MARKETS = [
    {"secid": "100.DJIA", "name": "道琼斯", "group": "美股"},
    {"secid": "100.NDX", "name": "纳斯达克", "group": "美股"},
    {"secid": "100.SPX", "name": "标普500", "group": "美股"},
    {"secid": "100.N225", "name": "日经225", "group": "亚太"},
    {"secid": "100.HSI", "name": "恒生指数", "group": "亚太"},
    {"secid": "100.GDAXI", "name": "德国DAX30", "group": "欧洲"},
    {"secid": "100.FTSE", "name": "英国富时100", "group": "欧洲"},
    {"secid": "101.GC00Y", "name": "COMEX黄金", "group": "商品"},
    {"secid": "101.SI00Y", "name": "COMEX白银", "group": "商品"},
    {"secid": "101.HG00Y", "name": "COMEX铜", "group": "商品"},
    {"secid": "133.USDCNH", "name": "美元兑离岸人民币", "group": "汇率"},
]

NEWS_KEYS = ("id", "title", "summary", "url", "source", "source_id",
             "published", "category", "tags", "heat")

# 新鲜度窗口：published 可解析且早于 now - 3 天 -> 丢弃（缺失/不可解析 -> 保留）
FRESH_WINDOW_DAYS = 3
# HN：item 逐条拉取是主要耗时，硬上限 20 条 + 内层全部并发（目标全流程 < 5s）。
# firebaseio 单请求延迟抖动大（0.2~6s）：topstories 5s 快速失败不重试，
# item 20 条一次并发 + 2s 超时砍长尾（实测 HN 整体 ~4s，全流程 <5s）。
HN_MAX_ITEMS = 20
HN_ITEM_WORKERS = 20
HN_ITEM_TIMEOUT = 2
HN_TOP_TIMEOUT = 5


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def news_id(source_id: str, title: str) -> str:
    """sha1(source_id|title) 前 10 位（CONTRACT §3）。"""
    raw = f"{source_id}|{title}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:10]


def _clean(v, max_len: int = 0) -> str:
    return C.clean_text(v or "", max_len=max_len)


def _norm_url(u) -> str:
    """补全 //host/... 形式的协议相对 URL；非法/相对(无 host)返回 ''。"""
    s = str(u or "").strip()
    if not s:
        return ""
    if s.startswith("//"):
        return "https:" + s
    if s.startswith(("http://", "https://")):
        return s
    return ""


def _published(v) -> str | None:
    """任意时间值 -> ISO+08:00 字符串 / None。"""
    try:
        return C.iso(C.parse_dt_any(v))
    except Exception:  # noqa: BLE001 - 防御式
        return None


def _to_int(v):
    f = C.safe_float(v)
    return int(f) if f is not None else None


def _is_fresh(item: dict, ref_time=None) -> bool:
    """published 可解析且 >= ref - FRESH_WINDOW_DAYS 天 -> True。

    published 缺失 / 无法解析 -> True（不能因缺时间字段就丢新闻）。
    """
    pub = C.parse_dt_any((item or {}).get("published"))
    if pub is None:
        return True
    ref = ref_time or C.now_cn()
    try:
        return pub >= ref - timedelta(days=FRESH_WINDOW_DAYS)
    except TypeError:  # 防御：极端脏值
        return True


def _filter_fresh(items: list[dict], ref_time=None) -> list[dict]:
    """丢弃早于时间窗的陈旧新闻（保住今日简报的新鲜度）。"""
    ref = ref_time or C.now_cn()
    return [it for it in items or [] if _is_fresh(it, ref)]


def _spec(cfg: dict, sid: str) -> dict:
    """合并 config/sources.json 中 sid 的配置与默认值。"""
    raw = ((cfg or {}).get("sources") or {}).get(sid) or {}
    name = raw.get("name") or next(
        (d["name"] for d in SOURCE_DEFS if d["id"] == sid), sid)
    weight = C.safe_float(raw.get("weight"), DEFAULT_WEIGHTS.get(sid, 1.0))
    if weight is None:
        weight = DEFAULT_WEIGHTS.get(sid, 1.0)
    limit = _to_int(raw.get("limit"))
    if limit is None:
        limit = DEFAULT_LIMITS.get(sid)
    return {
        "id": sid,
        "name": str(name),
        "weight": float(weight),
        "limit": limit,
        "enabled": bool(raw.get("enabled", True)),
    }


def _per_source_limit(spec: dict, cfg: dict) -> int:
    """单源条数上限 = min(源 limit, limits.per_source_max)。"""
    per_max = _to_int((cfg.get("limits") or {}).get("per_source_max")) or 60
    lim = spec.get("limit") or per_max
    return max(1, min(int(lim), int(per_max)))


def _hn_limit(spec: dict, cfg: dict) -> int:
    """HN 条数 = min(配置 limit, per_source_max, HN_MAX_ITEMS)；默认 20。"""
    return max(1, min(_per_source_limit(spec, cfg), HN_MAX_ITEMS))


def _world_news_max(cfg: dict) -> int:
    n = _to_int((cfg.get("limits") or {}).get("world_news_max")) or 240
    return max(1, int(n))


# --------------------------------------------------------------------------- #
# 纯解析函数（离线可单测；输入是已取到的文本 / JSON 对象）
# --------------------------------------------------------------------------- #
def _parse_em_7x24(payload) -> list[dict]:
    """东方财富 7×24：``var ajaxResult={"LivesList":[...]}`` 文本或已解析 dict。

    字段 ``title/digest/showtime/newsid``；url 缺失时按契约拼
    ``https://finance.eastmoney.com/a/{newsid}.html``。
    """
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "replace")
    if isinstance(payload, str):
        payload = json.loads(C.strip_jsonp(payload))
    if not isinstance(payload, dict):
        raise ValueError("EM 7x24: unexpected payload type %r" % type(payload))

    rows = payload.get("LivesList") or payload.get("livesList")
    if rows is None:
        data = payload.get("data") or {}
        rows = data.get("LivesList") or data.get("livesList") or []
    if not isinstance(rows, list):
        rows = []

    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = _clean(row.get("title") or row.get("simtitle") or "")
        if not title:
            continue
        newsid = str(row.get("newsid") or row.get("id") or "").strip()
        url = _norm_url(row.get("url_w") or row.get("url_unique") or row.get("url"))
        if not url and newsid:
            url = "https://finance.eastmoney.com/a/%s.html" % newsid
        if not url:
            continue
        out.append({
            "title": title,
            "summary": _clean(row.get("digest") or row.get("simdigest") or "", 240),
            "url": url,
            "published": _published(row.get("showtime") or row.get("ordertime")),
        })
    return out


def _parse_wscn(payload) -> list[dict]:
    """华尔街见闻 lives：``data.items[]``（content_text/title/display_time/uri/score）。"""
    if not isinstance(payload, dict):
        raise ValueError("WSCN: unexpected payload type %r" % type(payload))
    items = (payload.get("data") or {}).get("items") or []
    if not isinstance(items, list):
        items = []
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        body = _clean(it.get("content_text") or it.get("content") or "", 800)
        title = _clean(it.get("title") or it.get("highlight_title") or "")
        if not title:
            title = C.truncate(body, 80)
        title = _clean(title, 120)
        if not title:
            continue
        live_id = str(it.get("id") or "").strip()
        url = _norm_url(it.get("uri"))
        if not url and live_id:
            url = "https://wallstreetcn.com/livenews/%s" % live_id
        if not url:
            continue
        out.append({
            "title": title,
            "summary": C.truncate(body, 240),
            "url": url,
            "published": _published(it.get("display_time") or it.get("created_at")),
        })
    return out


def _parse_baidu_hot(payload) -> list[dict]:
    """百度热搜：``data.cards[0].content[]``（word/query/hotScore/url/desc/index）。

    返回 ``[{title,url,summary,rank,heat}]``（rank 从 1 开始，heat=hotScore）。
    """
    if not isinstance(payload, dict):
        raise ValueError("Baidu: unexpected payload type %r" % type(payload))
    cards = (payload.get("data") or {}).get("cards") or []
    rows: list = []
    for card in cards:
        content = (card or {}).get("content") or []
        if isinstance(content, list) and content:
            rows = content
            break
    out: list[dict] = []
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        title = _clean(row.get("word") or row.get("query") or row.get("title") or "")
        if not title:
            continue
        url = _norm_url(row.get("url") or row.get("rawUrl") or row.get("appUrl"))
        if not url:
            url = "https://www.baidu.com/s?wd=" + urllib.parse.quote(title)
        rank = _to_int(row.get("index"))
        rank = (rank + 1) if rank is not None and rank >= 0 else idx + 1
        out.append({
            "title": title,
            "url": url,
            "summary": _clean(row.get("desc") or "", 240),
            "rank": int(rank),
            "heat": _to_int(row.get("hotScore")),
        })
    return out


def _parse_hn_items(items) -> list[dict]:
    """Hacker News item 对象列表 -> 标准化新闻列表。

    无 ``url`` 的（Ask HN / Show HN 文本帖）按契约用
    ``https://news.ycombinator.com/item?id={id}``。
    """
    out: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        title = _clean(it.get("title") or "")
        if not title:
            continue
        item_id = str(it.get("id") or "").strip()
        url = _norm_url(it.get("url"))
        if not url and item_id:
            url = HN_ITEM_PAGE.format(id=item_id)
        if not url:
            continue
        score = _to_int(it.get("score"))
        comments = _to_int(it.get("descendants"))
        bits = []
        if score is not None:
            bits.append("%d points" % score)
        if comments is not None:
            bits.append("%d comments" % comments)
        if it.get("by"):
            bits.append("by %s" % _clean(it.get("by"), 40))
        out.append({
            "title": title,
            "summary": ", ".join(bits),
            "url": url,
            "published": _published(it.get("time")),
        })
    return out


def _parse_markets(payload, world_markets) -> list[dict]:
    """东财 ulist.np 快照 -> markets（顺序严格按 world_markets 配置）。

    字段映射：f2=最新价 f3=涨跌幅% f4=涨跌额 f12=代码 f14=名称。
    缺失/停牌（'-'）输出 ``None``，不编造。
    """
    if not isinstance(payload, dict):
        payload = {}
    data = payload.get("data") or {}
    rows = data.get("diff") or []
    if isinstance(rows, dict):
        rows = [rows[k] for k in rows]
    if not isinstance(rows, list):
        rows = []
    by_code: dict[str, dict] = {}
    for row in rows:
        if isinstance(row, dict):
            by_code[str(row.get("f12") or "").strip()] = row

    out: list[dict] = []
    for m in world_markets or []:
        if not isinstance(m, dict):
            continue
        secid = str(m.get("secid") or "")
        code = secid.split(".")[-1] if secid else ""
        row = by_code.get(code) or {}
        out.append({
            "name": str(m.get("name") or _clean(row.get("f14")) or code),
            "symbol": str(row.get("f12") or code),
            "group": str(m.get("group") or "其他"),
            "price": C.safe_float(row.get("f2")),
            "change": C.safe_float(row.get("f4")),
            "change_pct": C.safe_float(row.get("f3")),
        })
    return out


def _parse_rss_raw(raw, source: str = "", limit: int = 0) -> list[dict]:
    """RSS 2.0 / Atom bytes|str -> 标准化新闻列表（复用 common.parse_rss）。"""
    items = C.parse_rss(raw, source=source)
    out: list[dict] = []
    for it in items:
        title = _clean(it.get("title") or "")
        if not title:
            continue
        url = _norm_url(it.get("url"))
        if not url:
            continue
        out.append({
            "title": title,
            "summary": C.truncate(it.get("summary") or "", 240),
            "url": url,
            "published": it.get("published") or _published(None),
        })
    if limit:
        out = out[:limit]
    return out


# --------------------------------------------------------------------------- #
# 抓取函数（只做 HTTP 调用 + 调纯解析；异常向上抛，由 _collect_one 兜底）
# --------------------------------------------------------------------------- #
def _fetch_em_7x24(limit: int) -> list[dict]:
    text = C.http_get_text(EM_7X24_URL, timeout=15)
    return _parse_em_7x24(text)[:limit]


def _fetch_wscn(limit: int) -> list[dict]:
    obj = C.http_get_json(WSCN_URL, timeout=15)
    return _parse_wscn(obj)[:limit]


def _fetch_rss(url: str, source: str, limit: int) -> list[dict]:
    raw = C.http_get(url, timeout=25)
    return _parse_rss_raw(raw, source=source, limit=limit)


def _fetch_hn(limit: int) -> list[dict]:
    # topstories 一个请求 ~1.3-2s；item 单请求抖动大 -> 全部并发 + 2s 超时砍长尾。
    ids = C.http_get_json(HN_TOP_URL, timeout=HN_TOP_TIMEOUT, retries=0)
    if not isinstance(ids, list):
        raise ValueError("HN topstories: unexpected payload")
    ids = [int(i) for i in ids if isinstance(i, (int, str)) and str(i).isdigit()][:limit]

    def one(item_id):
        try:
            obj = C.http_get_json(HN_ITEM_URL.format(id=item_id),
                                  timeout=HN_ITEM_TIMEOUT, retries=0)
            return obj if isinstance(obj, dict) else None
        except Exception:  # noqa: BLE001 - 单条失败不影响整榜
            return None

    pairs = C.run_parallel(one, ids, workers=HN_ITEM_WORKERS)
    got = {item: res for item, res, _err in pairs if isinstance(res, dict)}
    ordered = [got[i] for i in ids if i in got]
    return _parse_hn_items(ordered)[:limit]


def _fetch_baidu(limit: int) -> list[dict]:
    obj = C.http_get_json(BAIDU_URL, timeout=15,
                          referer="https://top.baidu.com/board?tab=realtime")
    return _parse_baidu_hot(obj)[:limit]


def _fetch_markets(cfg: dict) -> list[dict]:
    world_markets = cfg.get("world_markets") or DEFAULT_WORLD_MARKETS
    secids = ",".join(str(m.get("secid")) for m in world_markets
                      if isinstance(m, dict) and m.get("secid"))
    if not secids:
        raise ValueError("world_markets config is empty")
    path = PUSH2_PATH.format(secids=secids)
    last: Exception | None = None
    for host in PUSH2_HOSTS:  # 实测 https 被拒 -> http 优先
        try:
            obj = C.http_get_json(host + path, timeout=15, retries=1,
                                  referer="https://quote.eastmoney.com/")
            markets = _parse_markets(obj, world_markets)
            if markets and any(m.get("price") is not None for m in markets):
                return markets
            last = ValueError("markets snapshot empty: %s" % host)
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise last if last else RuntimeError("markets fetch failed")


# --------------------------------------------------------------------------- #
# 单源采集（异常在此吞掉，产出 sources 记录）
# --------------------------------------------------------------------------- #
def _attach(raw_items, spec: dict) -> list[dict]:
    """补全 news 的统一字段（id/source/source_id/source_weight）。"""
    sid, name, weight = spec["id"], spec["name"], float(spec["weight"])
    out: list[dict] = []
    for it in raw_items or []:
        title = _clean(it.get("title") or "")
        if not title:
            continue
        url = _norm_url(it.get("url"))
        if not url:
            continue
        out.append({
            "id": news_id(sid, title),
            "title": title,
            "summary": C.truncate(it.get("summary") or "", 240),
            "url": url,
            "source": name,
            "source_id": sid,
            "published": it.get("published") or None,
            "source_weight": weight,
        })
    return out


def _record(sid: str, name: str, t0: float, *, ok: bool, count: int = 0,
            error=None, news=None, social=None, markets=None) -> dict:
    return {
        "id": sid,
        "name": name,
        "ok": bool(ok),
        "count": int(count),
        "error": error,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "news": news or [],
        "social": social or [],
        "markets": markets or [],
    }


def _news_record(sid: str, name: str, t0: float, raw_items, spec: dict,
                 limit: int, ref_time) -> dict:
    """统一收口单源新闻：attach -> 新鲜度过滤 -> 截断 -> sources.count。"""
    news = _attach(raw_items, spec)
    news = _filter_fresh(news, ref_time)
    news = news[:limit]
    return _record(sid, name, t0, ok=True, count=len(news), news=news)


def _collect_one(job: dict, date_str: str, cfg: dict) -> dict:
    """采集单个源；任何异常都转成 ok=False 记录，绝不向上抛。"""
    t0 = time.time()
    sid = job["id"]
    spec = _spec(cfg, sid)
    name = spec["name"]
    ref = C.now_cn()  # 本次采集的新鲜度参考时刻（now-3 天为界）
    try:
        kind = job.get("kind")
        if kind == "rss":
            url = job.get("url") or ""
            limit = _per_source_limit(spec, cfg)
            return _news_record(sid, name, t0, _fetch_rss(url, name, limit),
                                spec, limit, ref)
        if sid == "eastmoney_7x24":
            limit = _per_source_limit(spec, cfg)
            return _news_record(sid, name, t0, _fetch_em_7x24(limit),
                                spec, limit, ref)
        if sid == "wallstreetcn":
            limit = _per_source_limit(spec, cfg)
            return _news_record(sid, name, t0, _fetch_wscn(limit),
                                spec, limit, ref)
        if sid == "hackernews":
            limit = _hn_limit(spec, cfg)  # 硬上限 20 条（提速）
            return _news_record(sid, name, t0, _fetch_hn(limit),
                                spec, limit, ref)
        if sid == "baidu_hot":
            limit = _per_source_limit(spec, cfg)
            rows = _fetch_baidu(limit)[:limit]
            # 百度热搜进 social（rank/heat=hotScore）；board 无发布时间，故不进 news，
            # 避免 news[].published 出现大面积为 null（契约 §4 要求 ISO 时间）。
            social = [{
                "title": r["title"],
                "url": r["url"],
                "source": name,
                "rank": int(r.get("rank") or i + 1),
                "heat": r.get("heat"),
            } for i, r in enumerate(rows)]
            return _record(sid, name, t0, ok=True, count=len(social), social=social)
        if sid == "world_markets":
            markets = _fetch_markets(cfg)
            return _record(sid, name, t0, ok=True, count=len(markets), markets=markets)
        raise ValueError("unknown source: %s" % sid)
    except Exception as exc:  # noqa: BLE001 - 单源失败降级
        msg = "%s: %s" % (type(exc).__name__, str(exc))
        return _record(sid, name, t0, ok=False, error=msg[:300])


# --------------------------------------------------------------------------- #
# 汇总：合并 -> 去重 -> 分类/热度 -> 截断 -> by_category
# --------------------------------------------------------------------------- #
def _canonical_news(it: dict) -> dict:
    """只保留 CONTRACT §4 规定的 news 字段，并补默认值。"""
    sid = str(it.get("source_id") or "unknown")
    title = _clean(it.get("title") or "")
    return {
        "id": str(it.get("id") or news_id(sid, title)),
        "title": title,
        "summary": _clean(it.get("summary") or "", 240),
        "url": _norm_url(it.get("url")),
        "source": str(it.get("source") or "未知来源"),
        "source_id": sid,
        "published": it.get("published") or None,
        "category": str(it.get("category") or C.DEFAULT_CATEGORY),
        "tags": [str(t) for t in (it.get("tags") or [])][:4],
        "heat": round(float(it.get("heat") or 0.0), 1),
    }


def _build_news(raw_news: list[dict], cfg: dict,
                ref_time=None) -> tuple[list[dict], list[dict]]:
    """合并后的 news -> (截断后的 news, by_category 按 count 降序)。

    这里再做一次新鲜度过滤（防陈旧源/脏数据进入"今日"数据）。
    """
    news = _filter_fresh(list(raw_news), ref_time)
    news = C.dedupe(news)
    news = C.score_items(news, cfg, ref_time=ref_time)
    news = [_canonical_news(it) for it in news[: _world_news_max(cfg)]]
    news.sort(key=lambda x: x["heat"], reverse=True)
    counts = Counter(n["category"] for n in news)
    by_category = [{"name": k, "count": v}
                   for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    return news, by_category


def _jobs(cfg: dict) -> list[dict]:
    jobs = []
    for d in SOURCE_DEFS:
        spec = _spec(cfg, d["id"])
        if not spec["enabled"]:
            continue
        job = dict(d)
        jobs.append(job)
    return jobs


# --------------------------------------------------------------------------- #
# 对外接口（CONTRACT §5）
# --------------------------------------------------------------------------- #
def collect(date_str: str, cfg: dict) -> dict:
    """采集全球信息，返回 ``{news, by_category, markets, social, sources}``。

    每个源独立降级：单源失败只体现在 ``sources[].ok=false``，不会抛异常。
    """
    cfg = cfg or {}
    jobs = _jobs(cfg)
    results = C.run_parallel(lambda job: _collect_one(job, date_str, cfg),
                             jobs, workers=8)

    by_id: dict[str, dict] = {}
    for job, rec, err in results:
        if not isinstance(rec, dict):
            t0 = time.time()
            spec = _spec(cfg, job["id"])
            rec = _record(job["id"], spec["name"], t0, ok=False,
                          error="%s: %s" % (type(err).__name__, err))
        by_id[rec["id"]] = rec
    ordered = [by_id[j["id"]] for j in jobs if j["id"] in by_id]

    raw_news: list[dict] = []
    markets: list[dict] = []
    social: list[dict] = []
    sources: list[dict] = []
    for rec in ordered:
        if rec["id"] == "world_markets":
            markets = rec.get("markets") or []
        else:
            raw_news.extend(rec.get("news") or [])
            if rec["id"] == "baidu_hot" and rec["ok"]:
                social = rec.get("social") or []
        sources.append({
            "id": rec["id"],
            "name": rec["name"],
            "ok": bool(rec["ok"]),
            "count": int(rec["count"]),
            "error": rec.get("error"),
            "elapsed_ms": int(rec.get("elapsed_ms") or 0),
        })

    news, by_category = _build_news(raw_news, cfg)
    return {
        "news": news,
        "by_category": by_category,
        "markets": markets,
        "social": social,
        "sources": sources,
    }


# --------------------------------------------------------------------------- #
# CLI：python intel/src/sources_world.py [YYYY-MM-DD]
# --------------------------------------------------------------------------- #
def _print_report(date: str, res: dict, duration: float) -> None:
    news = res["news"]
    sources = res["sources"]
    ok_n = sum(1 for s in sources if s["ok"])
    print("=" * 78)
    print("每日情报雷达 · 全球信息采集  date=%s  schema=%d" % (date, SCHEMA_VERSION))
    print("总耗时 %.2fs ｜ news=%d ｜ markets=%d ｜ social=%d ｜ 数据源 %d/%d 成功"
          % (duration, len(news), len(res["markets"]), len(res["social"]), ok_n, len(sources)))
    print("=" * 78)

    print("\n-- 数据源 --")
    for s in sources:
        status = "OK  " if s["ok"] else "FAIL"
        tail = "" if s["ok"] else ("  error=%s" % s["error"])
        print("  %-4s %-16s %-12s count=%-4d %6dms%s"
              % (status, s["id"], s["name"], s["count"], s["elapsed_ms"], tail))

    print("\n-- 分类分布 --")
    for c in res["by_category"]:
        print("  %-8s %3d" % (c["name"], c["count"]))

    print("\n-- Top 10 新闻（按 heat） --")
    for i, n in enumerate(news[:10], 1):
        print("  %2d. [%5.1f] %s  (%s / %s)"
              % (i, n["heat"], n["title"], n["source"], n["category"]))

    if res["markets"]:
        print("\n-- 全球市场快照 --")
        for m in res["markets"]:
            price = "—" if m["price"] is None else ("%.2f" % m["price"])
            pct = "—" if m["change_pct"] is None else ("%+.2f%%" % m["change_pct"])
            print("  %-22s %-6s %12s %10s" % (m["name"], m["group"], price, pct))

    if res["social"]:
        print("\n-- 百度热搜 Top 5 --")
        for s in res["social"][:5]:
            print("  #%d [%s] %s" % (s["rank"], s.get("heat"), s["title"]))

    failed = [s for s in sources if not s["ok"]]
    print("\n失败源: %s" % (", ".join("%s(%s)" % (s["id"], s["error"]) for s in failed)
                          if failed else "无"))


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    intel_root = os.path.dirname(here)
    cfg_path = os.path.join(intel_root, "config", "sources.json")
    cfg = C.load_json(cfg_path, {}) or {}
    if not cfg:
        C.log("WARN: cannot read %s, using built-in defaults" % cfg_path)

    date = sys.argv[1] if len(sys.argv) > 1 else C.today_cn()
    t0 = time.time()
    res = collect(date, cfg)
    duration = time.time() - t0

    out_path = os.path.join(intel_root, "out", "world_preview.json")
    C.save_json(out_path, {
        "schema_version": SCHEMA_VERSION,
        "date": date,
        "generated_at": C.iso(C.now_cn()),
        "duration_sec": round(duration, 2),
        **res,
    })

    _print_report(date, res, duration)
    print("\npreview -> %s" % out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
