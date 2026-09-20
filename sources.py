# -*- coding: utf-8 -*-
"""数据源：Anki 官方论坛 + Reddit 公开 RSS。都不需要任何凭据。

【为什么不用 Reddit 的官方 API】（2026-09-20 查证）。Reddit 在 2025 年 11 月的
Responsible Builder Policy 之后关掉了自助注册：现在要提交工单、说明用途、等人工
审批，而且「开发者」这个身份明确是**非商业**用途——拿它去申请一个给产品做推广的
工具，是已知的被拒理由。诚实走商业那条路的话又重又慢。所以这里改用两条不需要
审批的公开渠道，Reddit 那部分另外靠 F5Bot（它有自己的渠道）兜着。

【公开渠道要克制】：`.json` 已经 403，`.rss` 还能用，但连发几次就会 429。所以
每次请求之间强制歇一会儿，请求数按"一天几十次"来设计，不是"一分钟几十次"。
识别自己（User-Agent 带上用途）也是这个道理。

【Anki 论坛信噪比更高】：那里的人全是 Anki 用户，一条抱怨同步的帖子就是一条
抱怨同步的帖子；Reddit 上同样的词可能在讲别的东西。
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

TIMEOUT = 30
RETRY_AFTER = 60   # 429 没给 Retry-After 时等多久
ATOM = "{http://www.w3.org/2005/Atom}"


class SourceError(RuntimeError):
    pass


class RateLimited(SourceError):
    """【限流要和别的错分开】：版块改名、临时私有化这类错，跳过它继续扫下一个是对的；
    而被限流时继续扫下一个，等于一边挨罚一边加压——正确的反应是立刻停下。"""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def _get(url, user_agent, accept):
    # 【HTTP 头只能是 ASCII】：config.json 里的 user_agent 留了中文占位符时，
    # urllib 会抛一个看不出所以然的 latin-1 编码错误。这里当场说清楚。
    try:
        user_agent.encode("ascii")
    except UnicodeEncodeError:
        raise SourceError(
            "config.json 里的 user_agent 含非 ASCII 字符（中文/全角符号），"
            "HTTP 头只允许 ASCII。改成纯英文，例如："
            'anki-radar/1.0 (personal monitoring; contact: you@example.com)'
        ) from None

    request = urllib.request.Request(url)
    request.add_header("User-Agent", user_agent)
    request.add_header("Accept", accept)

    # 【被限流就等一会儿再试一次，只试一次】：429 通常是"刚才太密了"，歇一会儿
    # 就过去了；但一直重试等于继续加压，所以失败第二次就老实报错、跳过这个源。
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt == 1:
                wait = int(exc.headers.get("Retry-After") or RETRY_AFTER)
                print(f"  被限流，等 {wait} 秒再试一次…")
                time.sleep(wait)
                continue
            if exc.code == 429:
                wait = int(exc.headers.get("Retry-After") or 0) or None
                raise RateLimited(
                    "被限流了（HTTP 429）。把 config.json 里的 pause_seconds 调大，"
                    "或者过一阵再扫。", retry_after=wait,
                ) from exc
            hint = ""
            if exc.code == 403:
                hint = "（这个端点已经不对匿名访问开放了）"
            raise SourceError(f"HTTP {exc.code}{hint}：{url}") from exc
        except urllib.error.URLError as exc:
            raise SourceError(f"{exc.reason}：{url}") from exc


def _strip_html(text):
    """去标签、还原实体、剪掉 RSS 的模板尾巴。

    【用 html.unescape，不要自己列实体表】：Reddit 的 RSS 里有 &#32; 这类数字实体，
    手写的替换表永远漏，漏掉的会原样印在摘要里。

    【尾巴要剪】：Reddit 每条内容后面都跟着 "submitted by /u/x [link] [comments]"，
    每条都一样，占掉摘要里最值钱的位置，而且读起来像乱码。
    """
    import html as html_module

    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html_module.unescape(text)
    text = re.sub(r"\s*submitted by\s*/u/\S+.*$", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\s*\[link\]\s*\[comments\]\s*$", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _stamp(text):
    """ISO 时间转 unix 秒。解析不了就当成现在——宁可显示"刚刚"，不要丢掉一条。"""
    if not text:
        return int(time.time())
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return int(time.time())


# --- Anki 官方论坛（Discourse）-------------------------------------------------


def anki_forum(keyword, user_agent, base="https://forums.ankiweb.net", days=14):
    """用论坛自己的搜索，一个关键词一次请求，返回的结果自带摘要。

    【用 search.json，不逐个翻版块】：版块列表只给标题，要拿正文得一条条再请求；
    搜索接口一次就把标题、摘要、链接都给全了，请求数从几十降到一。

    【查询里就限定时间范围】：不限的话，搜 "AnkiWeb" 会把 2020 年的帖子一起翻出来，
    第一次跑就收进一百多条陈年旧帖——它们早就有人回答过了，回复毫无意义。
    """
    from datetime import date, timedelta

    after = (date.today() - timedelta(days=days)).isoformat()
    query = f"{keyword} after:{after} order:latest"
    url = f"{base}/search.json?{urllib.parse.urlencode({'q': query})}"
    payload = json.loads(_get(url, user_agent, "application/json").decode("utf-8"))
    topics = {t["id"]: t for t in payload.get("topics") or []}
    items = []
    for post in payload.get("posts") or []:
        topic = topics.get(post.get("topic_id")) or {}
        slug = topic.get("slug") or "t"
        items.append(
            {
                "external_id": f"ankiforum:{post.get('id')}",
                "source": "ankiforum",
                "kind": "post" if post.get("post_number") == 1 else "comment",
                "community": "forums.ankiweb.net",
                "title": topic.get("title") or "",
                "body": _strip_html(post.get("blurb")),
                "author": post.get("username") or "",
                "permalink": f"{base}/t/{slug}/{post.get('topic_id')}/{post.get('post_number') or 1}",
                "created_utc": _stamp(post.get("created_at")),
            }
        )
    return items


# --- Reddit 公开 RSS -----------------------------------------------------------


def reddit_rss(subreddit, user_agent, kind="post"):
    """一个版块里最新的帖子或评论，走公开 RSS。

    【只取 new】：要找的是"刚刚有人卡住了"。hot 排的是已经热起来的旧帖，
    等它热起来，提问的人早就自己凑合过去了。
    """
    path = "comments/.rss" if kind == "comment" else "new.rss"
    url = f"https://www.reddit.com/r/{subreddit}/{path}?limit=50"
    root = ET.fromstring(_get(url, user_agent, "application/atom+xml"))
    items = []
    for entry in root.findall(f"{ATOM}entry"):
        ident = (entry.findtext(f"{ATOM}id") or "").strip()
        link = entry.find(f"{ATOM}link")
        author = entry.find(f"{ATOM}author")
        items.append(
            {
                "external_id": f"reddit:{ident}",
                "source": "reddit",
                "kind": kind,
                "community": subreddit,
                "title": (entry.findtext(f"{ATOM}title") or "").strip(),
                "body": _strip_html(entry.findtext(f"{ATOM}content") or ""),
                "author": (author.findtext(f"{ATOM}name") if author is not None else "") or "",
                "permalink": (link.get("href") if link is not None else "") or "",
                "created_utc": _stamp(entry.findtext(f"{ATOM}published")),
            }
        )
    return items


def pause(seconds):
    """两次请求之间歇一会儿。被 429 之后再重试，代价比多等几秒高得多。"""
    time.sleep(max(0, seconds))
