"""中文语料：抓取配置的中文科技媒体 RSS，记录观察范围内最早的标题命中。

这个模块不负责「发现中文新词」，它只做两件事：
  1. update()  把中文源的标题存进 state/cn.db（去重，保留 60 天）；
  2. match()   判定一个英文候选名，在中文标题里有没有出现过、最早是哪天。

判定规则：去掉虚词后，同一条标题须包含名字或一个变体的所有词。
英文按词匹配，中文按连续文字匹配。

用法
----
  python -m radar.cn_feeds --state state            # 日常：抓一遍，存起来
  python -m radar.cn_feeds --state state --show 7   # 看看最近 7 天抓到了什么
"""
from __future__ import annotations

import gzip
import json
import re
import sqlite3
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

UA = "Mozilla/5.0 (compatible; new-term-radar-cn/1.0)"
TIMEOUT = 25
KEEP_DAYS = 60

# 2026-11 实测可用的源。36氪的 /feed 与机器之心的 /rss 已返回 HTML，虎嗅超时，不要加回来；
# 没有官方 RSS 的（新智元、量子位公众号版）只能靠第三方转出，不稳定，这里不收。
FEEDS = [
    {"name": "量子位",   "url": "https://www.qbitai.com/feed"},
    {"name": "雷锋网",   "url": "https://www.leiphone.com/feed"},
    {"name": "极客公园", "url": "https://www.geekpark.net/rss"},
    {"name": "爱范儿",   "url": "https://www.ifanr.com/feed"},
    {"name": "InfoQ",    "url": "https://www.infoq.cn/feed"},
    {"name": "钛媒体",   "url": "https://www.tmtpost.com/rss"},
]

