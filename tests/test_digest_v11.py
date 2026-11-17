import json
from contextlib import closing
from datetime import date, timedelta

import pytest

from radar import cn_feeds, digest, lag


@pytest.mark.parametrize("feeds_state, expected", [
    ("partial", "unknown"),
    ("empty", "unknown"),
    ("skipped", "unknown"),
    ("complete", "cn_uncovered"),
])
def test_digest_limits_absence_claims_to_complete_cn_monitoring(tmp_path, monkeypatch,
                                                              feeds_state, expected):
    today = date.today()
    state, out = tmp_path / "state", tmp_path / "draft"
    report = tmp_path / "report.json"
    for_name = "StudioMyna"
    report.write_text(json.dumps({"breakout": {"repos": [{
        "name": for_name, "repo": "example/studio-myna", "description": "A browser editor for audio clips",
        "stars": 800, "velocity": 200, "age_days": 3,
    }]}}), encoding="utf-8")
    with closing(cn_feeds.connect(state)) as conn:
        with conn:
            conn.execute("INSERT INTO articles VALUES (?,?,?,?,?)",
                         ("https://media.example/old", "早期来源", "早期语料", today.isoformat(), today.isoformat()))

    xml = f"""<rss><channel><item><title>别的项目</title>
        <link>https://media.example/new</link><pubDate>{today}</pubDate>
        </item></channel></rss>"""
    monkeypatch.setattr(cn_feeds, "FEEDS", [
        {"name": "正常源", "url": "https://media.example/rss"},
        {"name": "备用源", "url": "https://backup.example/rss"},
    ])

    def fetch(url):
        if feeds_state == "skipped":
            pytest.fail("--no-cn must skip fetching")
        if url.startswith("https://backup"):
            if feeds_state == "partial":
                raise OSError("unavailable")
            if feeds_state == "empty":
                return "<html>Service unavailable</html>"
        return xml

    monkeypatch.setattr(cn_feeds, "_fetch", fetch)
    draft = digest.run(report, state, tmp_path / "missing.json", out,
                       offline=True, no_cn=feeds_state == "skipped", today=today)
    card, = draft["review_candidates"]
    assert card["lag"]["state"] == expected
    assert card["lag_bonus"] == 0
    assert draft["stats"]["cn_corpus"] > 0
    assert draft["stats"]["cn_complete"] is (feeds_state == "complete")
    assert draft["review_status"] == "blocked"
    assert draft["ready_to_publish"] is False
    assert {path.name for path in out.iterdir()} == {
        f"{today}.{suffix}" for suffix in (
            "json", "md", "html", "preview.html", "txt", "titles.txt", "links.txt", "links.html",
        )
    }
    for path in out.iterdir():
        assert path.stat().st_size > 0


def test_incomplete_cn_monitoring_preserves_a_recorded_hit(tmp_path):
    with closing(lag.connect(tmp_path)) as conn:
        lag.record(conn, "Project Myna", "en", date(2026, 11, 12), "github")
        lag.record(conn, "Project Myna", "cn", date(2026, 11, 16), "测试源", "https://media.example/1")
        result = lag.relate(conn, "Project Myna", date(2026, 11, 16), cn_complete=False)
    assert result["state"] == "cn_late"
    assert result["lag"] == 4
    assert result["cn_url"] == "https://media.example/1"


def test_corpus_keeps_chinese_aliases_separate_and_uses_earliest_hit(tmp_path):
    today = date.today()
    yesterday = (today - timedelta(days=1)).isoformat()
    with closing(cn_feeds.connect(tmp_path)) as conn:
        with conn:
            conn.executemany("INSERT INTO articles VALUES (?,?,?,?,?)", [
                ("https://media.example/current", "来源", "发布雨燕助手", today.isoformat(), today.isoformat()),
                ("https://media.example/early", "来源", "飞燕工具开放下载", yesterday, today.isoformat()),
                ("https://media.example/other", "来源", "rainbow 工具更新", yesterday, today.isoformat()),
            ])
    aliases = digest._variants({"leads": [
        {"term": "雨燕助手", "variants": ["飞燕工具"]},
        {"term": "别的工具", "variants": ["rain"]},
    ]})
    corpus = cn_feeds.Corpus(tmp_path)
    assert corpus.match("雨燕助手", aliases["雨燕助手"])["url"] == "https://media.example/early"
    assert corpus.match("别的工具", aliases["别的工具"]) is None
