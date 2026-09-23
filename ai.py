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
import socket
import time
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

Posts may be in Chinese or English; judge them the same way.

Answer with JSON only, in this exact shape:
{"items": [{"id": <id>, "score": <0-3>, "reason": "<at most 12 words, in Chinese>"}]}
No prose, no code fences.

Items:
"""


class AIError(RuntimeError):
    pass


# 【503 和 429 要重试，别的不要】：前者是模型临时过载、后者是超额度，两个都
# 等一会儿就好；而 400（请求写错了）、404（模型下线了）重试一百次也是错的，
# 早点报出来才有人去改。
RETRY_CODES = (429, 503)
BACKOFF = (5, 15, 40)


def _post(url, payload, headers):
    body = json.dumps(payload).encode("utf-8")
    for attempt, wait in enumerate(BACKOFF, start=1):
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        # 【一定要带 User-Agent】：不带的话 urllib 会自报 "Python-urllib/3.x"，
        # Groq 前面的 Cloudflare 直接按客户端签名封掉，返回 403 error code 1010
        # ——那个报错和 key 无关，很容易被误诊成"key 填错了"。
        request.add_header("User-Agent", "anki-radar/1.0")
        request.add_header("Accept", "application/json")
        for key, value in headers.items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code in RETRY_CODES and attempt < len(BACKOFF):
                print(f"  模型暂时不可用（HTTP {exc.code}），等 {wait} 秒再试…", flush=True)
                time.sleep(wait)
                continue
            raise AIError(f"HTTP {exc.code}：{detail}") from exc
        except socket.timeout as exc:
            # 【读超时必须变成 AIError】：它原本是 socket.timeout，既不算 HTTPError
            # 也不算 URLError，于是一路抛穿 _ask，既不重试也不切备用，整轮打分
            # 直接崩掉——表现是帖子已经入库却一条都没打分，页面上于是摆着一堆
            # 没筛过的噪音（2026-09-21 运营者的那一轮 Reddit 就是这么没的）。
            if attempt < len(BACKOFF):
                print(f"  模型没在 {TIMEOUT} 秒内回话，等 {wait} 秒再试…", flush=True)
                time.sleep(wait)
                continue
            raise AIError(f"连续 {len(BACKOFF)} 次都没在 {TIMEOUT} 秒内回话") from exc
        except urllib.error.URLError as exc:
            raise AIError(str(exc.reason)) from exc
    raise AIError("重试之后仍然失败")


def _extract_json(text):
    """从模型的回答里取出那份列表。

    【三重保险】：接口层面已经要求 JSON（见下面两个 asker），但小模型偶尔还是会
    裹上 ```json、或者干脆退化成 "Id 16: 1, ..." 这种自由格式（2026-09-20 实测
    在一批中文内容上发生过）。所以这里既认对象也认裸列表，认不出来就报错——
    而调用方遇到报错会退回纯关键词，不会把一批乱数据当成分数写进库里。
    """
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start : end + 1])
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return data["items"]
        except json.JSONDecodeError:
            pass
    start, end = text.find("["), text.rfind("]")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise AIError(f"模型没有返回 JSON：{text[:150]}")


def _ask_gemini(prompt, cfg, want_json=True):
    model = cfg.get("model", "gemini-2.5-flash")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        f"?key={cfg['api_key']}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        # 【温度调到 0】：这是分类，不是创作。同样的帖子今天 3 分明天 1 分，
        # 会让人不再相信这个分数。
        # 【在接口层面强制 JSON】：只在提示词里写"请输出 JSON"是不够的，
        # 模型偶尔会退化成自由格式，那一整批打分就全丢了。
        # 【但只对要 JSON 的调用开】：答题要点要的是给人读的纯文本，逼它输出
        # JSON 会得到一段裹在引号里的东西。
        "generationConfig": {"temperature": 0, "maxOutputTokens": 4096},
    }
    if want_json:
        payload["generationConfig"]["responseMimeType"] = "application/json"
    data = _post(url, payload, {})
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise AIError(f"返回的结构不对：{json.dumps(data)[:200]}") from exc


def _ask_openai_compatible(prompt, cfg, want_json=True):
    """OpenAI 兼容接口：OpenAI 本身、DeepSeek、以及一堆网关都走这个形状。"""
    base = cfg.get("base_url", "https://api.openai.com/v1").rstrip("/")
    payload = {
        "model": cfg.get("model", "gpt-4o-mini"),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    # 同上：接口层面要求 JSON 对象（所以提示词里的形状是 {"items": [...]}）。
    # 【Groq 还多一条规矩】：用 response_format 时，提示词里必须出现 "json"
    # 这个词，否则 400 —— 'messages' must contain the word 'json' in some form。
    # 答题要点的提示词里当然没有这个词，所以这个参数只能按需开，不能一直挂着
    # （2026-09-23 运营者点"写要点"就撞上了这个 400）。
    if want_json:
        payload["response_format"] = {"type": "json_object"}
    data = _post(
        f"{base}/chat/completions", payload, {"Authorization": f"Bearer {cfg['api_key']}"}
    )
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise AIError(f"返回的结构不对：{json.dumps(data)[:200]}") from exc


ASKERS = {"gemini": _ask_gemini, "openai": _ask_openai_compatible, "deepseek": _ask_openai_compatible}


def _ask(prompt, cfg, want_json=True):
    """按配置问一次。主用挂了就换备用。

    【为什么要备用】：免费额度是按天算的，而模型偶尔会 503（2026-09-20 实测
    Gemini 就来过一次）。两家的免费额度互相独立，主用不行时换一家，比让
    这一轮直接退回纯关键词要好——退回去意味着噪音全都涌进页面。

    【备用只在"这一家用不了"时才上】：格式错、JSON 解析失败这类问题换一家
    也一样错，那是提示词的事，不该靠切供应商掩盖过去。
    """
    chain = [cfg]
    fallback = cfg.get("fallback")
    if fallback and fallback.get("api_key"):
        chain.append(fallback)

    last = None
    for index, settings in enumerate(chain):
        provider = settings.get("provider", "gemini")
        asker = ASKERS.get(provider)
        if asker is None:
            raise AIError(f"不认识的 provider：{provider}（可选 {'/'.join(ASKERS)}）")
        if not settings.get("api_key"):
            raise AIError("config.json 的 ai.api_key 是空的")
        try:
            return asker(prompt, settings, want_json=want_json)
        except AIError as exc:
            last = exc
            if index + 1 < len(chain):
                nxt = chain[index + 1]
                print(f"  {provider} 不行了（{str(exc)[:80]}），换 "
                      f"{nxt.get('provider')} / {nxt.get('model')}", flush=True)
    raise last


def score(rows, cfg):
    """给一批条目打分。返回 {external_id: (score, reason)}。

    【一次问完，不是一条一问】：几十条塞进一个请求，成本和延迟都低一个数量级。
    条目多的时候分批，免得撑爆上下文。
    """
    results = {}
    batch_size = int(cfg.get("batch_size", 25))
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        lines = []
        for index, row in enumerate(batch):
            title = (row["title"] or "").strip()
            body = (row["body"] or "").strip()[:500]
            lines.append(json.dumps({"id": index, "title": title, "text": body}, ensure_ascii=False))
        answer = _ask(PROMPT + "\n".join(lines), cfg)
        for item in _extract_json(answer):
            try:
                row = batch[int(item["id"])]
            except (KeyError, ValueError, IndexError):
                continue
            results[row["external_id"]] = (int(item.get("score", 0)), str(item.get("reason", ""))[:120])
    return results


# --- 翻译 --------------------------------------------------------------------

TRANSLATE_PROMPT = """Translate forum comments about the flashcard app Anki into
natural, plain Simplified Chinese, for a reader who does not know the source
language.

For each item: if it is already in Chinese or English, return an empty "zh".
Otherwise translate the whole text faithfully — keep the person's tone, keep
error messages and button labels in their original wording inside 「」 so they
can be quoted back, and do not add, summarise or explain anything.

Answer with JSON only, in this exact shape:
{"items": [{"id": <id>, "lang": "<ISO code>", "zh": "<translation or empty>"}]}

Items:
"""


def translate(rows, cfg):
    """把非中文、非英文的条目翻成中文。返回 {external_id: 译文或空串}。

    【为什么自己翻，不用 YouTube 那个"翻译"按钮】（2026-09-21 运营者问）：那个
    按钮要一条条点开原视频才有，而且不是每条评论都有。这里是在打完分之后，
    只把留下来的几条一次翻完，存进库里——页面上直接看中文，原文还在上面。

    【中文和英文不翻】：英文运营者自己读得懂，翻了反而多一段要看的字。
    由模型判断语言，因为葡语不带重音也照样是葡语，靠字符集判断不准。
    """
    results = {}
    batch_size = int(cfg.get("translate_batch_size", 10))
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        lines = []
        for index, row in enumerate(batch):
            text = "\n".join(x for x in ((row["title"] or "").strip(),
                                        (row["body"] or "").strip()[:800]) if x)
            lines.append(json.dumps({"id": index, "text": text}, ensure_ascii=False))
        answer = _ask(TRANSLATE_PROMPT + "\n".join(lines), cfg)
        for item in _extract_json(answer):
            try:
                row = batch[int(item["id"])]
            except (KeyError, ValueError, IndexError):
                continue
            results[row["external_id"]] = str(item.get("zh") or "").strip()[:2000]
    return results


# --- 把中文回复翻成对方的语言 --------------------------------------------------

REPLY_PROMPT = """A person is replying to the comment below. He wrote his reply
in Chinese. Put his reply into the same language the original comment is written
in, so he can post it there.

This is translation, not writing. Rules:
- Say exactly what he said: nothing added, nothing dropped, nothing polished
  into marketing language.
- Keep his register: a helpful person answering on a forum, first person, plain
  words, no greetings or sign-offs he did not write.
- Product names, error messages, menu items and URLs stay exactly as they are.
- Output the reply text only. No quotes around it, no notes, no alternatives.

The original comment he is replying to:
{original}

His reply, in Chinese:
{reply}
"""


def reply_in_their_language(row, chinese, cfg):
    """把运营者用中文写的回复，翻成原帖所用的那门语言。

    【目标语言不用人来指定】：模型看得见原帖，葡语、西语还是英语由它照着原文
    定。少一个下拉框，也少一次"选错了没发现"。

    【和写要点一样用备用那家】：这是点一次跑一次的小活，别去挤打分的额度。
    """
    text = (chinese or "").strip()
    if not text:
        raise AIError("先写点什么再翻")

    settings = dict(cfg.get("fallback") or {})
    if not settings.get("api_key"):
        settings = dict(cfg)
    settings.pop("fallback", None)
    if not settings.get("api_key"):
        raise AIError("config.json 里没有可用的 api_key")

    original = "\n".join(
        x for x in ((row.get("title") or "").strip(), (row.get("body") or "").strip()[:800]) if x
    )
    prompt = REPLY_PROMPT.format(original=original, reply=text[:3000])
    # 【不要 JSON】：要的是可以直接粘出去的一段话。
    return _ask(prompt, settings, want_json=False).strip()


# --- 答题要点 ----------------------------------------------------------------

BRIEF_PROMPT = """You are helping someone answer one forum post about Anki.

He will write the reply himself, in his own words. Your job is only to give him
the material: what the person is stuck on, and which facts apply. Write in
Chinese, except for technical terms and anything he should quote verbatim.

Hard rules:
- Use ONLY the facts listed below. If a technical detail is not in that list,
  do not state it — write "需要确认" instead. Getting a limit or a setting
  wrong in public costs him more than saying nothing.
- Do not write the reply itself. No greetings, no sign-off, no marketing.
- If the post is too vague to answer, say what to ask him first.

Facts you may use:
{facts}

How he answers:
{style}

Output exactly these three sections, in Chinese, no code fences:

他卡在哪
- （一到三条）

可以告诉他的事实
- （只能来自上面的清单；每条尽量短）

要不要提 LeeAB
- （提 / 不提，一句话说明为什么；要提的话给出那句身份说明）

The post:
"""


def brief(row, cfg):
    """给一条帖子生成答题要点。返回一段纯文本。

    【用备用那家】（默认 Groq）：筛选是每天都要跑的、必须稳；写要点是点一次跑一次、
    量小。分开用两家的免费额度，谁也不挤谁。
    """
    settings = dict(cfg.get("fallback") or {})
    if not settings.get("api_key"):
        settings = dict(cfg)   # 没配备用就用主用
    settings.pop("fallback", None)
    if not settings.get("api_key"):
        raise AIError("config.json 里没有可用的 api_key")

    import facts as facts_module

    prompt = BRIEF_PROMPT.format(
        facts="\n".join(f"- {f}" for f in facts_module.FACTS),
        style="\n".join(f"- {s}" for s in facts_module.STYLE),
    )
    body = json.dumps(
        {"title": row.get("title") or "", "text": (row.get("body") or "")[:1500],
         "where": row.get("community") or "", "kind": row.get("kind") or "post"},
        ensure_ascii=False,
    )
    # 【不要 JSON】：这里要的是给人抄材料用的纯文本。
    return _ask(prompt + body, settings, want_json=False).strip()
