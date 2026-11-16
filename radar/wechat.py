"""把草稿渲染成公众号能直接粘贴的 HTML（以及纯文本备份）。

公众号编辑器只认**行内样式**：<style> 块和 class 会被丢掉，外链图片会被转存。所以这里每个标签
都自带 style，不用 class、不用 <style>、不引用任何外部资源，粘贴进去版式就对了。

三个细节是按手机阅读调的：
  · 正文 15px、行高 1.9，段间距 12px——微信默认字号偏小，手机上一屏看不下太多行；
  · 每个字段前加一个浅色小标签，扫读时眼睛有落点；
  · 名字做成标题行，下面四个短行（干什么/给谁/门槛/怎么试），不写长段落。

用法
----
  python -m radar.wechat --draft draft/2026-11-16.json --out draft/2026-11-16.html
"""
from __future__ import annotations

import json
from html import escape
from pathlib import Path

MARK = {"video": " 🎬", "voice": " 🎙", "image": " 🖼", "text": ""}
C = {"ink": "#2b2b2b", "sub": "#8a8a8a", "line": "#e8e8e8", "accent": "#0b7a5a",
     "tagbg": "#eef7f3", "warn": "#b4530a", "cardbg": "#fafafa"}


def _p(text: str, size: int = 15, color: str | None = None, weight: str = "400",
       mt: int = 0, mb: int = 0, lh: float = 1.9) -> str:
    return (f'<p style="margin:{mt or 0}px 0 {mb or 0}px;font-size:{size}px;line-height:{lh};'
            f'color:{color or C["ink"]};font-weight:{weight};">{text}</p>')


def _field(label: str, value: str) -> str:
    if not value:
        return ""
    return (f'<p style="margin:0 0 6px;font-size:14px;line-height:1.8;color:{C["ink"]};">'
            f'<span style="color:{C["sub"]};font-size:12px;">{label}</span>　{escape(value)}</p>')


def _card(card: dict, index: int | None, big: bool) -> str:
    name = escape(card["name"]) + MARK.get(card.get("format"), "")
    title_size = 20 if big else 17
    tag = card.get("tag") or ""
    head = (f'<span style="display:inline-block;background:{C["tagbg"]};color:{C["accent"]};'
            f'font-size:11px;padding:1px 6px;border-radius:3px;margin-right:6px;">{escape(tag)}</span>'
            if tag else "")
    num = f'<span style="color:{C["sub"]};font-size:13px;margin-right:6px;">{index}.</span>' if index else ""
    body = [f'<p style="margin:0 0 8px;font-size:{title_size}px;line-height:1.5;font-weight:600;color:{C["ink"]};">'
            f'{num}{head}{name}</p>']
    if card.get("one_line"):
        body.append(f'<p style="margin:0 0 10px;font-size:16px;line-height:1.8;color:{C["ink"]};font-weight:500;">'
                    f'{escape(card["one_line"])}</p>')
    body.append(_field("给谁用", card.get("audience", "")))
    body.append(_field("为什么今天", card.get("why_today", "")))
    body.append(_field("门槛", card.get("caveat", "")))
    body.append(_field("怎么试", card.get("try_now", "")))
    lag = card.get("lag") or {}
    if lag.get("note"):
        body.append(_field("时间差", lag["note"]))
    if card.get("url"):
        body.append(f'<p style="margin:8px 0 0;font-size:12px;color:{C["sub"]};">来源：{escape(card["url"])}</p>')
    border = f'border-left:3px solid {C["accent"]};' if big else f'border-bottom:1px solid {C["line"]};'
    return (f'<section style="margin:0 0 22px;padding:14px 14px 10px;background:{C["cardbg"]};{border}'
            f'border-radius:4px;">' + "".join(body) + "</section>")


def html(draft: dict) -> str:
    d = draft["date"]
    out = [f'<section style="font-family:-apple-system,BlinkMacSystemFont,\'PingFang SC\',\'Microsoft YaHei\',sans-serif;">']
    out.append(_p(f'AI 新东西 · {d}', 13, C["sub"], mt=0, mb=2))
    st = draft.get("stats") or {}
    if st:
        out.append(_p(f'今天筛了 {st.get("candidates", 0)} 个新名字，留下 {st.get("cards", 0)} 个能用的，'
                      f'挑了 {len([x for x in [draft.get("lead")] + (draft.get("worth") or []) if x])} 个讲', 13, C["sub"], mb=16))

    for w in draft.get("warnings") or []:
        out.append(f'<p style="margin:0 0 16px;font-size:12px;line-height:1.7;color:{C["warn"]};">⚠️ {escape(w)}</p>')

    lead = draft.get("lead")
    if lead:
        out.append(_card(lead, None, True))
    else:
        out.append(_p("今天没有够分的内容。这里会换成「本周回声」或「一个模型的一句话解读」。", 15, C["sub"], mb=20))

    worth = draft.get("worth") or []
    if worth:
        out.append(f'<p style="margin:26px 0 12px;font-size:15px;font-weight:600;color:{C["ink"]};">还值得看一眼</p>')
        for i, c in enumerate(worth, 1):
            out.append(_card(c, i, False))

    quick = draft.get("quick") or []
    if quick:
        out.append(f'<p style="margin:26px 0 8px;font-size:15px;font-weight:600;color:{C["ink"]};">一句话速览</p>')
        items = "".join(_p(f'· <b>{escape(q["name"])}</b>　{escape(q["one_line"])}', 14, C["ink"], mb=6) for q in quick)
        out.append(items)

    obs = draft.get("observation")
    if obs:
        out.append(f'<p style="margin:26px 0 8px;font-size:15px;font-weight:600;color:{C["ink"]};">今天的观察</p>')
        out.append(f'<p style="margin:0 0 6px;font-size:14px;font-weight:600;color:{C["accent"]};">{escape(obs["title"])}</p>')
        out.append(_p(escape(obs["text"]), 14, "#555555"))

    out.append(f'<p style="margin:28px 0 0;padding-top:12px;border-top:1px solid {C["line"]};'
               f'font-size:12px;line-height:1.8;color:{C["sub"]};">'
               f'数据来自 GitHub、Hugging Face、Product Hunt、Show HN、App Store 与中文科技媒体，'
               f'交叉比对后人工筛过一遍。新名字当天都有热度，不代表值得长期用。</p>')
    out.append("</section>")
    return "\n".join(out)


