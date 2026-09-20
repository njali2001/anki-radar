# -*- coding: utf-8 -*-
"""AI 相关性打分：把关键词筛出来的几十条，压成真正值得回的那几条。

【为什么关键词不够】：`AnkiWeb` 这种词分不出「我传不上去了」和「我做了个插件，
顺口提到 AnkiWeb」。实测一轮 55 条里 54 条是它带来的，而其中一半和我们无关。

【两段式：先关键词后模型】。关键词那一步不花钱，把几百条压到几十条；模型只看
这几十条，一天的花费是几分钱。反过来的话，每天要为几百条无关内容付费。

【没有 key 就整段跳过】：这个模块是可选的，工具在没有它的时候照常能用，
只是报告里混的噪音多一点。**不要让它变成必需品**——一个每天都可能因为额度、
网络、模型改名而挂掉的东西，不该挡在"今天有谁需要帮忙"前面。

【模型是配置项】：换供应商只改 config.json。目前实现了 Gemini（免费额度够用）
和任何兼容 OpenAI 接口的服务（DeepSeek 也走这条，给大陆那边用）。
"""

import json
import urllib.error
import urllib.request

TIMEOUT = 60

PROMPT = """You are triaging forum posts for someone who runs a paid Anki sync
hosting service. He answers posts personally; he does not spam.

For each item decide how likely it is that THIS person is currently stuck on
something his service could genuinely help with:

  3 = clearly stuck on syncing or collection/media size right now
      (sync fails, sync is very slow, collection too large for AnkiWeb,
       looking for a self-hosted or third-party sync server)
  2 = related problem, might be helped (slow media, sync errors, huge decks)
  1 = mentions syncing but is not stuck (feature talk, add-on announcement)
  0 = unrelated to syncing or size

Answer with JSON only: a list of objects {"id": <id>, "score": <0-3>,
"reason": "<at most 12 words, in Chinese>"}. No prose, no code fences.

Items:
"""


class AIError(RuntimeError):
    pass


def _post(url, payload, headers):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST"
    )
    request.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise AIError(f"HTTP {exc.code}：{body}") from exc
    except urllib.error.URLError as exc:
        raise AIError(str(exc.reason)) from exc


def _extract_json(text):
    """模型有时会裹上 ```json 之类的东西。取第一个 [ 到最后一个 ] 之间的部分。"""
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < 0:
        raise AIError(f"模型没有返回 JSON：{text[:200]}")
    return json.loads(text[start : end + 1])


def _ask_gemini(prompt, cfg):
    model = cfg.get("model", "gemini-2.5-flash")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        f"?key={cfg['api_key']}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        # 【温度调到 0】：这是分类，不是创作。同样的帖子今天 3 分明天 1 分，
        # 会让人不再相信这个分数。
        "generationConfig": {"temperature": 0, "maxOutputTokens": 2048},
    }
    data = _post(url, payload, {})
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise AIError(f"返回的结构不对：{json.dumps(data)[:200]}") from exc


def _ask_openai_compatible(prompt, cfg):
    """OpenAI 兼容接口：OpenAI 本身、DeepSeek、以及一堆网关都走这个形状。"""
    base = cfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
    payload = {
        "model": cfg.get("model", "gpt-4o-mini"),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    data = _post(
        f"{base}/chat/completions", payload, {"Authorization": f"Bearer {cfg['api_key']}"}
    )
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise AIError(f"返回的结构不对：{json.dumps(data)[:200]}") from exc


ASKERS = {"gemini": _ask_gemini, "openai": _ask_openai_compatible, "deepseek": _ask_openai_compatible}


def score(rows, cfg):
    """给一批条目打分。返回 {external_id: (score, reason)}。

    【一次问完，不是一条一问】：几十条塞进一个请求，成本和延迟都低一个数量级。
    条目多的时候分批，免得撑爆上下文。
    """
    provider = cfg.get("provider", "gemini")
    asker = ASKERS.get(provider)
    if asker is None:
        raise AIError(f"不认识的 provider：{provider}（可选 {'/'.join(ASKERS)}）")
    if not cfg.get("api_key"):
        raise AIError("config.json 的 ai.api_key 是空的")

    results = {}
    batch_size = int(cfg.get("batch_size", 25))
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        lines = []
        for index, row in enumerate(batch):
            title = (row["title"] or "").strip()
            body = (row["body"] or "").strip()[:500]
            lines.append(json.dumps({"id": index, "title": title, "text": body}, ensure_ascii=False))
        answer = asker(PROMPT + "\n".join(lines), cfg)
        for item in _extract_json(answer):
            try:
                row = batch[int(item["id"])]
            except (KeyError, ValueError, IndexError):
                continue
            results[row["external_id"]] = (int(item.get("score", 0)), str(item.get("reason", ""))[:120])
    return results
