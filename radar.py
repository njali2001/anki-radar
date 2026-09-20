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

import ai
import sources
import ui
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

    【中文不能加词边界】：中文字本身算 \w，而中文写起来字和字之间没有空格，
    所以"同步"前面只要还有一个汉字，`(?<!\w)` 就不成立——整个词永远匹配不到。
    含非 ASCII 字符时退回子串匹配。
    """
    word = keyword.strip()
    if any(ord(ch) > 127 for ch in word):
        return re.compile(re.escape(word), re.IGNORECASE)
    return re.compile(rf"(?<!\w){re.escape(word)}(?!\w)", re.IGNORECASE)


def matches(text, patterns):
    return [keyword for keyword, pattern in patterns if pattern.search(text or "")]


def collect(config, use_sample, only=None, progress=None):
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

    def say(text):
        print(f"  {text}", flush=True)
        if progress:
            progress(text)

    forum = config.get("anki_forum", {})
    if forum.get("enabled", True) and only in (None, "ankiforum"):
        # 【论坛用关键词搜，一个词一次请求】：那里的人全是 Anki 用户，
        # 搜索接口直接带摘要，信噪比比按版块翻高得多。
        for keyword in config["keywords"]:
            if not first:
                sources.pause(forum.get("pause_seconds", 3))
            first = False
            say(f"Anki 论坛：{keyword}")
            try:
                items.extend(
                    sources.anki_forum(keyword, user_agent, days=config.get("max_age_days", 14))
                )
            except sources.SourceError as exc:
                print(f"  跳过 Anki 论坛「{keyword}」：{exc}")

    bili = config.get("bilibili", {})
    if bili.get("enabled") and only in (None, "bilibili"):
        patterns = [(k, pattern_for(k)) for k in bili.get("keywords", []) if k.strip()]
        pause = bili.get("pause_seconds", 3)
        seen_videos = []
        for keyword in bili.get("search", []):
            if not first:
                sources.pause(pause)
            first = False
            say(f"B站 搜索：{keyword}")
            try:
                seen_videos.extend(sources.bilibili_videos(keyword))
            except sources.RateLimited as exc:
                # 【和 Reddit 一样：被判异常就停】，接着请求只会被盯得更紧。
                say("B站 判定为异常请求，这一轮到此为止")
                if bili.get("include_videos"):
                    items.extend(seen_videos)
                exc.collected = items
                raise
            except sources.SourceError as exc:
                print(f"  跳过 B站「{keyword}」：{exc}")

        # 【默认只收评论，不收视频本身】：搜到的视频绝大多数是教程，发布时间
        # 动辄一两年前，收进来只会把"最近有人卡住了"这件事淹掉；而同一个视频下面
        # 的评论是新的——2026-09-20 实测，两条进榜的视频其实都是 500 多天前的教程。
        if bili.get("include_videos"):
            items.extend(seen_videos)

        # 【只抓"标题或简介像那么回事"的视频的评论】：每个视频一次请求，
        # 不筛的话 20 个视频就是 20 次请求，而其中大半是纯教程、没人抱怨。
        picked, taken = [], set()
        for video in seen_videos:
            if video["_bvid"] in taken:
                continue
            if matches(f"{video['title']}\n{video['body']}", patterns):
                taken.add(video["_bvid"])
                picked.append(video)
        for video in picked[: int(bili.get("max_videos_for_comments", 5))]:
            sources.pause(pause)
            say(f"B站 评论：{video['title'][:24]}")
            try:
                items.extend(sources.bilibili_comments(video["_aid"], video["_bvid"]))
            except sources.RateLimited as exc:
                say("B站 判定为异常请求，这一轮到此为止")
                exc.collected = items
                raise
            except sources.SourceError as exc:
                print(f"  跳过评论：{exc}")

    reddit_cfg = config.get("reddit_rss", {})
    if reddit_cfg.get("enabled", True) and only in (None, "reddit"):
        # 【Reddit 只走公开 RSS，而且要很克制】：.json 已经 403，.rss 还能用，
        # 但连发几次就 429。每个请求之间歇 pause_seconds 秒。
        for subreddit in reddit_cfg.get("subreddits", []):
            # 【评论默认不扫】：帖子和评论各要一次请求，扫评论等于把请求数翻倍，
            # 而限流正是按请求数算的。真需要的话在 config 里打开 include_comments。
            for kind in (("post", "comment") if reddit_cfg.get("include_comments") else ("post",)):
                if not first:
                    sources.pause(pause_seconds)
                first = False
                say(f"r/{subreddit} 的{'评论' if kind == 'comment' else '帖子'}"
                    f"（每次请求之间等 {pause_seconds} 秒）")
                try:
                    items.extend(sources.reddit_rss(subreddit, user_agent, kind=kind))
                except sources.RateLimited as exc:
                    # 【限流就整轮停下】（2026-09-20 运营者要求）：接着扫下一个版块
                    # 只会让冷却时间更长。已经取到的挂在异常上带出去照常入库——
                    # 它们已经花掉了请求配额，扔掉才是浪费。
                    say(f"被限流，这一轮 Reddit 到此为止（已取到 {len(items)} 条）")
                    exc.collected = items
                    raise
                except sources.SourceError as exc:
                    print(f"  跳过 r/{subreddit} 的{kind}：{exc}")
    return items


class ScanRateLimited(RuntimeError):
    """这一轮因为限流提前结束了。"""

    def __init__(self, retry_after=None):
        super().__init__("被限流，本轮提前结束")
        self.retry_after = retry_after


def scan(store, config, use_sample, only=None, progress=None):
    patterns = [(k, pattern_for(k)) for k in config["keywords"] if k.strip()]
    now = int(time.time())
    # 【太老的直接扔掉】：一条两周前的求助帖，要么早有人答了，要么提问的人已经
    # 放弃了。回复它没有意义，而它会把今天真正值得看的挤出报告。
    # 【时间窗口按源分开】：Anki 论坛和 Reddit 每天都有新帖，两周窗口正好；
    # 而 B站 上 Anki 是个冷门话题，同一个视频下面的评论隔几个月才来一条，
    # 拿两周去卡等于永远是空的（2026-09-20 实测：最新一条评论是四个月前）。
    def cutoff_for(source):
        days = SOURCE_CONFIG_KEY.get(source)
        days = config.get(days, {}).get("max_age_days") if days else None
        return now - int(days or config.get("max_age_days", 14)) * 86400

    cutoffs = {source: cutoff_for(source) for source in SOURCE_CONFIG_KEY}
    # 【论坛上一半的命中是系统消息和版主回复】：自动关帖通知、"我已经帮你恢复了"
    # 这类内容里照样有关键词，但它们不是求助，回复它们没有意义。
    noise = [n.lower() for n in config.get("skip_if_contains", [])]
    added = 0
    seen = 0
    stale = 0
    limited = None
    try:
        batch = collect(config, use_sample, only=only, progress=progress)
    except sources.RateLimited as exc:
        # 已经取回来的那部分照样入库（collect 把它们挂在异常上带出来）。
        batch, limited = getattr(exc, "collected", []), exc
    for item in batch:
        seen += 1
        if item["created_utc"] < cutoffs.get(item.get("source"), cutoff_for(None)):
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
    if limited is not None:
        raise ScanRateLimited(getattr(limited, "retry_after", None))
    return seen, added, stale


COOLDOWN_SECONDS = 900   # 被限流之后冷静 15 分钟再说


def cooldown_left(store, source):
    """这个源还要等多久才能再扫。0 = 现在就可以。"""
    until = store.get_meta(f"cooldown_{source}")
    # 【用 float 再取整】：存进去的可能是 time.time() 那种带小数的值，
    # 直接 int("1789928079.09") 会抛 ValueError，而这条路径在渲染页面时会走到——
    # 一个本来只是"要不要禁用按钮"的小问题，会变成整页打不开。
    return max(0, int(float(until)) - int(time.time())) if until else 0


def scan_source(store, config, source, progress=None):
    """网页版点一个按钮时走的路径：只扫这个源，然后给这个源出一批。"""
    left = cooldown_left(store, source)
    if left:
        raise RuntimeError(f"上一轮被限流了，还要等 {left // 60 + 1} 分钟")

    try:
        seen, added, stale = scan(store, config, False, only=source, progress=progress)
    except ScanRateLimited as exc:
        # 【进冷却，别让人一气之下连点几次】：对方给了 Retry-After 就听它的，
        # 没给就默认 15 分钟。这期间按钮是灰的，点了也只会告诉你还剩几分钟。
        wait = exc.retry_after or COOLDOWN_SECONDS
        store.set_meta(f"cooldown_{source}", int(time.time()) + wait)
        # 已经取到的那部分照常出榜单——这一轮不是白跑的。
        pick_for(store, config, config.get("daily_limit", 5), source)
        raise RuntimeError(f"被限流，已停止扫描；{wait // 60 + 1} 分钟后可以再试") from exc

    if progress:
        progress("AI 打分…" if config.get("ai", {}).get("enabled") else "整理结果…")
    pick_for(store, config, config.get("daily_limit", 5), source)
    return seen, added, stale


def pick_for(store, config, limit, source, use_ai=True):
    """给某一个源挑出这一批。被 AI 刷掉的标成 ai_rejected，不再出现在任何榜单里。"""
    cfg = config.get("ai", {})
    now = int(time.time())

    if not (use_ai and cfg.get("enabled") and cfg.get("api_key")):
        return store.pending(source, limit)

    candidates = store.unscored_pending(source, int(cfg.get("candidates", 25)))
    if not candidates:
        return store.pending(source, limit)

    print(f"  AI 打分：{len(candidates)} 条…", flush=True)
    try:
        scores = ai.score(candidates, cfg)
    except ai.AIError as exc:
        # 【AI 挂了不挡事】：额度、网络、返回格式变了，都退回纯关键词，
        # 并把原因打出来——不要静悄悄地变成另一种行为。
        print(f"  AI 打分失败（这一轮按关键词排）：{exc}", flush=True)
        return store.pending(source, limit)

    store.set_scores(scores)
    minimum = int(cfg.get("min_score", 2))
    rejected = [
        row["external_id"] for row in candidates
        if (scores.get(row["external_id"]) or (minimum, ""))[0] < minimum
        and row["external_id"] in scores
    ]
    if rejected:
        store.mark_rejected(rejected)
        print(f"  AI 判定不相关，刷掉 {len(rejected)} 条", flush=True)
    store.mark_reported([r["external_id"] for r in candidates if r["external_id"] in scores], now)
    return store.pending(source, limit)


def pick(store, config, limit, use_ai=True):
    """挑出这次要进报告的几条。返回 (要显示的, 被 AI 判定不相关而跳过的条数)。

    【打过分的都标记成"已报告"，包括被刷掉的】：否则明天会为同样的条目再花一次钱,
    而它们的分数不会因为过了一夜就变高。
    """
    cfg = config.get("ai", {})
    now = int(time.time())

    if not (use_ai and cfg.get("enabled") and cfg.get("api_key")):
        rows = store.unreported(limit)
        if rows:
            store.mark_reported([r["external_id"] for r in rows], now)
        return rows, 0

    # 【先取一批候选，再打分，最后才截取】：直接取 limit 条去打分的话，
    # 万一这几条全被判无关，今天就一条都没有了。
    candidates = store.unreported(int(cfg.get("candidates", 25)))
    if not candidates:
        return [], 0

    print(f"  AI 打分：{len(candidates)} 条候选…", flush=True)
    try:
        scores = ai.score(candidates, cfg)
    except ai.AIError as exc:
        # 【AI 挂了不能挡住整件事】：退回纯关键词，把原因说出来。
        print(f"  AI 打分失败（退回关键词模式）：{exc}")
        rows = candidates[:limit]
        store.mark_reported([r["external_id"] for r in rows], now)
        return rows, 0

    store.set_scores(scores)
    minimum = int(cfg.get("min_score", 2))
    kept = []
    for row in candidates:
        value = scores.get(row["external_id"])
        if value is None:
            continue
        row["ai_score"], row["ai_reason"] = value
        if value[0] >= minimum:
            kept.append(row)

    # 分高的在前；同分按时间新的在前（unreported 已经排好序，sort 是稳定的）。
    kept.sort(key=lambda r: r["ai_score"], reverse=True)
    shown = kept[:limit]

    # 打过分的全部标记掉：留着它们只会在明天再花一次钱。
    store.mark_reported([r["external_id"] for r in candidates if r["external_id"] in scores], now)
    return shown, len(candidates) - len(kept)


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
.src-bili { background: #3d2233; color: #f0a8d0; }
.ai { background: #2a2440; color: #c3b6f0; }

/* 【指示灯就是按钮本身】（2026-09-20 运营者定）：绿 = 这一轮扫过了，灰 = 还没扫。
   灰的点一下就在后台扫，扫完页面自己更新。不另做一排状态图标——一个东西
   既表示状态又是操作入口，比"图标 + 旁边一个按钮"少一次找。 */
.bar { display: flex; gap: 10px; align-items: center; margin: 0 0 26px; flex-wrap: wrap; }
.src-btn { display: inline-flex; align-items: center; gap: 8px; cursor: pointer;
           border: 1px solid #2f3540; border-radius: 999px; padding: 8px 16px;
           background: #1a1d22; color: #aeb5c0; font: inherit; font-size: 14px; }
.src-btn:hover { border-color: #46505f; }
.src-btn .dot { width: 9px; height: 9px; border-radius: 50%; background: #5a6472; }
.src-btn.done { color: #cfe8a8; border-color: #3a4a26; }
.src-btn.done .dot { background: #8fd14f; }
.src-btn.busy { color: #e8d9a8; border-color: #4a4326; }
.src-btn.busy .dot { background: #e8c35a; animation: pulse 1s infinite; }
.src-btn[disabled] { cursor: default; opacity: .75; }
.src-btn.cooling { color: #e8b0a0; border-color: #4a2f26; }
.src-btn.cooling .dot { background: #c96a4e; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .3; } }
.status { color: #8b93a1; font-size: 13.5px; }
.err { color: #f0a08a; font-size: 13.5px; }
h2 { font-size: 16px; margin: 30px 0 12px; color: #cdd3dc; font-weight: 600; }
h2 .count { color: #8b93a1; font-weight: 400; font-size: 13.5px; margin-left: 6px; }
.snippet { margin-top: 10px; color: #aeb5c0; font-size: 14.5px; white-space: pre-wrap; }
.snippet mark, .card a.title mark { background: #3d4d24; color: #dcf5a0; border-radius: 3px;
                                    padding: 0 2px; }
.empty { color: #8b93a1; }
/* 【处理按钮放在卡片右下角】：读完一条的动作是"看完 → 决定 → 下一条"，
   按钮跟在内容后面最顺手；放在标题旁边会和"打开原帖"抢注意力。 */
.acts { margin-top: 12px; display: flex; gap: 8px; }
.act { cursor: pointer; border: 1px solid #2f3540; background: #1a1d22; color: #9aa3b0;
       border-radius: 8px; padding: 5px 12px; font: inherit; font-size: 13px; }
.act:hover { border-color: #46505f; color: #cdd3dc; }
.act.done:hover { border-color: #3a5a26; color: #cfe8a8; }
.card.gone { opacity: .35; }
/* 要点是给人抄材料用的，不是成品回复——用等宽字体和缩进把它和帖子正文分开，
   免得看着像"可以直接贴出去的东西"。 */
.brief { margin-top: 12px; padding: 12px 14px; border-left: 3px solid #3d4d24;
         background: #171a1f; color: #cdd3dc; font-size: 13.5px; line-height: 1.75;
         white-space: pre-wrap; font-family: ui-monospace, Consolas, monospace; }
.brief.error { border-left-color: #6b3a2c; color: #f0a08a; }
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


# 每个源在 config.json 里的那一段叫什么（用来取它自己的 max_age_days）。
SOURCE_CONFIG_KEY = {
    "ankiforum": "anki_forum",
    "reddit": "reddit_rss",
    "bilibili": "bilibili",
}

SOURCE_LABELS = {
    "reddit": ("Reddit", "src-reddit"),
    "ankiforum": ("Anki 论坛", "src-forum"),
    "bilibili": ("B站", "src-bili"),
}


def score_tag(row):
    """AI 的判断。【分数和理由一起显示】：只有分数的话，你无从判断该不该信它；
    有了理由，错判一眼就能看出来，也才知道要不要调提示词。"""
    value = row.get("ai_score")
    if value is None:
        return ""
    reason = html.escape(row.get("ai_reason") or "")
    return f'<span class="tag ai">AI {value}/3 · {reason}</span>'


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


def cards_for(rows):
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
      {score_tag(row)}
    </div>
    <div class="snippet">{highlight(snippet, hits)}</div>
    <div class="acts" data-id="{html.escape(row['external_id'])}">
      <button class="act brief-btn">写要点</button>
      <button class="act done" data-value="done">已处理</button>
      <button class="act" data-value="ignored">忽略</button>
    </div>
  </div>""")

    return "\n".join(cards) if cards else ""


