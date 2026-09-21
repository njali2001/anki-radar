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

import hashlib
import http.cookiejar
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


def reddit_search(query, label, user_agent, window="week"):
    """全站搜索，而不是只盯着几个订阅的版块。

    【为什么要加这个】：卡在同步上的人不一定在 r/Anki 发问——他可能在 r/MCAT2、
    r/BeginnerKorean、或者任何一个我们没列进订阅表的地方（2026-09-20 实测，一周
    内的命中就跨了七八个版块）。订阅表再长也总有漏的，而搜索一个请求覆盖全站。

    【搜出来的不再过关键词】：查询语句本身就是筛子，命中的理由就是这条查询。
    再拿关键词表卡一遍的话，"Help - Cannot boot up Anki"这种会被直接扔掉——
    可那正是要看的人。剩下的相关性判断交给 AI。
    """
    url = ("https://www.reddit.com/search.rss?q=" + urllib.parse.quote(query)
           + f"&sort=new&t={window}")
    root = ET.fromstring(_get(url, user_agent, "application/atom+xml"))
    items = []
    for entry in root.findall(f"{ATOM}entry"):
        ident = (entry.findtext(f"{ATOM}id") or "").strip()
        # 【搜索结果里混着"版块"本身】：搜 anki 会连 r/Anki、r/AnkiMCAT 这些版块
        # 一起返回，它们没有正文、也没人在那儿求助，是纯噪音（2026-09-20 实测，
        # 25 条新收里有 4 条是这种）。帖子的 id 是 t3_ 开头，只留这些。
        if "t3_" not in ident:
            continue
        link = entry.find(f"{ATOM}link")
        author = entry.find(f"{ATOM}author")
        category = entry.find(f"{ATOM}category")
        # 标签给的是 "r/Anki"，而 community 字段存的是光秃秃的版块名，
        # 页面上再自己加 r/ 前缀——不剥掉就会显示成 r/r/Anki。
        community = (category.get("label") if category is not None else "") or "reddit"
        items.append(
            {
                "external_id": f"reddit:{ident}",
                "source": "reddit",
                "kind": "post",
                "community": community[2:] if community.startswith("r/") else community,
                "title": (entry.findtext(f"{ATOM}title") or "").strip(),
                "body": _strip_html(entry.findtext(f"{ATOM}content") or ""),
                "author": (author.findtext(f"{ATOM}name") if author is not None else "") or "",
                "permalink": (link.get("href") if link is not None else "") or "",
                "created_utc": _stamp(entry.findtext(f"{ATOM}published")),
                "matched_hint": [label],
            }
        )
    return items


def pause(seconds):
    """两次请求之间歇一会儿。被 429 之后再重试，代价比多等几秒高得多。"""
    time.sleep(max(0, seconds))


# --- B站 ---------------------------------------------------------------------
#
# 【为什么是 B站，不是贴吧】（2026-09-20 实测）：贴吧连首页都 403——百度把机房 IP
# 和脚本特征直接挡掉，从美国和大陆服务器试都一样，带 cookie 也没用。B站 的搜索和
# 评论接口不需要登录、不需要签名，从境内境外都能读。
#
# 【真正的抱怨在评论区，不在视频里】：遇到同步问题的人很少专门发视频，但会在
# 「Anki 怎么同步」这类教程下面留一句"传半天传不上"。所以搜到视频只是第一步，
# 值钱的是它下面的评论。

BILI_API = "https://api.bilibili.com"
BILI_HEADERS = {
    # 【必须带浏览器 UA 和 Referer】：少了任何一个，接口会返回 -403。
    # 光有这两个还不够，见下面 _bili_session 的 cookie 握手。
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}


# 【进门要先擦鞋】：B站 的接口对"一上来就直接调 API、身上一块 cookie 都没有"
# 的请求返回 412。浏览器不会这样——它先打开首页，拿到一个匿名的设备标识
# （buvid3），之后每次请求都带着。这里照做一次：开一个带 cookie 罐的 opener，
# 进程里第一次用到 B站 时访问一次首页，后面所有请求复用同一罐 cookie。
# 不登录、不带账号，拿到的仍然只是公开数据。
_bili_opener = None


def _bili_session():
    global _bili_opener
    if _bili_opener is not None:
        return _bili_opener
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = list(BILI_HEADERS.items())
    try:
        with opener.open("https://www.bilibili.com/", timeout=TIMEOUT) as response:
            response.read(1024)     # 要的是响应头里的 Set-Cookie，正文不关心
    except Exception:
        # 拿不到 cookie 也继续——后面的请求自己会报 412，错误信息更具体。
        pass
    _bili_opener = opener
    return opener