TOOLBAR_JS = """
<script>
(function () {
  var btn = document.getElementById('copyBtn');
  var msg = document.getElementById('msg');
  var box = document.getElementById('content');
  function say(t) { msg.textContent = t; }
  btn.addEventListener('click', function () {
    var sel = window.getSelection();
    var range = document.createRange();
    range.selectNodeContents(box);
    sel.removeAllRanges();
    sel.addRange(range);
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    sel.removeAllRanges();
    if (ok) { say('已复制，去公众号编辑器按 Cmd+V 粘贴'); return; }
    // file:// 下 execCommand 偶尔被拒，再试一次剪贴板 API（需要 https 或 localhost）
    var html = box.innerHTML, text = box.innerText;
    if (navigator.clipboard && window.ClipboardItem) {
      navigator.clipboard.write([new ClipboardItem({
        'text/html': new Blob([html], { type: 'text/html' }),
        'text/plain': new Blob([text], { type: 'text/plain' })
      })]).then(function () { say('已复制，去公众号编辑器按 Cmd+V 粘贴'); })
        .catch(function () { say('自动复制被浏览器拒绝：请点正文任意处，Cmd+A 全选、Cmd+C 复制'); });
    } else {
      say('自动复制不可用：请点正文任意处，Cmd+A 全选、Cmd+C 复制');
    }
  });
})();
</script>
"""


def preview(draft: dict, title: str | None = None) -> str:
    """完整网页：手机宽度预览 + 顶部「复制全文」按钮。

    为什么要这个：公众号编辑器要的是**带行内样式的富文本**。直接复制 .html 文件的源码是没用的，
    必须先让浏览器渲染一遍再复制。这个页面把渲染和复制合成一步，点一下按钮就等于 Cmd+A、Cmd+C。
    """
    head = title or f"草稿 {draft['date']}"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(head)}</title>
<style>
  body {{ margin:0; background:#eef0f2; font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif; }}
  .bar {{ position:sticky; top:0; z-index:9; display:flex; align-items:center; gap:10px; padding:10px 14px;
          background:#fff; border-bottom:1px solid #e3e6e8; box-shadow:0 1px 4px rgba(0,0,0,.04); }}
  .bar button {{ border:0; border-radius:6px; padding:9px 16px; font-size:14px; font-weight:600;
                 color:#fff; background:#0b7a5a; cursor:pointer; }}
  .bar button:active {{ background:#096349; }}
  #msg {{ font-size:12px; color:#8a8a8a; }}
  main {{ padding:18px 12px 60px; }}
  .phone {{ max-width:420px; margin:0 auto; background:#fff; border-radius:10px; padding:20px 18px;
            box-shadow:0 2px 12px rgba(0,0,0,.06); }}
  @media (max-width:480px) {{ .phone {{ border-radius:0; box-shadow:none; padding:16px 14px; }} main {{ padding:0 0 40px; }} }}
</style>
</head>
<body>
<div class="bar">
  <button id="copyBtn">复制全文（带排版）</button>
  <span id="msg">点按钮，然后到公众号编辑器 ⌘V</span>
</div>
<main><div class="phone" id="content">
{html(draft)}
</div></main>
{TOOLBAR_JS}
</body>
</html>
"""


def text(draft: dict) -> str:
    """纯文本备份：贴到知乎、社群、或者微信失败时用。"""
    lines = [f"AI 新东西 · {draft['date']}", ""]
    lead = draft.get("lead")
    if lead:
        lines += [f"【{lead['name']}】{lead['one_line']}",
                  f"给谁用：{lead['audience']}",
                  f"为什么今天：{lead['why_today']}",
                  f"门槛：{lead['caveat']}",
                  f"怎么试：{lead['try_now']}", ""]
    for i, c in enumerate(draft.get("worth") or [], 1):
        lines += [f"{i}. {c['name']}｜{c['one_line']}", f"   {c['why_today']}", ""]
    if draft.get("quick"):
        lines += ["速览："] + [f"- {q['name']}：{q['one_line']}" for q in draft["quick"]] + [""]
    if draft.get("observation"):
        lines += [f"观察：{draft['observation']['title']}", draft["observation"]["text"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="python -m radar.wechat", description="草稿 → 公众号可粘贴 HTML")
    p.add_argument("--draft", required=True)
    p.add_argument("--out", help="不给就打到屏幕")
    p.add_argument("--preview", action="store_true", help="出完整网页（带「复制全文」按钮），双击就能用")
    args = p.parse_args(argv)
    draft = json.load(open(args.draft, encoding="utf-8"))
    page = preview(draft) if args.preview else html(draft)
    if args.out:
        Path(args.out).write_text(page, encoding="utf-8")
        Path(args.out).with_suffix(".txt").write_text(text(draft), encoding="utf-8")
        print(f"写出 {args.out}" + ("（预览页：双击打开，点「复制全文」）" if args.preview else "（片段 HTML）"))
    else:
        print(page)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
