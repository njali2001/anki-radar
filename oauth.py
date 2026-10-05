# -*- coding: utf-8 -*-
"""一次性：拿到 YouTube Analytics 的 refresh token。

    runtime\\python.exe oauth.py

【为什么要这一步】。Data API v3（观看/点赞/评论数）只要一把 API key，因为那些
数字是公开的。而观看时长、流量来源、搜索词、地理分布属于**频道主才看得到的
数据**，所以 Google 要你本人授权一次，换一个长期可用的 refresh token。

===========================================================================
【最容易白做一遍的地方：同意屏幕必须发布成 In production】
===========================================================================

发布状态留在默认的 **Testing**，签发的 refresh token **7 天就过期**
（Google 官方文档原话：publishing status 为 "Testing" 的项目拿到的
refresh token expiring in 7 days）。

一周后监控会安静地停掉——不会报错，只是数字不再更新。所以在 Google Cloud
控制台里要把 OAuth 同意屏幕从 Testing 改成 **In production**。
个人自用的话，授权时会弹一个"Google 尚未验证此应用"的警告，
点「高级」→「继续前往」就行，这是未验证应用的正常表现。

===========================================================================
【用回环地址，不用已废弃的 OOB】
===========================================================================

Google 2022 年起停掉了"把 code 显示在页面上让你复制"那种 out-of-band 流程。
现在桌面应用的做法是：本地起一个只活几十秒的 HTTP 服务，让浏览器把 code
回调到 http://127.0.0.1:<port>/ 上。所以创建凭据时类型要选
**桌面应用（Desktop app）**——那种类型才允许回环地址，而且不要求你预先
登记端口号。

【只读，而且只要这一个 scope】：yt-analytics.readonly。
带 monetary 的那个是收入数据，这里不需要，要了只会让授权页更吓人。
"""

import http.server
import io
import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"

CONFIG_PATH = "config.json"


def _load():
    with io.open(CONFIG_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _save(cfg):
    # 【先写临时文件再改名】：写到一半断电的话，直接覆盖会留下一个半截的
    # config.json，而那会让整个程序起不来。
    tmp = CONFIG_PATH + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    os.replace(tmp, CONFIG_PATH)


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _Catcher(http.server.BaseHTTPRequestHandler):
    """只负责接住那一次回调，然后让浏览器显示一句话。"""

    code = None
    error = None

    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Catcher.code = (query.get("code") or [None])[0]
        _Catcher.error = (query.get("error") or [None])[0]
        body = ("<meta charset=utf-8><body style='font:16px system-ui;padding:40px'>"
                "<p>好了，可以关掉这个页面，回到命令行。</p></body>")
        if _Catcher.error:
            body = ("<meta charset=utf-8><body style='font:16px system-ui;padding:40px'>"
                    f"<p>授权被拒绝：{_Catcher.error}</p></body>")
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, *args):
        pass  # 不要往命令行里吐 HTTP 日志，人在看提示


def _exchange(params):
    data = urllib.parse.urlencode(params).encode("utf-8")
    request = urllib.request.Request(TOKEN_URL, data=data)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"换取 token 失败（HTTP {exc.code}）：{body[:400]}")


def main():
    cfg = _load()
    section = cfg.setdefault("video", {})
    oauth = section.setdefault("oauth", {})
    client_id = (oauth.get("client_id") or "").strip()
    client_secret = (oauth.get("client_secret") or "").strip()

    if not client_id or not client_secret:
        print("config.json 里 video.oauth.client_id / client_secret 还没填。")
        print("先到 Google Cloud 控制台创建【桌面应用】类型的 OAuth 凭据，")
        print("把那两个值填进去，再跑这个脚本。详细步骤见这个文件顶上的注释。")
        return 1

    port = _free_port()
    redirect = f"http://127.0.0.1:{port}/"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPE,
        # 【这两个一个都不能少】：没有 access_type=offline 就只给一个一小时
        # 后失效的 access token，根本没有 refresh token；而已经授权过的账号
        # 再授权时，Google 默认【不再重发】refresh token——prompt=consent
        # 强制它重发。少了任一个，这里都会拿到一个没有 refresh_token 的响应。
        "access_type": "offline",
        "prompt": "consent",
    })

    server = http.server.HTTPServer(("127.0.0.1", port), _Catcher)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print("浏览器要打开一个 Google 授权页，用【拥有这个 YouTube 频道的账号】登录。")
    print("如果弹出「Google 尚未验证此应用」，点「高级」→「继续前往」——")
    print("那是未验证应用的正常表现，这个应用就是你自己的。")
    print()
    print("没自动打开的话，手动复制这个地址：")
    print(url)
    print()
    try:
        webbrowser.open(url)
    except Exception:
        pass

    server.serve_forever()  # 回调进来之后 _Catcher 会把它关掉
    server.server_close()

    if _Catcher.error or not _Catcher.code:
        print(f"没拿到授权码（{_Catcher.error or '窗口被关掉了？'}）")
        return 1

    token = _exchange({
        "code": _Catcher.code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
    })

    refresh = token.get("refresh_token")
    if not refresh:
        print("Google 没给 refresh_token。")
        print("最常见的原因是这个账号之前已经授权过这个应用——")
        print("去 https://myaccount.google.com/permissions 把它删掉，再跑一次。")
        return 1

    oauth["refresh_token"] = refresh
    _save(cfg)
    print("拿到 refresh token 了，已经写进 config.json 的 video.oauth。")
    print()
    print("【提醒】如果同意屏幕的发布状态还是 Testing，这个 token 7 天后会失效。")
    print("去 Google Cloud 控制台 → OAuth 同意屏幕 → 改成 In production。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
