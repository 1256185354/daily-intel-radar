# -*- coding: utf-8 -*-
"""intel.common — shared utilities for the Daily Intel Radar pipeline.

Owner: Lead. Other agents: read only.

Pure standard library (no pip installs). Works on the bundled Python 3.13 runtime.

Design notes
------------
* All datetimes are Asia/Shanghai (UTC+8) aware.
* Every network helper raises `HttpError` on failure — callers must catch and
  degrade gracefully (collectors never crash the pipeline).
* `http_get` transparently handles gzip/deflate and retries transient failures.
"""
from __future__ import annotations

import concurrent.futures as _cf
import gzip
import html as _html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #
TZ_CN = timezone(timedelta(hours=8))

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
    "%Y年%m月%d日 %H:%M", "%Y年%m月%d日", "%m-%d %H:%M",
)


def now_cn() -> datetime:
    return datetime.now(TZ_CN)


def today_cn() -> str:
    return now_cn().strftime("%Y-%m-%d")


def iso(dt: datetime | None) -> str | None:
    """Aware datetime -> ISO-8601 string with +08:00 offset (or None)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_CN)
    return dt.astimezone(TZ_CN).isoformat(timespec="seconds")


def parse_dt_any(value) -> datetime | None:
    """Parse the many date formats Chinese finance sites use. Returns aware dt or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=TZ_CN)
    if isinstance(value, (int, float)):
        n = float(value)
        if n > 1e12:      # ms epoch
            n /= 1000.0
        try:
            return datetime.fromtimestamp(n, TZ_CN)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    if not s or s in ("-", "None", "null", "0"):
        return None
    # ISO-8601 first (handles 2026-11-18T12:00:00+08:00 and Z)
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=TZ_CN)
    except ValueError:
        pass
    # RFC-822 (RSS)
    try:
        d = parsedate_to_datetime(s)
        if d is not None:
            return d if d.tzinfo else d.replace(tzinfo=TZ_CN)
    except (TypeError, ValueError):
        pass
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=TZ_CN)
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=TZ_CN)
        except ValueError:
            return None
    return None


def date_str(dt: datetime | None = None) -> str:
    return (dt or now_cn()).astimezone(TZ_CN).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class HttpError(RuntimeError):
    pass


