"""把审过资料的草稿渲染成带行内样式的公众号正文与本地预览。"""
from __future__ import annotations

import json
from html import escape
from pathlib import Path

from . import publication

INK = "#20242a"
MUTED = "#576265"
ACCENT = "#087459"
LINE = "#dce4df"
BODY_FIELDS = (("适合", "audience"), ("为什么关注", "why_today"), ("怎么试", "try_now"), ("注意", "caveat"))


def _p(text: str, *, size: int = 15, color: str = INK, weight: int = 400,
       margin: str = "0 0 8px") -> str:
    return (f'<p style="margin:{margin};font-size:{size}px;line-height:1.75;'
            f'color:{color};font-weight:{weight};">{escape(text)}</p>')


def _heading(title: str) -> str:
    return (f'<h2 style="margin:24px 0 12px;padding-top:12px;border-top:1px solid {LINE};'
            f'font-size:15px;line-height:1.6;color:{ACCENT};font-weight:600;">{title}</h2>')


def _lead_details(card: dict) -> list[str]:
    return [f"{label}　{card[key]}" for label, key in BODY_FIELDS if card.get(key)]


def html(draft: dict) -> str:
    """生成能复制到公众号编辑器的正文片段。"""
    body = ['<section style="font-family:-apple-system,BlinkMacSystemFont,\'PingFang SC\','
            '\'Microsoft YaHei\',sans-serif;">',
            _p(f"新东西雷达 · {draft['date']}", size=13, color=MUTED)]
    if draft.get("review_status") == publication.BLOCKED:
        return "\n".join([*body, _p("本期缺少符合条件的重点项目。"), "</section>"])

    lead = draft.get("lead")
    if lead:
        body.extend([_heading("重点推荐"), _p(lead["name"], size=21, weight=600),
                     _p(lead["one_line"], size=16, margin="0 0 12px")])
        body.extend(_p(line, size=14) for line in _lead_details(lead))

    worth = draft.get("worth") or []
    if worth:
        body.append(_heading("精选项目"))
        for card in worth:
            body.append(_p(card["name"], size=16, weight=600, margin="14px 0 2px"))
            body.append(_p(card["one_line"]))
            if card.get("caveat"):
                body.append(_p(f"注意　{card['caveat']}", size=13, color=MUTED))

    quick = draft.get("quick") or []
    if quick:
        body.append(_heading("一句话速览"))
        body.extend(_p(f"{card['name']}｜{card['one_line']}", size=14) for card in quick)

    body.append("</section>")
    return "\n".join(body)


TOOLBAR_JS = """
<script>
(function () {
  var btn = document.getElementById('copyBtn');
  var msg = document.getElementById('copyStatus');
  var box = document.getElementById('content');
  btn.addEventListener('click', async function () {
    var selection = window.getSelection();
    var range = document.createRange();
    range.selectNodeContents(box);
    selection.removeAllRanges();
    selection.addRange(range);
    var copied = false;
    try { copied = document.execCommand('copy'); } catch (e) {}
    selection.removeAllRanges();
    if (!copied && navigator.clipboard && window.ClipboardItem) {
      try {
        await navigator.clipboard.write([new ClipboardItem({
          'text/html': new Blob([box.innerHTML], { type: 'text/html' }),
          'text/plain': new Blob([box.innerText], { type: 'text/plain' })
        })]);
        copied = true;
      } catch (e) {}
    }
    msg.textContent = copied ? '已复制。粘贴到公众号草稿后，请用手机预览核对。'
      : '自动复制不可用，请手动选中正文后复制。';
  });
})();
</script>
"""


def _preview_review(draft: dict, status: str) -> str:
    parts = [f'<aside aria-label="内部审稿"><h1>{escape(status)}</h1>']
    titles = draft.get("titles") or publication.titles(draft)
    parts.append('<h2>备选标题</h2><ul>' + "".join(
        f'<li><strong>{escape(item["style"])}</strong>　{escape(item["text"])}</li>' for item in titles
    ) + "</ul>")

    summary = draft.get("review_summary") or {}
    blocked = summary.get("blocked_examples") or []
    if blocked:
        parts.append(f'<details><summary>{summary["blocked"]} 个候选待处理</summary><ul>')
        parts.extend(f'<li>{escape(item["name"])}　{escape(item["reason"])}</li>' for item in blocked)
        parts.append("</ul></details>")

    sources = []
    cards = [draft.get("lead"), *(draft.get("worth") or []), *(draft.get("quick") or [])]
    for card in cards:
        if not card:
            continue
        evidence = card.get("evidence_check") or {}
        refs = [url for url in evidence.get("evidence_urls") or [] if publication.safe_link(url)]
        if refs:
            anchors = "　".join(f'<a href="{escape(url)}" rel="noopener noreferrer">资料 {i}</a>'
                                for i, url in enumerate(refs, 1))
            sources.append(f'<li>{escape(card["name"])}　{anchors}</li>')
    if sources:
        parts.append("<details><summary>核对项目资料</summary><ul>" + "".join(sources) + "</ul></details>")

    if draft.get("warnings"):
        parts.append("<details><summary>生成记录</summary><ul>")
        parts.extend(f'<li>{escape(warning)}</li>' for warning in draft["warnings"])
        parts.append("</ul></details>")
    return "\n".join([*parts, "</aside>"])


