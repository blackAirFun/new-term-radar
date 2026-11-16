"""编辑层：把 report.json 的原始候选，变成能直接发公众号的卡片。

为什么要有这一层
----------------
上游做的是「发现」：哪个名字在涨。它按数据源分块、按涨幅排序、描述直接截断英文原文——所以
第一眼看不出「这东西是干嘛的、跟我有什么关系」。这一层做的是「编辑」：

  候选 candidates → 硬过滤 clean → 同族合并 cluster → 打分 score → 改写 rewrite → 候选池 pool

产出 draft/YYYY-MM-DD.json，再由 wechat.py 渲染成可粘贴的 HTML、由本文件的 markdown() 渲染
成给你审稿的 Markdown。

四条不写在别处、只写在这里的编辑规矩
------------------------------------
1. 说不出「一句话」的，不进正文。名字有了但没人能解释它是干嘛的（README 首行是 "Chat Demo"），
   对读者就是噪音，丢掉比放进去好。
2. 一句话必须是**重写**，不是截断。英文原文照搬 + 省略号，是现在「看不出做什么」的直接原因。
3. 必须有「门槛」。不写坑的播报号没有信任；写了坑，读者才敢点。
4. 排序不看涨幅，看「读者今天能不能用上」。一个 33B 模型涨得再快，普通职场人也跑不动。

用法
----
  python -m radar.digest --report report.json --state state        # 推荐：走 digest 的总入口
  python -m radar.editor --report report.json --out draft.json     # 只跑这一层，看草稿结构
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

MARK = {"video": " 🎬", "voice": " 🎙", "image": " 🖼", "text": ""}

DEFAULTS = {
    "top": 3,                  # 一篇推几个
    "pool_days": 7,            # 候选池滚动几天
    "min_about": 12,           # 少于这么多字符的描述，一律不进正文
    "min_score": 2.4,          # 低于这个分不入选（满分 5）
    "cluster": True,           # 同族合并
    "weights": {"usable": 0.35, "fresh": 0.25, "buzz": 0.20, "evidence": 0.20},
    # 名字本身就是噪音的：MCP 注册表 id、包名、路径碎片、用户名、列表页
    "deny": [r"^io\.github\.", r"^connectors?\b", r"\bcom\.[a-z]", r"^org\.", r"^net\.",
             r"\bawesome\b", r"\bcustomer stories?\b", r"\bchangelog\b", r"\bdocs?\b$", r"\bpricing\b"],
    # 整词命中就丢：这些词在导航站差分里天天出现，从来不是「新东西」
    "noise": ["law", "connect", "patterns", "discord", "tools", "app", "apps", "blog", "news", "home",
              "login", "signup", "terms", "privacy", "search", "about", "index", "sitemap", "rss",
              "agent", "agents", "skill", "skills", "mcp", "server", "servers", "prompt", "prompts"],
    # 一句话里出现这些，说明「普通人用不上」，usable 扣分
    "weak_words": ["library", "framework", "sdk", "benchmark", "dataset", "checkpoint", "lora", "quantiz",
                   "kernel", "cuda", "c++", "api wrapper", "boilerplate", "parser", "compiler", "emulator",
                   "recompilation", "static recompilation", "firmware", "iso", "rom", "reverse engineer"],
    "strong_words": ["app", "tool", "generator", "editor", "maker", "studio", "platform", "website", "web",
                     "chrome", "extension", "plugin", "bot", "calculator", "translator", "converter",
                     "工具", "生成器", "编辑器", "平台", "插件", "助手"],
}

LLM_DEFAULTS = {
    "base_url": "https://api.deepseek.com/v1",     # 任何 OpenAI 兼容的地址都行
    "model": "deepseek-chat",
    "key_env": "LLM_API_KEY",
    "batch": 8,
    "timeout": 90,
}

SYSTEM = """你是一个中文科技公众号的编辑，读者是「对 AI 有一点了解、想长期跟踪、但不会写代码」的普通职场人。

