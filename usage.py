# -*- coding: utf-8 -*-
"""额度：今天用掉了多少、还剩多少。

【三家给的东西不一样，所以显示的可信度也不一样】（2026-09-23 实测）：

- Groq 在每个响应头里直接给真实剩余量（x-ratelimit-remaining-requests /
  -tokens 和重置时间）。这是唯一"人家自己说"的数字。
- Gemini 一个额度相关的响应头都没有，只在响应体里给这一次用了多少 token。
  所以只能我们自己累加——请求数准确，剩余多少则不知道，因为免费额度的
  具体上限没有接口可查。
- YouTube 的响应里也没有额度，但它的单价是公开且固定的（搜索 100 单位、
  读一个视频的评论 1 单位，免费额度一天 10000）。所以按次数乘单价自己算，
  结果是准确的估算。

【宁可写"我们自己数的"，也不要让人以为那是官方数字】：额度快用完的时候，
人会根据这个数决定要不要再扫一轮；把估算说成实测，等于让他在错误的基础上
做决定。
"""

import json
import time

YOUTUBE_DAILY_UNITS = 10000     # 免费额度，官方文档写死的
SEARCH_UNITS = 100              # search.list
COMMENTS_UNITS = 1              # commentThreads.list


def _today():
    return time.strftime("%Y-%m-%d")


def _key(provider):
    return f"usage:{_today()}:{provider}"


def bump(store, provider, calls=0, tokens=0, units=0):
    """把这一次的用量加进今天的账上。"""
    if store is None:
        return
    try:
        current = json.loads(store.get_meta(_key(provider)) or "{}")
    except ValueError:
        current = {}
    current["calls"] = int(current.get("calls", 0)) + calls
    current["tokens"] = int(current.get("tokens", 0)) + tokens
    current["units"] = int(current.get("units", 0)) + units
    store.set_meta(_key(provider), json.dumps(current))


def record_limits(store, provider, headers):
    """把 Groq 这类会在响应头里报剩余量的，原样记下来。"""
    if store is None or not headers:
        return
    wanted = {k.lower(): v for k, v in headers.items()
              if k.lower().startswith("x-ratelimit-")}
    if not wanted:
        return
    wanted["at"] = int(time.time())
    store.set_meta(f"limits:{provider}", json.dumps(wanted))


def _get(store, provider):
    try:
        return json.loads(store.get_meta(_key(provider)) or "{}")
    except ValueError:
        return {}


def _limits(store, provider):
    try:
        return json.loads(store.get_meta(f"limits:{provider}") or "{}")
    except ValueError:
        return {}


def provider_name(cfg):
    """配置里那一段该叫什么名字。"""
    if cfg.get("provider") == "gemini":
        return "gemini"
    base = (cfg.get("base_url") or "").lower()
    if "groq" in base:
        return "groq"
    if "deepseek" in base:
        return "deepseek"
    if "openai" in base:
        return "openai"
    return cfg.get("provider") or "ai"


LABELS = {"gemini": "Gemini", "groq": "Groq", "deepseek": "DeepSeek",
          "openai": "OpenAI", "youtube": "YouTube"}


def _bar(used, total, label, note=""):
    """一根条子该知道的全部：用了多少、满格多少、几成、什么颜色。

    【阈值和 dashboard 一致】：<80 绿、80–90 橙、>=90 红。红是最后一档，
    90 以上一律红——再往上没有更强的颜色可用了。
    """
    total = max(1, int(total or 1))
    used = max(0, int(used or 0))
    percent = min(100, round(used * 100 / total))
    level = "ok" if percent < 80 else ("warn" if percent < 90 else "high")
    return {"label": label, "used": used, "total": total,
            "percent": percent, "level": level, "note": note}


def summary(store, config):
    """给页面用：每家一块，每块一到两根条子。

    【条子必须有分母，而三家的分母来路不同】：Groq 的是它自己在响应头里给的；
    YouTube 的是官方文档写死的 10000；Gemini 两样都没有——免费额度的上限没有
    接口可查，所以用配置里那个"每天大约用多少次"当分母，标签上写明这是预算，
    不是人家的上限。把自己设的预算说成官方额度，会让人在错的基础上决定
    "还能不能再扫一轮"。
    """
    rows = []
    ai_cfg = config.get("ai", {})
    budget = int(ai_cfg.get("daily_request_budget") or 1000)
    for cfg, role in ((ai_cfg, "主用"), (ai_cfg.get("fallback") or {}, "备用")):
        if not cfg.get("api_key"):
            continue
        provider = provider_name(cfg)
        used = _get(store, provider)
        limits = _limits(store, provider)
        bars = []

        remaining = limits.get("x-ratelimit-remaining-requests")
        if remaining is not None:
            # 【Groq：条子画的是"这一分钟的窗口"，不是今天】：它的限额按分钟
            # 滚动，闲一会儿就自己满了。写清楚，免得看见满格以为今天没得用了。
            limit = int(limits.get("x-ratelimit-limit-requests") or 1)
            bars.append(_bar(limit - int(remaining), limit, "请求",
                             f"剩 {remaining}/{limit}，"
                             f"{limits.get('x-ratelimit-reset-requests', '')} 后回满"))
            tok_left = limits.get("x-ratelimit-remaining-tokens")
            if tok_left is not None:
                tok_limit = int(limits.get("x-ratelimit-limit-tokens") or 1)
                bars.append(_bar(tok_limit - int(tok_left), tok_limit, "token",
                                 f"剩 {tok_left}/{tok_limit}，"
                                 f"{limits.get('x-ratelimit-reset-tokens', '')} 后回满"))
        else:
            bars.append(_bar(used.get("calls", 0), budget, "请求",
                             f"今天 {used.get('calls', 0)}/{budget} 次（预算，不是官方上限）"))

        # 【这家管哪些活，用标签列出来】（2026-09-23 运营者定）：两家的分工不是
        # 对称的——主用管每天都要跑的筛选和翻译，备用除了顶班，还独占"写要点"和
        # "翻译回复"这两件点一次跑一次的活。标签比一句话好认：一家限流了，扫一眼
        # 就知道接下来哪个按钮会失灵。
        rows.append({
            "name": f"{LABELS.get(provider, provider)}（{role}·{cfg.get('model', '')}）",
            "tags": (["筛帖子", "翻译评论"] if role == "主用"
                     else ["写要点", "翻译回复", "备用AI"]),
            "bars": bars})

    tube = config.get("youtube", {})
    if tube.get("enabled") and tube.get("api_key"):
        used = _get(store, "youtube")
        rows.append({
            "name": "YouTube Data API",
            "tags": ["搜视频", "读评论"],
            "bars": [_bar(used.get("units", 0), YOUTUBE_DAILY_UNITS, "配额",
                          f"今天 {used.get('units', 0)}/{YOUTUBE_DAILY_UNITS} 单位，"
                          f"太平洋时间零点重置")],
        })
    return rows