def preview(draft: dict, title: str | None = None) -> str:
    """完整预览页。复制按钮仅选中正文，备选标题和审核信息在正文外。"""
    head = title or f"公众号草稿 {draft['date']}"
    state = draft.get("review_status")
    lead = draft.get("lead")
    can_copy = state == publication.NEEDS_HUMAN_REVIEW and bool(lead) and not publication.block_reason(lead)
    if can_copy:
        status = "待人工审核"
        copy_notice = "核对标题、项目资料和链接后，可复制到公众号草稿。"
    elif state == publication.BLOCKED:
        status = "审核阻塞"
        copy_notice = "本期缺少符合条件的重点项目。"
    else:
        status = "审核状态待确认"
        copy_notice = "请重新生成草稿以检查审核状态。"
    disabled = "" if can_copy else " disabled"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(head)}</title>
<style>
  body {{ margin:0; background:#eef2ef; color:{INK}; font:15px/1.65 system-ui,'PingFang SC',sans-serif; }}
  header {{ position:sticky; top:0; display:flex; align-items:center; flex-wrap:wrap; gap:8px 12px;
            padding:12px 16px; background:#fff; border-bottom:1px solid {LINE}; }}
  button {{ border:0; border-radius:6px; padding:9px 16px; font:inherit; font-weight:600;
            color:#fff; background:{ACCENT}; cursor:pointer; }}
  button:disabled {{ color:#46534e; background:#dbe2de; cursor:default; }}
  #copyStatus {{ font-size:13px; color:{MUTED}; }}
  aside, main {{ box-sizing:border-box; margin:16px auto; width:min(100% - 24px, 29rem);
                 padding:16px 20px; background:#fff; border:1px solid {LINE}; border-radius:10px; }}
  aside {{ font-size:13px; overflow-wrap:anywhere; }}
  h1 {{ font-size:17px; margin:0 0 8px; }}
  aside h2 {{ font-size:14px; margin:0; }}
  aside ul {{ margin:4px 0 8px; padding-left:18px; }}
  details {{ margin-top:6px; }}
  summary {{ cursor:pointer; }}
  a {{ color:{ACCENT}; }}
  main {{ min-height:8rem; margin-bottom:32px; padding-bottom:24px; }}
</style>
</head>
<body>
<header>
  <button id="copyBtn" type="button"{disabled}>复制公众号正文</button>
  <span id="copyStatus" role="status">{copy_notice}</span>
</header>
{_preview_review(draft, status)}
<main id="content" aria-label="公众号正文">
{html(draft)}
</main>
{TOOLBAR_JS if can_copy else ""}
</body>
</html>
"""


def text(draft: dict) -> str:
    """纯文本备份，与公众号正文使用相同的栏目。"""
    lines = [f"新东西雷达 · {draft['date']}", ""]
    if draft.get("review_status") == publication.BLOCKED:
        return "\n".join([*lines, "本期缺少符合条件的重点项目。", ""])

    lead = draft.get("lead")
    if lead:
        lines.extend(["重点推荐", lead["name"], lead["one_line"], *_lead_details(lead), ""])

    worth = draft.get("worth") or []
    if worth:
        lines.append("精选项目")
        for card in worth:
            lines.append(f"{card['name']}｜{card['one_line']}")
            if card.get("caveat"):
                lines.append(f"注意　{card['caveat']}")
        lines.append("")

    quick = draft.get("quick") or []
    if quick:
        lines.extend(["一句话速览", *(f"{card['name']}｜{card['one_line']}" for card in quick), ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m radar.wechat", description="把草稿渲染成公众号正文")
    p.add_argument("--draft", required=True, type=Path)
    p.add_argument("--out", type=Path, help="不给就打到屏幕")
    p.add_argument("--preview", action="store_true", help="出完整网页（带复制正文按钮）")
    args = p.parse_args(argv)
    draft = json.loads(args.draft.read_text(encoding="utf-8"))
    page = preview(draft) if args.preview else html(draft)
    if args.out:
        args.out.write_text(page, encoding="utf-8")
        args.out.with_suffix(".txt").write_text(text(draft), encoding="utf-8")
        print(f"写出 {args.out}")
    else:
        print(page)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
