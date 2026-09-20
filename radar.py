# -*- coding: utf-8 -*-
"""anki-radar —— 找出论坛上"有人卡在 Anki 同步或容量上"的帖子。只读，不发帖。

    python radar.py                 扫一轮，生成并打开报告
    python radar.py --sample        用离线样例数据（没有 Reddit 凭据时用这个）
    python radar.py --limit 10      这份报告里最多放 10 条
    python radar.py --again         重新打开上一份报告，不扫描
    python radar.py --stats         各关键词分别带来了多少条
    python radar.py --no-open       生成报告但不自动打开浏览器

为什么只读、为什么回复要你自己发，见 README。
"""

import argparse
import html
import json
import re
import sys
import time
import webbrowser
from pathlib import Path

import reddit
from store import Store

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
EXAMPLE = HERE / "config.example.json"
REPORT = HERE / "report.html"


def load_config():
    path = CONFIG if CONFIG.exists() else EXAMPLE
    if not path.exists():
        sys.exit("找不到 config.json，也找不到 config.example.json")
    config = json.loads(path.read_text(encoding="utf-8"))
    if path is EXAMPLE:
        print("提示：还没有 config.json，这次用的是 config.example.json 里的默认值。")
    return config


def pattern_for(keyword):
    """整词匹配、大小写不敏感。

    【要整词】：`anki sync` 不整词会命中一堆 URL 里的片段；而 `AnkiWeb` 这种
    本来就没歧义的词，整词与否结果一样。统一整词最省心。
    """
    return re.compile(rf"(?<!\w){re.escape(keyword.strip())}(?!\w)", re.IGNORECASE)


def matches(text, patterns):
    return [keyword for keyword, pattern in patterns if pattern.search(text or "")]


def collect(config, use_sample):
    """取回一批候选条目。样例模式不联网。"""
    if use_sample:
        items = json.loads((HERE / "sample_posts.json").read_text(encoding="utf-8"))
        # 样例里的时间戳写的是"几小时前"，这样每次跑起来都像是刚发的。
        now = int(time.time())
        for item in items:
            item["created_utc"] = now - item.pop("hours_ago", 1) * 3600
        return items

    settings = config["reddit"]
    token = reddit.get_token(
        settings.get("client_id", ""), settings.get("client_secret", ""),
        settings["user_agent"],
    )
    items = []
    for subreddit in config["subreddits"]:
        for kind in ("post", "comment"):
            try:
                items.extend(reddit.fetch(subreddit, token, settings["user_agent"], kind=kind))
            except reddit.RedditError as exc:
                # 【一个版块坏掉不该让整轮失败】：版块改名、临时私有化都会这样，
                # 而其它版块的结果照样有用。
                print(f"  跳过 r/{subreddit} 的{kind}：{exc}")
    return items


def scan(store, config, use_sample):
    patterns = [(k, pattern_for(k)) for k in config["keywords"] if k.strip()]
    now = int(time.time())
    added = 0
    seen = 0
    for item in collect(config, use_sample):
        seen += 1
        hit = matches(f"{item['title']}\n{item['body']}", patterns)
        if not hit:
            continue
        if store.add(item, hit, now):
            added += 1
    return seen, added


# --- 报告 --------------------------------------------------------------------

STYLE = """
:root { color-scheme: dark; }
body { margin: 0; padding: 32px 28px 60px; background: #14161a; color: #e8eaed;
       font: 16px/1.6 "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; }
h1 { font-size: 22px; margin: 0 0 4px; }
.meta { color: #8b93a1; font-size: 14px; margin-bottom: 28px; }
.card { border: 1px solid #262a31; border-radius: 12px; padding: 18px 20px; margin-bottom: 14px;
        background: #1a1d22; }
.card a.title { color: #e8eaed; font-size: 17px; font-weight: 600; text-decoration: none; }
.card a.title:hover { text-decoration: underline; }
.tags { margin-top: 6px; font-size: 13px; color: #8b93a1; }
.tag { display: inline-block; background: #232830; border-radius: 999px; padding: 2px 10px;
       margin-right: 6px; color: #b9c0cc; }
.hit { background: #2c3a20; color: #c7e88a; }
.snippet { margin-top: 10px; color: #aeb5c0; font-size: 14.5px; white-space: pre-wrap; }
.empty { color: #8b93a1; }
.note { margin-top: 36px; padding-top: 16px; border-top: 1px solid #262a31;
        color: #8b93a1; font-size: 13.5px; }
"""