def render(rows, sample):
    body = cards_for(rows) or '<p class="empty">这一轮没有新的。</p>'
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


def ago_text(stamp):
    if not stamp:
        return "还没扫过"
    return f"{ago(int(stamp))}扫过"


SAMPLE_FOR_CHECK = [
    {"external_id": "t1", "title": "My collection is too large to sync",
     "body": "AnkiWeb refuses it, 312MB. What do I do?"},
    {"external_id": "t2", "title": "I made an Anki add-on for gamification",
     "body": "It is an arcade with four games."},
    {"external_id": "t3", "title": "Media sync stuck at 0%",
     "body": "AnkiDroid keeps failing on 8GB of images"},
]
EXPECTED = {"t1": "高", "t2": "低", "t3": "高"}


def check_ai(config):
    """自检：主用和备用各跑一次那三条样例，看模型名、key 对不对，判得准不准。

    【期望值写在代码里】：光打印分数的话，你得自己回忆"插件公告应该是几分"。
    写出来才能一眼看出是配置坏了还是模型判错了。
    """
    base = dict(config.get("ai", {}))
    fallback = base.pop("fallback", None)
    targets = [("主用", base)]
    if fallback:
        targets.append(("备用", dict(fallback)))

    for label, cfg in targets:
        name = f"{cfg.get('provider')} / {cfg.get('model')}"
        if not cfg.get("api_key"):
            print(f"{label}（{name}）：没有填 api_key，跳过")
            continue
        print(f"{label}（{name}）：", end="", flush=True)
        try:
            # 单独测这一家，不让它掉到备用上去——否则"备用能用"会被误读成"主用能用"。
            result = ai.score(SAMPLE_FOR_CHECK, {**cfg, "fallback": None})
        except ai.AIError as exc:
            print(f"不可用 —— {exc}")
            continue
        print("可用")
        for ident, (value, reason) in result.items():
            want = EXPECTED.get(ident, "?")
            got = "高" if value >= 2 else "低"
            mark = "对" if got == want else "**判错了**"
            print(f"    {ident} {value}/3 {mark}  {reason}")