def _decode(raw: bytes, encoding: str | None = None) -> str:
    if encoding:
        try:
            return raw.decode(encoding, "replace")
        except LookupError:
            pass
    for enc in ("utf-8", "gb18030", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def http_get(url: str, timeout: float = 15, retries: int = 2,
             headers: dict | None = None, referer: str | None = None,
             backoff: float = 0.35) -> bytes:
    """GET bytes, gzip/deflate aware, with bounded retries.

    `backoff` is the base seconds between retries (attempt n waits backoff * (n+1)).
    Use a larger value (e.g. 1.5) for rate-limited hosts such as Eastmoney push2.
    """
    h = {
        "User-Agent": DEFAULT_UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }
    if referer:
        h["Referer"] = referer
    if headers:
        h.update(headers)
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc:
                raw = gzip.decompress(raw)
            elif "deflate" in enc:
                try:
                    raw = zlib.decompress(raw)
                except zlib.error:
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            return raw
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (400, 401, 403, 404, 410):
                break
        except Exception as exc:  # noqa: BLE001 - network layer is best-effort
            last = exc
        if attempt < retries:
            time.sleep(0.35 * (attempt + 1))
    raise HttpError(f"GET {url} failed: {type(last).__name__}: {last}")


def http_get_text(url: str, encoding: str | None = None, **kwargs) -> str:
    return _decode(http_get(url, **kwargs), encoding)


def strip_jsonp(text: str) -> str:
    """`cb({...});` / `var ajaxResult={...};` -> `{...}`."""
    s = text.strip()
    starts = [i for i in (s.find("{"), s.find("[")) if i >= 0]
    if not starts:
        raise ValueError("no JSON payload in response")
    start = min(starts)
    end = max(s.rfind("}"), s.rfind("]"))
    if end < start:
        raise ValueError("malformed JSON payload")
    return s[start:end + 1]


def http_get_json(url: str, encoding: str | None = "utf-8", **kwargs):
    return json.loads(strip_jsonp(http_get_text(url, encoding=encoding, **kwargs)))


def run_parallel(fn, items, workers: int = 8) -> list[tuple]:
    """Run fn(item) concurrently -> [(item, result, error), ...] (never raises)."""
    results: list[tuple] = []
    if not items:
        return results
    with _cf.ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        futures = {pool.submit(fn, it): it for it in items}
        for fut in _cf.as_completed(futures):
            item = futures[fut]
            try:
                results.append((item, fut.result(), None))
            except Exception as exc:  # noqa: BLE001
                results.append((item, None, exc))
    return results


# --------------------------------------------------------------------------- #
# text helpers
# --------------------------------------------------------------------------- #
_TAG_RE = re.compile(r"<[^>]+>")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_WS_RE = re.compile(r"[ \t\u00a0]+")


def strip_html(text) -> str:
    if not text:
        return ""
    s = _TAG_RE.sub(" ", str(text))
    return _html.unescape(s)


def clean_text(text, max_len: int = 0) -> str:
    if not text:
        return ""
    s = _WS_RE.sub(" ", strip_html(text)).strip()
    s = re.sub(r"\s*\n\s*", "\n", s)
    s = re.sub(r"\n{2,}", "\n", s)
    if max_len and len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "…"
    return s


def truncate(text: str, n: int = 240) -> str:
    return clean_text(text, max_len=n)


def safe_float(value, default=None):
    """Parse '1,234.5' / '12.3%' / '-' / None -> float | default."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        try:
            f = float(value)
            return f if f == f and f not in (float("inf"), float("-inf")) else default
        except (TypeError, ValueError):
            return default
    s = str(value).strip().replace(",", "").replace("%", "").replace("＋", "+")
    if s in ("", "-", "--", "None", "null", "N/A", "nan"):
        return default
    try:
        return float(s)
    except ValueError:
        return default


def fmt_amount(value, unit: str = "元") -> str:
    """12345678 -> '1234.57万' / 1.2e9 -> '12.00亿'."""
    v = safe_float(value)
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e8:
        return f"{v / 1e8:.2f}亿{unit}"
    if a >= 1e4:
        return f"{v / 1e4:.2f}万{unit}"
    return f"{v:.0f}{unit}"


def fmt_pct(value, digits: int = 2, sign: bool = True) -> str:
    v = safe_float(value)
    if v is None:
        return "—"
    return f"{v:+.{digits}f}%" if sign else f"{v:.{digits}f}%"


# --------------------------------------------------------------------------- #
# RSS / Atom
# --------------------------------------------------------------------------- #
_ATOM_NS = "http://www.w3.org/2005/Atom"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child_text(node, names: tuple[str, ...]) -> str:
    for child in node:
        if _local(child.tag) in names:
            txt = "".join(child.itertext()).strip()
            if txt:
                return txt
    return ""


def parse_rss(data, source: str = "", weight: float = 1.0) -> list[dict]:
    """Parse RSS 2.0 or Atom bytes/str -> normalized item dicts."""
    text = _decode(data) if isinstance(data, bytes) else data
    text = _CTRL_RE.sub("", text)
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        # salvage: keep only well-formed <item> blocks
        chunks = re.findall(r"<item>.*?</item>", text, re.S)
        if not chunks:
            raise
        root = ET.fromstring("<rss><channel>" + "".join(chunks) + "</channel></rss>")

    out: list[dict] = []
    nodes = [n for n in root.iter() if _local(n.tag) in ("item", "entry")]
    for node in nodes:
        title = _child_text(node, ("title",))
        link = _child_text(node, ("link",))
        if not link:
            for child in node:
                if _local(child.tag) == "link" and child.get("href"):
                    link = child.get("href")
                    break
        summary = _child_text(node, ("description", "summary", "content:encoded", "encoded", "content"))
        pub = _child_text(node, ("pubDate", "published", "updated", "date"))
        cats = [clean_text("".join(c.itertext())) for c in node if _local(c.tag) == "category"]
        cats = [c for c in cats if c][:4]
        if not title:
            continue
        out.append({
            "title": clean_text(title),
            "url": link.strip(),
            "summary": truncate(summary, 260),
            "published": iso(parse_dt_any(pub)),
            "source": source,
            "source_weight": weight,
            "rss_categories": cats,
        })
    return out


# --------------------------------------------------------------------------- #
# classification / heat
# --------------------------------------------------------------------------- #
DEFAULT_CATEGORY = "综合"


def load_json(path: str, default=None):
    try:
        # utf-8-sig 兼容 Windows 记事本/PowerShell 写出的 UTF-8 BOM 文件
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path: str, obj) -> None:
    ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_text(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return default


def write_text(path: str, text: str) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def ensure_dir(path: str) -> None:
    if path:
        os.makedirs(path, exist_ok=True)


def log(msg: str) -> None:
    sys.stderr.write(f"[intel {now_cn().strftime('%H:%M:%S')}] {msg}\n")
    sys.stderr.flush()


def classify(text: str, rules: dict) -> tuple[str, list[str], float]:
    """Keyword classification -> (category, tags, weight)."""
    blob = (text or "").lower()
    best_cat, best_weight, best_hits = DEFAULT_CATEGORY, 0.0, []
    for cat, spec in (rules.get("categories") or {}).items():
        hits = [kw for kw in spec.get("keywords", []) if kw.lower() in blob]
        if not hits:
            continue
        score = len(hits) * float(spec.get("weight", 1.0))
        if score > best_weight:
            best_cat, best_weight, best_hits = cat, score, hits
    tags = [h for h in best_hits[:3]]
    return best_cat, tags, round(best_weight, 3)


def score_items(items: list[dict], rules: dict, ref_time: datetime | None = None,
                half_life_hours: float = 10.0) -> list[dict]:
    """Assign category/tags/heat and sort descending. Mutates and returns items."""
    ref = ref_time or now_cn()
    for it in items:
        cat, tags, kw_score = classify(it.get("title", "") + " " + it.get("summary", ""), rules)
        it["category"] = cat
        it["tags"] = tags
        pub = parse_dt_any(it.get("published"))
        age_h = max(0.0, (ref - pub).total_seconds() / 3600.0) if pub else 12.0
        recency = 0.5 ** (age_h / half_life_hours)
        base = float(it.get("source_weight", 1.0))
        heat = 100.0 * base * recency * (1.0 + 0.12 * len(tags)) * (1.0 + min(kw_score, 3) * 0.08)
        it["heat"] = round(heat, 1)
    items.sort(key=lambda x: x.get("heat", 0), reverse=True)
    return items


def dedupe(items: list[dict], title_key: str = "title") -> list[dict]:
    """Drop near-duplicate titles (normalized, first wins)."""
    seen: set[str] = set()
    out: list[dict] = []
    for it in items:
        norm = re.sub(r"[\s\W_]+", "", str(it.get(title_key, ""))).lower()[:60]
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(it)
    return out


# --------------------------------------------------------------------------- #
# A-share helpers
# --------------------------------------------------------------------------- #
def secid(code: str) -> str:
    """'600519' -> '1.600519' (SH); '300848' -> '0.300848' (SZ/BJ)."""
    c = str(code).strip().split(".")[-1]
    if c.startswith(("600", "601", "603", "605", "688", "689", "900", "5", "1")):
        return "1." + c
    return "0." + c


def market_label(code: str) -> str:
    c = str(code).strip()
    if c.startswith(("600", "601", "603", "605", "688", "689", "900")):
        return "SH"
    if c.startswith(("8", "4", "92")):
        return "BJ"
    return "SZ"


def load_watchlist(path: str) -> list[dict]:
    """读取自选股配置。

    - A股：`code` 为 6 位数字，secid 自动推导（1.600519 / 0.300848）。
    - 非A股（美股/港股）：必须显式给 `secid` + `market`，例如
      {"code": "NVDA", "name": "英伟达", "market": "US", "secid": "105.NVDA"}
      （东财约定：105.=纳斯达克，106.=纽交所，116.=港股）
    """
    cfg = load_json(path, {}) or {}
    stocks = cfg.get("stocks") or []
    out = []
    for s in stocks:
        code = str(s.get("code", "")).strip()
        explicit_secid = str(s.get("secid") or "").strip()
        explicit_market = str(s.get("market") or "").strip().upper()
        if re.fullmatch(r"\d{6}", code):
            out.append({
                "code": code,
                "name": s.get("name") or code,
                "tags": s.get("tags") or [],
                "note": s.get("note") or "",
                "secid": explicit_secid or secid(code),
                "market": explicit_market or market_label(code),
                "currency": s.get("currency") or "CNY",
                "is_ashare": True,
            })
        elif code and explicit_secid and "." in explicit_secid and explicit_market:
            out.append({
                "code": code,
                "name": s.get("name") or code,
                "tags": s.get("tags") or [],
                "note": s.get("note") or "",
                "secid": explicit_secid,
                "market": explicit_market,
                "currency": s.get("currency") or ("USD" if explicit_market == "US" else ""),
                "is_ashare": False,
            })
        # 其余（缺 secid/market 的非6位代码）忽略，避免下游用错市场
    return out


# --------------------------------------------------------------------------- #
# self-test (python src/common.py)
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    assert parse_dt_any("2026-11-18 09:30:00").hour == 9
    assert parse_dt_any("2026-11-18T09:30:00+08:00").isoformat().startswith("2026-11-18T09:30")
    assert parse_dt_any("Wed, 18 Nov 2026 09:30:00 +0800").year == 2026
    assert safe_float("1,234.5") == 1234.5
    assert safe_float("-") is None and safe_float("12.3%") == 12.3
    assert secid("600519") == "1.600519" and secid("300848") == "0.300848"
    assert market_label("300848") == "SZ" and market_label("600519") == "SH"
    assert fmt_amount(12345678) == "1234.57万元"
    assert strip_jsonp('var ajaxResult={"a":1};') == '{"a":1}'
    assert strip_jsonp('cb({"a":1});') == '{"a":1}'
    rss = parse_rss(
        '<?xml version="1.0"?><rss><channel><item><title>标题 A</title>'
        '<link>https://x.test/1</link><description>摘要</description>'
        '<pubDate>Wed, 18 Nov 2026 09:30:00 +0800</pubDate></item></channel></rss>',
        source="测试")
    assert len(rss) == 1 and rss[0]["title"] == "标题 A" and rss[0]["source"] == "测试"
    rules = {"categories": {"科技": {"keywords": ["芯片", "AI"], "weight": 1.0}}}
    cat, tags, _ = classify("芯片与AI大涨", rules)
    assert cat == "科技" and "芯片" in tags
    items = [{"title": "芯片 AI", "summary": "", "source_weight": 1.0, "published": iso(now_cn())}]
    score_items(items, rules)
    assert items[0]["heat"] > 0
    assert dedupe([{"title": "AB"}, {"title": "AB"}]) == [{"title": "AB"}]
    print("common.py self-test OK")