def ago(seconds):
    hours = max(0, int((time.time() - seconds) // 3600))
    if hours < 1:
        return "刚刚"
    if hours < 24:
        return f"{hours} 小时前"
    return f"{hours // 24} 天前"


def render(rows, sample):
    cards = []
    for row in rows:
        title = row["title"] or (row["body"][:90] + "…")
        snippet = (row["body"] or "")[:400]
        tags = "".join(
            f'<span class="tag hit">{html.escape(k)}</span>'
            for k in row["matched"].split(",") if k
        )
        cards.append(f"""
  <div class="card">
    <a class="title" href="{html.escape(row['permalink'])}" target="_blank" rel="noopener">{html.escape(title)}</a>
    <div class="tags">
      <span class="tag">r/{html.escape(row['community'])}</span>
      <span class="tag">{'评论' if row['kind'] == 'comment' else '帖子'}</span>
      <span class="tag">{ago(row['posted_at'])}</span>
      {tags}
    </div>
    <div class="snippet">{html.escape(snippet)}</div>
  </div>""")

    body = "\n".join(cards) if cards else '<p class="empty">这一轮没有新的。</p>'
    banner = '<p class="meta">⚠ 这是离线样例数据，不是真实的 Reddit 内容。</p>' if sample else ""
    stamp = time.strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>anki-radar {stamp}</title>
<style>{STYLE}</style></head>
<body>
<h1>今天值得看的 {len(rows)} 条</h1>
<p class="meta">{stamp} 生成 · 点标题在新标签页打开原帖</p>
{banner}
{body}
<p class="note">
  这些是<strong>链接，不是草稿</strong>。回复请用你自己的账号发，提到 LeeAB 时说明身份。<br>
  已经列进这份报告的条目不会再出现在下一份里。
</p>
</body></html>
"""


def write_report(rows, sample, open_browser):
    REPORT.write_text(render(rows, sample), encoding="utf-8")
    print(f"报告：{REPORT}")
    if open_browser:
        webbrowser.open(REPORT.as_uri())


# --- 入口 --------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="anki-radar：只读地找值得回复的帖子")
    parser.add_argument("--sample", action="store_true", help="用离线样例数据，不联网")
    parser.add_argument("--limit", type=int, help="这份报告里最多几条")
    parser.add_argument("--again", action="store_true", help="重新打开上一份报告")
    parser.add_argument("--stats", action="store_true", help="看各关键词带来了多少条")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    config = load_config()
    store = Store(HERE / config.get("database", "radar.sqlite3"))
    limit = args.limit or config.get("daily_limit", 5)

    try:
        if args.stats:
            counts = store.keyword_stats()
            totals = store.totals()
            print(f"库里一共 {totals['all']} 条，还没进过报告的 {totals['pending']} 条")
            print("各关键词命中数（多的那个如果全是噪音，就该换掉）：")
            for keyword, count in counts.items():
                print(f"  {count:>5}  {keyword}")
            return

        if args.again:
            write_report(store.last_report(limit), args.sample, not args.no_open)
            return

        print("扫描中…" + ("（离线样例）" if args.sample else ""))
        seen, added = scan(store, config, args.sample)
        print(f"看过 {seen} 条，新收进来 {added} 条")

        rows = store.unreported(limit)
        write_report(rows, args.sample, not args.no_open)
        if rows:
            store.mark_reported([r["external_id"] for r in rows], int(time.time()))
    finally:
        store.close()


if __name__ == "__main__":
    main()