def render_page(store, config, status):
    """网页版的整页。两个源各一个按钮、各一份榜单。"""
    limit = config.get("daily_limit", 5)
    sections, buttons = [], []
    for source in ("ankiforum", "reddit", "bilibili"):
        if source == "bilibili" and not config.get("bilibili", {}).get("enabled"):
            continue
        label = SOURCE_LABELS[source][0]
        last = store.get_meta(f"last_scan_{source}")
        busy = status.get("busy") == source
        cooling = cooldown_left(store, source)
        css = "busy" if busy else ("done" if last else "")
        if busy:
            note = "扫描中…"
        elif cooling:
            # 【冷却时按钮是灰的、点不动】：被限流之后最要命的反应是一气之下连点几次，
            # 那只会把冷却时间拖得更长。
            note = f"限流冷却中，还有 {cooling // 60 + 1} 分钟"
            css = "cooling"
        else:
            note = ago_text(last)
        disabled = " disabled" if (busy or cooling) else ""
        buttons.append(
            f'<button class="src-btn {css}" data-source="{source}"{disabled}>'
            f'<span class="dot"></span>{label}'
            f'<span class="status">· {note}</span></button>'
        )
        rows = store.pending(source, limit)
        waiting = store.pending_count(source)
        more = f"，还有 {waiting - len(rows)} 条排队" if waiting > len(rows) else ""
        sections.append(
            f'<h2>{label}<span class="count">{len(rows)} 条{more}</span></h2>'
            + (cards_for(rows) or '<p class="empty">这一轮没有值得看的。</p>')
        )

    err = f'<p class="err">出错了：{html.escape(status.get("error") or "")}</p>' if status.get("error") else ""
    stamp = time.strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>anki-radar</title>
