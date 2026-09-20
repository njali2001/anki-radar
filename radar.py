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

import sources
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
    """取回一批候选条目。样例模式不联网。

    【一个源坏掉不该让整轮失败】：版块改名、论坛升级、临时限流都会这样，
    而其它源的结果照样有用。坏掉的那条打印出来，别静悄悄地少一半。
    """
    if use_sample:
        items = json.loads((HERE / "sample_posts.json").read_text(encoding="utf-8"))
        # 样例里的时间戳写的是"几小时前"，这样每次跑起来都像是刚发的。
        now = int(time.time())
        for item in items:
            item["created_utc"] = now - item.pop("hours_ago", 1) * 3600
        return items

    user_agent = config["user_agent"]
    pause_seconds = config.get("pause_seconds", 20)
    items = []
    first = True

    forum = config.get("anki_forum", {})
    if forum.get("enabled", True):
        # 【论坛用关键词搜，一个词一次请求】：那里的人全是 Anki 用户，
        # 搜索接口直接带摘要，信噪比比按版块翻高得多。
        for keyword in config["keywords"]:
            if not first:
                sources.pause(forum.get("pause_seconds", 3))
            first = False
            try:
                items.extend(
                    sources.anki_forum(keyword, user_agent, days=config.get("max_age_days", 14))
                )
            except sources.SourceError as exc:
                print(f"  跳过 Anki 论坛「{keyword}」：{exc}")

    reddit_cfg = config.get("reddit_rss", {})
    if reddit_cfg.get("enabled", True):
        # 【Reddit 只走公开 RSS，而且要很克制】：.json 已经 403，.rss 还能用，
        # 但连发几次就 429。每个请求之间歇 pause_seconds 秒。
        for subreddit in reddit_cfg.get("subreddits", []):
            for kind in ("post", "comment"):
                if not first:
                    sources.pause(pause_seconds)
                first = False
                try:
                    items.extend(sources.reddit_rss(subreddit, user_agent, kind=kind))
                except sources.SourceError as exc:
                    print(f"  跳过 r/{subreddit} 的{kind}：{exc}")
    return items


def scan(store, config, use_sample):
    patterns = [(k, pattern_for(k)) for k in config["keywords"] if k.strip()]
    now = int(time.time())
    # 【太老的直接扔掉】：一条两周前的求助帖，要么早有人答了，要么提问的人已经
    # 放弃了。回复它没有意义，而它会把今天真正值得看的挤出报告。
    oldest = now - config.get("max_age_days", 14) * 86400
    # 【论坛上一半的命中是系统消息和版主回复】：自动关帖通知、"我已经帮你恢复了"
    # 这类内容里照样有关键词，但它们不是求助，回复它们没有意义。
    noise = [n.lower() for n in config.get("skip_if_contains", [])]
    added = 0
    seen = 0
    stale = 0
    for item in collect(config, use_sample):
        seen += 1
        if item["created_utc"] < oldest:
            stale += 1
            continue
        haystack = f"{item['title']}\n{item['body']}"
        if any(n in haystack.lower() for n in noise):
            stale += 1
            continue
        hit = matches(haystack, patterns)
        if not hit:
            continue
        if store.add(item, hit, now):
            added += 1
    return seen, added, stale


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
/* 【来源用颜色分，不只是文字】：一眼扫过去要能分清这条是论坛的还是 Reddit 的，
   因为两边该用的语气和该给的答案深度不一样。 */
.src { font-weight: 600; }
.src-forum { background: #1f3346; color: #9cc9f0; }
.src-reddit { background: #46281f; color: #f0b79c; }
.snippet { margin-top: 10px; color: #aeb5c0; font-size: 14.5px; white-space: pre-wrap; }
.snippet mark, .card a.title mark { background: #3d4d24; color: #dcf5a0; border-radius: 3px;
                                    padding: 0 2px; }
.empty { color: #8b93a1; }
.note { margin-top: 36px; padding-top: 16px; border-top: 1px solid #262a31;
        color: #8b93a1; font-size: 13.5px; }
"""


def highlight(text, keywords):
    """把命中的关键词裹上 <mark>。

    【先转义再高亮，顺序不能反】：反过来的话我们自己插入的 <mark> 会被转义成
    可见的字符串。关键词本身是字母数字和空格，转义不会改变它们的写法，
    所以在转义后的文本上按原词匹配是安全的。
    """
    escaped = html.escape(text)
    for keyword in sorted(keywords, key=len, reverse=True):
        if not keyword.strip():
            continue
        escaped = re.sub(
            rf"(?<!\w)({re.escape(html.escape(keyword.strip()))})(?!\w)",
            r"<mark>\1</mark>",
            escaped,
            flags=re.IGNORECASE,
        )
    return escaped


SOURCE_LABELS = {
    "reddit": ("Reddit", "src-reddit"),
    "ankiforum": ("Anki 论坛", "src-forum"),
}


def source_tag(row):
    """来源标签：Reddit 还是 Anki 官方论坛。"""
    label, css = SOURCE_LABELS.get(row.get("source") or "", (row.get("source") or "未知", "src-forum"))
    return f'<span class="tag src {css}">{html.escape(label)}</span>'


def where(row):
    """版块 / 分区。【论坛不加 r/】：那个前缀是 Reddit 的写法，套在论坛上会让人
    以为有一个叫 forums.ankiweb.net 的版块。"""
    if row.get("source") == "reddit":
        return f"r/{row['community']}"
    return row["community"]


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
        hits = [k for k in row["matched"].split(",") if k.strip()]
        tags = "".join(
            f'<span class="tag hit">{html.escape(k)}</span>'
            for k in row["matched"].split(",") if k
        )
        cards.append(f"""
  <div class="card">
    <a class="title" href="{html.escape(row['permalink'])}" target="_blank" rel="noopener">{highlight(title, hits)}</a>
    <div class="tags">
      {source_tag(row)}
      <span class="tag">{html.escape(where(row))}</span>
      <span class="tag">{'评论' if row['kind'] == 'comment' else '帖子'}</span>
      <span class="tag">{ago(row['posted_at'])}</span>
      {tags}
    </div>
    <div class="snippet">{highlight(snippet, hits)}</div>
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
    parser.add_argument("--forum-only", action="store_true", help="只扫 Anki 论坛，不碰 Reddit")
    args = parser.parse_args()

    config = load_config()
    if args.forum_only:
        config = {**config, "reddit_rss": {**config.get("reddit_rss", {}), "enabled": False}}
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
        seen, added, stale = scan(store, config, args.sample)
        print(f"看过 {seen} 条，太老跳过 {stale} 条，新收进来 {added} 条")

        rows = store.unreported(limit)
        write_report(rows, args.sample, not args.no_open)
        if rows:
            store.mark_reported([r["external_id"] for r in rows], int(time.time()))
    finally:
        store.close()


if __name__ == "__main__":
    main()
