# -*- coding: utf-8 -*-
"""intel.sources_ashare — A股数据采集模块（owner: ashare）。

契约：``intel/CONTRACT.md`` §4（ashare 段 schema）+ §6（接口清单）。
只依赖 Python 标准库 + ``intel/src/common.py``，不引入 requests/pandas/bs4/akshare。

入口
----
``collect(date_str, watchlist, cfg) -> dict``
    返回契约 §4 的 ``ashare`` 段：``indices / breadth / sectors / movers /
    limit_up_pool / watchlist / watchlist_meta / sources``（另附 ``alerts``，
    方便 run_daily 组装顶层 alerts 段）。单接口/单标的失败一律降级，不抛异常。

``python intel/src/sources_ashare.py``
    读 config/sources.json + config/watchlist.json 跑一次 live 采集，
    写 ``intel/out/ashare_preview.json`` 并打印摘要（自测/联调用）。

实测记录（2026-11-18 本机，抓包核对过原始响应）
--------------------------------------------------
1. ``push2.eastmoney.com`` 的 ulist.np / clist / stock/get / fflow 接口在带
   ``fltt=2`` 时**直接返回小数**，例如：

       GET /api/qt/stock/get?secid=0.300848&fltt=2&fields=f43,f170,f168
       -> {"data":{"f43":9.1,"f170":-1.3,"f168":1.22,...}}

   而不带 ``fltt=2`` 时同一接口返回 ×100 的整数：

       -> {"data":{"f43":910,"f170":-130,"f168":122,...}}

   => 本模块所有「个股/指数快照」请求都显式带 ``fltt=2``，解析按“已经是小数”
     处理；``parse_stock_quote(..., scaled=True)`` 保留给不带 fltt=2 的旧路径
     （价格/涨跌/PE/PB/换手率等字段 ÷100），并写清注释避免误用。
2. **成交量口径**：个股快照 f47、K线第 6 列、资金流/快照成交量的原始单位是
   “手”，而契约 §4 要求 ``volume`` 为“股”，所以统一 ×100（``_shares``）。
3. 全市场 clist：单页硬上限 100 条（pz 再大也只回 100），``data.total`` 约
   5565；``fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23``。停牌/异常标的的
   ``f2/f3`` 等为字符串 ``"-"``，统一用 ``safe_float -> None`` 跳过。
4. 涨停池 ``getTopicZTPool`` 实测可用（date=20261118 返回 41 只）；
   ``getTopicDTPool`` 同日返回 ``data: null``，跌停池不可用 → 空列表降级，
   跌停家数用 breadth 的阈值统计兜底。
5. 板块 clist ``fs=m:90+t:2`` 共 496 个板块；字段实测：
   f2=板块指数 f3=涨跌幅% f12=板块代码 f14=板块名 f104=上涨家数 f105=下跌家数
   f128=领涨股名 f136=领涨股涨跌幅% f140=领涨股代码。
6. 个股新闻是 JSONP（``cb({...})``），``common.strip_jsonp`` 可直接吃；
   标题里的 ``<em>`` 高亮标签由 ``common.clean_text`` 去除。
7. ``push2.eastmoney.com`` / ``push2his`` 在高频请求（>80 请求/20s）后会批量
   ``RemoteDisconnected``（限流），非 push2 域（公告/新闻/研报）不受影响。
   v2 对策：http 直连优先（https 仅兜底 1 次）、push2 系并发上限 4 + 随机抖动、
   ``retries=3, backoff=2.0`` 退避、breadth 改 **二分定位涨跌分界页**
   （~10 次请求代替 56 页全量）。实测连续运行不再触发断连。
8. 涨停池 ``fbt`` 是 **HHMMSS 整数**（``92500`` -> ``09:25:00``，``92502`` ->
   09:25:02），不是“当日秒数”；``_hms`` 两种口径都兼容。
9. 板块跌幅榜必须用 ``po=0``（升序）单独请求；拿涨幅榜倒序当跌幅榜是错的
   （v1 bug，已修）。
10. **多源降级（实战验证）**：东财 push2 的 ``clist/get`` / ``stock/get`` /
    ``push2his kline`` 会被 WAF 单独限流（而 ulist/fflow/push2ex 正常）。因此：
    * 个股/指数快照：ulist.np 批量为主（1 次请求覆盖全部自选股，含 f10 量比），
      stock/get 仅在批量缺失时兜底；
    * 日K：push2his 快失败 -> **腾讯** ``web.ifzq.gtimg.cn/.../fqkline``；
    * 全市场宽度：东财 clist 二分 -> **新浪** ``Market_Center.getHQNodeData`` 二分
      + ``getHQNodeStockCount``（实测 11 页/5579 只）；
    * 板块：东财 clist -> **新浪行业** ``newSinaHy.php``（gb18030）；
    * 涨跌/成交额榜：东财 clist -> 新浪 hs_a 榜（sort=changepercent/amount）。
    ``sources`` 里会同时记录主源失败与 fallback 命中，便于看板展示数据质量。
11. 涨跌停家数优先取涨停池/跌停池条数（``data.tc`` / ``pool`` 长度，
    实测跌停池只给 ``tc=40, pool=[]``），拿不到才用 breadth 的阈值统计兜底。
12. **美股支持（自选股 A股 + 美股混排）**：``common.load_watchlist`` 返回
    ``is_ashare`` / ``currency``。``collect()`` 按 ``is_ashare`` 分流：
    * A股路径完全保持原来逻辑（ulist 批量 + fflow + 公告 + 新闻 + 研报 +
      腾讯/新浪 fallback）；
    * 美股：1 次 ulist 批量快照（A股/美股分开请求）+ 每只腾讯日K
      （symbol 必须带交易所后缀，``105.NVDA -> usNVDA.OQ``、``106.XX -> usXX.N``，
      否则腾讯只回 2 行稀疏数据）+ 每只新闻（搜索中文名）；
      **公告/研报/资金流对美股不适用** → ``[]`` / ``None``，不发请求，
      sources 里记 ``us_applicable``；quote 的 ``amount``(USD)/``turnover``/
      ``volume_ratio`` 一律 None（避免把 USD 标成“元”），货币见
      ``watchlist[].currency``（USD）；涨跌提示阈值放宽到 8%，不做涨跌停提示。
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import sys
import threading
import time
import urllib.parse
from datetime import timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import common as C  # noqa: E402  （common.py 与本文件同目录）

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #
_REF = "https://quote.eastmoney.com/"          # 东财行情页 Referer
_REF_SO = "https://so.eastmoney.com/"          # 搜索页 Referer
_REF_DATA = "https://data.eastmoney.com/"      # 数据中心 Referer

# 实测（worldwatch + ashare 双方验证）：本机 Python/urllib 请求 push2 系列域名时
# **https 会频繁 RemoteDisconnected，http 稳定** → http 优先，https 兜底重试。
PUSH2_HOSTS = ("http://push2.eastmoney.com", "https://push2.eastmoney.com")
PUSH2HIS_HOSTS = ("http://push2his.eastmoney.com", "https://push2his.eastmoney.com")
PUSH2EX_HOSTS = ("http://push2ex.eastmoney.com", "https://push2ex.eastmoney.com")
PUSH2 = PUSH2_HOSTS[0]
PUSH2HIS = PUSH2HIS_HOSTS[0]
PUSH2EX = PUSH2EX_HOSTS[0]
UT = "7eea3edcaed734bea9cbfc24409ed989"       # 东财公开 ut 参数

# 全市场：沪主板 t:2 + 深主板 t:6 + 创业板 t:80 + 科创板 t:23
FS_ALL = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
FS_SECTOR = "m:90+t:2"

_EPS = 1e-9

# push2/push2his/push2ex 并发上限（防触发东财限流：>80 请求/20s 会批量断连）
_EASTMONEY_SEM = threading.Semaphore(4)
_PACE_LOCK = threading.Lock()
_PACE_NEXT = [0.0]
_REQ_LOCK = threading.Lock()
_REQ_COUNT = {"eastmoney": 0, "other": 0}


def _pace(min_gap: float = 0.1) -> None:
    """全局限速：保证两次东财 push2 请求发起间隔 >= min_gap 秒（≈10 req/s）。"""
    with _PACE_LOCK:
        now = time.time()
        wait = _PACE_NEXT[0] - now
        if wait > 0:
            time.sleep(wait)
        _PACE_NEXT[0] = time.time() + min_gap


def _bump_requests(kind: str, n: int = 1) -> None:
    with _REQ_LOCK:
        _REQ_COUNT[kind] = _REQ_COUNT.get(kind, 0) + n


def _request_snapshot() -> dict:
    with _REQ_LOCK:
        return dict(_REQ_COUNT)

# 公告标题关键词 -> alert level（契约 §6「当日公告含关键词」）
_ANN_KEYWORDS = {
    "减持": "warn", "问询": "warn", "停牌": "warn", "立案": "warn",
    "业绩": "info", "增持": "info", "回购": "info", "中标": "info",
    "重组": "info", "定增": "info", "分红": "info",
}
_LEVEL_RANK = {"high": 0, "warn": 1, "info": 2}

# 契约 §4 要求的所有 quote 字段（缺失给 None，key 必须存在）
_QUOTE_KEYS = (
    "price", "change", "change_pct", "open", "high", "low", "prev_close",
    "volume", "amount", "turnover", "volume_ratio", "pe", "pb",
    "market_cap", "float_cap",
)


# --------------------------------------------------------------------------- #
# small numeric helpers
# --------------------------------------------------------------------------- #
def _f(value):
    """东财数值 -> float | None；``"-"``/``None``/空串 -> None。"""
    return C.safe_float(value)


def _i(value):
    f = C.safe_float(value)
    return int(f) if f is not None else None


def _div100(value, divisor):
    """带 fltt=2 时 divisor=1（原值已是小数）；旧路径 divisor=100。"""
    f = C.safe_float(value)
    return None if f is None else f / divisor


def _shares(hand):
    """成交量：手 -> 股（×100，契约 §4 口径）。"""
    f = C.safe_float(hand)
    return None if f is None else int(round(f * 100))


def _text(value, max_len=0):
    return C.clean_text(value, max_len=max_len) if value else ""


_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([，。；：、！？）】》」』,.;:!?)\]])")
_SPACE_AFTER_OPEN_RE = re.compile(r"([（【《「『(\[])\s+")


def _clean_title(value, max_len=0):
    """标题清洗：去 HTML 标签 + 去掉 CJK 标点两侧多余空格。

    ``clean_text`` 把 ``</em>：`` 变成 ``" ："``，需要再收一次空格
    （例：``<em>美瑞新材</em>：公告`` -> ``美瑞新材：公告``）。
    """
    s = C.clean_text(value, max_len=max_len)
    s = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", s)
    s = _SPACE_AFTER_OPEN_RE.sub(r"\1", s)
    return s


def _hms(seconds):
    """涨停池 fbt/lbt 时间 -> 'HH:MM:SS'。

    实测东财返回的是 **HHMMSS 整数编码**（``fbt=92500`` -> 09:25:00，
    ``92502`` -> 09:25:02），契约里写的“秒数”是误记；这里两者都兼容：
    > 86400 的按 HHMMSS 解，否则按“当日秒数”解。
    """
    n = _i(seconds)
    if n is None or n < 0:
        return None
    if n > 86400:
        s = "%06d" % n
        if len(s) == 6 and int(s[2:4]) < 60 and int(s[4:6]) < 60:
            return "%s:%s:%s" % (s[0:2], s[2:4], s[4:6])
        n = 86399
    return "%02d:%02d:%02d" % (n // 3600, (n % 3600) // 60, n % 60)


def _round(value, digits=3):
    f = C.safe_float(value)
    return None if f is None else round(f, digits)


# --------------------------------------------------------------------------- #
# secid / market helpers（单测覆盖）
# --------------------------------------------------------------------------- #
def normalize_stock(stock: dict) -> dict:
    """把 watchlist 条目补全成 {code,name,tags,note,secid,market,currency,is_ashare}。

    兼容 ``common.load_watchlist`` 的新旧输出：
    * A股：``market ∈ {SH,SZ,BJ}``、``currency="CNY"``、``is_ashare=True``；
    * 美股/港股：显式 ``market="US"/"HK"`` + ``secid``（如 ``105.NVDA``），
      ``currency`` 默认 USD（US）/ 空（其他）；A股/美股代码都保持原样（不改大小写）。
    """
    s = dict(stock or {})
    code = str(s.get("code") or "").strip()
    explicit_market = str(s.get("market") or "").strip().upper()
    if not explicit_market and "." in code:        # 兼容 "0.300848" 这种写法
        code = code.split(".")[-1]
    s["code"] = code
    s["name"] = s.get("name") or code
    s["tags"] = list(s.get("tags") or [])
    s["note"] = s.get("note") or ""
    if explicit_market:
        s["market"] = explicit_market
        s["secid"] = s.get("secid") or C.secid(code)
    else:
        s["market"] = C.market_label(code)
        s["secid"] = s.get("secid") or C.secid(code)
    if not s.get("currency"):
        s["currency"] = "USD" if s["market"] == "US" else (
            "CNY" if s["market"] in ("SH", "SZ", "BJ") else "")
    if "is_ashare" in s:
        s["is_ashare"] = bool(s["is_ashare"])
    else:
        s["is_ashare"] = s["market"] in ("SH", "SZ", "BJ")
    return s


# --------------------------------------------------------------------------- #
# pure parsers（每个都可离线单测）
# --------------------------------------------------------------------------- #
def parse_ulist_indices(jobj: dict, indices_cfg: list[dict] | None = None) -> list[dict]:
    """指数快照（ulist.np）。按 config 顺序输出，字段 f2/f3/f4/f6/f12/f14。"""
    data = (jobj or {}).get("data") or {}
    diff = data.get("diff") or []
    if not isinstance(diff, list):
        return []
    by_code = {str(it.get("f12") or ""): it for it in diff if isinstance(it, dict)}
    out: list[dict] = []
    specs = indices_cfg or [
        {"secid": "%s.%s" % (it.get("f13", 0), it.get("f12") or ""), "name": it.get("f14")}
        for it in diff if isinstance(it, dict)
    ]
    for spec in specs:
        secid = str(spec.get("secid") or "")
        code = secid.split(".")[-1]
        it = by_code.get(code)
        if it is None:
            continue
        out.append({
            "code": code,
            "name": _text(it.get("f14")) or spec.get("name") or code,
            "price": _f(it.get("f2")),
            "change": _f(it.get("f4")),
            "change_pct": _f(it.get("f3")),
            "amount": _f(it.get("f6")),
        })
    return out


def parse_clist_rows(jobj: dict) -> list[dict]:
    """clist 通用行解析（全市场/板块/涨跌榜）：``data.diff[]`` -> 归一化 dict。

    缺字段/``"-"`` 统一 None，保留 code/name 让下游自行决定是否使用。
    """
    data = (jobj or {}).get("data") or {}
    diff = data.get("diff")
    if diff is None:
        diff = data.get("list") or []
    if not isinstance(diff, list):
        return []
    rows: list[dict] = []
    for it in diff:
        if not isinstance(it, dict):
            continue
        code = str(it.get("f12") or "").strip()
        if not code:
            continue
        rows.append({
            "code": code,
            "name": _text(it.get("f14")),
            "price": _f(it.get("f2")),
            "change_pct": _f(it.get("f3")),
            "amount": _f(it.get("f6")),
            "turnover": _f(it.get("f8")),
            "pe": _f(it.get("f9")),
            "market_cap": _f(it.get("f20")),
            "pb": _f(it.get("f23")),
        })
    return rows


def clist_total(jobj: dict):
    data = (jobj or {}).get("data") or {}
    return _i(data.get("total"))


def parse_movers(jobj: dict, count: int = 15) -> list[dict]:
    """涨跌幅/成交额榜：取 clist 前 count 条，转成契约 §4 movers 结构。"""
    out = []
    for r in parse_clist_rows(jobj)[:count]:
        out.append(_row_to_mover(r))
    return out


def _row_to_mover(row: dict) -> dict:
    return {
        "code": row.get("code"),
        "name": row.get("name"),
        "price": row.get("price"),
        "change_pct": row.get("change_pct"),
        "amount": row.get("amount"),
        "turnover": row.get("turnover"),
        "pe": row.get("pe"),
        "market_cap": row.get("market_cap"),
        "pb": row.get("pb"),
    }


def parse_sectors(jobj: dict, limit: int | None = None) -> list[dict]:
    """板块行情（clist fs=m:90+t:2）。

    实测字段：f12 板块代码 / f14 板块名 / f3 涨跌幅% / f104 上涨家数 /
    f105 下跌家数 / f128 领涨股名 / f136 领涨股涨跌幅% / f140 领涨股代码。
    返回按 change_pct 降序（None 排最后）。
    """
    data = (jobj or {}).get("data") or {}
    diff = data.get("diff") or []
    rows: list[dict] = []
    for it in diff if isinstance(diff, list) else []:
        if not isinstance(it, dict):
            continue
        code = str(it.get("f12") or "").strip()
        if not code:
            continue
        rows.append({
            "code": code,
            "name": _text(it.get("f14")),
            "change_pct": _f(it.get("f3")),
            "up_count": _i(it.get("f104")),
            "down_count": _i(it.get("f105")),
            "leader_name": _text(it.get("f128")) or None,
            "leader_code": str(it.get("f140") or "") or None,
            "leader_change_pct": _f(it.get("f136")),
        })
    rows.sort(key=lambda r: (r.get("change_pct") is None,
                             -(r.get("change_pct") or 0.0)))
    return rows[:limit] if limit else rows


def parse_limit_pool(jobj: dict, limit: int = 60) -> list[dict]:
    """涨停池 getTopicZTPool：``data.pool[]``，字段 c/n/zdp/fbt/lbc/hybk。

    ``first_seal_time`` 由 fbt（当日秒数）转 HH:MM:SS；``boards`` 保留连板数，
    ``reason`` 用行业板块名（契约 §4 示例口径），report 会用到连板数。
    """
    data = (jobj or {}).get("data") or {}
    pool = data.get("pool") or []
    out: list[dict] = []
    for it in pool if isinstance(pool, list) else []:
        if not isinstance(it, dict):
            continue
        code = str(it.get("c") or "").strip()
        if not code:
            continue
        out.append({
            "code": code,
            "name": _text(it.get("n")),
            "change_pct": _f(it.get("zdp")),
            "first_seal_time": _hms(it.get("fbt")),
            "reason": _text(it.get("hybk")) or None,
            "boards": _i(it.get("lbc")),
        })
        if len(out) >= limit:
            break
    return out


def _empty_quote() -> dict:
    return {k: None for k in _QUOTE_KEYS}


def parse_stock_quote(jobj: dict, scaled: bool = False) -> dict:
    """个股快照（stock/get）-> 契约 §4 quote。

    ``scaled=False``（默认，推荐）：请求带 fltt=2，返回已是小数，直接使用。
    ``scaled=True``：请求未带 fltt=2 的旧行为，返回 ×100 整数，需 ÷100
    （价格/涨跌/涨跌幅/量比/PE/PB/换手/振幅；成交额/市值/成交量不缩）。
    """
    d = (jobj or {}).get("data")
    q = _empty_quote()
    if not isinstance(d, dict):
        return q
    div = 100.0 if scaled else 1.0
    q["price"] = _div100(d.get("f43"), div)
    q["high"] = _div100(d.get("f44"), div)
    q["low"] = _div100(d.get("f45"), div)
    q["open"] = _div100(d.get("f46"), div)
    q["volume"] = _shares(d.get("f47"))          # f47 单位=手 -> 股
    q["amount"] = _f(d.get("f48"))
    q["volume_ratio"] = _div100(d.get("f50"), div)
    q["prev_close"] = _div100(d.get("f60"), div)
    q["market_cap"] = _f(d.get("f116"))
    q["float_cap"] = _f(d.get("f117"))
    q["pe"] = _div100(d.get("f162"), div)
    q["pb"] = _div100(d.get("f167"), div)
    q["turnover"] = _div100(d.get("f168"), div)
    q["change"] = _div100(d.get("f169"), div)
    q["change_pct"] = _div100(d.get("f170"), div)
    q["amplitude"] = _div100(d.get("f171"), div)
    return q


def parse_ulist_quote(jobj: dict, code: str) -> dict:
    """个股快照兜底（ulist.np，单 secid）——stock/get 失败/缺字段时用。

    实测 ulist 字段：f2 价 f3 涨跌幅 f4 涨跌额 f5 成交量(手) f6 成交额
    f8 换手率 f9 PE f10 量比 f15 最高 f16 最低 f17 今开 f18 昨收
    f20 总市值 f21 流通市值 f23 PB。
    """
    q = _empty_quote()
    data = (jobj or {}).get("data") or {}
    diff = data.get("diff") or []
    if not isinstance(diff, list):
        return q
    code = str(code).strip()
    for it in diff:
        if isinstance(it, dict) and str(it.get("f12")) == code:
            q["price"] = _f(it.get("f2"))
            q["change_pct"] = _f(it.get("f3"))
            q["change"] = _f(it.get("f4"))
            q["volume"] = _shares(it.get("f5"))
            q["amount"] = _f(it.get("f6"))
            q["turnover"] = _f(it.get("f8"))
            q["pe"] = _f(it.get("f9"))
            q["volume_ratio"] = _f(it.get("f10"))       # f10=量比
            q["high"] = _f(it.get("f15"))
            q["low"] = _f(it.get("f16"))
            q["open"] = _f(it.get("f17"))
            q["prev_close"] = _f(it.get("f18"))
            q["market_cap"] = _f(it.get("f20"))
            q["float_cap"] = _f(it.get("f21"))
            q["pb"] = _f(it.get("f23"))
            break
    return q


_ULIST_QUOTE_FIELDS = ("f2,f3,f4,f5,f6,f8,f9,f10,f12,f14,f15,f16,f17,f18,"
                       "f20,f21,f23")


def _merge_quote(primary: dict, fallback: dict) -> dict:
    """用 fallback 的缺失字段补 primary（主快照优先）。"""
    out = dict(primary)
    for key in _QUOTE_KEYS:
        if out.get(key) is None and fallback.get(key) is not None:
            out[key] = fallback[key]
    return out


def _parse_us_quote(jobj: dict, code: str) -> dict:
    """美股快照解析（东财 ulist.np，``f1=3`` 表示美股）。

    实测字段：``f2`` 价 ``f3`` 涨跌幅 ``f4`` 涨跌额 ``f5`` 成交量
    ``f9`` PE ``f15`` 最高 ``f16`` 最低 ``f17`` 今开 ``f18`` 昨收
    ``f20`` 总市值 ``f21`` 流通市值 ``f23`` PB（``f1=3`` 见返回行内）。
    **货币是 USD**：``amount``(f6)/``turnover``/``volume_ratio`` 一律 None，
    避免下游把 USD 当“元”；货币口径见 watchlist[].currency。
    """
    q = _empty_quote()
    data = (jobj or {}).get("data") or {}
    diff = data.get("diff") or []
    if not isinstance(diff, list):
        return q
    code = str(code).strip()
    for it in diff:
        if not isinstance(it, dict) or str(it.get("f12")) != code:
            continue
        q["price"] = _f(it.get("f2"))
        q["change_pct"] = _f(it.get("f3"))
        q["change"] = _f(it.get("f4"))
        q["volume"] = _i(it.get("f5"))      # 美股成交量口径以东财原始为准（股）
        q["open"] = _f(it.get("f17"))
        q["high"] = _f(it.get("f15"))
        q["low"] = _f(it.get("f16"))
        q["prev_close"] = _f(it.get("f18"))
        q["pe"] = _f(it.get("f9"))
        q["pb"] = _f(it.get("f23"))
        q["market_cap"] = _f(it.get("f20"))
        q["float_cap"] = _f(it.get("f21"))
        break
    return q


def parse_kline(jobj: dict, limit: int = 0) -> list[dict]:
    """日K：``data.klines[]`` 逗号串。

    顺序（契约 §6 + 实测）：
      日期,开,收,高,低,成交量(手),成交额(元),振幅%,涨跌幅%,涨跌额,换手率%
    ``volume`` 统一 ×100 转“股”；``amount`` 元；涨跌幅已是百分数。
    """
    data = (jobj or {}).get("data") or {}
    rows: list[dict] = []
    for line in data.get("klines") or []:
        parts = str(line).split(",")
        if len(parts) < 7:
            continue
        rows.append({
            "date": parts[0],
            "open": _f(parts[1]),
            "close": _f(parts[2]),
            "high": _f(parts[3]),
            "low": _f(parts[4]),
            "volume": _shares(parts[5]),
            "amount": _f(parts[6]),
            "amplitude": _f(parts[7]) if len(parts) > 7 else None,
            "change_pct": _f(parts[8]) if len(parts) > 8 else None,
            "change": _f(parts[9]) if len(parts) > 9 else None,
            "turnover": _f(parts[10]) if len(parts) > 10 else None,
        })
    return rows[-limit:] if limit and limit > 0 else rows


def parse_fund_flow(jobj: dict) -> dict:
    """资金流（fflow/kline）：``data.klines[]`` = 日期,主力净流入,小单,中单,大单,超大单。

    实测校验：主力净流入 == 大单 + 超大单（样例 -56773 + -3073984 = -3130757）。
    单位元。返回 ``{"today_main_net": float|None, "recent": [...]}``。
    """
    data = (jobj or {}).get("data") or {}
    recent: list[dict] = []
    for line in data.get("klines") or []:
        parts = str(line).split(",")
        if len(parts) < 2:
            continue
        item = {"date": parts[0], "main_net": _f(parts[1])}
        if len(parts) >= 6:
            item["small_net"] = _f(parts[2])
            item["medium_net"] = _f(parts[3])
            item["large_net"] = _f(parts[4])
            item["super_large_net"] = _f(parts[5])
        recent.append(item)
    today = recent[-1].get("main_net") if recent else None
    return {"today_main_net": today, "recent": recent}


def parse_announcements(jobj: dict, code: str, limit: int = 20) -> list[dict]:
    """公告（security/ann）：``data.list[]`` -> {title,date,type,url}。"""
    data = (jobj or {}).get("data") or {}
    lst = data.get("list") or []
    out: list[dict] = []
    for it in lst if isinstance(lst, list) else []:
        if not isinstance(it, dict):
            continue
        art = str(it.get("art_code") or "").strip()
        cols = it.get("columns") or []
        ctype = None
        if isinstance(cols, list) and cols and isinstance(cols[0], dict):
            ctype = _text(cols[0].get("column_name")) or None
        notice = it.get("notice_date") or it.get("display_time") or ""
        date = str(notice)[:10] or None
        title = _clean_title(it.get("title") or it.get("title_ch"))
        if not title:
            continue
        url = ("https://data.eastmoney.com/notices/detail/%s/%s.html" % (code, art)
               if art else None)
        out.append({"title": title, "date": date, "type": ctype, "url": url})
        if len(out) >= limit:
            break
    return out


def parse_news(payload, limit: int = 8) -> list[dict]:
    """个股新闻（search-api-web JSONP）。

    ``payload`` 可以是 JSONP 文本或已解析的 dict；``result.cmsArticleWebOld[]``
    -> {title(去 <em>),date(ISO+08:00),source,url}。
    """
    obj = payload
    if isinstance(obj, (bytes, str)):
        text = obj.decode("utf-8", "replace") if isinstance(obj, bytes) else obj
        obj = json.loads(C.strip_jsonp(text))
    result = (obj or {}).get("result") or {}
    items = result.get("cmsArticleWebOld") or []
    out: list[dict] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        title = _clean_title(it.get("title"))
        if not title:
            continue
        date = it.get("date")
        out.append({
            "title": title,
            "date": C.iso(C.parse_dt_any(date)) or (str(date) if date else None),
            "source": _text(it.get("mediaName")) or None,
            "url": str(it.get("url") or "") or None,
        })
        if len(out) >= limit:
            break
    return out


def parse_reports(jobj: dict, limit: int = 5) -> list[dict]:
    """研报（reportapi）：``data[]`` -> {title,org,date,url}。"""
    data = (jobj or {}).get("data") or []
    out: list[dict] = []
    for it in data if isinstance(data, list) else []:
        if not isinstance(it, dict):
            continue
        title = _clean_title(it.get("title"))
        if not title:
            continue
        info = str(it.get("infoCode") or "").strip()
        pub = it.get("publishDate") or ""
        out.append({
            "title": title,
            "org": _text(it.get("orgSName") or it.get("orgName")) or None,
            "date": str(pub)[:10] or None,
            "url": ("https://data.eastmoney.com/report/info/%s.html" % info
                    if info else None),
        })
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# fallback 数据源：腾讯日K / 新浪行情（东财 push2 的 clist/stock/get/kline
# 端点被 WAF 限流时使用；实测 2026-11-18 当天 push2 仅 ulist/fflow 可用）
# --------------------------------------------------------------------------- #
_TENCENT_REF = "https://gu.qq.com/"
_TENCENT_KLINE = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                  "?param=%s,day,,,%d,qfq")
_SINA_REF = "https://finance.sina.com.cn/"
_SINA_HQ = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeData")
_SINA_COUNT = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
               "json_v2.php/Market_Center.getHQNodeStockCount?node=hs_a")
_SINA_HY = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"


def _tencent_symbol(code: str, market: str | None = None) -> str:
    """代码/市场 -> 腾讯 symbol：A股 sh/sz/bj 前缀 + 6 位；美股 us 前缀 + 大写。

    '300848'/'SZ' -> 'sz300848'；'NVDA'/'US' -> 'usNVDA'。
    """
    m = str(market or C.market_label(code)).upper()
    c = str(code).strip()
    if m == "US":
        return "us" + c.upper()
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(m, "sz") + c


def parse_tencent_kline(jobj: dict, symbol: str, limit: int = 0,
                        volume_in_hands: bool = True) -> list[dict]:
    """腾讯日K 解析（A股 ``qfqday`` / 美股 ``day`` 两种 key 都兼容）。

    ``data[symbol].qfqday | data[symbol].day`` 每行：
      ``[date, open, close, high, low, volume]``（实测 2026-11-18 A股样例
      ``['2026-11-18','9.350','9.100','9.450','9.060','30814.000']``）。
    ``volume_in_hands=True``（A股）：成交量单位“手” → ×100 转“股”；
    美股腾讯返回单位是“股”，传 ``False`` 原样保留。
    ``change_pct`` 用前一日收盘计算；该接口无成交额/换手率 → ``None``。
    """
    data = (jobj or {}).get("data") or {}
    node = data.get(symbol) or {}
    raw_rows = node.get("qfqday") or node.get("day") or []
    rows: list[dict] = []
    for raw in raw_rows if isinstance(raw_rows, list) else []:
        if not isinstance(raw, (list, tuple)) or len(raw) < 6:
            continue
        volume = _shares(raw[5]) if volume_in_hands else _i(raw[5])
        rows.append({
            "date": str(raw[0]), "open": _f(raw[1]), "close": _f(raw[2]),
            "high": _f(raw[3]), "low": _f(raw[4]),
            "volume": volume,
            "amount": None, "amplitude": None, "change_pct": None,
            "change": None, "turnover": None,
        })
    prev = None
    for row in rows:
        close, high, low = row.get("close"), row.get("high"), row.get("low")
        if prev and close is not None:
            row["change"] = round(close - prev, 4)
            row["change_pct"] = round((close - prev) / prev * 100, 4)
            if high is not None and low is not None:
                row["amplitude"] = round((high - low) / prev * 100, 4)
        prev = close
    return rows[-limit:] if limit and limit > 0 else rows


# 兼容 Lead 验收单测命名（_parse_tencent_kline）
_parse_tencent_kline = parse_tencent_kline


def _tencent_us_symbols(code: str, sid: str) -> list[str]:
    """美股腾讯 symbol 候选：``usNVDA.OQ``（纳斯达克）/``usNVDA.N``（纽交所）。

    实测腾讯美股日K：不带交易所后缀只回 2 行稀疏数据，带 ``.OQ`` 才回完整 30 根。
    东财 secid 约定：105.=纳斯达克 -> .OQ；106.=纽交所 -> .N。
    """
    base = _tencent_symbol(code, "US")
    prefix = str(sid or "").split(".")[0].strip()
    suffix = {"105": ".OQ", "106": ".N"}.get(prefix, "")
    out = []
    if suffix:
        out.append(base + suffix)
    out.append(base)
    return out


def _fetch_kline(sid: str, code: str, market: str, days: int,
                 skip_push2: bool = False):
    """日K：push2his 优先（快失败），失败切腾讯（A股 qfqday/美股 day）。

    返回 ``(rows, source_id, error)``；source_id ∈ {"em","tencent","none"}。
    美股传 ``skip_push2=True`` 直连腾讯（东财 push2his 对美股同样被限流，
    且美股 code 不是 6 位），symbol 先试 ``usNVDA.OQ`` 再退 ``usNVDA``。
    """
    last: Exception | None = None
    if not skip_push2:
        path = ("/api/qt/stock/kline/get?secid=%s&fields1=f1,f2,f3,f4,f5,f6"
                "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
                "&klt=101&fqt=1&lmt=%d&end=20500101" % (sid, days))
        try:
            rows = parse_kline(_multi_json(PUSH2HIS_HOSTS, path, retries=1,
                                           backoff=0.4, timeout=8))
            if rows:
                return rows, "em", None
            last = RuntimeError("push2his klines 为空")
        except Exception as exc:  # noqa: BLE001
            last = exc
    is_us = str(market or "").upper() == "US"
    symbols = _tencent_us_symbols(code, sid) if is_us else [_tencent_symbol(code, market)]
    for sym in symbols:
        try:
            jobj = _json(_TENCENT_KLINE % (sym, days), referer=_TENCENT_REF,
                         retries=1)
            rows = parse_tencent_kline(jobj, sym, limit=days,
                                       volume_in_hands=(not is_us))
            if rows:
                return rows, "tencent", None
            last = last or RuntimeError("腾讯 %s klines 为空" % sym)
        except Exception as exc:  # noqa: BLE001
            last = last or exc
    return [], "none", last


def _sina_get_json(url: str):
    _bump_requests("other")
    return C.http_get_json(url, referer=_SINA_REF, timeout=12, retries=2)


def _sina_stock_count() -> int:
    """新浪 A 股总数（返回 JSON 字符串 "5579"）。"""
    _bump_requests("other")
    text = C.http_get_text(_SINA_COUNT, referer=_SINA_REF, timeout=10,
                           retries=2).strip().strip('"')
    try:
        return int(float(text))
    except ValueError:
        return 0


def _sina_page(pn: int, pz: int = 100) -> list[dict]:
    """新浪 A 股分页（sort=changepercent&asc=0，跨页降序，可用于二分宽度）。"""
    url = ("%s?page=%d&num=%d&sort=changepercent&asc=0&node=hs_a"
           "&symbol=&_s_r_a=page" % (_SINA_HQ, pn, pz))
    data = _sina_get_json(url)
    rows: list[dict] = []
    for it in data if isinstance(data, list) else []:
        if not isinstance(it, dict):
            continue
        code = str(it.get("code") or "").strip()
        if not code:
            continue
        rows.append({
            "code": code,
            "name": _text(it.get("name")),
            "price": _f(it.get("trade")),
            "change_pct": _f(it.get("changepercent")),
            "amount": _f(it.get("amount")),
            "volume": _shares(it.get("volume")),
        })
    return rows


def parse_sina_movers(items: list[dict]) -> list[dict]:
    """新浪 hs_a 列表 -> 契约 §4 movers 结构（mktcap/nmc 单位万元 -> 元）。"""
    out: list[dict] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        code = str(it.get("code") or "").strip()
        if not code:
            continue
        mktcap = _f(it.get("mktcap"))
        out.append({
            "code": code,
            "name": _text(it.get("name")),
            "price": _f(it.get("trade")),
            "change_pct": _f(it.get("changepercent")),
            "amount": _f(it.get("amount")),
            "turnover": _f(it.get("turnoverratio")),
            "pe": _f(it.get("per")),
            "market_cap": round(mktcap * 1e4, 2) if mktcap is not None else None,
            "pb": _f(it.get("pb")),
        })
    return out


def _sina_movers(kind: str, count: int) -> list[dict]:
    sort_key, asc = {"gainers": ("changepercent", 0),
                     "losers": ("changepercent", 1),
                     "turnover": ("amount", 0)}[kind]
    url = ("%s?page=1&num=%d&sort=%s&asc=%d&node=hs_a&symbol=&_s_r_a=page"
           % (_SINA_HQ, count, sort_key, asc))
    data = _sina_get_json(url)
    return parse_sina_movers(data if isinstance(data, list) else [])


def parse_sina_sectors(text: str) -> list[dict]:
    """新浪行业板块解析（gb18030 文本，形如 var S_Finance_bankuai_sinaindustry = {...}）。

    每项逗号串（实测 49 个板块）：
      ``[0]code [1]名称 [2]只数 [3]均价 [4]涨跌额 [5]涨跌幅% [6]成交量
      [7]成交额 [8]领涨股symbol [9]领涨股涨幅% [10]领涨股价 [11]领涨股涨跌额
      [12]领涨股名``
    """
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("新浪行业板块响应格式异常")
    obj = json.loads(text[start:end + 1])
    rows: list[dict] = []
    for _key, val in (obj or {}).items():
        parts = str(val).split(",")
        if len(parts) < 13:
            continue
        sym = str(parts[8]).strip()
        rows.append({
            "code": str(parts[0]).strip(),
            "name": str(parts[1]).strip(),
            "change_pct": _f(parts[5]),
            "up_count": None,
            "down_count": None,
            "leader_name": str(parts[12]).strip() or None,
            "leader_code": sym[2:] if len(sym) > 2 else (sym or None),
            "leader_change_pct": _f(parts[9]),
        })
    rows.sort(key=lambda r: (r.get("change_pct") is None,
                             -(r.get("change_pct") or 0.0)))
    return rows


def aggregate_breadth(pages: list[list[dict]], limit_pct: float = 9.5) -> dict:
    """把全市场分页行聚合成契约 §4 breadth。

    ``total`` 用实际统计到的家数（up+down+flat），保证
    ``up + down + flat == total``；``|change_pct| >= limit_pct`` 计涨跌停。
    """
    up = down = flat = limit_up = limit_down = 0
    for rows in pages or []:
        for r in rows or []:
            pct = C.safe_float(r.get("change_pct"))
            if pct is None:
                continue
            if pct > _EPS:
                up += 1
            elif pct < -_EPS:
                down += 1
            else:
                flat += 1
            if pct >= limit_pct:
                limit_up += 1
            elif pct <= -limit_pct:
                limit_down += 1
    total = up + down + flat
    return {
        "total": total,
        "up": up,
        "down": down,
        "flat": flat,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "up_ratio": round(up / total, 4) if total else None,
    }


# --------------------------------------------------------------------------- #
# alerts（异动提示，契约 §6）
# --------------------------------------------------------------------------- #
def _within_days(date_value, today: str | None, days: int) -> bool:
    """date_value（'YYYY-MM-DD' 或 ISO）是否落在 today 往前 days 天内。"""
    dt = C.parse_dt_any(date_value)
    ref = C.parse_dt_any(today) if today else C.now_cn()
    if dt is None or ref is None:
        return False
    return (ref - dt).total_seconds() <= days * 86400 + 3600 and \
        (ref - dt).total_seconds() >= -86400


def build_stock_alerts(stock: dict, thresholds: dict | None = None,
                       today: str | None = None,
                       recent_days: int = 7, max_alerts: int = 4) -> list[dict]:
    """单只自选股的 alerts（level/text，契约 §4 watchlist[].alerts）。

    * 只取 **近 recent_days 天**（默认 7）的公告/新闻触发提示；
    * 同文本/同公告关键词**只保留一条**（避免「回购进展」重复刷屏）；
    * 每只股票最多 ``max_alerts`` 条（默认 4），按 level 排序。
    """
    th = thresholds or {}
    big = float(th.get("big_move_pct") or 5.0)
    limit = float(th.get("limit_pct") or 9.5)
    spike = float(th.get("volume_spike_ratio") or 2.0)
    quote = stock.get("quote") or {}
    name = stock.get("name") or stock.get("code") or ""
    alerts: list[dict] = []
    seen_text: set[str] = set()

    def _add(level: str, kind: str, text: str) -> None:
        key = re.sub(r"\s+", "", text)
        if key in seen_text:
            return
        seen_text.add(key)
        alerts.append({"level": level, "kind": kind, "text": text})

    pct = C.safe_float(quote.get("change_pct"))
    if pct is not None:
        if pct >= limit:
            _add("high", "limit_up", "%s 涨停，涨幅 %+.2f%%" % (name, pct))
        elif pct <= -limit:
            _add("high", "limit_down", "%s 跌停，跌幅 %+.2f%%" % (name, pct))
        elif pct >= big:
            _add("high", "big_move", "%s 大涨 %+.2f%%，注意异动" % (name, pct))
        elif pct <= -big:
            _add("warn", "big_move", "%s 大跌 %+.2f%%，注意风险" % (name, pct))

    vr = C.safe_float(quote.get("volume_ratio"))
    if vr is not None and vr >= spike:
        _add("info", "volume_spike", "%s 量比 %.2f，明显放量" % (name, vr))

    # 公告：近 7 天 + 按关键词去重（同类只提示一次）
    hit_keywords: set[str] = set()
    for ann in stock.get("announcements") or []:
        if not _within_days(ann.get("date"), today, recent_days):
            continue
        title = ann.get("title") or ""
        for kw, level in _ANN_KEYWORDS.items():
            if kw in title and kw not in hit_keywords:
                hit_keywords.add(kw)
                _add(level, "announcement", "公告：%s" % _clean_title(title, 72))
                break

    # 新闻：近 7 天内标题命中股票名的计数
    news = [n for n in (stock.get("news") or [])
            if _within_days(n.get("date"), today, recent_days)]
    if name and news:
        hits = sum(1 for n in news if name in (n.get("title") or ""))
        if hits:
            _add("info", "news", "近 %d 天 %d 条新闻标题命中「%s」"
                 % (recent_days, hits, name))

    reports = [r for r in (stock.get("reports") or [])
               if _within_days(r.get("date"), today, 90)]
    if reports:
        orgs = []
        for r in reports:
            org = r.get("org")
            if org and org not in orgs:
                orgs.append(org)
        if orgs:
            _add("info", "report", "近 90 天研报 %d 篇（%s）"
                 % (len(reports), "、".join(orgs[:3])))

    alerts.sort(key=lambda a: _LEVEL_RANK.get(a.get("level"), 9))
    return alerts[:max_alerts]


def flatten_alerts(watchlist: list[dict], max_alerts: int = 20,
                   when: str | None = None) -> list[dict]:
    """把各股 alerts 汇总成契约 §4 顶层 alerts（去重 + level 排序 + 截断）。"""
    flat: list[dict] = []
    seen: set[str] = set()
    for st in watchlist or []:
        code = st.get("code")
        name = st.get("name") or code
        for a in st.get("alerts") or []:
            text = a.get("text") or ""
            if name and name not in text:
                text = "%s %s" % (name, text)
            key = re.sub(r"\s+", "", text)
            if key in seen:
                continue
            seen.add(key)
            flat.append({
                "level": a.get("level") or "info",
                "kind": a.get("kind") or "general",
                "text": text,
                "ref": code,
                "when": when,
            })
    flat.sort(key=lambda a: _LEVEL_RANK.get(a.get("level"), 9))
    return flat[:max_alerts]


def build_watchlist_meta(watchlist: list[dict]) -> dict:
    """契约 §4 watchlist_meta：count/avg_change_pct/best/worst/total_amount。

    * ``count`` / ``avg_change_pct`` / ``best`` / ``worst`` 统计**全部标的**
      （A股 + 美股，涨跌幅都是百分数可直接比较）；
    * ``total_amount`` **只累加 A股（CNY）**，避免把 USD 成交额混进“元”。
    """
    rows = []
    total_amount = 0.0
    has_amount = False
    for st in watchlist or []:
        q = st.get("quote") or {}
        pct = C.safe_float(q.get("change_pct"))
        amt = C.safe_float(q.get("amount"))
        currency = st.get("currency") or ("USD" if st.get("market") == "US" else "CNY")
        if amt is not None and currency == "CNY":
            total_amount += amt
            has_amount = True
        rows.append({"code": st.get("code"), "name": st.get("name"), "change_pct": pct})
    ranked = [r for r in rows if r["change_pct"] is not None]
    best = max(ranked, key=lambda r: r["change_pct"]) if ranked else None
    worst = min(ranked, key=lambda r: r["change_pct"]) if ranked else None
    avg = (sum(r["change_pct"] for r in ranked) / len(ranked)) if ranked else None
    return {
        "count": len(watchlist or []),
        "avg_change_pct": _round(avg, 3),
        "best": best,
        "worst": worst,
        "total_amount": round(total_amount, 2) if has_amount else None,
    }


# --------------------------------------------------------------------------- #
# HTTP layer（含降级与重试）
# --------------------------------------------------------------------------- #
def _multi_json(hosts, path_qs: str, referer: str = _REF,
                retries: int = 3, backoff: float = 2.0, timeout: float = 10):
    """东财行情（push2 系）请求：**并发限 4 + 抖动 + 退避重试**。

    限流原因：一次带 56 页 breadth + 6 股多接口的 live 会把 push2 打成
    批量 RemoteDisconnected，因此这里用模块级信号量把东财行情域并发压到 4，
    每次请求前加 0.1~0.25s 随机抖动，失败按 ``backoff`` 退避（common.http_get）。
    第一个 host 用 ``retries`` 次重试；后续 host（https 兜底）只试 1 次。
    """
    with _EASTMONEY_SEM:
        time.sleep(random.uniform(0.05, 0.15))
        _pace(0.1)
        last: Exception | None = None
        for idx, host in enumerate(hosts):
            _bump_requests("eastmoney")
            try:
                return C.http_get_json(host + path_qs, referer=referer,
                                       timeout=timeout,
                                       retries=retries if idx == 0 else 0,
                                       backoff=backoff)
            except Exception as exc:  # noqa: BLE001 - 网络层 best-effort
                last = exc
        if last is not None:
            raise last
        raise C.HttpError("empty request")


def _push2_json(path_qs: str, retries: int = 3, timeout: float = 10,
                backoff: float = 2.0):
    """push2 行情接口：http 优先 + https 兜底（本机 https 实测频繁断连）。"""
    return _multi_json(PUSH2_HOSTS, path_qs, referer=_REF,
                       retries=retries, backoff=backoff, timeout=timeout)


def _json(url: str, referer: str = _REF, retries: int = 2, timeout: float = 12):
    """非 push2 域（公告/新闻/研报，https 稳定）：普通请求 + 计数。"""
    _bump_requests("other")
    return C.http_get_json(url, referer=referer, timeout=timeout, retries=retries)


def _record(sources: list[dict], sid: str, name: str, ok: bool, *,
            count=None, error=None, elapsed_ms=None) -> None:
    """往 sources 里记一条状态（线程安全：list.append 原子）。"""
    item = {"id": sid, "name": name, "ok": bool(ok), "error": None}
    if count is not None:
        item["count"] = int(count)
    if elapsed_ms is not None:
        item["elapsed_ms"] = int(elapsed_ms)
    if error is not None:
        item["error"] = str(error)[:180]
    sources.append(item)


# --------------------------------------------------------------------------- #
# aggregator collectors
# --------------------------------------------------------------------------- #
def _collect_indices(acfg: dict, sources: list[dict]) -> list[dict]:
    specs = acfg.get("indices") or []
    secids = ",".join(str(s.get("secid") or C.secid(str(s.get("code") or "")))
                      for s in specs)
    if not secids:
        _record(sources, "em_indices", "指数快照", False, error="config.ashare.indices 为空")
        return []
    url = ("%s/api/qt/ulist.np/get?fltt=2&invt=2"
           "&fields=f1,f2,f3,f4,f6,f12,f13,f14&secids=%s" % (PUSH2, secids))
    t0 = time.time()
    try:
        jobj = _push2_json(url[len(PUSH2):], retries=2)
        rows = parse_ulist_indices(jobj, specs)
        _record(sources, "em_indices", "指数快照", bool(rows), count=len(rows),
                elapsed_ms=(time.time() - t0) * 1000,
                error=None if rows else "响应无 data")
        return rows
    except Exception as exc:  # noqa: BLE001
        _record(sources, "em_indices", "指数快照", False, error=exc,
                elapsed_ms=(time.time() - t0) * 1000)
        return []


def _collect_sectors(acfg: dict, sources: list[dict]) -> dict:
    """板块涨幅榜 / 跌幅榜（东财 clist 主，新浪行业 fallback）。

    东财实测字段：f12 板块代码 f14 板块名 f3 涨跌幅% f104 涨家数 f105 跌家数
    f128 领涨股 f136 领涨股涨跌幅% f140 领涨股代码；跌幅榜必须用 ``po=0``（升序）
    单独请求 —— 不能拿涨幅榜倒序充数。
    东财 clist 被限流时切新浪行业（gb18030，49 个板块，无涨跌家数 → None）。
    """
    top_n = int(acfg.get("sectors_top") or 12)
    out = {"top": [], "bottom": []}
    t0 = time.time()
    errors: list[str] = []
    fields = "f2,f3,f12,f14,f104,f105,f128,f136,f140"
    common_qs = ("/api/qt/clist/get?pn=1&np=1&fltt=2&invt=2&fid=f3&fs=" + FS_SECTOR +
                 "&fields=" + fields + "&pz=%d&po=%d")
    ok_any = False
    for key, po, pz in (("top", 1, top_n), ("bottom", 0, 5)):
        try:
            jobj = _push2_json(common_qs % (pz, po), retries=1, backoff=0.4)
            rows = [r for r in parse_sectors(jobj) if r.get("change_pct") is not None]
            if key == "bottom":
                rows.sort(key=lambda r: r.get("change_pct"))       # 升序
                rows = rows[:5]
            else:
                rows = rows[:top_n]                               # 已降序
            out[key] = rows
            ok_any = ok_any or bool(rows)
            if not rows:
                errors.append("%s 无数据" % key)
        except Exception as exc:  # noqa: BLE001
            errors.append("%s: %s" % (key, str(exc)[:70]))

    if ok_any:
        _record(sources, "em_sectors", "板块行情(东财)", True,
                count=len(out["top"]) + len(out["bottom"]),
                elapsed_ms=(time.time() - t0) * 1000,
                error=None if not errors else "；".join(errors))
        return out
    _record(sources, "em_sectors", "板块行情(东财)", False,
            elapsed_ms=(time.time() - t0) * 1000,
            error="；".join(errors) or "clist 不可用")

    # ---- fallback：新浪行业板块 ------------------------------------------
    try:
        _bump_requests("other")
        text = C.http_get_text(_SINA_HY, encoding="gb18030", referer=_SINA_REF,
                               timeout=12, retries=2)
        rows = [r for r in parse_sina_sectors(text) if r.get("change_pct") is not None]
        if not rows:
            raise RuntimeError("新浪行业板块为空")
        out["top"] = rows[:top_n]
        out["bottom"] = sorted(rows, key=lambda r: r["change_pct"])[:5]
        _record(sources, "sina_sectors", "新浪-行业板块(fallback)", True,
                count=len(rows), elapsed_ms=(time.time() - t0) * 1000)
        return out
    except Exception as exc:  # noqa: BLE001
        _record(sources, "sina_sectors", "新浪-行业板块(fallback)", False,
                error=exc, elapsed_ms=(time.time() - t0) * 1000)
        return out


def _collect_movers(acfg: dict, sources: list[dict]) -> dict:
    """涨跌幅/成交额榜：各取 movers_count 条（东财 clist 主，新浪 hs_a fallback）。"""
    n = int(acfg.get("movers_count") or 15)
    out = {"gainers": [], "losers": [], "turnover": []}
    t0 = time.time()
    errors: list[str] = []
    base = ("/api/qt/clist/get?pn=1&np=1&fltt=2&invt=2&fs=" + FS_ALL +
            "&pz=%d&fields=f2,f3,f6,f8,f9,f12,f14,f20,f23" % n)
    for key, extra in (("gainers", "&po=1&fid=f3"),
                       ("losers", "&po=0&fid=f3"),
                       ("turnover", "&po=1&fid=f6")):
        try:
            rows = parse_movers(_push2_json(base + extra, retries=1,
                                            backoff=0.4), count=n)
            out[key] = rows
            if not rows:
                errors.append("%s 无数据" % key)
        except Exception as exc:  # noqa: BLE001
            errors.append("%s: %s" % (key, str(exc)[:70]))

    if any(out.values()):
        _record(sources, "em_movers", "涨跌幅/成交额榜(东财)",
                True, count=sum(len(v) for v in out.values()),
                elapsed_ms=(time.time() - t0) * 1000,
                error=None if not errors else "；".join(errors))
        return out
    _record(sources, "em_movers", "涨跌幅/成交额榜(东财)", False,
            elapsed_ms=(time.time() - t0) * 1000,
            error="；".join(errors) or "clist 不可用")

    # ---- fallback：新浪 hs_a 榜单 ----------------------------------------
    try:
        for key in ("gainers", "losers", "turnover"):
            out[key] = _sina_movers(key, n)
        total = sum(len(v) for v in out.values())
        if total <= 0:
            raise RuntimeError("新浪榜单为空")
        _record(sources, "sina_movers", "新浪-涨跌/成交额榜(fallback)", True,
                count=total, elapsed_ms=(time.time() - t0) * 1000)
        return out
    except Exception as exc:  # noqa: BLE001
        _record(sources, "sina_movers", "新浪-涨跌/成交额榜(fallback)", False,
                error=exc, elapsed_ms=(time.time() - t0) * 1000)
        return out


def _collect_limit_pools(date_str: str, sources: list[dict]) -> dict:
    """涨停池 getTopicZTPool + 跌停池 getTopicDTPool（同构；push2ex 实测可用）。

    实测：20261118 涨停池 data.pool 41 条；跌停池返回 ``data: null``
    （该日接口无数据）→ 空列表降级，跌停家数回退到 breadth 阈值统计。
    """
    compact = str(date_str or "").replace("-", "")
    out = {"up": [], "down": [], "up_ok": False, "down_ok": False,
           "up_count": 0, "down_count": 0}
    t0 = time.time()
    errors: list[str] = []
    for key, topic in (("up", "getTopicZTPool"), ("down", "getTopicDTPool")):
        path = ("/%s?ut=%s&dpt=wz.ztzt&Pageindex=0&pagesize=300"
                "&sort=fbt%%3Aasc&date=%s" % (topic, UT, compact))
        try:
            jobj = _multi_json(PUSH2EX_HOSTS, path, retries=2)
            pool = parse_limit_pool(jobj, limit=300)
            data = (jobj or {}).get("data")
            ok = isinstance(data, dict)          # data=null 表示当日无此类数据
            out[key] = pool
            out[key + "_ok"] = ok
            # 池子可能只给总数 pooled=[]（实测 getTopicDTPool: tc=40, pool=[]）
            tc = _i((data or {}).get("tc")) if isinstance(data, dict) else None
            out[key + "_count"] = max(len(pool), tc or 0)
            if not ok:
                errors.append("%s data=null（当日无该池数据）" % key)
        except Exception as exc:  # noqa: BLE001
            errors.append("%s: %s" % (key, str(exc)[:70]))
    _record(sources, "em_limit_pools", "涨跌停池", out["up_ok"] or out["down_ok"],
            count=out["up_count"] + out["down_count"],
            elapsed_ms=(time.time() - t0) * 1000,
            error=None if not errors else "；".join(errors))
    return out


def _counts_from_index(jobj: dict):
    """从指数快照里取全市场涨跌家数。

    东财指数行情带 ``f104`` 上涨家数 / ``f105`` 下跌家数 / ``f106`` 平盘家数
    （板块行情同名字段是涨/跌家数，指数上则是全市场口径）。实测可用时
    1 次请求即可得到整个市场宽度；不可用则返回 None，回退分页统计。
    """
    data = (jobj or {}).get("data") or {}
    diff = data.get("diff") or []
    if not isinstance(diff, list):
        return None
    for it in diff:
        if not isinstance(it, dict):
            continue
        up, down, flat = _i(it.get("f104")), _i(it.get("f105")), _i(it.get("f106"))
        if up is not None and down is not None and up + down >= 1000:
            return {"up": up, "down": down, "flat": flat or 0}
    return None


_BREADTH_CLIST_PATH = ("/api/qt/clist/get?po=1&np=1&fltt=2&invt=2&fid=f3&fs="
                       + FS_ALL + "&fields=f3,f12,f14")


def _count_breadth_rows(rows: list[dict], acc: dict) -> None:
    """把一页行按涨/跌/平计入 acc（``"-"``/None 跳过，不编造）。"""
    for r in rows or []:
        pct = C.safe_float(r.get("change_pct"))
        if pct is None:
            continue
        if pct > _EPS:
            acc["up"] += 1
        elif pct < -_EPS:
            acc["down"] += 1
        else:
            acc["flat"] += 1


def _breadth_binary(pz: int, max_pages: int, limit_pct: float = 9.5,
                    fetch_page=None):
    """**二分定位涨跌分界页**统计全市场宽度（≈1+log2(页数) 次请求）。

    ``clist`` 按 ``f3`` 降序（``fid=f3&po=1``）时页内有序、跨页有序：
      1. 取第 1 页（同时拿到 ``data.total``）；
      2. 二分找 L = 第一个「本页最后一行 f3 <= 0」的页 → 1..L-1 页全是上涨整页；
      3. L 页逐行数（正/负/平/停牌 "-" 都在这附近）；
      4. 从 L+1 往后：若某页首行 f3 < 0，则该页起到倒数第二页都是纯下跌整页，
         末页单独逐行数（可能含停牌 "-"）。
    另外用「首/尾页扫描」数出 ``|f3| >= limit_pct`` 的涨跌停家数（供跌停池
    ``data=null`` 时兜底；正常情况下会被涨停池/跌停池条数覆盖）。
    返回 ``{"acc": {up,down,flat,limit_up,limit_down}, "total_listed", "pages",
    "fetched"}`` 或 None（数据不可用）。
    """
    cache: dict[int, tuple] = {}

    def get(pn: int):
        if pn not in cache:
            if fetch_page is not None:
                cache[pn] = fetch_page(pn)
            else:
                jobj = _push2_json("%s&pn=%d&pz=%d" % (_BREADTH_CLIST_PATH, pn, pz),
                                   retries=1, backoff=0.4)
                cache[pn] = (parse_clist_rows(jobj), clist_total(jobj) or 0)
        return cache[pn]

    rows1, total = get(1)
    if not rows1 or total <= 0:
        return None
    n_pages = min(max_pages, int(math.ceil(total / pz)))
    acc = {"up": 0, "down": 0, "flat": 0, "limit_up": 0, "limit_down": 0}

    # ---- 阈值涨跌停家数（只扫首/尾边界页；分界页外一定在阈值之外）--------
    def _scan_limits():
        p = 1
        while p <= n_pages:                     # 涨停：从首 页往后
            rows = get(p)[0]
            if not rows:
                break
            for r in rows:
                pct = C.safe_float(r.get("change_pct"))
                if pct is not None and pct >= limit_pct:
                    acc["limit_up"] += 1
            last_pct = C.safe_float(rows[-1].get("change_pct"))
            if last_pct is None or last_pct < limit_pct:
                break
            p += 1
        p = n_pages
        while p >= 1:                           # 跌停：从末页往前（跌得最狠在最后）
            rows = get(p)[0]
            if not rows:
                break
            for r in rows:
                pct = C.safe_float(r.get("change_pct"))
                if pct is not None and pct <= -limit_pct:
                    acc["limit_down"] += 1
            last_pct = C.safe_float(rows[-1].get("change_pct"))
            if last_pct is None or last_pct > -limit_pct:
                break
            p -= 1

    if n_pages <= 1:
        _count_breadth_rows(rows1, acc)
        _scan_limits()
        return {"acc": acc, "total_listed": total, "pages": 1,
                "fetched": len(cache)}

    # ---- 1) 二分找 L：第一个「尾行 <= 0 或 None」的页 ---------------------
    lo, hi, L = 1, n_pages, None
    while lo <= hi:
        mid = (lo + hi) // 2
        rows, _t = get(mid)
        if not rows:
            hi = mid - 1
            continue
        last_pct = C.safe_float(rows[-1].get("change_pct"))
        if last_pct is None or last_pct <= 0:
            L = mid
            hi = mid - 1
        else:
            lo = mid + 1

    if L is None:
        # 所有页尾行都 >0：全市场上涨（整页按 pz 计，末页逐行兜底 null）
        acc["up"] += pz * (n_pages - 1)
        _count_breadth_rows(get(n_pages)[0], acc)
        _scan_limits()
        return {"acc": acc, "total_listed": total, "pages": n_pages,
                "fetched": len(cache)}

    # ---- 2) 1..L-1 纯上涨整页；L 页逐行数 --------------------------------
    if L > 1:
        acc["up"] += pz * (L - 1)
    _count_breadth_rows(get(L)[0], acc)

    # ---- 3) L+1.. 找纯下跌整页起点，末页逐行数 ---------------------------
    page_no = L + 1
    while page_no <= n_pages:
        if page_no == n_pages:
            _count_breadth_rows(get(page_no)[0], acc)
            break
        rows = get(page_no)[0]
        if not rows:
            page_no += 1
            continue
        first_pct = C.safe_float(rows[0].get("change_pct"))
        if first_pct is not None and first_pct < 0:
            # page_no .. n_pages-1 全是下跌整页；最后一页逐行（可能有 "-"）
            acc["down"] += pz * (n_pages - page_no)
            _count_breadth_rows(get(n_pages)[0], acc)
            break
        _count_breadth_rows(rows, acc)   # 零/None 边界页：逐行数
        page_no += 1
    _scan_limits()
    return {"acc": acc, "total_listed": total, "pages": n_pages,
            "fetched": len(cache)}


def _collect_breadth(acfg: dict, sources: list[dict]) -> dict:
    """全市场宽度 breadth（多源降级）。

    1. 主路径：东财 clist **二分定位涨跌分界页**（~1+log2(56)≈7 次请求）；
    2. 东财 clist 被 WAF 限流时 → **新浪 getHQNodeData 二分**（同样思路，
       用 `Market_Center.getHQNodeStockCount` 拿总数）；
    3. 全失败：up/down/flat/up_ratio 一律 ``null``（**不填 0**）；
       limit_up/limit_down 留空由涨停池/跌停池在 ``collect`` 里回填。
    """
    pz = int(acfg.get("breadth_page_size") or 100)
    max_pages = int(acfg.get("breadth_max_pages") or 60)
    limit_pct = float((acfg.get("thresholds") or {}).get("limit_pct") or 9.5)
    t0 = time.time()
    errors: list[str] = []

    def _accept(res, method: str, source_id: str, source_name: str):
        acc = res["acc"]
        total = acc["up"] + acc["down"] + acc["flat"]
        if total <= 0:
            return None
        breadth = {
            "total": total,
            "up": acc["up"],
            "down": acc["down"],
            "flat": acc["flat"],
            # 阈值兜底值；涨停池/跌停池可用时会被 collect 覆盖
            "limit_up": acc.get("limit_up"),
            "limit_down": acc.get("limit_down"),
            "up_ratio": round(acc["up"] / total, 4),
            "pages_fetched": res["fetched"],
            "pages_total": res["pages"],
            "total_listed": res["total_listed"],
            "method": method,
        }
        _record(sources, source_id, source_name, True, count=total,
                elapsed_ms=(time.time() - t0) * 1000,
                error="；".join(errors) or None)
        return breadth

    # ---- 主路径：东财 clist 二分 ------------------------------------------
    try:
        res = _breadth_binary(pz, max_pages, limit_pct)
    except Exception as exc:  # noqa: BLE001
        res = None
        errors.append("em clist二分: %s" % str(exc)[:80])
    if res:
        breadth = _accept(res, "clist_binary", "em_breadth", "全市场宽度")
        if breadth:
            return breadth
        errors.append("em clist二分: 统计结果为 0")
    _record(sources, "em_breadth", "全市场宽度(东财)", False,
            elapsed_ms=(time.time() - t0) * 1000,
            error="；".join(errors) or "clist 不可用")

    # ---- fallback：新浪 getHQNodeData 二分 --------------------------------
    try:
        sina_total = _sina_stock_count()
        if sina_total <= 0:
            raise RuntimeError("新浪市场总数获取失败")

        def _sina_fetch(pn: int):
            rows = _sina_page(pn, pz)
            return rows, sina_total

        res = _breadth_binary(pz, max_pages, limit_pct, fetch_page=_sina_fetch)
        if res:
            breadth = _accept(res, "sina_binary", "sina_breadth",
                              "新浪-市场宽度(fallback)")
            if breadth:
                return breadth
            errors.append("sina: 统计结果为 0")
    except Exception as exc:  # noqa: BLE001
        errors.append("sina: %s" % str(exc)[:80])

    # ---- 全失败：数值一律 null（不编造 0）--------------------------------
    _record(sources, "sina_breadth", "新浪-市场宽度(fallback)", False,
            elapsed_ms=(time.time() - t0) * 1000,
            error="；".join(errors) or "无可用宽度源")
    return {"total": None, "up": None, "down": None, "flat": None,
            "limit_up": None, "limit_down": None, "up_ratio": None,
            "pages_fetched": 0, "pages_total": 0, "total_listed": None,
            "method": "unavailable"}


# --------------------------------------------------------------------------- #
# per-stock collectors
# --------------------------------------------------------------------------- #
def _quote_url(sid: str) -> str:
    return ("/api/qt/stock/get?secid=%s&fltt=2&invt=2"
            "&fields=f43,f44,f45,f46,f47,f48,f50,f57,f58,f60,f107,f116,f117,"
            "f162,f167,f168,f169,f170,f171" % sid)


def _fetch_bulk_quotes(stocks: list[dict], us: bool = False):
    """一次 ulist.np 批量拿快照（A股/美股分开调用）。返回 ``(quotes, error)``。

    * A股：``parse_ulist_quote``（f10=量比等）；
    * 美股：``_parse_us_quote``（f1=3，amount/turnover/volume_ratio 不取）；
    批量结果用于：① 自选股快照主数据源；② stock/get 缺字段/失败时兜底。
    """
    secids = ",".join(s["secid"] for s in stocks if s.get("secid"))
    if not secids:
        return {}, None
    path = ("/api/qt/ulist.np/get?fltt=2&invt=2&fields=" + _ULIST_QUOTE_FIELDS
            + "&secids=" + secids)
    try:
        jobj = _push2_json(path)
        data = (jobj or {}).get("data") or {}
        diff = data.get("diff") or []
        out: dict[str, dict] = {}
        for it in diff if isinstance(diff, list) else []:
            if not isinstance(it, dict):
                continue
            code = str(it.get("f12") or "").strip()
            if not code:
                continue
            if us:
                q = _parse_us_quote({"data": {"diff": [it]}}, code)
            else:
                q = parse_ulist_quote({"data": {"diff": [it]}}, code)
            if q.get("price") is not None:
                out[code] = q
        return out, None
    except Exception as exc:  # noqa: BLE001
        return {}, exc


def _fetch_us_quote_fallback(sid: str, code: str):
    """美股单只快照兜底（批量 ulist 缺失时补 1 次请求）。返回 (quote, error)。"""
    path = ("/api/qt/ulist.np/get?fltt=2&invt=2&fields=" + _ULIST_QUOTE_FIELDS
            + "&secids=" + sid)
    try:
        quote = _parse_us_quote(_push2_json(path, retries=1, backoff=0.4), code)
        return quote, None
    except Exception as exc:  # noqa: BLE001
        return _empty_quote(), exc


def _fetch_quote(sid: str, code: str, bulk: dict | None = None):
    """个股快照：**优先用批量 ulist 结果**（push2 限流环境下最稳、最省请求）。

    ulist 已含契约需要的全部字段（f2/f3/f4/f5/f6/f8/f9/f10/f15-f18/f20/f21/f23）；
    只有批量缺失该股时才回退单只 stock/get（拿不到就 None 降级，不编造）。
    """
    if bulk and bulk.get("price") is not None:
        return dict(bulk), None
    last: Exception | None = None
    try:
        quote = parse_stock_quote(_push2_json(_quote_url(sid), retries=1,
                                              backoff=0.4))
        if quote.get("price") is not None:
            return _merge_quote(quote, bulk or {}), None
        last = RuntimeError("stock/get 返回空 data")
    except Exception as exc:  # noqa: BLE001
        last = exc
    if bulk is not None:
        return dict(bulk), None
    return _empty_quote(), (last or RuntimeError("quote 无价格"))


def _filter_news(items: list[dict], today: str, max_days: int = 14,
                 keep_min: int = 3, limit: int = 8) -> list[dict]:
    """新闻时效处理：按日期降序，优先保留近 max_days 天；不足 keep_min 条时
    用更老的补齐（仍按时间降序），绝不编造。"""
    rows = [n for n in items if n.get("title")]
    rows.sort(key=lambda n: (C.parse_dt_any(n.get("date")) or C.now_cn()),
              reverse=True)
    fresh = [n for n in rows if _within_days(n.get("date"), today, max_days)]
    if len(fresh) >= keep_min:
        return fresh[:limit]
    picked = list(fresh)
    for n in rows:
        if len(picked) >= min(keep_min, len(rows)):
            break
        if n not in picked:
            picked.append(n)
    picked.sort(key=lambda n: (C.parse_dt_any(n.get("date")) or C.now_cn()),
                reverse=True)
    return picked[:limit]


def _news_param(name: str, size: int) -> dict:
    """个股新闻搜索参数（``sort`` 优先 time=按时间，回退 default）。"""
    return {
        "uid": "", "keyword": name, "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": "default", "sort": "time",
            "pageIndex": 1, "pageSize": size,
            "preTag": "<em>", "postTag": "</em>",
        }},
    }


def _collect_one_stock(stock: dict, date_str: str, limits: dict,
                       thresholds: dict, sources: list[dict],
                       bulk_quote: dict | None = None) -> dict:
    """单只自选股：quote/kline/fund_flow/announcements/news/reports + alerts。

    每个子接口独立降级；最后在 ``sources`` 里只记 **1 条**
    （``stock_{code}``：ok=是否拿到行情、count=成功子请求数、error=失败子接口）。
    """
    st = normalize_stock(stock)
    code, name, sid = st["code"], st["name"], st["secid"]
    kline_days = int(limits.get("kline_days") or 30)
    ann_limit = int(limits.get("announcements_per_stock") or 20)
    news_limit = int(limits.get("watchlist_news_per_stock") or 8)
    report_limit = int(limits.get("watchlist_reports_per_stock") or 5)
    t_stock = time.time()
    sub_failed: list[str] = []
    sub_ok = 0

    result = {
        "code": code, "name": name, "market": st["market"],
        "currency": st.get("currency") or "CNY",
        "tags": st["tags"], "note": st["note"],
        "quote": _empty_quote(), "kline": [],
        "fund_flow": {"today_main_net": None, "recent": []},
        "announcements": [], "news": [], "reports": [], "alerts": [],
    }

    def _mark(ok: bool, tag: str) -> None:
        nonlocal sub_ok
        if ok:
            sub_ok += 1
        else:
            sub_failed.append(tag)

    # ---- 1) quote ---------------------------------------------------------
    try:
        quote, err = _fetch_quote(sid, code, bulk_quote)
        result["quote"] = quote
        ok = quote.get("price") is not None
        _mark(ok, "quote")
        if err is not None:
            sub_failed.append("quote:%s" % str(err)[:60])
    except Exception as exc:  # noqa: BLE001
        _mark(False, "quote")
        sub_failed.append("quote:%s" % str(exc)[:60])

    # ---- 2) 日K（push2his 优先，失败切腾讯前复权）-------------------------
    try:
        result["kline"], kline_src, kline_err = _fetch_kline(
            sid, code, st["market"], kline_days)
        _mark(bool(result["kline"]), "kline")
        if kline_err is not None:
            sub_failed.append("kline(%s):%s" % (kline_src, str(kline_err)[:50]))
    except Exception as exc:  # noqa: BLE001
        _mark(False, "kline")
        sub_failed.append("kline:%s" % str(exc)[:60])

    # ---- 3) 资金流（lmt=5 只有 1 天时用 lmt=0 再拉一次近 5 日）-------------
    fflow_base = ("/api/qt/stock/fflow/kline/get?secid=%s&fields1=f1,f2,f3,f7"
                  "&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&lmt=%s")
    try:
        ff = parse_fund_flow(_push2_json(fflow_base % (sid, 5)))
        if not ff.get("recent"):                     # 完全没数据才补拉一次
            try:
                ff2 = parse_fund_flow(_push2_json(fflow_base % (sid, 0),
                                                  retries=1))
                if len(ff2.get("recent") or []) > len(ff.get("recent") or []):
                    ff = ff2
            except Exception:  # noqa: BLE001 - 拿不到就保持空
                pass
        result["fund_flow"] = {
            "today_main_net": ff.get("today_main_net"),
            "recent": list(ff.get("recent") or [])[-5:],
        }
        _mark(bool(result["fund_flow"]["recent"]), "fund_flow")
    except Exception as exc:  # noqa: BLE001
        _mark(False, "fund_flow")
        sub_failed.append("fund_flow:%s" % str(exc)[:60])

    # ---- 4) 公告 ----------------------------------------------------------
    try:
        ann_url = ("https://np-anotice-stock.eastmoney.com/api/security/ann"
                   "?sr=-1&page_size=%d&page_index=1&ann_type=A"
                   "&client_source=web&stock_list=%s" % (ann_limit, code))
        result["announcements"] = parse_announcements(
            _json(ann_url, referer=_REF_DATA, retries=2), code, limit=ann_limit)
        _mark(True, "announcements")
    except Exception as exc:  # noqa: BLE001
        _mark(False, "announcements")
        sub_failed.append("announcements:%s" % str(exc)[:60])

    # ---- 5) 个股新闻（JSONP；sort=time 按时间，再本地时效过滤）-----------
    try:
        news_url = ("https://search-api-web.eastmoney.com/search/jsonp?cb=cb&param="
                    + urllib.parse.quote(json.dumps(
                        _news_param(name, news_limit), ensure_ascii=False)))
        payload = C.http_get_text(news_url, referer=_REF_SO, timeout=10, retries=2)
        result["news"] = _filter_news(parse_news(payload, limit=max(news_limit, 8)),
                                      today=date_str, limit=news_limit)
        _mark(True, "news")
    except Exception as exc:  # noqa: BLE001
        _mark(False, "news")
        sub_failed.append("news:%s" % str(exc)[:60])

    # ---- 6) 研报 ----------------------------------------------------------
    try:
        # 研报时间窗：近 90 天（date_str 往前推 90 天）
        begin_d = (C.parse_dt_any(date_str) or C.now_cn()) - timedelta(days=90)
        rep_url = ("https://reportapi.eastmoney.com/report/list?industryCode=*"
                   "&pageSize=%d&industry=*&rating=&ratingChange=&beginTime=%s"
                   "&endTime=%s&pageNo=1&qType=0&orgCode=&code=%s&rcode="
                   "&p=1&pageNum=1"
                   % (report_limit, C.date_str(begin_d), date_str, code))
        result["reports"] = parse_reports(
            _json(rep_url, referer=_REF_DATA, retries=2), limit=report_limit)
        _mark(True, "reports")
    except Exception as exc:  # noqa: BLE001
        _mark(False, "reports")
        sub_failed.append("reports:%s" % str(exc)[:60])

    # ---- 7) 异动提示（近 7 天公告/新闻，去重，最多 4 条）------------------
    result["alerts"] = build_stock_alerts(result, thresholds, today=date_str,
                                          recent_days=7, max_alerts=4)

    _record(sources, "stock_%s" % code, "%s %s" % (code, name),
            result["quote"].get("price") is not None, count=sub_ok,
            elapsed_ms=(time.time() - t_stock) * 1000,
            error=None if not sub_failed else "失败: " + ",".join(sub_failed))
    return result


def _collect_one_us_stock(stock: dict, date_str: str, limits: dict,
                          thresholds: dict, sources: list[dict],
                          bulk_quote: dict | None = None) -> dict:
    """单只**美股**自选股：快照（ulist 批量）+ 腾讯日K + 个股新闻 + alerts。

    美股只做「行情 / 日K / 新闻 / 涨跌提示」：
      * 公告、研报、资金流对美股不适用 → ``[]`` / ``None``，**不发请求**；
      * quote 的 ``amount``(USD)/``turnover``/``volume_ratio`` 一律 None；
      * 涨跌提示阈值放宽到 8%（美股波动大，避免天天触发）。
    """
    st = normalize_stock(stock)
    code, name, sid = st["code"], st["name"], st["secid"]
    market = st["market"] or "US"
    currency = st.get("currency") or "USD"
    kline_days = int(limits.get("kline_days") or 30)
    news_limit = int(limits.get("watchlist_news_per_stock") or 8)
    t_stock = time.time()
    sub_failed: list[str] = []
    sub_ok = 0

    result = {
        "code": code, "name": name, "market": market, "currency": currency,
        "tags": st["tags"], "note": st["note"],
        "quote": _empty_quote(), "kline": [],
        "fund_flow": None,                    # 美股资金流接口不适用
        "announcements": [], "news": [], "reports": [], "alerts": [],
    }

    def _mark(ok: bool, tag: str) -> None:
        nonlocal sub_ok
        if ok:
            sub_ok += 1
        else:
            sub_failed.append(tag)

    # ---- 1) 快照：批量 ulist 结果；缺失才补单只 ulist ----
    quote = dict(bulk_quote or {})
    if quote.get("price") is None:
        quote, err = _fetch_us_quote_fallback(sid, code)
        if err is not None:
            sub_failed.append("quote:%s" % str(err)[:50])
    result["quote"] = quote
    _mark(quote.get("price") is not None, "quote")

    # ---- 2) 日K：腾讯美股（day key，volume 单位股，不 ×100）----
    try:
        result["kline"], kline_src, kline_err = _fetch_kline(
            sid, code, market, kline_days, skip_push2=True)
        _mark(bool(result["kline"]), "kline")
        if kline_err is not None:
            sub_failed.append("kline(%s):%s" % (kline_src, str(kline_err)[:50]))
    except Exception as exc:  # noqa: BLE001
        _mark(False, "kline")
        sub_failed.append("kline:%s" % str(exc)[:60])

    # ---- 3) 个股新闻（东财搜索按中文名，美股同样可用）----
    try:
        news_url = ("https://search-api-web.eastmoney.com/search/jsonp?cb=cb&param="
                    + urllib.parse.quote(json.dumps(
                        _news_param(name, news_limit), ensure_ascii=False)))
        payload = C.http_get_text(news_url, referer=_REF_SO, timeout=10, retries=2)
        result["news"] = _filter_news(parse_news(payload, limit=max(news_limit, 8)),
                                      today=date_str, limit=news_limit)
        _mark(True, "news")
    except Exception as exc:  # noqa: BLE001
        _mark(False, "news")
        sub_failed.append("news:%s" % str(exc)[:60])

    # ---- 4) alerts：美股只做涨跌阈值（放宽到 8%）+ 新闻命中 ----
    us_thresholds = dict(thresholds or {})
    us_thresholds["big_move_pct"] = max(
        8.0, float((thresholds or {}).get("big_move_pct") or 5.0))
    us_thresholds["limit_pct"] = 999.0        # 美股无涨跌停
    us_thresholds["volume_spike_ratio"] = 999.0
    result["alerts"] = build_stock_alerts(result, us_thresholds, today=date_str,
                                          recent_days=7, max_alerts=4)

    _record(sources, "stock_%s" % code, "%s %s" % (code, name),
            result["quote"].get("price") is not None, count=sub_ok,
            elapsed_ms=(time.time() - t_stock) * 1000,
            error=None if not sub_failed else "失败: " + ",".join(sub_failed))
    return result


def _empty_stock_row(stock: dict) -> dict:
    """采集线程整体崩溃时的兜底行（保持契约 key，数值为 null/[]）。"""
    st = normalize_stock(stock)
    is_a = bool(st.get("is_ashare"))
    return {
        "code": st["code"], "name": st["name"], "market": st["market"],
        "currency": st.get("currency") or ("CNY" if is_a else "USD"),
        "tags": st["tags"], "note": st["note"],
        "quote": _empty_quote(), "kline": [],
        "fund_flow": ({"today_main_net": None, "recent": []} if is_a else None),
        "announcements": [], "news": [], "reports": [], "alerts": [],
    }


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def collect(date_str: str, watchlist: list[dict], cfg: dict) -> dict:
    """采集 A 股数据，返回契约 §4 的 ashare 段（含 sources，不抛异常）。

    Parameters
    ----------
    date_str : str
        数据归属日 ``YYYY-MM-DD``（Asia/Shanghai）。
    watchlist : list[dict]
        ``common.load_watchlist`` 的输出（code/name/tags/note/secid/market）。
    cfg : dict
        ``config/sources.json`` 解析后的 dict。
    """
    date_str = str(date_str or C.today_cn())
    cfg = cfg or {}
    acfg = cfg.get("ashare") or {}
    limits = cfg.get("limits") or {}
    thresholds = acfg.get("thresholds") or {}
    sources: list[dict] = []
    t_start = time.time()
    req_start = _request_snapshot()

    # ---- 第一段：大盘（指数/板块/涨跌榜/涨跌停池 并发 + 二分宽度）-----------
    tasks = [
        ("indices", _collect_indices, (acfg, sources)),
        ("sectors", _collect_sectors, (acfg, sources)),
        ("movers", _collect_movers, (acfg, sources)),
        ("limit_pools", _collect_limit_pools, (date_str, sources)),
    ]
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
    agg: dict = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(fn, *args): key for key, fn, args in tasks}
        for fut in futs:
            key = futs[fut]
            try:
                agg[key] = fut.result()
            except Exception as exc:  # noqa: BLE001 - 兜底，收集器内部本应已降级
                agg[key] = None
                C.log("[ashare] %s collector crashed: %s" % (key, exc))

    breadth = _collect_breadth(acfg, sources)
    pools = agg.get("limit_pools") or {"up": [], "down": [],
                                       "up_ok": False, "down_ok": False}
    # 涨跌停家数优先用涨停池/跌停池条数；拿不到才用 breadth 的阈值统计兜底
    th_label = "threshold(%.1f%%)" % float(thresholds.get("limit_pct") or 9.5)
    if pools.get("up_ok"):
        breadth["limit_up"] = pools.get("up_count", len(pools.get("up") or []))
        breadth["limit_up_source"] = "limit_pool"
    else:
        breadth["limit_up_source"] = (th_label
                                      if breadth.get("limit_up") is not None else None)
    if pools.get("down_ok"):
        breadth["limit_down"] = pools.get("down_count", len(pools.get("down") or []))
        breadth["limit_down_source"] = "limit_pool"
    else:
        breadth["limit_down_source"] = (th_label
                                        if breadth.get("limit_down") is not None else None)

    # ---- 第二段：自选股并发（A股 6 接口 / 美股 3 接口；workers=4 防限流）----
    stocks = [normalize_stock(s) for s in (watchlist or [])]
    a_stocks = [s for s in stocks if s.get("is_ashare")]
    us_stocks = [s for s in stocks if not s.get("is_ashare")]

    # 批量快照：A股、美股各 1 次 ulist.np（比逐只 stock/get 省请求且当前可用）
    bulk_quotes: dict[str, dict] = {}
    bulk_errors: list[str] = []
    t_bulk = time.time()
    if a_stocks:
        got, err = _fetch_bulk_quotes(a_stocks, us=False)
        bulk_quotes.update(got)
        if err is not None:
            bulk_errors.append("A股: %s" % str(err)[:60])
    if us_stocks:
        got, err = _fetch_bulk_quotes(us_stocks, us=True)
        bulk_quotes.update(got)
        if err is not None:
            bulk_errors.append("美股: %s" % str(err)[:60])
    if stocks:
        _record(sources, "em_quotes_bulk", "自选股快照(批量)", bool(bulk_quotes),
                count=len(bulk_quotes),
                elapsed_ms=(time.time() - t_bulk) * 1000,
                error=None if bulk_quotes else ("；".join(bulk_errors)
                                                or "批量 ulist 无数据"))
    if us_stocks:
        # 契约 §6 的美股不适用项（公告/研报/资金流），明确标注而不是静默缺失
        _record(sources, "us_applicable", "美股不适用(公告/研报/资金流)", True,
                count=0)

    def _run_one(stock):
        if stock.get("is_ashare"):
            return _collect_one_stock(stock, date_str, limits, thresholds,
                                      sources, bulk_quotes.get(stock["code"]))
        return _collect_one_us_stock(stock, date_str, limits, thresholds,
                                     sources, bulk_quotes.get(stock["code"]))

    if stocks:
        pairs = C.run_parallel(_run_one, stocks, workers=4)
        order = {s["code"]: i for i, s in enumerate(stocks)}
        rows = []
        for stock, res, err in pairs:
            if err is not None or res is None:
                C.log("[ashare] %s failed: %s" % (stock.get("code"), err))
                rows.append(_empty_stock_row(stock))
            else:
                rows.append(res)
        rows.sort(key=lambda r: order.get(r.get("code"), 999))
    else:
        rows = []

    watchlist_meta = build_watchlist_meta(rows)
    alerts_top = flatten_alerts(rows, max_alerts=20,
                                when=C.iso(C.now_cn()))

    req_end = _request_snapshot()
    http_requests = {
        "eastmoney": req_end.get("eastmoney", 0) - req_start.get("eastmoney", 0),
        "other": req_end.get("other", 0) - req_start.get("other", 0),
    }
    http_requests["total"] = http_requests["eastmoney"] + http_requests["other"]

    return {
        "indices": agg.get("indices") or [],
        "breadth": breadth,
        "sectors": agg.get("sectors") or {"top": [], "bottom": []},
        "movers": agg.get("movers") or {"gainers": [], "losers": [], "turnover": []},
        "limit_up_pool": pools.get("up") or [],
        "watchlist": rows,
        "watchlist_meta": watchlist_meta,
        "sources": sources,
        # 契约 §4 的顶层 alerts 由 run_daily 组装；这里额外附一份方便直接取用
        "alerts": alerts_top,
        "elapsed_sec": round(time.time() - t_start, 2),
        "http_requests": http_requests,
    }


# --------------------------------------------------------------------------- #
# CLI / preview
# --------------------------------------------------------------------------- #
def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_configs():
    root = _project_root()
    cfg = C.load_json(os.path.join(root, "config", "sources.json"), {}) or {}
    path = os.path.join(root, "config", "watchlist.json")
    watchlist = C.load_watchlist(path)
    if not watchlist:
        watchlist = [normalize_stock(s) for s in
                     ((C.load_json(path, {}) or {}).get("stocks") or [])]
    return cfg, watchlist


def _fmt_pct(value) -> str:
    return C.fmt_pct(value) if value is not None else "—"


def print_summary(data: dict) -> None:
    """打印 live 采集摘要（自测/联调用）。"""
    print("=" * 78)
    print("指数：")
    for it in data.get("indices") or []:
        print("  %-6s %-8s %10s  %8s  %s"
              % (it.get("code"), it.get("name"),
                 ("%.2f" % it["price"]) if it.get("price") is not None else "—",
                 _fmt_pct(it.get("change_pct")),
                 C.fmt_amount(it.get("amount"))))
    b = data.get("breadth") or {}
    print("-" * 78)
    print("市场宽度：total=%s up=%s down=%s flat=%s limit_up=%s(%s) limit_down=%s(%s) "
          "up_ratio=%s method=%s pages=%s/%s"
          % (b.get("total"), b.get("up"), b.get("down"), b.get("flat"),
             b.get("limit_up"), b.get("limit_up_source") or "—",
             b.get("limit_down"), b.get("limit_down_source") or "—",
             ("%.1f%%" % (b["up_ratio"] * 100)) if b.get("up_ratio") is not None else "—",
             b.get("method") or "—", b.get("pages_fetched"), b.get("pages_total")))
    sectors = data.get("sectors") or {}
    print("-" * 78)
    print("板块涨幅 top5：")
    for s in (sectors.get("top") or [])[:5]:
        print("  %-6s %-12s %8s  ↑%s ↓%s  领涨:%s(%s)"
              % (s.get("code"), s.get("name"), _fmt_pct(s.get("change_pct")),
                 s.get("up_count"), s.get("down_count"),
                 s.get("leader_name"), _fmt_pct(s.get("leader_change_pct"))))
    print("板块跌幅 top5：")
    for s in (sectors.get("bottom") or [])[:5]:
        print("  %-6s %-12s %8s" % (s.get("code"), s.get("name"),
                                    _fmt_pct(s.get("change_pct"))))
    pool = data.get("limit_up_pool") or []
    print("-" * 78)
    print("涨停池：%d 只（前 5：%s）"
          % (len(pool), "、".join("%s(%s)" % (p.get("name"), p.get("boards"))
                                  for p in pool[:5]) if pool else "无数据"))
    movers = data.get("movers") or {}
    print("涨幅榜前 3：%s" % "、".join(
        "%s %s" % (m.get("name"), _fmt_pct(m.get("change_pct")))
        for m in (movers.get("gainers") or [])[:3]))
    print("成交额榜前 3：%s" % "、".join(
        "%s %s" % (m.get("name"), C.fmt_amount(m.get("amount")))
        for m in (movers.get("turnover") or [])[:3]))

    print("=" * 78)
    print("自选股（%d 只）：" % len(data.get("watchlist") or []))
    for st in data.get("watchlist") or []:
        q = st.get("quote") or {}
        ann = st.get("announcements") or []
        news = st.get("news") or []
        reps = st.get("reports") or []
        cur = st.get("currency") or ""
        price_txt = ("%.2f" % q["price"]) if q.get("price") is not None else "—"
        if cur and cur != "CNY":
            price_txt += " " + cur
        flow = st.get("fund_flow")
        flow_txt = (C.fmt_amount((flow or {}).get("today_main_net"))
                    if isinstance(flow, dict) else "—（不适用）")
        print("  %s %-6s[%s] 价=%s 涨跌=%s 量比=%s 成交额=%s 换手=%s PE=%s K线=%d"
              % (st.get("code"), st.get("name"), st.get("market"),
                 price_txt, _fmt_pct(q.get("change_pct")),
                 ("%.2f" % q["volume_ratio"]) if q.get("volume_ratio") is not None else "—",
                 C.fmt_amount(q.get("amount")),
                 ("%.2f%%" % q["turnover"]) if q.get("turnover") is not None else "—",
                 ("%.1f" % q["pe"]) if q.get("pe") is not None else "—",
                 len(st.get("kline") or [])))
        print("      公告 %d / 新闻 %d / 研报 %d / 资金流主力净额 %s / 提示 %d 条：%s"
              % (len(ann), len(news), len(reps), flow_txt,
                 len(st.get("alerts") or []),
                 "；".join(a.get("text", "") for a in (st.get("alerts") or [])[:2]) or "无"))
    meta = data.get("watchlist_meta") or {}
    print("  watchlist_meta: avg=%s best=%s worst=%s total_amount=%s"
          % (_fmt_pct(meta.get("avg_change_pct")),
             "%s %s" % ((meta.get("best") or {}).get("name"),
                        _fmt_pct((meta.get("best") or {}).get("change_pct"))),
             "%s %s" % ((meta.get("worst") or {}).get("name"),
                        _fmt_pct((meta.get("worst") or {}).get("change_pct"))),
             C.fmt_amount(meta.get("total_amount"))))
    failed = [s for s in (data.get("sources") or []) if not s.get("ok")]
    print("=" * 78)
    print("sources: %d 个（失败 %d）" % (len(data.get("sources") or []), len(failed)))
    for s in failed:
        print("  [FAIL] %-24s %s" % (s.get("id"), (s.get("error") or "")[:110]))
    req = data.get("http_requests") or {}
    print("请求数：push2 系 %s / 其他 %s / 合计 %s"
          % (req.get("eastmoney"), req.get("other"), req.get("total")))
    print("耗时 %.2fs" % (data.get("elapsed_sec") or 0))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    date_str = C.today_cn()
    if "--date" in argv:
        idx = argv.index("--date")
        if idx + 1 < len(argv):
            date_str = argv[idx + 1]
    cfg, watchlist = _load_configs()
    print("[ashare] date=%s watchlist=%d 只，开始采集（live）…"
          % (date_str, len(watchlist)))
    data = collect(date_str, watchlist, cfg)
    out_path = os.path.join(_project_root(), "out", "ashare_preview.json")
    try:
        C.save_json(out_path, data)
        print("[ashare] 预览已写入 %s" % out_path)
    except Exception as exc:  # noqa: BLE001 - 写盘失败不影响打印摘要
        print("[ashare] 写预览失败: %s" % exc)
    print_summary(data)
    prices = [(st.get("code"), (st.get("quote") or {}).get("price"))
              for st in data.get("watchlist") or []]
    missing = [c for c, p in prices if p is None]
    if missing:
        print("[ashare][WARN] 以下自选股 quote.price 缺失：%s" % ",".join(missing))
    else:
        print("[ashare] 全部自选股 quote.price 均有值 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