<style>{STYLE}</style></head>
<body>
<h1>值得看的帖子</h1>
<p class="meta">{stamp} · 点标题在新标签页打开原帖 · 灰色的按钮点一下就去扫</p>
<div class="bar">{''.join(buttons)}<span class="status" id="step">{html.escape(status.get("step") or "")}</span></div>
{err}
{''.join(sections)}
<p class="note">
  这些是<strong>链接，不是草稿</strong>。回复请用你自己的账号发，提到 LeeAB 时说明身份。<br>
  点【已处理】或【忽略】之后那一条就不再出现；没点的下次打开还在。
  AI 判定不相关的会被直接刷掉，不占位置。
</p>
<script>
// 【点一下就在后台扫，页面每秒问一次进度】：Reddit 那边一轮要四五分钟
// （每个请求之间强制等 20 秒），没有进度的话人会以为它卡死了。
const step = document.getElementById("step");
document.querySelectorAll(".src-btn").forEach(btn => {{
  btn.addEventListener("click", async () => {{
    if (btn.disabled) return;
    btn.classList.add("busy");
    btn.disabled = true;
    const res = await fetch("/scan?source=" + btn.dataset.source);
    if (!res.ok) {{ step.textContent = "另一个源正在扫，等它扫完"; return; }}
    poll();
  }});
}});
// 【点完就地消失，不刷新整页】：刷新会跳回页首，而人正读到第三条。
// 【写要点：点了才生成】。不自动给每条生成——既省额度，也免得一屏草稿把
// "今天该看哪几条"这件事淹掉。生成的是材料，不是成品回复，回复由人自己写。
document.querySelectorAll(".brief-btn").forEach(btn => {{
  btn.addEventListener("click", async () => {{
    const acts = btn.closest(".acts");
    const card = acts.closest(".card");
    if (card.querySelector(".brief")) return;   // 已经生成过就不重复花钱
    const box = document.createElement("div");
    box.className = "brief";
    box.textContent = "生成中…";
    card.appendChild(box);
    btn.disabled = true;
    try {{
      const res = await fetch("/brief?id=" + encodeURIComponent(acts.dataset.id));
      const data = await res.json();
      if (data.text) {{ box.textContent = data.text; }}
      else {{ box.className = "brief error"; box.textContent = "生成失败：" + (data.error || "未知原因"); }}
    }} catch (e) {{
      box.className = "brief error";
      box.textContent = "生成失败：连不上本地服务，刷新页面试试";
    }}
    btn.disabled = false;
  }});
}});