def _bili(url):
    opener = _bili_session()
    try:
        with opener.open(url, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 412:
            # B站 用 412 表示"你看起来像爬虫"，继续请求只会被盯得更紧。
            raise RateLimited("B站 判定为异常请求（HTTP 412），本轮停止") from exc
        raise SourceError(f"HTTP {exc.code}：{url}") from exc
    except urllib.error.URLError as exc:
        raise SourceError(f"{exc.reason}：{url}") from exc

    code = payload.get("code")
    if code == -412:
        raise RateLimited("B站 判定为异常请求（code -412），本轮停止")
    if code != 0:
        raise SourceError(f"B站 返回 code={code}：{payload.get('message')}")
    return payload.get("data") or {}


# 【评论接口要签名】：老的 x/v2/reply 现在一律返回 0 条评论——不报错，就是空的
# （2026-09-20 实测）。网页端走的是 x/v2/reply/wbi/main，每个请求都要按 B站 的
# "wbi"规则算一个 w_rid 参数：从 nav 接口取两段 key 拼起来，按一张固定的顺序表
# 重排前 32 位，再和排好序的查询串一起做 md5。算法是公开的，不需要账号——
# nav 对未登录返回 code=-101，但我们要的 wbi_img 照样在 data 里。
WBI_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43,
           5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16,
           24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59,
           6, 63, 57, 62, 11, 36, 20, 34, 44, 52]
_wbi_key = None


def _bili_wbi_key():
    """那两段 key 每天换一次，一个进程里取一次就够。"""
    global _wbi_key
    if _wbi_key:
        return _wbi_key
    opener = _bili_session()
    try:
        with opener.open(f"{BILI_API}/x/web-interface/nav", timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError) as exc:
        raise SourceError(f"取不到 B站 的 wbi key：{exc}") from exc
    images = (payload.get("data") or {}).get("wbi_img") or {}
    if not images.get("img_url"):
        raise SourceError("B站 的 nav 接口里没有 wbi key")
    raw = "".join(
        images[name].rsplit("/", 1)[-1].split(".")[0] for name in ("img_url", "sub_url")
    )
    _wbi_key = "".join(raw[i] for i in WBI_TAB)[:32]
    return _wbi_key


def _bili_signed(path, params):
    params = dict(params, wts=int(time.time()))
    query = urllib.parse.urlencode(sorted(params.items()))
    params["w_rid"] = hashlib.md5((query + _bili_wbi_key()).encode("utf-8")).hexdigest()
    return _bili(f"{BILI_API}{path}?{urllib.parse.urlencode(sorted(params.items()))}")


def bilibili_videos(keyword, limit=20):
    """搜视频。返回的条目里 aid 用来接着抓评论。"""
    query = urllib.parse.urlencode({"search_type": "video", "keyword": keyword, "page": 1})
    data = _bili(f"{BILI_API}/x/web-interface/search/type?{query}")
    items = []
    for video in (data.get("result") or [])[:limit]:
        bvid = video.get("bvid") or ""
        items.append(
            {
                "external_id": f"bili:{bvid}",
                "source": "bilibili",
                "kind": "post",
                "community": "bilibili",
                "title": _strip_html(video.get("title")),
                "body": _strip_html(video.get("description")),
                "author": video.get("author") or "",
                "permalink": f"https://www.bilibili.com/video/{bvid}",
                "created_utc": int(video.get("pubdate") or 0),
                "_aid": video.get("aid"),
                "_bvid": bvid,
            }
        )
    return items


def bilibili_comments(aid, bvid, limit=20):
    """一个视频下的评论。

    【按时间排，不按热度】：热度排出来的是三年前那条点赞最多的，而我们要找的是
    "最近有人卡在这儿"。mode=2 是时间倒序，mode=3 是热度。
    """
    data = _bili_signed("/x/v2/reply/wbi/main",
                        {"oid": aid, "type": 1, "mode": 2, "plat": 1, "web_location": 1315875})
    items = []
    for reply in (data.get("replies") or [])[:limit]:
        content = (reply.get("content") or {}).get("message") or ""
        items.append(
            {
                "external_id": f"bili:{reply.get('rpid')}",
                "source": "bilibili",
                "kind": "comment",
                "community": "bilibili",
                "title": "",
                "body": _strip_html(content),
                "author": (reply.get("member") or {}).get("uname") or "",
                # 【链接到视频，不到评论】：B站 的评论没有稳定的直达链接，
                # 给一个打不开的锚点比不给更糟。
                "permalink": f"https://www.bilibili.com/video/{bvid}#reply{reply.get('rpid')}",
                "created_utc": int(reply.get("ctime") or 0),
            }
        )
    return items
