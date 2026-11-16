"""时间差：一个名字，英文源第一次出现是哪天，中文源第一次出现是哪天，差几天。

三个数字
--------
  t_en   英文首现日。GitHub 仓库用它的**创建日**（report.json 里的 age_days 反推，这是最接近
         "这个名字第一次出现在世界上"的时间）；Hugging Face、Product Hunt、Show HN、App Store
         用**首次进入榜/首次被看到**的那天（就是这次运行的日子）。
  t_cn   中文首现日。中文科技媒体的标题里第一次出现这个名字的那天，由 cn_feeds 的语料给出。
  ahead  你已经领先多少天 = 今天 − t_en。

判定的四种状态（给编辑层决定"还值不值得写"）
--------------------------------------------
  cn_uncovered  中文圈还没人写        → 最值得写，你是第一个讲的
  cn_late       中文比英文晚 ≥3 天    → 值得写，中文圈反应慢
  cn_same       中文在 2 天内跟上     → 降级为速览，大家都写了
  cn_early      中文先出现            → 反向信号：这东西可能起于中文圈

用法
----
  python -m radar.lag --state state --report report.json   # 打印今天候选的时间差
  python -m radar.lag --state state --stats                # 累计：覆盖率、中位时间差
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

LATE_DAYS = 3           # 中文晚这么多天，才算"来得及"
SAME_DAYS = 2           # 中文在这个天数内跟上，就算"已经被写过了"

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
  term   TEXT NOT NULL,
  lang   TEXT NOT NULL,          -- 'en' | 'cn'
  day    TEXT NOT NULL,          -- YYYY-MM-DD
  source TEXT NOT NULL DEFAULT '',
  url    TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (term, lang)
)"""


def connect(state: Path) -> sqlite3.Connection:
    state.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state / "lag.db")
    conn.executescript(SCHEMA)
    return conn


def _d(s: str) -> date | None:
    try:
        return date.fromisoformat(s[:10])
    except (TypeError, ValueError):
        return None


def record(conn: sqlite3.Connection, term: str, lang: str, day: str | date,
           source: str = "", url: str = "") -> None:
    """只保留最早的那一天：重复记录不会把首现日往后推。"""
    when = day.isoformat() if isinstance(day, date) else str(day)[:10]
    conn.execute("""INSERT INTO seen (term, lang, day, source, url) VALUES (?,?,?,?,?)
                    ON CONFLICT(term, lang) DO UPDATE SET
                      day    = CASE WHEN excluded.day < seen.day THEN excluded.day ELSE seen.day END,
                      source = CASE WHEN excluded.day < seen.day THEN excluded.source ELSE seen.source END,
                      url    = CASE WHEN excluded.day < seen.day THEN excluded.url ELSE seen.url END""",
                 (term.strip().lower(), lang, when, source, url))
    conn.commit()


def relate(conn: sqlite3.Connection, term: str, today: date | None = None) -> dict:
    """这个名字的时间差。缺任何一边都照样返回，只是对应字段为 None。"""
    today = today or date.today()
    rows = conn.execute("SELECT lang, day, source, url FROM seen WHERE term = ?", (term.strip().lower(),)).fetchall()
    row = {lang: {"day": day, "source": src, "url": url} for lang, day, src, url in rows}
    en, cn = row.get("en"), row.get("cn")
    t_en, t_cn = _d(en["day"]) if en else None, _d(cn["day"]) if cn else None
    out = {"term": term, "t_en": t_en.isoformat() if t_en else None, "t_cn": t_cn.isoformat() if t_cn else None,
           "en_source": en["source"] if en else "", "cn_source": cn["source"] if cn else "",
           "cn_url": cn["url"] if cn else "", "ahead": None, "lag": None, "state": "unknown", "note": ""}
    if t_en is None and t_cn is None:
        return out
    if t_en is None:                       # 只有中文侧：说明是中文圈先有
        out.update(state="cn_only", note=f"只在中文源出现（{cn['source']}）")
        return out
    out["ahead"] = (today - t_en).days
    if t_cn is None:
        out.update(state="cn_uncovered", note=f"中文圈未覆盖，已领先 {out['ahead']} 天")
        return out
    lag = (t_cn - t_en).days
    out["lag"] = lag
    if lag < 0:
        out.update(state="cn_early", note=f"中文比英文早 {-lag} 天，可能起于中文圈")
    elif lag <= SAME_DAYS:
        out.update(state="cn_same", note=f"中文 {lag} 天内已跟上（{cn['source']}）")
    elif lag < LATE_DAYS:
        out.update(state="cn_late", note=f"中文晚 {lag} 天（{cn['source']}）")
    else:
        out.update(state="cn_late", note=f"中文晚 {lag} 天，还来得及（{cn['source']}）")
    return out


def bonus(rel: dict) -> float:
    """给打分用：越早越加分，中文已覆盖就减分。"""
    return {"cn_uncovered": 0.6, "cn_late": 0.3, "cn_early": 0.2, "cn_same": -0.5, "cn_only": 0.0,
            "unknown": 0.0}.get(rel.get("state", "unknown"), 0.0)


def stats(conn: sqlite3.Connection) -> dict:
    """累计指标：有多少名字中文圈覆盖了，覆盖的晚几天。这是"领先天数"的账本。"""
    rows = conn.execute("SELECT term, lang, day FROM seen").fetchall()
    by: dict[str, dict[str, str]] = {}
    for term, lang, day in rows:
        by.setdefault(term, {})[lang] = day
    both = [(t, (date.fromisoformat(v["cn"]) - date.fromisoformat(v["en"])).days)
            for t, v in by.items() if "en" in v and "cn" in v]
    lags = sorted(l for _, l in both)
    return {
        "names": len(by),
        "cn_covered": len(both),
        "cn_uncovered": sum(1 for v in by.values() if "en" in v and "cn" not in v),
        "median_lag": lags[len(lags) // 2] if lags else None,
        "covered_within_same": sum(1 for l in lags if l <= SAME_DAYS),
        "late": sum(1 for l in lags if l >= LATE_DAYS),
        "early": sum(1 for l in lags if l < 0),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="python -m radar.lag", description="看名字在英文源和中文源之间的时间差")
    p.add_argument("--state", default="state")
    p.add_argument("--report", help="report.json：打印里面每个候选的时间差")
    p.add_argument("--stats", action="store_true")
    args = p.parse_args(argv)
    conn = connect(Path(args.state))
    if args.report:
        data = json.load(open(args.report, encoding="utf-8"))
        names = [r["term"] for r in data.get("leads", [])]
        for n in names:
            rel = relate(conn, n)
            if rel["state"] != "unknown":
                print(f"{n:32} {rel['state']:13} en={rel['t_en']} cn={rel['t_cn']} lag={rel['lag']} {rel['note']}")
    if args.stats or not args.report:
        print(json.dumps(stats(conn), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
