"""一天的活：抓中文源 → 记首现 → 编辑成卡片 → 出三个文件（JSON / Markdown / 公众号 HTML）。

它不改上游的任何模块，只在 report.json 之后接一段自己的流程，所以上游升级、你 rebase，都不用
改这里的东西（除非 report.json 的结构变了）。

  report.json ──► 候选 ──► 硬过滤 ──► 同族合并 ──► 打分 ──┬──► 模型改写 ──► 候选池 ──► 今日 3 条
                                                            │
        中文 RSS ──► 中文语料库 ───────────────────────────┘（时间差加成）
                                                            │
                                  draft/2026-11-16.json ────┴──► .md（审稿）/ .html（粘贴）

用法
----
  # 每天跑完 radar daily 之后（同一目录）
  python -m radar.digest --report report.json --state state --out draft

  # 第一次：不配模型，只看过滤和打分干不干净
  python -m radar.digest --report report.json --offline

  # 不抓中文源（离线调试 / 网络被墙时）
  python -m radar.digest --no-cn

退出码一直是 0：这一层失败不该让当天的雷达结果丢失，错误写在 stdout 和 draft 的 warnings 里。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

from . import cn_feeds, editor, lag, wechat


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _variants(report: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in report.get("leads", []):
        out[_norm(r.get("term", ""))] = list(r.get("variants") or [])
    return out


def _record_english(conn, cands: list[dict], today: date) -> None:
    """英文侧的首现日：仓库用创建日（最接近「这名字第一次出现在世界上」），榜单用今天。"""
    for c in cands:
        ev = c.get("evidence") or {}
        if c["source"] == "github" and ev.get("age_days"):
            day = today - timedelta(days=int(ev["age_days"]))
            lag.record(conn, c["name"], "en", day, "github", c.get("url", ""))
        else:
            lag.record(conn, c["name"], "en", today, c["source"], c.get("url", ""))


def run(report_path: Path, state: Path, config_path: Path, out_dir: Path,
        offline: bool = False, no_cn: bool = False, today: date | None = None) -> dict:
    today = today or date.today()
    cfg = editor.load_cfg(config_path)
    state.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    corpus = cn_feeds.Corpus(state)
    if not no_cn:
        try:
            rep = cn_feeds.update(state)
            corpus = cn_feeds.Corpus(state)
            print(f"中文源：新增 {rep['added']} 条" + (f"，{len(rep['errors'])} 个源失败" if rep["errors"] else ""))
        except Exception as exc:                      # noqa: BLE001 — 抓不到中文源也要出稿
            errors.append(f"中文源失败：{type(exc).__name__}: {str(exc)[:80]}")
    print(f"中文语料库：{len(corpus)} 条")

    report = json.load(open(report_path, encoding="utf-8"))
    cands = editor.candidates(report)
    conn = lag.connect(state)
    _record_english(conn, cands, today)
    variants = _variants(report)

    def lag_fn(term: str) -> dict:
        if not len(corpus):
            rel = lag.relate(conn, term)
            rel.update(state="unknown", note="中文源没抓到，时间差未知")
            return rel
        hit = corpus.match(term, variants.get(_norm(term), []))
        if hit:
            lag.record(conn, term, "cn", hit["published"], hit["source"], hit["url"])
        return lag.relate(conn, term)

    draft = editor.build(report, state, cfg, today=today, offline=offline, lag_fn=lag_fn)
    draft["warnings"] = (draft.get("warnings") or []) + errors
    draft["stats"]["cn_corpus"] = len(corpus)
    draft["stats"]["lag"] = lag.stats(conn)

    stem = out_dir / f"{today.isoformat()}"
    stem.with_suffix(".json").write_text(json.dumps(draft, ensure_ascii=False, indent=1), encoding="utf-8")
    stem.with_suffix(".md").write_text(editor.markdown(draft), encoding="utf-8")
    stem.with_suffix(".html").write_text(wechat.html(draft), encoding="utf-8")
    stem.with_suffix(".txt").write_text(wechat.text(draft), encoding="utf-8")

    st = draft["stats"]
    print(f"\n候选 {st['candidates']} → 留下 {st['kept']} → 丢弃 {st['dropped']} → 卡片 {st['cards']} → 池中 {st['pooled']}")
    ls = st["lag"]
    if ls.get("names"):
        med = f"{ls['median_lag']} 天" if ls.get("median_lag") is not None else "—（还没有两边都出现的名字）"
        print(f"时间差账本：{ls['names']} 个名字，中文覆盖 {ls['cn_covered']} 个，未覆盖 {ls['cn_uncovered']} 个，中位时间差 {med}")
    for card in [draft.get("lead")] + (draft.get("worth") or []):
        if card:
            print(f"  ★ {card['name']:<28} {card['score']:<5} {'/'.join(str(card[k]) for k in ('usable','fresh','buzz'))}"
                  f"  {(card.get('lag') or {}).get('note','')}")
            print(f"    {card['one_line']}")
    for w in draft["warnings"]:
        print(f"  ⚠️ {w}")
    print(f"\n写出：{stem}.json / .md / .html / .txt")
    return draft


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="python -m radar.digest",
                                description="report.json → 公众号草稿（过滤 + 打分 + 改写 + 时间差）")
    p.add_argument("--report", default="report.json")
    p.add_argument("--state", default="state")
    p.add_argument("--config", default="config.json")
    p.add_argument("--out", default="draft", help="输出的目录")
    p.add_argument("--offline", action="store_true", help="不调用模型（只看过滤/打分）")
    p.add_argument("--no-cn", action="store_true", help="不抓中文 RSS")
    p.add_argument("--top", type=int, help="今天推几条（覆盖 config.json 的 editor.top）")
    args = p.parse_args(argv)
    if args.top:
        raw = json.load(open(args.config, encoding="utf-8")) if Path(args.config).exists() else {}
        raw.setdefault("editor", {})["top"] = args.top
        # 用临时文件承载覆盖值，避免改动用户的 config.json
        tmp = Path(".digest-config.json")
        tmp.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        args.config = str(tmp)
    run(Path(args.report), Path(args.state), Path(args.config), Path(args.out),
        offline=args.offline, no_cn=args.no_cn)
    if args.top:
        Path(".digest-config.json").unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
