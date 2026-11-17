import pytest

from radar import publication, wechat


def _card(name="Example", repo="example", **overrides):
    source = f"https://github.com/example-owner/{repo}"
    return {
        "name": name,
        "url": source,
        "one_line": "将零散录音整理成文本",
        "caveat": "兼容性与费用尚未核实",
        "rewrite_status": "complete",
        "evidence_check": {
            "status": "verified",
            "source": source,
            "evidence_urls": [source, f"{source}/blob/main/README.md"],
        },
        **overrides,
    }


def test_prepared_issue_has_three_sections_titles_and_sources():
    lead = _card("<Focus>", "focus", try_now="按文档运行示例")
    worth = _card("Featured", "featured")
    quick = _card("Quick", "quick")
    rejected = _card("Unverified", "unverified", evidence_check={"status": "partial", "reason": "README 暂不可用"})
    draft = publication.prepare({"date": "2026-11-16", "lead": lead, "worth": [worth, rejected], "quick": [quick],
                                 "review_candidates": [lead, worth, quick, rejected], "warnings": ["<内部提醒>"]})

    assert draft["review_status"] == publication.NEEDS_HUMAN_REVIEW
    assert draft["ready_to_publish"] is False
    assert draft["human_review_required"] is True
    assert draft["review_summary"]["eligible"] == 3
    assert draft["review_summary"]["blocked"] == 1
    assert len({title["text"] for title in draft["titles"]}) == 3
    assert "本期 3 个项目" in publication.titles_txt(draft)

    article = wechat.html(draft)
    assert article.index("重点推荐") < article.index("精选项目") < article.index("一句话速览")
    assert "&lt;Focus&gt;" in article and "<Focus>" not in article
    assert "Unverified" not in article and "内部提醒" not in article
    assert "<style>" not in article
    assert "按文档运行示例" in wechat.text(draft)
    assert publication.links(draft)[-1]["url"] == quick["url"]

    preview = wechat.preview(draft)
    assert "<button id=\"copyBtn\" type=\"button\">" in preview
    assert preview.index('href="' + lead["evidence_check"]["evidence_urls"][1]) < preview.index('id="content"')
    assert preview.index("Unverified") < preview.index('id="content"')
    assert "&lt;内部提醒&gt;" in preview
    assert "待人工审核" in preview


@pytest.mark.parametrize("override", [
    {"skip": True},
    {"url": "https://github.com@127.0.0.1/example"},
    {"rewrite_status": "pending"},
    {"one_line": "Organizes audio transcripts"},
    {"one_line": "将零散录音……"},
    {"caveat": "暂无门槛"},
    {"evidence_check": {"status": "verified"}},
    {"evidence_check": {"status": "partial"}},
])
def test_publication_block_reason(override):
    assert publication.block_reason(_card(**override))
    assert not publication.block_reason(_card())


def test_missing_eligible_lead_blocks_copy_and_keeps_internal_candidates():
    pending = _card("PendingProject", rewrite_status="pending")
    ranked_quick = _card("QuickProject", "quick")
    draft = publication.prepare({"date": "2026-11-16", "lead": pending, "quick": [ranked_quick],
                                 "review_candidates": [pending, ranked_quick], "ready_to_publish": True})

    assert draft["review_status"] == publication.BLOCKED
    assert draft["ready_to_publish"] is False
    assert draft["lead"] is None and draft["quick"] == []
    assert len(draft["titles"]) == 3
    assert "以下标题供内部审稿" in publication.titles_txt(draft)
    assert publication.links(draft) == []
    assert "暂无已选用的项目链接" in publication.links_html(draft)
    assert "PendingProject" not in wechat.html(draft)
    assert "PendingProject" not in wechat.text(draft)
    assert "PendingProject" in wechat.preview(draft)
    assert 'id="copyBtn" type="button" disabled' in wechat.preview(draft)


def test_index_deduplicates_canonical_source_and_escapes_contents():
    lead = _card('<Project>', url="https://example.com/alt")
    duplicate = _card("Duplicate")
    external = {"name": 'Project & site', "one_line": '<b>测试</b>', "url": 'https://example.com/tool?a=1&b=2'}
    malformed = {"name": "Malformed", "url": "https://[broken", "one_line": "非法链接"}
    unsafe = {"name": "Unsafe", "url": "javascript:alert(1)", "one_line": "非法链接"}
    draft = {"date": "2026-11-16", "lead": lead, "worth": [duplicate, external, malformed, unsafe]}

    assert len(publication.links(draft)) == 2
    assert "https://github.com/example-owner/example" in publication.links_txt(draft)
    assert "https://example.com/alt" not in publication.links_txt(draft)
    page = publication.links_html(draft)
    assert 'href="https://example.com/tool?a=1&amp;b=2"' in page
    assert "&lt;Project&gt;" in page and "<b>测试</b>" not in page
    assert "javascript:" not in page