STOP_TOKENS = {"the", "and", "for", "with", "from", "your", "you", "app", "tool", "ai", "an", "of", "to", "in",
               "on", "is", "are", "how", "what", "new", "best"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
  url       TEXT PRIMARY KEY,
  source    TEXT NOT NULL,
  title     TEXT NOT NULL,
  published TEXT NOT NULL,       -- YYYY-MM-DD
  fetched   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS articles_published ON articles(published);
"""

_ITEM = re.compile(r"<item\b[^>]*>(.*?)</item>", re.S | re.I)
_ENTRY = re.compile(r"<entry\b[^>]*>(.*?)</entry>", re.S | re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_LINK = re.compile(r"<link[^>]*>(.*?)</link>", re.S | re.I)
_GUID = re.compile(r"<guid[^>]*>(.*?)</guid>", re.S | re.I)
_DATE = re.compile(r"<(?:pubDate|published|updated|dc:date)[^>]*>(.*?)</(?:pubDate|published|updated|dc:date)>", re.S | re.I)
_CDATA = re.compile(r"^\s*<!\[CDATA\[(.*?)\]\]>\s*$", re.S)
_TAG = re.compile(r"<[^>]+>")


def _text(s: str) -> str:
    s = (s or "").strip()
    m = _CDATA.match(s)
    if m:
        s = m.group(1)
    return re.sub(r"\s+", " ", _TAG.sub(" ", s)).strip()


def _day(s: str) -> str:
    s = _text(s)
    if not s:
        return ""
    try:
        return parsedate_to_datetime(s).date().isoformat()
    except (TypeError, ValueError):
        pass
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def _pick(pattern: re.Pattern, block: str) -> str:
    m = pattern.search(block)
    return _text(m.group(1)) if m else ""


def _fetch(url: str, retries: int = 2) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip",
                                               "Accept": "application/rss+xml, application/xml, text/xml, */*"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
                body = res.read()
            if body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)
            return body.decode("utf-8", "replace")
        except (OSError, ValueError):
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    return ""


def items(xml: str) -> list[dict]:
    """RSS 的 <item> 和 Atom 的 <entry> 都收，返回 title/url/published。"""
    out = []
    for block in (_ITEM.findall(xml) + _ENTRY.findall(xml)):
        title = _pick(_TITLE, block)
        link = _pick(_LINK, block)
        if not link:                       # Atom 常把地址放在 <link href="...">，没有正文
            m = re.search(r'<link[^>]*href="([^"]+)"', block, re.I)
            link = m.group(1) if m else _pick(_GUID, block)
        when = _day(_pick(_DATE, block))
        if title and link:
            out.append({"title": title, "url": link.strip(), "published": when or date.today().isoformat()})
    return out


def connect(state: Path) -> sqlite3.Connection:
    state.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state / "cn.db")
    conn.executescript(SCHEMA)
    return conn


def update(state: Path, feeds: list[dict] | None = None, keep_days: int = KEEP_DAYS) -> dict:
    """抓一遍所有源，新的存进去。一个源失败不影响其他源。"""
    feeds = feeds or FEEDS
    conn = connect(state)
    today = date.today().isoformat()
    report = {"feeds": {}, "added": 0, "errors": []}
    for f in feeds:
        try:
            got = items(_fetch(f["url"]))
            if not got:
                raise ValueError("未解析到可用的 RSS/Atom 条目")
        except Exception as exc:                    # noqa: BLE001 — 一个源坏了不能停
            report["feeds"][f["name"]] = {"error": f"{type(exc).__name__}: {str(exc)[:80]}"}
            report["errors"].append(f"{f['name']}: {str(exc)[:80]}")
            continue
        added = 0
        for it in got:
            cur = conn.execute("INSERT OR IGNORE INTO articles (url, source, title, published, fetched) VALUES (?,?,?,?,?)",
                               (it["url"], f["name"], it["title"], it["published"], today))
            added += cur.rowcount
        report["feeds"][f["name"]] = {"items": len(got), "added": added}
        report["added"] += added
    cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
    conn.execute("DELETE FROM articles WHERE published < ?", (cutoff,))
    conn.commit()
    conn.close()
    return report


def tokens(term: str) -> list[str]:
    parts = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", term.casefold())
    return [part for part in parts if len(part) >= 2 and not part.isdigit() and part not in STOP_TOKENS]


class Corpus:
    """一次读进内存，给几十个候选名反复匹配，比每个名字查一次库快得多。"""

    def __init__(self, state: Path, days: int = KEEP_DAYS):
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        conn = connect(state)
        rows = conn.execute("SELECT title, url, source, published FROM articles WHERE published >= ?", (cutoff,)).fetchall()
        conn.close()
        self.rows = [{"title": r[0], "lower": r[0].lower(), "url": r[1], "source": r[2], "published": r[3]} for r in rows]
        self.rows.sort(key=lambda r: r["published"])

    def __len__(self) -> int:
        return len(self.rows)

    def match(self, term: str, variants: list[str] | None = None) -> dict | None:
        """返回最早的命中：{"published", "title", "url", "source"}，没有则 None。

        变体（比如 "niko strata" 的 "niko1221 strata"）一起试，取最早的。
        """
        patterns = []
        for name in [term, *(variants or [])]:
            words = tokens(name)
            if words:
                patterns.append([re.compile(rf"(?<![a-z0-9]){word}(?![a-z0-9])" if word.isascii() else word)
                                 for word in words])
        for row in self.rows:
            if any(all(pattern.search(row["lower"]) for pattern in group) for group in patterns):
                return row
        return None


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="python -m radar.cn_feeds", description="抓中文科技媒体的 RSS，供时间差判定")
    p.add_argument("--state", default="state")
    p.add_argument("--show", type=int, default=0, help="打印最近 N 天的标题")
    args = p.parse_args(argv)
    state = Path(args.state)
    rep = update(state)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    corpus = Corpus(state)
    print(f"\n库里 {len(corpus)} 条中文标题（{KEEP_DAYS} 天内）")
    if args.show:
        cutoff = (date.today() - timedelta(days=args.show)).isoformat()
        for r in corpus.rows:
            if r["published"] >= cutoff:
                print(f"{r['published']}  [{r['source']}]  {r['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
