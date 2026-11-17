import json
from datetime import date, timedelta

from radar import editor

TODAY = date(2026, 11, 16)


def repo(name, description, *, stars=200, velocity=40):
    return {
        "repo": f"sample/{name.replace(' ', '-')}", "name": name, "description": description,
        "stars": stars, "velocity": velocity, "age_days": 2,
    }


def verified(candidate, *, description=None, excerpt="## Usage\nFollow the project documentation."):
    url = candidate["url"]
    return {
        "status": "verified",
        "description": description or candidate["about"],
        "readme_excerpt": excerpt,
        "has_usage": True,
        "source": url,
        "evidence_urls": [url, f"{url}/blob/main/README.md"],
        "stars": candidate["evidence"]["stars"],
    }


def revisions(monkeypatch, descriptions):
    def chat(_cfg, payload):
        return json.dumps([{
            "name": candidate["name"],
            "one_line": descriptions[candidate["name"]],
            "caveat": "暂无",
            "skip": False,
            "usable": 5,
            "fresh": 5,
            "why_today": "凭空获得九千万星",
        } for candidate in json.loads(payload) if candidate["name"] in descriptions])

    monkeypatch.setattr(editor, "_chat", chat)


def test_candidates_do_not_merge_conflicting_sources_or_filter_checkable_stubs(tmp_path):
    report = {
        "breakout": {"repos": [repo("Chat Demo", "Chat Demo"), repo("pricing", "web app"), {"name": "Broken"}]},
        "feed": {"new": {"hn": [
            {"name": "Chat Demo", "about": "Unrelated paid product", "url": "https://chat-demo.example"},
            {"name": "图鉴", "about": "通过照片整理收藏", "url": "https://gallery.example"},
            {"name": "Unsafe", "about": "Tool", "url": "http://localhost/foo"},
            {"name": "Empty App", "about": "Coming soon", "url": "https://coming.example"},
        ]}},
        "hf": {"terms": [{"term": "Fresh Space", "examples": ["org/demo (gradio, ♥538)"]}]},
        "leads": [{"term": "Chat Demo", "repo": "unrelated/Chat-Demo"}],
    }
    cfg = editor.load_cfg(tmp_path / "absent.json")
    candidates, dropped = editor.clean(editor.candidates(report), cfg)
    chat = next(c for c in candidates if c["name"] == "Chat Demo")
    assert chat["about"] == "Chat Demo"
    assert chat["types"] == ["github"]
    assert chat["evidence"]["repo"] == "sample/Chat-Demo"
    assert {c["name"] for c in dropped} == {"pricing", "Unsafe", "Empty App"}
    assert next(c for c in candidates if c["name"] == "Fresh Space")["evidence"]["space"] is True
    assert any(c["name"] == "图鉴" for c in candidates)


def test_reader_value_drives_sections_and_preserves_sources(tmp_path, monkeypatch):
    rows = [
        repo("QuartzKernel", "A framework and kernel SDK requiring CUDA and a GPU", stars=50_000, velocity=5_000),
        repo("StudioMyna", "Browser app and audio editor for trimming voice recordings", stars=1_200),
        repo("FableChat", "Chat Demo", stars=800),
        repo("EveningImage", "Website and image editor for artists", stars=3_000),
    ]
    report = {"breakout": {"repos": rows}}
    revisions(monkeypatch, {
        "StudioMyna": "在浏览器裁剪录音",
        "QuartzKernel": "编译显卡计算模块",
        "FableChat": "在网页开启独立对话",
        "EveningImage": "在网页编辑素材图片",
    })

    def check(candidate):
        if candidate["name"] == "EveningImage":
            return {"status": "partial", "reason": "接口暂不可用"}
        if candidate["name"] == "FableChat":
            return verified(candidate, excerpt="## Usage\nOpen the web app in a browser and configure an API key.")
        return verified(candidate)

    draft = editor.build(report, tmp_path, editor.load_cfg(tmp_path / "absent.json"), today=TODAY,
                         evidence_fn=check, lag_fn=lambda _: {"state": "cn_uncovered"})
    assert draft["review_status"] == "needs_human_review"
    assert draft["ready_to_publish"] is False
    assert len(draft["titles"]) == 3
    assert draft["lead"]["name"] == "StudioMyna"
    assert [card["name"] for card in draft["worth"]] == ["FableChat"]
    assert [card["name"] for card in draft["quick"]] == ["QuartzKernel"]
    assert all(card["lag_bonus"] == 0 for card in draft["review_candidates"])
    assert draft["lead"]["evidence_check"]["evidence_urls"][-1].endswith("README.md")
    assert draft["lead"]["caveat"] == "兼容性与费用尚未核实"
    assert draft["lead"]["why_today"] == "核实时 GitHub 获 1,200 星"
    assert {"name": "EveningImage", "reason": "接口暂不可用"} in draft["review_summary"]["blocked_examples"]


