# -*- coding: utf-8 -*-
"""YouTube Analytics：只有频道主看得到的那一档。

和 video.py 的分工：

  video.py      Data API v3，公开数字（观看/点赞/评论），一把 API key 就够，
                接近实时。
  analytics.py  Analytics API，频道主数据（观看时长、流量来源、搜索词、
                地理），要 OAuth 授权，**有 24-48 小时延迟**。

【两边的观看数对不上是正常的】。Data API 给的是接近实时的累计数，Analytics
这边要等一两天才补齐。页面上把两个数并排显示过，人会以为哪边错了——所以
分开两块写，各自标明口径。

===========================================================================
【最值钱的是搜索词，不是观看数】
===========================================================================

观看数在哪儿都看得到。而 insightTrafficSourceDetail 在筛掉只剩 YT_SEARCH
之后，给的是【观众搜了什么词找到这条片子】——那直接回答两个问题：
官网的 SEO 该压哪些词，下一条片子该拍什么。

===========================================================================
【access token 在内存里缓存，refresh token 不动】
===========================================================================

access token 一小时过期，refresh token 长期有效（前提是同意屏幕发布成
In production——Testing 状态下 7 天就作废，见 oauth.py 顶上那段）。

所以这里每次取数前看一眼缓存里的 access token 还有没有效，没效才去换一个。
【不要每次都换】：换得太勤会撞上 Google 对同一个 refresh token 的频率限制，
而且那种限制返回的是 400 invalid_grant——看着像"授权失效了"，
会让人白白重做一遍授权。
"""

import datetime
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://oauth2.googleapis.com/token"
REPORTS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
TIMEOUT = 30

# 取最近多少天。30 天是个平衡：短了看不出趋势，长了会把早期的冷启动
# 混进来，让"现在靠什么来人"失真。
WINDOW_DAYS = 30
TOP_N = 10

_token = {"value": "", "expires_at": 0}
_lock = threading.Lock()


class AnalyticsError(Exception):
    """取数失败。调用方负责显示，不要让它冒到页面渲染上去。"""


def configured(config):
    o = config.get("video", {}).get("oauth", {})
    return bool((o.get("client_id") or "").strip()
                and (o.get("client_secret") or "").strip()
                and (o.get("refresh_token") or "").strip())


def _access_token(config):
    with _lock:
        if _token["value"] and time.time() < _token["expires_at"] - 60:
            return _token["value"]
        o = config["video"]["oauth"]
        data = urllib.parse.urlencode({
            "client_id": o["client_id"],
            "client_secret": o["client_secret"],
            "refresh_token": o["refresh_token"],
            "grant_type": "refresh_token",
        }).encode("utf-8")
        try:
            request = urllib.request.Request(TOKEN_URL, data=data)
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if "invalid_grant" in body:
                # 【说清楚最可能的原因】：光报 invalid_grant 的话，人会去重做
                # 整套 Google Cloud 配置，而十有八九只是同意屏幕还停在 Testing。
                raise AnalyticsError(
                    "授权失效了（invalid_grant）。最常见的原因是 OAuth 同意屏幕"
                    "还停在 Testing——那种状态下 refresh token 只有 7 天。"
                    "去 Google Cloud 控制台改成 In production，再跑一次 oauth.py。")
            raise AnalyticsError(f"换 access token 失败 HTTP {exc.code}：{body[:200]}")
        except urllib.error.URLError as exc:
            raise AnalyticsError(f"换 access token 失败：{exc.reason}")
        _token["value"] = payload.get("access_token", "")
        _token["expires_at"] = time.time() + int(payload.get("expires_in") or 3600)
        return _token["value"]


def _report(config, params):
    token = _access_token(config)
    url = REPORTS_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        if exc.code == 403:
            raise AnalyticsError(
                f"没有权限读这份报表（403）。是不是 YouTube Analytics API 没启用，"
                f"或者授权用的不是拥有这个频道的账号？{body[:160]}")
        raise AnalyticsError(f"Analytics HTTP {exc.code}：{body[:200]}")
    except urllib.error.URLError as exc:
        raise AnalyticsError(f"Analytics 取数失败：{exc.reason}")


def _window():
    end = datetime.date.today()
    return (end - datetime.timedelta(days=WINDOW_DAYS)).isoformat(), end.isoformat()


