"""将 Radar 的 report.json 生成公众号审稿稿、正文、标题和项目链接索引。"""
from __future__ import annotations

import json
import sys
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from . import cn_feeds, editor, lag, publication, wechat


def _variants(report: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in report.get("leads", []):
        out[editor._name_key(r.get("term") or "")] = list(r.get("variants") or [])
    return out


def _record_english(conn, cands: list[dict], today: date) -> None:
    """记录英文侧观测日期，GitHub 项目用仓库创建日，榜单用当日。"""
    for c in cands:
        ev = c.get("evidence") or {}
        if c["source"] == "github" and ev.get("age_days"):
            day = today - timedelta(days=int(ev["age_days"]))
            lag.record(conn, c["name"], "en", day, "github", c.get("url", ""))
        else:
            lag.record(conn, c["name"], "en", today, c["source"], c.get("url", ""))


def run(report_path: Path, state: Path, config_path: Path, out_dir: Path,
        offline: bool = False, no_cn: bool = False, today: date | None = None, *,
        top: int | None = None) -> dict:
    today = today or date.today()
    cfg = editor.load_cfg(config_path)
    if top is not None:
        cfg["top"] = top
    report = json.loads(report_path.read_text(encoding="utf-8"))
    state.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    cn_complete = False
    if not no_cn:
        try:
            rep = cn_feeds.update(state)
            cn_complete = bool(rep["feeds"]) and not rep["errors"]
            errors.extend(f"中文源：{error}" for error in rep["errors"])
            print(f"中文源：新增 {rep['added']} 条" + (f"，{len(rep['errors'])} 个源失败" if rep["errors"] else ""))
        except Exception as exc:                      # noqa: BLE001 — 抓不到中文源也要出稿
            errors.append(f"中文源失败：{type(exc).__name__}: {str(exc)[:80]}")
    try:
        corpus = cn_feeds.Corpus(state)
    except Exception as exc:                          # noqa: BLE001 — 保留编辑及已有时间差记录
        corpus = None
        errors.append(f"中文语料库读取失败：{type(exc).__name__}")
    corpus_size = len(corpus) if corpus is not None else 0
    cn_complete = cn_complete and corpus_size > 0
    print(f"中文语料库：{corpus_size} 条")

    variants = _variants(report)
    with closing(lag.connect(state)) as conn:
        _record_english(conn, editor.candidates(report), today)

        def lag_fn(term: str) -> dict:
            aliases = variants.get(editor._name_key(term)) or []
            hit = corpus.match(term, aliases) if corpus else None
            if hit:
                lag.record(conn, term, "cn", hit["published"], hit["source"], hit["url"])
            searchable = any(cn_feeds.tokens(name) for name in [term, *aliases])
            relation = lag.relate(conn, term, today, cn_complete=cn_complete and searchable)
            if cn_complete and not searchable and relation["state"] == "unknown":
                relation["note"] = "名称缺少可匹配的词，时间差未知"
            return relation

        draft = editor.build(report, state, cfg, today=today, offline=offline, lag_fn=lag_fn)
        draft["stats"]["lag"] = lag.stats(conn)
    draft["warnings"] = (draft.get("warnings") or []) + errors
    draft["stats"]["cn_corpus"] = corpus_size
    draft["stats"]["cn_complete"] = cn_complete

    stem = out_dir / f"{today.isoformat()}"
    stem.with_suffix(".json").write_text(json.dumps(draft, ensure_ascii=False, indent=1), encoding="utf-8")
    stem.with_suffix(".md").write_text(editor.markdown(draft), encoding="utf-8")
    stem.with_suffix(".html").write_text(wechat.html(draft), encoding="utf-8")
    preview_path = stem.with_name(stem.name + ".preview.html")
    preview_path.write_text(wechat.preview(draft), encoding="utf-8")
    stem.with_suffix(".txt").write_text(wechat.text(draft), encoding="utf-8")
    stem.with_suffix(".titles.txt").write_text(publication.titles_txt(draft), encoding="utf-8")
    stem.with_suffix(".links.txt").write_text(publication.links_txt(draft), encoding="utf-8")
    stem.with_suffix(".links.html").write_text(publication.links_html(draft), encoding="utf-8")

    st = draft["stats"]
    print(f"\n候选 {st['candidates']} → 留下 {st['kept']} → 丢弃 {st['dropped']} → 卡片 {st['cards']} → 池中 {st['pooled']}")
    ls = st["lag"]
    if ls.get("names"):
        med = f"{ls['median_lag']} 天" if ls.get("median_lag") is not None else "—（还没有两边都出现的名字）"
        print(f"观测记录：{ls['names']} 个名字，已记录中文命中 {ls['cn_covered']} 个，中位时间差 {med}")
    for card in [draft.get("lead")] + (draft.get("worth") or []):
        if card:
            print(f"  ★ {card['name']:<28} {card['score']:<5} {'/'.join(str(card[k]) for k in ('usable','fresh','buzz'))}"
                  f"  {(card.get('lag') or {}).get('note','')}")
            print(f"    {card['one_line']}")
    for w in draft["warnings"]:
        print(f"  ⚠️ {w}")
    print(f"\n写出：{stem}.json / .md / .html / .preview.html / .txt / .titles.txt / .links.txt / .links.html")
    print(f"审稿状态：{draft['review_status']}，审稿预览见 {preview_path}")
    return draft


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="python -m radar.digest",
                                description="report.json → 公众号草稿（过滤 + 打分 + 改写 + 时间差）")
    p.add_argument("--report", default="report.json")
    p.add_argument("--state", default="state")
    p.add_argument("--config", default="config.json")
    p.add_argument("--out", default="draft", help="输出的目录")
    p.add_argument("--offline", action="store_true", help="跳过模型和 GitHub 实时核实，可用缓存")
    p.add_argument("--no-cn", action="store_true", help="不抓中文 RSS")
    p.add_argument("--top", type=int, help="今天推几条（覆盖 config.json 的 editor.top）")
    args = p.parse_args(argv)
    if args.top is not None and args.top < 1:
        p.error("--top 必须大于 0")
    run(Path(args.report), Path(args.state), Path(args.config), Path(args.out),
        offline=args.offline, no_cn=args.no_cn, top=args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
