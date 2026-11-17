"""公众号人工审稿门槛、备选标题和项目链接索引。"""
from __future__ import annotations

import re
from html import escape
from ipaddress import ip_address
from urllib.parse import urlsplit

BLOCKED = "blocked"
NEEDS_HUMAN_REVIEW = "needs_human_review"


def safe_link(url: str) -> str | None:
    if not isinstance(url, str) or re.search(r"[\x00-\x20\x7f\\]", url):
        return None
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        parsed.port  # 验证端口格式与范围
    except ValueError:
        return None
    if parsed.scheme != "https" or not host or "@" in parsed.netloc or "%" in parsed.netloc:
        return None
    host = host.rstrip(".")
    try:
        address = ip_address(host)
    except ValueError:
        if host.endswith((".localhost", ".local")):
            return None
        domain = re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z][a-z0-9-]*", host)
        return url if domain else None
    return url if address.is_global else None


def _clear_chinese(value: str) -> bool:
    return (isinstance(value, str) and len(re.findall(r"[\u3400-\u9fff]", value)) >= 4
            and "…" not in value and "..." not in value)


def block_reason(card: dict) -> str:
    """返回单条卡片的首个审稿障碍。空字符串表示可以进入人工审稿。"""
    checked = card.get("evidence_check") or {}
    if card.get("skip"):
        return card.get("reason") or "尚无法确认项目用途"
    if checked.get("status") != "verified":
        return checked.get("reason") or "项目资料尚未核实"
    if not safe_link(checked.get("source")) or not any(safe_link(url) for url in checked.get("evidence_urls") or []):
        return "缺少可核对的证据链接"
    if not isinstance(card.get("name"), str) or not card["name"].strip() or not safe_link(card.get("url")):
        return "缺少项目名称或有效的 HTTPS 链接"
    if card.get("rewrite_status") != "complete" or not _clear_chinese(card.get("one_line")):
        return "缺少完整的中文用途说明"
    caveat = card.get("caveat")
    if not _clear_chinese(caveat) or caveat.strip("。！ ") in {"暂无门槛", "没有门槛", "无需核实"}:
        return "缺少使用门槛或待核实事项"
    return ""


def _cards(draft: dict) -> list[dict]:
    return [card for card in [draft.get("lead"), *(draft.get("worth") or []), *(draft.get("quick") or [])] if card]


def titles(draft: dict) -> list[dict]:
    lead = draft.get("lead")
    day = draft.get("date", "")
    if lead:
        name = lead["name"][:22].strip()
        usage = lead["one_line"].strip("。！ ")[:34]
        count = len(_cards(draft))
        options = [
            ("实用价值", f"{name}：{usage}"),
            ("项目发现", f"新项目发现｜{usage}"),
            ("本期合集", f"新东西雷达｜本期 {count} 个项目，从 {name} 开始"),
        ]
    else:
        options = [
            ("实用价值", f"AI 工具观察笔记｜{day}"),
            ("项目发现", f"新项目线索｜{day}"),
            ("本期合集", f"新东西雷达｜{day}"),
        ]
    return [{"style": style, "text": title} for style, title in options]


def prepare(draft: dict) -> dict:
    """核对编辑层选中的版面并生成审核状态。自动生成的稿件须经人工复核。"""
    chosen = _cards(draft)
    lead = draft.get("lead")
    if lead and not block_reason(lead):
        draft["worth"] = [card for card in draft.get("worth") or [] if not block_reason(card)]
        draft["quick"] = [card for card in draft.get("quick") or [] if not block_reason(card)]
        draft["review_status"] = NEEDS_HUMAN_REVIEW
    else:
        draft.update(lead=None, worth=[], quick=[], review_status=BLOCKED)
    blocked = {}
    for card in [*(draft.get("review_candidates") or []), *chosen]:
        reason = block_reason(card) or ("读者价值评分未达选用门槛" if card.get("tier") == "review" else "")
        if reason:
            name = card.get("name", "")
            blocked[card.get("key") or name] = {"name": name, "reason": reason}
    draft["review_summary"] = {
        "eligible": len(_cards(draft)),
        "blocked": len(blocked),
        "blocked_examples": list(blocked.values())[:8],
    }
    draft["titles"] = titles(draft)
    draft["human_review_required"] = True
    draft["ready_to_publish"] = False
    return draft


def links(draft: dict) -> list[dict]:
    rows = []
    seen = set()
    for card in _cards(draft):
        checked = card.get("evidence_check") or {}
        source = checked.get("source") if checked.get("status") == "verified" else None
        url = safe_link(source) or safe_link(card.get("url"))
        if url and url.rstrip("/") not in seen:
            rows.append({"name": card.get("name", ""), "url": url, "one_line": card.get("one_line", "")})
            seen.add(url.rstrip("/"))
    return rows


def links_txt(draft: dict) -> str:
    lines = [f"新东西雷达 · {draft.get('date', '')} · 项目链接索引", ""]
    rows = links(draft)
    for row in rows:
        lines.extend([f"{row['name']}｜{row['one_line']}", row["url"], ""])
    if not rows:
        lines.append("本期暂无已选用的项目链接。")
    return "\n".join(lines).rstrip() + "\n"


def links_html(draft: dict) -> str:
    items = "\n".join(
        f'<li><strong>{escape(row["name"])}</strong><p>{escape(row["one_line"])}</p>'
        f'<a href="{escape(row["url"], quote=True)}" rel="noopener noreferrer">{escape(row["url"])}</a></li>'
        for row in links(draft)
    )
    content = f"<ul>{items}</ul>" if items else "<p>本期暂无已选用的项目链接。</p>"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>新东西雷达 · 项目链接索引</title>
<style>
body {{max-width:42rem;margin:2rem auto;padding:0 1rem;font:1rem/1.65 system-ui;color:#20242a}}
ul {{padding-left:1.4rem}} li {{margin:0 0 1.25rem}} li p {{margin:0.1rem 0}}
a {{color:#087459;overflow-wrap:anywhere}}
</style>
</head>
<body><main><h1>项目链接索引</h1><p>新东西雷达 · {escape(draft.get('date', ''))}</p>
{content}
</main></body>
</html>
"""


def titles_txt(draft: dict) -> str:
    lines = [f"{item['style']}｜{item['text']}" for item in draft.get("titles") or titles(draft)]
    if draft.get("review_status") == BLOCKED:
        lines.insert(0, "本期缺少符合条件的重点项目，以下标题供内部审稿。")
    return "\n".join(lines) + "\n"