def fetch(store, config, video_id):
    """取一条片子的频道主数据，写进库。四次请求。"""
    start, end = _window()
    base = {"ids": "channel==MINE", "startDate": start, "endDate": end}
    now = int(time.time())

    # 1) 汇总指标
    data = _report(config, dict(
        base, metrics="views,estimatedMinutesWatched,averageViewDuration,"
                      "averageViewPercentage",
        filters=f"video=={video_id}"))
    rows = data.get("rows") or [[0, 0, 0, 0]]
    store.save_video_analytics(
        video_id=video_id, window_days=WINDOW_DAYS,
        views=int(rows[0][0] or 0),
        minutes=int(rows[0][1] or 0),
        avg_duration=int(rows[0][2] or 0),
        avg_percentage=float(rows[0][3] or 0.0),
        fetched_at=now)

    # 2) 三张分解表。【整批替换，不累加】：它们是"最近 30 天的排行"，
    #    累加的话窗口会越滑越长，而那不是我们要看的东西。
    breakdowns = []

    data = _report(config, dict(
        base, dimensions="insightTrafficSourceType", metrics="views",
        filters=f"video=={video_id}", sort="-views", maxResults=TOP_N))
    for row in data.get("rows") or []:
        breakdowns.append(("traffic", str(row[0]), int(row[1] or 0)))

    # 【搜索词要先筛到 YT_SEARCH】：不筛的话 insightTrafficSourceDetail 返回的
    # 是各种来源的明细混在一起（推荐它的那条视频、外链的那个域名……），
    # 看着像搜索词其实不是。
    data = _report(config, dict(
        base, dimensions="insightTrafficSourceDetail", metrics="views",
        filters=f"video=={video_id};insightTrafficSourceType==YT_SEARCH",
        sort="-views", maxResults=TOP_N))
    for row in data.get("rows") or []:
        breakdowns.append(("search", str(row[0]), int(row[1] or 0)))

    data = _report(config, dict(
        base, dimensions="country", metrics="views",
        filters=f"video=={video_id}", sort="-views", maxResults=TOP_N))
    for row in data.get("rows") or []:
        breakdowns.append(("country", str(row[0]), int(row[1] or 0)))

    store.save_video_breakdown(video_id, breakdowns, now)
    return True


def refresh_now(store, config):
    """设置页那个按钮用的。不受间隔限制，理由同 video.refresh_now。"""
    if not configured(config):
        return {"ok": False, "error": "还没配 OAuth（见 oauth.py）"}
    import video

    done, failed = 0, []
    for t in video.targets(config):
        try:
            fetch(store, config, t["id"])
            done += 1
        except AnalyticsError as exc:
            failed.append(f'{t["label"]}：{exc}')
    if not done and failed:
        return {"ok": False, "error": failed[0]}
    return {"ok": True, "done": done, "failed": failed}


# 流量来源的代号换成人话。YouTube 返回的是 YT_SEARCH 这种常量。
TRAFFIC_NAMES = {
    "YT_SEARCH": "YouTube 搜索",
    "RELATED_VIDEO": "其他视频的推荐位",
    "SUBSCRIBER": "订阅源 / 首页",
    "EXT_URL": "站外链接",
    "NO_LINK_OTHER": "直接打开",
    "NO_LINK_EMBEDDED": "嵌在别人页面里",
    "YT_CHANNEL": "频道页",
    "PLAYLIST": "播放列表",
    "NOTIFICATION": "通知",
    "YT_OTHER_PAGE": "YouTube 其他页面",
    "ADVERTISING": "广告",
    "PROMOTED": "推广",
    "SHORTS": "Shorts 信息流",
    "HASHTAGS": "话题标签",
    "END_SCREEN": "片尾画面",
    "ANNOTATION": "卡片 / 注释",
}


def status(store, config, video_id):
    """给设置页用。没配 OAuth 就返回 None，调用方据此决定显不显示这一块。"""
    if not configured(config):
        return None
    row = store.get_video_analytics(video_id)
    if not row:
        return {"empty": True}
    out = dict(row)
    out["empty"] = False
    out["window_days"] = row["window_days"]
    groups = {}
    for item in store.video_breakdown(video_id):
        groups.setdefault(item["kind"], []).append(item)
    out["traffic"] = [
        {"label": TRAFFIC_NAMES.get(i["label"], i["label"]), "views": i["views"]}
        for i in groups.get("traffic", [])]
    out["search"] = groups.get("search", [])
    out["country"] = groups.get("country", [])
    return out
