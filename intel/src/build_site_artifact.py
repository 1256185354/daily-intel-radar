# -*- coding: utf-8 -*-
"""intel/src/build_site_artifact.py — 把 data/ 组织成可发布的静态站点目录（供 GitHub Actions 用）。

输出结构（默认 `_site/`，与本地 publish_pages.stage_site 一致）：
  index.html                 归档首页（链接 <date>/index.html）
  <date>/index.html          每日看板
  <date>/brief.md            每日简报
  latest.html / latest.png   手机快捷入口（latest.png 若不存在则跳过）
  robots.txt                 Disallow 收录
  .nojekyll

用法：python src/build_site_artifact.py [--out _site] [--data intel/data] [--max-days 60]
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
INTEL = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import common  # noqa: E402

DATE_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")


def build(out_dir: str, data_root: str, max_days: int = 60) -> dict:
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir, ignore_errors=True)
    common.ensure_dir(out_dir)
    dates = sorted([n for n in os.listdir(data_root) if DATE_RE.match(n)
                    and os.path.isfile(os.path.join(data_root, n, "index.html"))])
    if not dates:
        raise RuntimeError(f"{data_root} 下没有可发布的日期页面")
    dates = dates[-max_days:]
    files = 0
    for day in dates:
        for name in ("index.html", "brief.md"):
            src = os.path.join(data_root, day, name)
            if os.path.isfile(src):
                dst = os.path.join(out_dir, day, name)
                common.ensure_dir(os.path.dirname(dst))
                shutil.copy2(src, dst)
                files += 1
    archive = os.path.join(data_root, "index.html")
    if os.path.isfile(archive):
        shutil.copy2(archive, os.path.join(out_dir, "index.html"))
        files += 1
    latest = dates[-1]
    shutil.copy2(os.path.join(data_root, latest, "index.html"), os.path.join(out_dir, "latest.html"))
    files += 1
    png = os.path.join(data_root, "手机分享", "latest.png")
    if os.path.isfile(png):
        shutil.copy2(png, os.path.join(out_dir, "latest.png"))
        files += 1
    common.write_text(os.path.join(out_dir, "robots.txt"), "User-agent: *\nDisallow: /\n")
    common.write_text(os.path.join(out_dir, ".nojekyll"), "")
    files += 2
    total = sum(os.path.getsize(os.path.join(dp, f))
                for dp, _dn, fn in os.walk(out_dir) for f in fn)
    return {"out": out_dir, "dates": dates, "files": files, "total_kb": round(total / 1024, 1)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成可发布的静态站点目录")
    ap.add_argument("--out", default="_site")
    ap.add_argument("--data", default=os.path.join(INTEL, "data"))
    ap.add_argument("--max-days", type=int, default=60)
    args = ap.parse_args(argv)
    try:
        info = build(args.out, args.data, args.max_days)
    except Exception as exc:  # noqa: BLE001
        print(f"[build_site_artifact] 失败：{exc}", file=sys.stderr)
        return 1
    print(f"[build_site_artifact] {info['dates'][-1] if info['dates'] else '-'} "
          f"days={len(info['dates'])} files={info['files']} size={info['total_kb']}KB -> {info['out']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
