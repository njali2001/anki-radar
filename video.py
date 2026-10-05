# -*- coding: utf-8 -*-
"""我们自己那条 YouTube 片子涨得怎么样。

【这和雷达其余部分不是一件事】。雷达找的是**别人的帖子**，为了去回；
这里看的是**我们自己的资产**。两者的节奏差得很远——前者一天看几次，
后者一周看一次就够。所以它和 watch 一样长在【设置页】上，不占首页：
首页是"今天有谁要回"。

【为什么不挂在 watch 上】。watch 盯的是"本不该变的事实"（AnkiWeb 的
250 MB 上限），一年里 364 天答案是"没变"，所以它一变就值得喊一声。
而播放量天天变，用那套逻辑会天天报"变了"——正好踩中 watch.py 自己写着的
那句"天天喊一次，第 365 天真变那次就没人看了"。

播放量要看的是【趋势】，不是"变没变"，所以这里存的是一天一行的时间序列，
页面上给出最近若干天的增量。

===========================================================================
【拿得到什么，拿不到什么】（2026-10-05 核实官方文档）
===========================================================================

这里用的是 **Data API v3 的 videos.list**，也就是任何人都看得到的公开数字：
观看数、点赞数、评论数。一次 1 个配额单位（搜索要 100），和雷达现有的
那把 key 共用。

**观看时长、流量来源、搜索词、地理分布拿不到**——那些属于 YouTube
Analytics API，要走 OAuth 拿频道主授权，是另一件事。

**字幕语言的使用量拿不到**，Analytics 里根本没有这个维度。做了七种语言的
字幕，没有办法知道哪一种被用了多少。最接近的代理指标是观众所在国家，
而那要第二档授权才有。

===========================================================================

【日增量是算出来的，不是人家给的】。YouTube 给的是累计总数，而且更新有延迟、
偶尔还会因为刷量清理而**往回掉**。所以两天之间的差值是估算：差值为负时按 0
显示，但原始累计数照样存着——存原始值才有可能事后重算，存算好的差值就没了。
"""

import time

import sources
import usage

# videos.list 读一条片子的统计：官方定价 1 个单位。
STATS_UNITS = 1


def _today(now=None):
    return time.strftime("%Y-%m-%d", time.localtime(now or time.time()))


def targets(config):
    """要盯哪几条片子。配置里没写就是不开。"""
    cfg = config.get("video", {})
    if not cfg.get("enabled", False):
        return []
    out = []
    for item in cfg.get("videos", []):
        vid = (item.get("id") or "").strip()
        if vid:
            out.append({"id": vid, "label": (item.get("label") or vid).strip()})
    return out


def _interval(config):
    minutes = config.get("video", {}).get("min_interval_minutes", 720)
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        minutes = 720
    return max(60, minutes) * 60


def is_stale(store, config):
    items = targets(config)
    if not items:
        return False
    if not config.get("youtube", {}).get("api_key"):
        return False
    oldest = min(store.video_checked_at(t["id"]) for t in items)
    return time.time() - oldest > _interval(config)


def fetch_one(store, video_id, key, now=None):
    """取一条片子的当前数字，按天写进表里。

    【一天一行，重复取就覆盖】。一天之内刷新很多次不该长出很多行——
    那样画出来的"趋势"会取决于你那天刷了几次页面。
    """
    now = now or time.time()
    data = sources.youtube_video_stats(video_id, key)
    usage.bump(store, "youtube", calls=1, units=STATS_UNITS)
    store.save_video_stats(
        video_id=video_id,
        day=_today(now),
        title=data.get("title", ""),
        views=data.get("views", 0),
        likes=data.get("likes", 0),
        comments=data.get("comments", 0),
        fetched_at=int(now),
    )
    return data


def refresh(store, config):
    """把所有盯着的片子取一遍。调用方负责不要在渲染线程里同步等网络。"""
    key = config.get("youtube", {}).get("api_key", "")
    if not key:
        return 0
    done = 0
    for t in targets(config):
        try:
            fetch_one(store, t["id"], key)
            done += 1
        except sources.RateLimited:
            # 【配额用完就整轮停下】。接着取下一条只会再撞一次同样的 403，
            # 白白把剩下的额度也耗在错误上。
            break
        except sources.SourceError:
            # 取不到就跳过这一条：别的片子还能取，而且下次刷新会再试。
            continue
    return done


def refresh_now(store, config):
    """【无视间隔，立刻取一次】。设置页上那个按钮用的就是这个。

    和 refresh_if_stale 分开写，是因为这两件事的默认值该相反：
    自动那条默认【不取】（省配额、省请求），手动这条默认【取】——
    人点下去就是要现在的数字，这时候跟他说"还没到 12 小时"没有意义。
    一次 1 个配额单位，一天免费 10000，点得再勤也花不完。
    """
    key = config.get("youtube", {}).get("api_key", "")
    if not key:
        return {"ok": False, "error": "还没填 YouTube API key"}
    items = targets(config)
    if not items:
        return {"ok": False, "error": "没有配置要盯的片子"}
    done, failed = 0, []
    for t in items:
        try:
            fetch_one(store, t["id"], key)
            done += 1
        except sources.RateLimited as exc:
            return {"ok": False, "error": str(exc), "done": done}
        except sources.SourceError as exc:
            failed.append(f'{t["label"]}：{exc}')
    return {"ok": True, "done": done, "failed": failed}


def status(store, config):
    """给设置页用的一行行数据。"""
    out = []
    for t in targets(config):
        series = store.video_series(t["id"], limit=31)
        if not series:
            out.append({"label": t["label"], "id": t["id"], "empty": True})
            continue
        latest = series[-1]
        # 【日增量按相邻两天的差算】，而累计数偶尔会往回掉（刷量清理），
        # 所以负数按 0 看——但存着的原始累计数没动。
        deltas = []
        for a, b in zip(series, series[1:]):
            deltas.append({
                "day": b["day"],
                "views": max(0, b["views"] - a["views"]),
                "likes": max(0, b["likes"] - a["likes"]),
                "comments": max(0, b["comments"] - a["comments"]),
            })
        week = deltas[-7:]
        out.append({
            "label": t["label"],
            "id": t["id"],
            "empty": False,
            "title": latest.get("title", ""),
            "views": latest["views"],
            "likes": latest["likes"],
            "comments": latest["comments"],
            "checked_at": latest["fetched_at"],
            "days": len(series),
            "week_views": sum(d["views"] for d in week),
            "week_days": len(week),
            "deltas": deltas[-14:],
        })
    return out