你的任务：把一批英文的新项目/新产品/新模型，改写成读者一眼能懂的卡片。

硬性要求：
1. one_line 必须用中文重写，说清「它做什么、给谁用」，必须含动词。禁止照抄或截断英文原文。
   禁止「赋能、颠覆、革命性、一站式、全新范式」这类词。不超过 22 个汉字。
2. audience 要具体到人群，写「做跨境电商的运营」而不是「开发者」。不超过 14 个汉字。
3. why_today 用给的数据说话（stars、涨幅、榜单名次、上了几个版本），不许编造。不超过 30 个汉字。
4. caveat 写门槛和风险：需要 API key、需要 GPU、需要越狱、只有英文、早期项目不稳定、收费。
   实在没有就写「暂无」。不超过 20 个汉字。
5. try_now 写「怎么开始」，要具体到动作（打开网页就能用 / 下载 App / 复制一条命令）。不超过 24 个汉字。
6. tag 只能是：工具、模型、项目、产品。
7. 如果你看完信息仍然说不清它是干嘛的，skip 填 true，并在 reason 里写一句为什么。
8. usable / fresh / buzz 各打 1-5 分。usable 是「普通职场人今天能不能用上」，跑不动的模型给 1-2 分。

只输出 JSON 数组，不要解释，不要 markdown 代码块。每个元素：
{"name":"原名","one_line":"","audience":"","why_today":"","caveat":"","try_now":"","tag":"","usable":3,"fresh":3,"buzz":3,"skip":false,"reason":""}"""


def load_cfg(path: Path | None = None) -> dict:
    """config.json 里的 editor / llm 两块。上游的 config.py 只认它自己认识的 key，所以这里直接读文件。"""
    cfg, raw = dict(DEFAULTS), {}
    try:
        raw = json.load(open(path or Path("config.json"), encoding="utf-8"))
    except (OSError, ValueError):
        pass
    for k, v in (raw.get("editor") or {}).items():
        cfg[k] = v
    cfg["llm"] = {**LLM_DEFAULTS, **(raw.get("llm") or {})}
    cfg["llm"]["base_url"] = os.environ.get("LLM_BASE_URL", cfg["llm"]["base_url"])
    cfg["llm"]["model"] = os.environ.get("LLM_MODEL", cfg["llm"]["model"])
    cfg["known_terms"] = [t.lower() for t in raw.get("known_terms") or []]
    return cfg


# ---------------------------------------------------------------- 1. 候选
def _hf_parts(example: str) -> dict:
    """'Pika-Labs/Pika-H3-Flex (image-to-video, ♥211)' → id/task/likes。"""
    m = re.match(r"(\S+) \(([^,]*), ♥(\d+)\)", example or "")
    if not m:
        return {}
    hid, kind, likes = m.groups()
    space = kind in ("gradio", "docker", "static", "streamlit")
    return {"id": hid, "task": kind, "likes": int(likes), "space": space,
            "url": f"https://huggingface.co/{'spaces/' if space else ''}{hid}"}


def _block(report: dict, key: str) -> dict:
    b = report.get(key)
    return b if isinstance(b, dict) and not b.get("skipped") else {}


def candidates(report: dict) -> list[dict]:
    """把 report.json 里各路信号摊平成统一的候选结构，同一个名字的多路信号合并。"""
    out: dict[str, dict] = {}

    def add(c: dict) -> None:
        key = re.sub(r"[^a-z0-9]+", "", c["name"].lower())
        if not key:
            return
        if key in out:
            old = out[key]
            old["types"] = sorted(set(old["types"]) | set(c["types"]))
            old["about"] = old.get("about") or c.get("about", "")
            old["url"] = old.get("url") or c.get("url", "")
            old["evidence"] = {**c.get("evidence", {}), **old.get("evidence", {})}
            return
        out[key] = c

    for r in _block(report, "breakout").get("repos", []):
        add({"name": r.get("name") or r.get("repo", ""), "url": f"https://github.com/{r['repo']}",
             "source": "github", "about": (r.get("description") or "").strip(), "types": ["github"],
             "format": "text",
             "evidence": {"stars": r.get("stars"), "velocity": r.get("velocity"), "delta": r.get("delta"),
                          "age_days": r.get("age_days"), "repo": r.get("repo")}})

    for h in _block(report, "hf").get("terms", []):
        parts = [p for p in map(_hf_parts, h.get("examples", [])) if p]
        main = parts[0] if parts else {}
        about = f"{main['id']}（{main['task']}）" if main else ""
        add({"name": h.get("term", ""), "url": main.get("url", ""), "source": "huggingface",
             "about": about, "types": ["hf"], "format": "text",
             "evidence": {"rank": h.get("rank"), "copies": h.get("copies"), "likes": main.get("likes"),
                          "task": main.get("task"), "models": [p["id"] for p in parts[:3]]}})

    feed = _block(report, "feed")
    for group, rows in (feed.get("new") or {}).items():
        for i in rows or []:
            about = (i.get("about") or "").strip()
            about = re.sub(r"^Show HN:\s*", "", about)
            add({"name": (i.get("name") or "").strip(), "url": i.get("url") or "", "source": group,
                 "about": about, "types": ["feed"], "format": "text",
                 "evidence": {"feed": group, "points": i.get("points")}})

    # leads 带的是「判过名字的」结果和格式标记（🎬🎙🖼）、仓库、付费产品描述，用来补全上面三路
    for r in report.get("leads", []):
        key = re.sub(r"[^a-z0-9]+", "", str(r.get("term", "")).lower())
        c = out.get(key)
        if not c:
            continue
        c["format"] = r.get("format") or c.get("format") or "text"
        if r.get("repo"):
            c["evidence"]["repo"] = r["repo"]
            c["about"] = c.get("about") or ""
        about = r.get("about")
        if isinstance(about, list) and len(about) == 2:      # feed 的 lead: [url, text]
            c["url"] = c.get("url") or about[0]
            c["about"] = c.get("about") or (about[1] or "").strip()

    return list(out.values())


# ---------------------------------------------------------------- 2. 硬过滤
def _denied(name: str, cfg: dict) -> str:
    low = name.strip().lower()
    if len(low) < 3 or len(low) > 60:
        return "长度不合适"
    if low in set(cfg["noise"]):
        return "通用词"
    if any(low == k or low.startswith(k + " ") for k in cfg["known_terms"]):
        return "已覆盖"
    for pat in cfg["deny"]:
        if re.search(pat, low):
            return f"匹配噪音规则 {pat}"
    if re.fullmatch(r"[a-z]+[0-9]{2,}", low) and not name[0].isupper():
        return "疑似用户名"
    return ""


def clean(cands: list[dict], cfg: dict, today: date | None = None) -> tuple[list[dict], list[dict]]:
    """返回 (留下的, 丢掉的)。丢掉的带 reason，方便你调词表。"""
    kept, dropped = [], []
    today = today or date.today()
    for c in cands:
        why = _denied(c["name"], cfg)
        about = (c.get("about") or "").strip()
        if not why and c["source"] == "github" and len(about) < cfg["min_about"]:
            why = f"描述太短（{len(about)} 字）"
        if not why and c["source"] == "github" and c["evidence"].get("age_days") and c["evidence"]["age_days"] > 60:
            why = "老仓库"
        if not why and not about and c["source"] in ("github", "feed"):
            why = "没有描述"
        if why:
            dropped.append({**c, "drop": why})
        else:
            kept.append(c)
    return kept, dropped


# ---------------------------------------------------------------- 3. 同族合并
def _family(name: str) -> str:
    """一个名字属于哪一族：取它的第一个有意义的词。

    "lfm3" / "lfm3 audio" / "lfm3 vl" → lfm3；"deepseek v4.1 flash" 与 "deepseek v4.1 flash 1017" → deepseek。
    不用"token 有交集就合并"——那会把所有带 flash 的模型连成一串（transitive closure），
    最后把不相干的东西并成一大坨，观察段就会胡说。
    """
    toks = [t.rstrip(".") for t in re.split(r"[^a-z0-9]+", name.lower()) if len(t) >= 2]
    skip = {"ai", "the", "my", "an", "a"}
    for t in toks:
        if t in skip:
            continue
        if len(t) >= 3 or any(c.isdigit() for c in t):
            return t
    return toks[0] if toks else ""


def cluster(cands: list[dict]) -> list[dict]:
    """同族合并：一遍扫过去，按族名分组，不做传递合并。"""
    groups: dict[str, list[dict]] = {}
    for c in sorted(cands, key=lambda x: -(x["evidence"].get("stars") or x["evidence"].get("velocity") or 0)):
        fam = _family(c["name"]) or c["name"]
        groups.setdefault(fam, []).append(c)
    return [{"tokens": {fam}, "members": ms, "lead": ms[0]} for fam, ms in groups.items()]


# ---------------------------------------------------------------- 4. 打分
def _score(c: dict, cfg: dict) -> dict:
    ev = c["evidence"]
    about = (c.get("about") or "").lower()
    usable = 3.0
    usable += 1.5 * sum(1 for w in cfg["strong_words"] if w in about)
    usable -= 1.5 * sum(1 for w in cfg["weak_words"] if w in about)
    if re.search(r"gpu|cuda|self-host|selfhost|docker|api key|api_key|需自己部署", about):
        usable -= 0.5

    if c["source"] == "github":
        age = ev.get("age_days") or 30
        fresh = 5.0 if age <= 3 else 4.0 if age <= 7 else 3.0 if age <= 14 else 2.0
        v = ev.get("velocity") or 0
        buzz = 5.0 if v >= 1000 else 4.0 if v >= 500 else 3.0 if v >= 200 else 2.0
        if (ev.get("stars") or 0) >= 10000:
            buzz = min(5.0, buzz + 1)
    elif c["source"] == "huggingface":
        rank = ev.get("rank") or 50
        fresh = 4.0 if rank <= 10 else 3.0
        buzz = 4.0 if rank <= 10 else 3.0 if rank <= 25 else 2.0
        # 模型本身普通读者跑不动，除非它有在线 demo（Space）
        if not ev.get("space"):
            usable -= 1.0
    else:
        fresh = 4.0
        buzz = 4.0 if (ev.get("points") or 0) >= 100 else 3.0

    evidence = min(5.0, 1.0 + 1.5 * len(c["types"]))
    w = cfg["weights"]
    total = (w["usable"] * max(1.0, min(5.0, usable)) + w["fresh"] * fresh +
             w["buzz"] * buzz + w["evidence"] * evidence) / sum(w.values())
    return {"usable": round(max(1.0, min(5.0, usable)), 1), "fresh": fresh, "buzz": buzz,
            "evidence": round(evidence, 1), "total": round(total, 2)}


# ---------------------------------------------------------------- 5. LLM 改写
def _chat(cfg: dict, user: str) -> str:
    llm = cfg["llm"]
    key = os.environ.get(llm["key_env"], "")
    if not key:
        raise RuntimeError(f"没有 {llm['key_env']}")
    body = json.dumps({"model": llm["model"], "temperature": 0.3,
                       "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]}).encode()
    req = urllib.request.Request(f"{llm['base_url'].rstrip('/')}/chat/completions", data=body,
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=llm["timeout"]) as res:
        data = json.loads(res.read())
    return data["choices"][0]["message"]["content"]


def _parse(text: str) -> list[dict]:
    m = re.search(r"\[.*\]", text, re.S)          # 模型偶尔会包一层解释或 ```json
    if not m:
        raise ValueError("响应里没有 JSON 数组")
    rows = json.loads(m.group(0))
    return rows if isinstance(rows, list) else []


def _payload(cands: list[dict]) -> str:
    lines = []
    for c in cands:
        ev = c["evidence"]
        bits = [f"名字: {c['name']}", f"来源: {c['source']}"]
        if c.get("about"):
            bits.append(f"英文描述: {c['about'][:300]}")
        if ev.get("stars"):
            bits.append(f"星数: {ev['stars']}，日均 +{ev.get('velocity')}，昨天 +{ev.get('delta')}，上线 {ev.get('age_days')} 天")
        if ev.get("rank"):
            bits.append(f"HF 热榜第 {ev['rank']} 名，{ev.get('copies', 1)} 个版本，类型 {ev.get('task')}")
        if ev.get("points"):
            bits.append(f"HN 分数: {ev['points']}")
        lines.append(" | ".join(bits))
    return "\n".join(lines)


def rewrite(cands: list[dict], cfg: dict) -> tuple[dict[str, dict], list[str]]:
    """一批一批地问模型。返回 {规范化名字: 改写结果} 和失败原因列表。

    没有 key、或者调用失败，都不抛异常：调用方走 fallback，草稿照常生成，只是标注「未改写」。
    """
    got: dict[str, dict] = {}
    errs: list[str] = []
    batch = cfg["llm"]["batch"]
    for i in range(0, len(cands), batch):
        chunk = cands[i:i + batch]
        try:
            rows = _parse(_chat(cfg, _payload(chunk)))
        except (RuntimeError, urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            errs.append(f"{type(exc).__name__}: {str(exc)[:90]}")
            break
        for r in rows:
            key = re.sub(r"[^a-z0-9]+", "", str(r.get("name", "")).lower())
            if key:
                got[key] = r
        time.sleep(0.3)
    return got, errs


def _fallback_card(c: dict) -> dict:
    """没有模型时的降级卡：如实标注，不假装看懂了。审稿时你看一眼就知道哪些要补。"""
    about = (c.get("about") or "").strip()
    return {"one_line": about[:40] + ("…" if len(about) > 40 else "") or "（没有描述，需要手动补）",
            "audience": "", "why_today": "", "caveat": "", "try_now": "",
            "tag": {"github": "项目", "huggingface": "模型"}.get(c["source"], "产品"),
            "usable": None, "fresh": None, "buzz": None, "skip": not about, "reason": "未改写（没有配置模型）"}


# ---------------------------------------------------------------- 6. 候选池
def pool_load(state: Path) -> dict:
    path = state / "editor_pool.json"
    try:
        return json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return {"items": {}}


def pool_save(state: Path, data: dict) -> None:
    state.mkdir(parents=True, exist_ok=True)
    json.dump(data, open(state / "editor_pool.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def pool_update(state: Path, cards: list[dict], cfg: dict, today: date) -> dict:
    """新卡进池，过期的出池。返回池子。"""
    data = pool_load(state)
    items = data.setdefault("items", {})
    for card in cards:
        key = card["key"]
        old = items.get(key) or {}
        # 重复出现时：分数取最新，首次进池日保留，已用过的标记保留
        items[key] = {**old, **card, "pooled": old.get("pooled") or today.isoformat()}
    cutoff = (today - timedelta(days=cfg["pool_days"])).isoformat()
    for key in [k for k, v in items.items() if (v.get("pooled") or "9999") < cutoff and not v.get("used_at")]:
        items.pop(key, None)
    pool_save(state, data)
    return data


def pick(data: dict, top: int, min_score: float, today: date | None = None) -> list[dict]:
    """从池子里挑今天的：没发过的、够分的，按分数；中文已覆盖的降权。

    同一天重跑时，今天已经用过的仍然可选——否则改一次配置就再也生成不出同一期。
    """
    today = (today or date.today()).isoformat()
    items = [v for v in data.get("items", {}).values()
             if v.get("used_at") in (None, today) and (v.get("rank_score") or 0) >= min_score]
    items = [v for v in items if (v.get("score") or 0) >= min_score]
    items.sort(key=lambda v: -(v.get("rank_score") or 0))
    chosen, seen_clusters = [], set()
    for v in items:
        tag = v.get("cluster") or v["key"]
        if tag in seen_clusters and len(chosen) < top:
            continue
        chosen.append(v)
        seen_clusters.add(tag)
        if len(chosen) >= top:
            break
    return chosen


def observe(cards: list[dict], clusters: list[dict]) -> dict | None:
    """不止列清单，给一句判断：今天冒出来的名字指向哪个方向。"""
    big = [g for g in clusters if len(g["members"]) >= 2]
    if not big:
        return None
    g = max(big, key=lambda x: len(x["members"]))
    names = "、".join(m["name"] for m in g["members"][:4])
    topic = max(g["tokens"], key=len) if g["tokens"] else ""
    return {"title": f"今天有 {len(g['members'])} 个名字指向同一件事",
            "text": f"{names} 是同一族的东西（共同关键词 {topic}）。这类扎堆通常说明有人验证过需求，"
                    f"接下来会有更多同类出现——值得放进观察名单，不用今天就用。"}


def build(report: dict, state: Path, cfg: dict, today: date | None = None,
          offline: bool = False, lag_fn=None) -> dict:
    today = today or date.today()
    cands = candidates(report)
    kept, dropped = clean(cands, cfg, today)
    clusters = cluster(kept) if cfg["cluster"] else [{"tokens": set(), "members": [c], "lead": c} for c in kept]

    rewritten, errs = ({}, ["offline"]) if offline else rewrite(kept, cfg)
    cards = []
    for g in clusters:
        lead = g["lead"]
        key = re.sub(r"[^a-z0-9]+", "", lead["name"].lower())
        rw = {**_fallback_card(lead), **(rewritten.get(key) or {})}
        if rw.get("skip"):
            continue
        sc = _score(lead, cfg)
        for dim in ("usable", "fresh", "buzz"):          # 模型给的分覆盖启发式
            if isinstance(rw.get(dim), (int, float)) and rw[dim] > 0:
                sc[dim] = float(rw[dim])
        w = cfg["weights"]
        total = (w["usable"] * sc["usable"] + w["fresh"] * sc["fresh"] + w["buzz"] * sc["buzz"] +
                 w["evidence"] * sc["evidence"]) / sum(w.values())
        rel = lag_fn(lead["name"]) if lag_fn else {"state": "unknown", "note": "", "ahead": None, "lag": None}
        cards.append({
            "key": key, "name": lead["name"], "url": lead.get("url", ""), "source": lead["source"],
            "cluster": "|".join(sorted(g["tokens"])) or key,
            "members": [m["name"] for m in g["members"]],
            "tag": rw.get("tag", "项目"), "format": lead.get("format", "text"),
            "one_line": rw.get("one_line", ""), "audience": rw.get("audience", ""),
            "why_today": rw.get("why_today", ""), "caveat": rw.get("caveat", ""),
            "try_now": rw.get("try_now", ""), "reason": rw.get("reason", ""),
            "usable": sc["usable"], "fresh": sc["fresh"], "buzz": sc["buzz"], "evidence": sc["evidence"],
            "score": round(total, 2), "lag": rel, "lag_bonus": round(0.15 * (1 if rel.get("state") == "cn_uncovered"
                                                                           else 0) - 0.1 * (1 if rel.get("state") == "cn_same" else 0), 2),
            "evidence_raw": lead["evidence"], "about": lead.get("about", ""),
        })
    for c in cards:
        c["rank_score"] = round(c["score"] + c["lag_bonus"], 2)

    data = pool_update(state, cards, cfg, today)
    chosen = pick(data, cfg["top"], cfg["min_score"], today)
    for c in chosen:
        data["items"][c["key"]]["used_at"] = today.isoformat()
    pool_save(state, data)

    quick = [c for c in sorted(cards, key=lambda x: -x["rank_score"]) if c["key"] not in {x["key"] for x in chosen}][:6]
    return {
        "date": today.isoformat(), "generated_at": f"{today.isoformat()}T{time.strftime('%H:%M:%S')}",
        "lead": chosen[0] if chosen else None,
        "worth": chosen[1:],
        "quick": [{"name": c["name"], "one_line": c["one_line"], "url": c["url"], "tag": c["tag"]} for c in quick],
        "observation": observe(cards, clusters),
        "stats": {"candidates": len(cands), "kept": len(kept), "dropped": len(dropped),
                  "cards": len(cards), "pooled": len(data.get("items", {}))},
        "warnings": ([] if rewritten else ["本期没有经过模型改写：一句话是英文原文的截断，发之前必须手动改。"]) + errs[:3],
        "dropped": dropped[:15],
    }


# ---------------------------------------------------------------- 7. 审稿用的 Markdown
def markdown(draft: dict) -> str:
    d = draft["date"]
    lines = [f"# 草稿 {d}", ""]
    for w in draft.get("warnings") or []:
        lines += [f"> ⚠️ {w}", ""]
    st = draft["stats"]
    lines += [f"候选 {st['candidates']} · 留下 {st['kept']} · 丢弃 {st['dropped']} · 卡片 {st['cards']} · 池中 {st['pooled']}", ""]
    lead = draft.get("lead")
    if not lead:
        lines += ["**今天池子里没有够分的条目。** 用降级形态：本周回声 / 一个模型的一句话解读。"]
    for label, card in [("头条", lead)] + [("值得试", c) for c in draft.get("worth", [])]:
        if not card:
            continue
        lag = card.get("lag") or {}
        lines += [f"## {label}：{card['name']}{MARK.get(card.get('format'), '')}",
                  f"- 一句话：{card['one_line']}",
                  f"- 给谁用：{card['audience']}",
                  f"- 为什么今天：{card['why_today']}",
                  f"- 门槛：{card['caveat']}",
                  f"- 怎么试：{card['try_now']}",
                  f"- 标签：{card['tag']} · 可用 {card['usable']} · 新鲜 {card['fresh']} · 热度 {card['buzz']} · 综合 {card['score']}",
                  f"- 时间差：{lag.get('note') or '未知'}" + (f"（英文首现 {lag['t_en']}）" if lag.get("t_en") else ""),
                  f"- 来源：{card['url']}"]
        if len(card.get("members") or []) > 1:
            lines.append(f"- 同族：{'、'.join(card['members'])}")
        lines.append("")
    if draft.get("quick"):
        lines += ["## 速览", ""] + [f"- **{q['name']}**：{q['one_line']}" for q in draft["quick"]] + [""]
    if draft.get("observation"):
        o = draft["observation"]
        lines += [f"## 观察：{o['title']}", o["text"], ""]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    import argparse
    from . import lag as lag_mod
    p = argparse.ArgumentParser(prog="python -m radar.editor", description="把 report.json 编辑成卡片草稿")
    p.add_argument("--report", default="report.json")
    p.add_argument("--state", default="state")
    p.add_argument("--config", default="config.json")
    p.add_argument("--out", default="draft.json")
    p.add_argument("--offline", action="store_true", help="不调用模型，只看过滤和分词干不干净")
    args = p.parse_args(argv)
    cfg = load_cfg(Path(args.config))
    report = json.load(open(args.report, encoding="utf-8"))
    state = Path(args.state)
    conn = lag_mod.connect(state)
    draft = build(report, state, cfg, offline=args.offline, lag_fn=lambda t: lag_mod.relate(conn, t))
    Path(args.out).write_text(json.dumps(draft, ensure_ascii=False, indent=1), encoding="utf-8")
    Path(args.out).with_suffix(".md").write_text(markdown(draft), encoding="utf-8")
    print(markdown(draft))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