def test_shortlist_and_offline_pool_recheck_without_premature_cooldown(tmp_path, monkeypatch):
    rows = [
        repo("StudioMyna", "A browser app and audio editor for trimming voice recordings"),
        repo("QuietLib", "A framework library for CUDA"),
    ]
    report = {"breakout": {"repos": rows}}
    cfg = editor.load_cfg(tmp_path / "absent.json")
    cfg["candidate_limit"] = 1
    calls = []
    revisions(monkeypatch, {"StudioMyna": "用浏览器裁剪录音"})

    def check(candidate):
        calls.append(candidate["name"])
        return verified(candidate)

    blocked = editor.build(report, tmp_path, cfg, today=TODAY, offline=True, evidence_fn=check)
    assert blocked["lead"] is None
    assert blocked["review_status"] == "blocked"
    assert len(blocked["titles"]) == 3
    assert calls == ["StudioMyna"]
    assert not editor.pool_load(tmp_path)["items"]["studiomyna"]["used_at"]

    next_day = TODAY + timedelta(days=1)
    regenerated = editor.build({}, tmp_path, cfg, today=next_day, evidence_fn=check)
    assert regenerated["lead"]["name"] == "StudioMyna"
    assert regenerated["lead"]["evidence_raw"]["age_days"] == 3
    rerun = editor.build({}, tmp_path, cfg, today=next_day, evidence_fn=check)
    assert rerun["lead"]["name"] == "StudioMyna"
    later = editor.build(report, tmp_path, cfg, today=next_day + timedelta(days=1), evidence_fn=check)
    assert later["lead"] is None
    assert "StudioMyna" not in [c["name"] for c in later["review_candidates"]]


def test_model_cannot_copy_summary_or_add_unverified_usage(tmp_path, monkeypatch):
    about = "在浏览器制作贴纸"
    rows = [repo("StudioMyna", about), repo("EveningImage", "Website and image editor for artists")]

    def chat(_cfg, payload):
        docs = {c["name"]: c for c in json.loads(payload)}
        assert docs["StudioMyna"]["unverified_report_summary"] == ""
        assert docs["EveningImage"]["readme_excerpt"] == ""
        return json.dumps([None, {
            "name": "StudioMyna", "skip": False, "one_line": about, "try_now": "打开网页就能免费使用",
        }, {
            "name": "EveningImage", "skip": False, "one_line": "在浏览器修改图片", "try_now": "输入命令进行安装",
        }])

    monkeypatch.setattr(editor, "_chat", chat)

    def check(candidate):
        return verified(candidate) if candidate["name"] == "StudioMyna" else {"status": "partial"}

    draft = editor.build({"breakout": {"repos": rows}}, tmp_path, editor.load_cfg(tmp_path / "absent.json"),
                         today=TODAY, evidence_fn=check)
    assert draft["review_status"] == "blocked"
    cards = {card["name"]: card for card in draft["review_candidates"]}
    assert cards["StudioMyna"]["one_line"] == ""
    assert cards["EveningImage"]["try_now"] == ""
