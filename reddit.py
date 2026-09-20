# -*- coding: utf-8 -*-
"""Reddit 只读客户端。只用标准库，不装任何依赖。

【用官方 API，不登录、不抓网页】：抓取违反服务条款，而且被识别出来封掉的是
你平时在用的那个账号。官方 API 的只读凭据（client_credentials）不需要账号密码,
拿到的令牌**没有发帖权限**——这是"这个工具不会替你发帖"这句话的物理保证。

【User-Agent 要能认出是谁】：Reddit 明确要求带应用名和联系方式，含糊的 UA 会
被限流。出问题时他们也照这个找人。
"""

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_ROOT = "https://oauth.reddit.com"
TIMEOUT = 30


class RedditError(RuntimeError):
    pass


def get_token(client_id, client_secret, user_agent):
    if not (client_id and client_secret):
        raise RedditError("config.json 里还没填 reddit.client_id / client_secret")

    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    request = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    request.add_header("User-Agent", user_agent)
    pair = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    request.add_header("Authorization", f"Basic {pair}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        hint = "（401 通常是 client_id / secret 填错了）" if exc.code == 401 else ""
        raise RedditError(f"取令牌失败：HTTP {exc.code}{hint}") from exc
    except urllib.error.URLError as exc:
        raise RedditError(f"取令牌失败：{exc.reason}") from exc

    token = payload.get("access_token")
    if not token:
        raise RedditError("Reddit 没有返回 access_token")
    return token


def fetch(subreddit, token, user_agent, kind="post", limit=100):
    """一个版块里最新的帖子或评论。

    【只取 new，不取 hot】：要找的是"刚刚有人卡住了"。hot 排的是已经热起来的旧帖,
    等它热起来，提问的人早就自己凑合过去了。
    """
    path = f"/r/{subreddit}/{'comments' if kind == 'comment' else 'new'}"
    url = f"{API_ROOT}{path}?{urllib.parse.urlencode({'limit': min(limit, 100)})}"
    request = urllib.request.Request(url)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("User-Agent", user_agent)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RedditError(f"读 r/{subreddit} 的{kind}失败：HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RedditError(f"读 r/{subreddit} 的{kind}失败：{exc.reason}") from exc

    items = []
    for child in (payload.get("data") or {}).get("children") or []:
        data = child.get("data") or {}
        name = data.get("name") or data.get("id")
        if not name:
            continue
        items.append(
            {
                "external_id": f"reddit:{name}",
                "kind": kind,
                "community": subreddit,
                "title": data.get("title") or "",
                "body": data.get("selftext") or data.get("body") or "",
                "author": data.get("author") or "",
                "permalink": f"https://www.reddit.com{data.get('permalink', '')}",
                "created_utc": data.get("created_utc") or 0,
            }
        )

    # 【两次请求之间歇一秒】：Reddit 限的是每分钟请求数，而一轮要扫十几个版块。
    # 歇一秒没有任何成本，撞上限流要等很久。
    time.sleep(1)
    return items