document.querySelectorAll(".acts").forEach(acts => {{
  acts.querySelectorAll(".act[data-value]").forEach(btn => {{
    btn.addEventListener("click", async () => {{
      const card = acts.closest(".card");
      card.classList.add("gone");
      const url = "/verdict?id=" + encodeURIComponent(acts.dataset.id)
                + "&value=" + btn.dataset.value;
      const res = await fetch(url);
      if (res.ok) {{ card.remove(); }}
      else {{ card.classList.remove("gone"); step.textContent = "标记失败，刷新页面再试"; }}
    }});
  }});
}});
async function poll() {{
  let state;
  try {{
    state = await (await fetch("/status")).json();
  }} catch (e) {{
    // 【断线要说出来】：本地服务被关掉之后，页面会永远停在"扫描中"那个闪烁
    // 状态，人以为还在扫，其实早就没人在扫了（2026-09-20 运营者遇到）。
    document.querySelectorAll(".src-btn.busy").forEach(b => b.classList.remove("busy"));
    step.textContent = "和本地服务断开了 —— 刷新页面；打不开就双击 run.bat 重开";
    return;
  }}
  step.textContent = state.step || "";
  if (state.busy) {{ setTimeout(poll, 1000); return; }}
  // 扫完了：重画整页，这样榜单、按钮颜色、时间全都跟着更新。
  location.reload();
}}
// 页面打开时如果后台正在扫（比如双击启动时那一轮），接着轮询。
if ({ 'true' if status.get('busy') else 'false' }) poll();
</script>
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
    parser.add_argument("--no-ai", action="store_true", help="这次不用 AI 打分，只按关键词")
    parser.add_argument("--serve", action="store_true",
                        help="开本地网页版：两个源各一个按钮，点灰色的那个去扫")
    parser.add_argument("--port", type=int, default=8899, help="网页版端口（默认 8899）")
    parser.add_argument("--check-ai", action="store_true",
                        help="用三条样例测一下 AI 配置（主用和备用各测一次）")
    args = parser.parse_args()

    config = load_config()
    if args.forum_only:
        config = {**config, "reddit_rss": {**config.get("reddit_rss", {}), "enabled": False}}
    store = Store(HERE / config.get("database", "radar.sqlite3"))
    limit = args.limit or config.get("daily_limit", 5)

    try:
        if args.check_ai:
            check_ai(config)
            return

        if args.serve:
            # 【启动时先扫一遍论坛】：它几十秒就完事，人打开页面时就已经有东西看了。
            # Reddit 不自动扫——那要四五分钟，该不该花这个时间由人决定。
            ui.serve(store, config, scan_source, render_page,
                     port=args.port, open_browser=not args.no_open,
                     warm_source="ankiforum")
            return

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

        print("扫描中…" + ("（离线样例）" if args.sample else ""), flush=True)
        seen, added, stale = scan(store, config, args.sample)
        print(f"看过 {seen} 条，太老跳过 {stale} 条，新收进来 {added} 条")

        rows, dropped = pick(store, config, limit, use_ai=not args.no_ai)
        if dropped:
            print(f"AI 判定不相关，跳过 {dropped} 条")
        write_report(rows, args.sample, not args.no_open)
    finally:
        store.close()


if __name__ == "__main__":
    main()
