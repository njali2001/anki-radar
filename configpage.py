# -*- coding: utf-8 -*-
"""本地网页上的配置页：改关键词、改间隔、换模型，不用手动编辑 config.json。

【为什么要有它】（2026-09-23 运营者提）：关键词现在散在四处——全局一份、B站
一份、YouTube 一份、Reddit 那边还有搜索语句——每次想删一个词都要在 JSON 里
数引号和逗号，"怕改坏"本身就足以让人不去改。而关键词是这个工具里最该常改的
东西：改不动，它就慢慢变成一堆没人维护的噪音。

【三条底线】：
- 密钥不回显。页面只说"已填（多少位）"，要换就整串重填。页面虽然只跑在
  127.0.0.1 上，但把密钥明文写进 DOM，截图、录屏、演示的时候就泄了。
- 校验不过就一个字都不写。宁可让人对着红字改，也不要写进去一份半对的配置——
  少做比做错安全。
- 注释键（那些 `_说明`）原样留着。这个文件是按键改值，不是重新生成。
"""

import json
import os
import pathlib
import shutil
import urllib.parse
from collections import OrderedDict

HERE = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
BACKUP_PATH = HERE / "config.bak.json"


# --- 哪些字段可以在页面上改 ----------------------------------------------------
#
# 【不是所有字段都放上来】：database 改了等于换一个库，ai.provider / base_url
# 改错了整个 AI 直接哑掉——这类留在文件里，页面底下写一句话指过去。
# 而 ai.model 特意放上来：半年里已经撞上两次模型下线（gemini-2.5-flash、
# llama-3.3-70b-versatile），那种时候要能在页面上换掉。

def field(path, label, kind, hint="", **extra):
    return dict(path=path, label=label, kind=kind, hint=hint, **extra)


SECTIONS = [
    {
        "key": "general",
        "label": "通用",
        "fields": [
            field("keywords", "关键词", "lines",
                  "一行一个。词尾写 * 按词干匹配（sincroniz* 能命中 sincronizar、"
                  "sincronização）。中文自动按子串匹配。"),
            field("skip_if_contains", "含这些词就跳过", "lines",
                  "系统通知、自动关帖这类内容里照样有关键词，但它们不是求助。"),
            field("max_age_days", "多久以前的就不要了（天）", "int", min=1, max=3650),
            field("daily_limit", "每个源一次显示几条", "int", min=1, max=50),
            field("pause_seconds", "请求之间等几秒", "int",
                  "Reddit 对这个最敏感，调小会 429。", min=0, max=300),
            field("user_agent", "User-Agent", "text",
                  "只能用 ASCII——HTTP 头里放不下中文。"),
        ],
    },
    {
        "key": "ankiforum",
        "label": "Anki 论坛",
        "source": "ankiforum",
        "fields": [
            field("anki_forum.enabled", "启用", "bool"),
            field("anki_forum.pause_seconds", "请求之间等几秒", "int", min=0, max=300),
        ],
    },
    {
        "key": "reddit",
        "label": "Reddit",
        "source": "reddit",
        "fields": [
            field("reddit_rss.enabled", "启用", "bool"),
            field("reddit_rss.searches", "全站搜索", "pairs",
                  "一行一条，写成「标签 | 查询语句」。查询支持布尔写法："
                  "anki (sync OR ankiweb)。搜索结果不再过关键词表——查询本身就是筛子。"),
            field("reddit_rss.subreddits", "订阅的版块", "lines",
                  "一行一个，不要写 r/。这些走的是版块新帖流，会过关键词表。"),
            field("reddit_rss.include_comments", "连评论一起扫", "bool",
                  "请求数翻倍，而限流按请求数算。"),
            field("reddit_rss.min_interval_minutes", "两次扫描至少隔（分钟）", "int",
                  min=0, max=10080),
        ],
    },
    {
        "key": "bilibili",
        "label": "B站",
        "source": "bilibili",
        "fields": [
            field("bilibili.enabled", "启用", "bool"),
            field("bilibili.search", "搜哪些视频", "lines", "一行一个。"),
            field("bilibili.keywords", "关键词", "lines",
                  "决定去读哪些视频的评论，也用来筛评论。"),
            field("bilibili.max_videos_for_comments", "每轮最多读几个视频的评论", "int",
                  "每个视频一次请求。匿名只能读到每个视频最新 3 条评论。", min=1, max=30),
            field("bilibili.include_videos", "把视频本身也收进来", "bool",
                  "默认不收：搜到的多是一两年前的教程，会把新评论淹掉。"),
            field("bilibili.max_age_days", "多久以前的就不要了（天）", "int", min=1, max=3650),
            field("bilibili.min_score", "AI 门槛（0–3）", "int",
                  "量小的源可以放低一档。0 分是广告和无关，永远刷掉。", min=1, max=3),
            field("bilibili.min_interval_minutes", "两次扫描至少隔（分钟）", "int",
                  min=0, max=10080),
            field("bilibili.pause_seconds", "请求之间等几秒", "int", min=0, max=300),
        ],
    },
    {
        "key": "youtube",
        "label": "YouTube",
        "source": "youtube",
        "fields": [
            field("youtube.enabled", "启用", "bool"),
            field("youtube.api_key", "API key", "secret",
                  "Google Cloud 控制台里启用 YouTube Data API v3 之后创建的那个。"
                  "AI Studio 的 Gemini key 在这里用不了，会 401。"),
            field("youtube.search", "搜哪些视频", "pairs",
                  "一行一条，写成「标签 | 查询语句 | 语言代码」，语言可以不写。"
                  "每次搜索花 100 单位配额，免费额度一天 10000。", third="lang"),
            field("youtube.keywords", "关键词", "lines",
                  "决定去读哪些视频的评论，也用来筛评论。葡语记得用词干写法。"),
            field("youtube.max_videos_for_comments", "每轮最多读几个视频的评论", "int",
                  "每个视频一次请求，只花 1 单位配额。", min=1, max=30),
            field("youtube.translate", "把非中英文的评论翻成中文", "bool"),
            field("youtube.max_age_days", "多久以前的就不要了（天）", "int", min=1, max=3650),
            field("youtube.min_interval_minutes", "两次扫描至少隔（分钟）", "int",
                  min=0, max=10080),
            field("youtube.pause_seconds", "请求之间等几秒", "int", min=0, max=300),
        ],
    },
    {
        "key": "ai",
        "label": "AI 与额度",
        "fields": [
            field("ai.enabled", "用 AI 筛选", "bool"),
            field("ai.daily_request_budget", "每天大约用多少次请求", "int",
                  "只用来画进度条的分母。Gemini 的免费额度上限没有接口可查，"
                  "所以这是你自己设的预算，不是官方数字。", min=1, max=100000),
            field("ai.min_score", "默认 AI 门槛（0–3）", "int",
                  "各个源可以自己覆盖这个值。", min=1, max=3),
            field("ai.candidates", "一轮最多给 AI 看几条", "int", min=1, max=200),
            field("ai.batch_size", "每个请求塞几条", "int", min=1, max=50),
        ],
    },
]


# 【分块】：照 app.leeab.net 的 dashboard 那套——小标题在卡片外面，内容装在
# 一张带边框的卡里。一屏十几个输入框平铺下来，人分不清哪几个是一回事；
# 分了块之后，"搜什么"和"多久扫一次"一眼就分得开。
GROUPS = {
    "general": [
        ("找什么", ["keywords", "skip_if_contains"]),
        ("留多少", ["max_age_days", "daily_limit"]),
        ("怎么请求", ["pause_seconds", "user_agent"]),
    ],
    "ankiforum": [
        ("开关与节流", ["anki_forum.enabled", "anki_forum.pause_seconds"]),
    ],
    "reddit": [
        ("开关", ["reddit_rss.enabled"]),
        ("搜什么", ["reddit_rss.searches", "reddit_rss.subreddits",
                 "reddit_rss.include_comments"]),
        ("多久扫一次", ["reddit_rss.min_interval_minutes"]),
    ],
    "bilibili": [
        ("开关", ["bilibili.enabled"]),
        ("搜什么", ["bilibili.search", "bilibili.keywords"]),
        ("留多少", ["bilibili.max_age_days", "bilibili.min_score",
                 "bilibili.include_videos"]),
        ("节流", ["bilibili.max_videos_for_comments",
                "bilibili.min_interval_minutes", "bilibili.pause_seconds"]),
    ],
    "youtube": [
        ("开关与密钥", ["youtube.enabled", "youtube.api_key", "youtube.translate"]),
        ("搜什么", ["youtube.search", "youtube.keywords"]),
        ("留多少", ["youtube.max_age_days"]),
        ("节流与配额", ["youtube.max_videos_for_comments",
                   "youtube.min_interval_minutes", "youtube.pause_seconds"]),
    ],
    "ai": [
        ("AI 筛选", ["ai.enabled", "ai.min_score", "ai.candidates", "ai.batch_size",
                  "ai.daily_request_budget"]),
    ],
}


# --- 读写配置 -----------------------------------------------------------------

def load(path=CONFIG_PATH):
    """【保序读】：配置文件里那些 `_说明` 是给人看的注释，顺序也是有意排的。"""
    with open(path, encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=OrderedDict)


def dig(data, path, default=None):
    for part in path.split("."):
        if not isinstance(data, dict) or part not in data:
            return default
        data = data[part]
    return data


def plant(data, path, value):
    parts = path.split(".")
    for part in parts[:-1]:
        data = data.setdefault(part, OrderedDict())
    data[parts[-1]] = value


def save(config, path=CONFIG_PATH):
    """先备份，再原子替换。

    【为什么不直接 open(path, "w")】：那会先把文件清空再写。写到一半出错
    （磁盘满、进程被杀），留下的是半个 JSON——下次启动连工具都起不来，而人
    根本不知道刚才那一下干了什么。先写临时文件、再 os.replace，要么是旧的
    要么是新的，不存在中间状态。
    """
    path = pathlib.Path(path)
    if path.exists():
        shutil.copyfile(path, BACKUP_PATH)
    body = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_bytes(body.encode("utf-8"))
    os.replace(tmp, path)


def restore(path=CONFIG_PATH):
    """把上一版换回来。返回是否真的换了。"""
    if not BACKUP_PATH.exists():
        return False
    current = pathlib.Path(path).read_bytes() if pathlib.Path(path).exists() else b""
    previous = BACKUP_PATH.read_bytes()
    tmp = pathlib.Path(path).with_suffix(".json.tmp")
    tmp.write_bytes(previous)
    os.replace(tmp, path)
    # 【互换而不是覆盖】：恢复之后再点一次"恢复"应该能回到刚才那一版，
    # 否则人点错一次就回不去了。
    BACKUP_PATH.write_bytes(current)
    return True


# --- 表单 <-> 配置 -------------------------------------------------------------

def _lines(text):
    return [line.strip() for line in (text or "").replace("\r", "").split("\n") if line.strip()]


def _pairs_to_text(rows, third=None):
    out = []
    for row in rows or []:
        parts = [str(row.get("label", "")), str(row.get("query", ""))]
        if third and row.get(third):
            parts.append(str(row[third]))
        out.append(" | ".join(parts))
    return "\n".join(out)


def _text_to_pairs(text, third=None):
    rows, errors = [], []
    for line in _lines(text):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            errors.append(f"「{line[:40]}」写法不对，应该是「标签 | 查询语句」")
            continue
        row = OrderedDict([("label", parts[0]), ("query", parts[1])])
        if third and len(parts) > 2 and parts[2]:
            row[third] = parts[2]
        rows.append(row)
    return rows, errors


def apply_form(config, form):
    """把表单的值写进配置的一份副本。返回 (新配置, 错误列表)。

    【错误要指到具体哪一项】：只说"保存失败"的话，人得自己去猜是哪一格填错了。
    """
    updated = json.loads(json.dumps(config), object_pairs_hook=OrderedDict)
    errors = []
    submitted = {p for p in (form.get("__fields") or "").split(",") if p}
    for section in SECTIONS:
        for spec in section["fields"]:
            path, kind = spec["path"], spec["kind"]
            raw = form.get(path)
            here = f"{section['label']} · {spec['label']}"

            if kind == "bool":
                # 【只处理这一版表单确实有的复选框】，理由见 render 里那段注释。
                if path in submitted:
                    plant(updated, path, path in form)
                continue
            if raw is None:
                # 【表单里没有这一格，就别动它】：旧标签页重发上来的表单会缺
                # 新加的字段，当成"填了个空"去校验，人看到的是一句莫名其妙的
                # "要填一个整数"，而他什么都没填过（2026-09-23 运营者遇到）。
                continue
            elif kind == "int":
                text = (raw or "").strip()
                if not text.lstrip("-").isdigit():
                    errors.append(f"{here}：要填一个整数")
                    continue
                value = int(text)
                low, high = spec.get("min"), spec.get("max")
                if low is not None and value < low or high is not None and value > high:
                    errors.append(f"{here}：要在 {low} 和 {high} 之间")
                    continue
                plant(updated, path, value)
            elif kind == "text":
                text = (raw or "").strip()
                if not text:
                    errors.append(f"{here}：不能留空")
                    continue
                if path == "user_agent" and any(ord(ch) > 127 for ch in text):
                    # 【HTTP 头只能放 ASCII】：填了中文的话，请求在发出去之前
                    # 就会抛 UnicodeEncodeError，而报错信息完全不指向这里。
                    errors.append(f"{here}：只能用 ASCII，不能有中文")
                    continue
                plant(updated, path, text)
            elif kind == "secret":
                # 【留空 = 不动】：页面从不回显密钥，所以空值只能理解成"没改"。
                text = (raw or "").strip()
                if text:
                    plant(updated, path, text)
            elif kind == "lines":
                values = _lines(raw)
                if not values and path in ("keywords",):
                    errors.append(f"{here}：至少要留一个")
                    continue
                plant(updated, path, values)
            elif kind == "pairs":
                rows, bad = _text_to_pairs(raw, spec.get("third"))
                errors.extend(f"{here}：{b}" for b in bad)
                if not bad:
                    plant(updated, path, rows)
    return updated, errors


# --- 关键词试一下（不联网）-----------------------------------------------------

def try_keywords(store, source, keywords):
    """拿库里已经存着的帖子试这套关键词，看会命中多少条。

    【不联网】：真去扫一轮要几分钟，还要占限流配额，而"这个词写法对不对"
    这个问题，用手上已有的几十条就能回答。
    """
    import radar  # 【延迟导入】：radar 也会用到这个模块，顶层互相导入会死锁

    words = [w for w in keywords if w.strip()]
    patterns = [(w, radar.pattern_for(w)) for w in words]
    rows = store.all_for(source) if source else store.all_for(None)
    hits = []
    for row in rows:
        text = f"{row['title'] or ''}\n{row['body'] or ''}"
        matched = [w for w, p in patterns if p.search(text)]
        if matched:
            hits.append((matched, (row["title"] or row["body"] or "")[:90]))
    return {
        "total": len(rows),
        "count": len(hits),
        "examples": [{"words": m, "text": t} for m, t in hits[:3]],
    }


def parse_form(body):
    """表单是 application/x-www-form-urlencoded，值里可能有换行和中文。"""
    parsed = urllib.parse.parse_qs(body, keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


# --- 页面 ---------------------------------------------------------------------

CONFIG_STYLE = """
/* 【表单也要接进那条 flex 链】：榜单页是 body.app > .wrap，两栏各滚各的；
   配置页在中间多包了一层 <form>，链子就断在这里——.wrap 拿不到高度，.main 的
   overflow-y:auto 等于没写，内容被 body 的 overflow:hidden 直接裁掉，鼠标怎么
   滚都不动（2026-09-23 运营者只看得到前两个字段）。 */
body.app form { flex: 1; min-height: 0; display: flex; flex-direction: column; }
/* 【保存和恢复钉在页头】（2026-09-23 运营者定）：右栏自己滚之后，长表单往下翻
   几屏就看不见按钮了——而"改完要保存"这件事恰恰是翻到最底下才想起来的。 */
body.app .topbar h1 { margin: 0; padding: 0; }
.cfg-actions { margin-left: auto; display: flex; gap: 10px; align-items: center; }
.quota { margin: 0 0 26px; }
.quota-row { border: 1px solid var(--border); border-radius: 10px; background: var(--surface);
             padding: 11px 14px; margin-bottom: 8px; }
.quota-row b { display: block; color: var(--text-2); font-size: 13.5px; font-weight: 600; }
.quota-use { display: block; color: var(--muted); font-size: 12.5px; margin: 2px 0 9px; }
/* 条子照 dashboard 那套：一整条圆角轨道，用掉的部分着色，读数摆在条子外面
   ——压在条子里的话，灰色越窄字越放不下，迟早读不出来。 */
.bar-row { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
.bar-label { flex: none; width: 3.2em; color: var(--muted); font-size: 12.5px; }
.bar { flex: 1 1 auto; min-width: 4rem; height: 10px; border-radius: 999px;
       overflow: hidden; background: var(--chip); }
.bar-fill { height: 100%; border-radius: 999px; }
.bar-fill.ok { background: var(--bar-ok); }
.bar-fill.warn { background: var(--bar-warn); }
.bar-fill.high { background: var(--bar-high); }
.bar-caption { flex: none; color: var(--muted); font-size: 12px; white-space: nowrap; }
/* 【块】：边框 + 圆角 + 卡片底色，小标题在块外面。抄的是 dashboard 的
   .acct-title / .acct-card 那一对。 */
.block { border: 1px solid var(--border); border-radius: 10px;
         background: var(--surface); padding: 16px 18px 2px; margin: 0 0 22px; }
.block-title { margin: 0 0 8px; font-size: 13px; font-weight: 600; color: var(--muted);
               letter-spacing: .02em; }
.block-title + .quota { margin-bottom: 22px; }
.cfg-field { margin: 0 0 16px; }
.cfg-field:last-child { margin-bottom: 14px; }
.cfg-field label.name { display: block; color: var(--text); font-size: 14.5px; margin-bottom: 4px; }
.cfg-hint { color: var(--muted); font-size: 12.5px; line-height: 1.6; margin: 0 0 8px; }
.cfg-field input[type=text], .cfg-field textarea {
  width: 100%; box-sizing: border-box; border: 1px solid var(--border-strong); border-radius: 8px;
  background: var(--sunken); color: var(--text); font: inherit; font-size: 14px; padding: 8px 11px; }
.cfg-field textarea { min-height: 96px; resize: vertical; line-height: 1.6;
                      font-family: ui-monospace, Consolas, monospace; font-size: 13.5px; }
.cfg-field input:focus, .cfg-field textarea:focus { outline: none; border-color: var(--border-hover); }
.cfg-field input[type=text].narrow { width: 130px; }
.cfg-check { display: flex; align-items: center; gap: 9px; color: var(--text); font-size: 14.5px; }
.cfg-check input { width: 16px; height: 16px; accent-color: var(--ok); }
.cfg-secret { color: var(--muted); font-size: 12.5px; margin-bottom: 6px; }
.cfg-bar { display: flex; gap: 10px; align-items: center; margin: 0 0 18px; flex-wrap: wrap; }
.cfg-bar .grow { margin-right: auto; color: var(--muted); font-size: 13px; }
.cfg-save { cursor: pointer; border: 1px solid var(--save-line); background: var(--save-bg); color: var(--save-text);
            border-radius: 8px; padding: 7px 18px; font: inherit; font-size: 14px; }
.cfg-save[disabled] { opacity: .45; cursor: default; }
.cfg-msg { padding: 10px 14px; border-left: 3px solid var(--ok-line); background: var(--good-bg);
           color: var(--save-text); font-size: 13.5px; margin: 0 0 18px; }
.cfg-msg.bad { border-left-color: var(--bad-line); background: var(--bad-bg); color: var(--bad-text); }
/* 【提示条要能关掉】（2026-09-23 运营者提）："保存好了"看过一眼就没用了，
   却一直占着表单顶上那块地方。 */
.cfg-msg { position: relative; padding-right: 34px; }
.msg-x { position: absolute; top: 4px; right: 8px; border: 0; background: none;
         color: inherit; opacity: .55; font-size: 18px; line-height: 1;
         cursor: pointer; padding: 2px 4px; }
.msg-x:hover { opacity: 1; }
.cfg-msg ul { margin: 6px 0 0; padding-left: 18px; }
table.counts { border-collapse: collapse; margin-top: 10px; font-size: 12.5px;
               color: var(--text-2); min-width: 18rem; }
table.counts th { text-align: left; font-weight: 600; color: var(--muted);
                  padding: 0 1.2rem .3rem 0; border-bottom: 1px solid var(--border); }
table.counts td { padding: .25rem 1.2rem .25rem 0; border-bottom: 1px solid var(--border); }
table.counts tr:last-child td { border-bottom: 0; }
table.counts .num { text-align: right; padding-right: 1.6rem; font-variant-numeric: tabular-nums; }
table.counts .when { color: var(--muted); white-space: nowrap; }
/* 【0 次的整行压暗，不是标红】：它未必是错的——也可能只是没人这么说。
   标红会把"要修的东西"和"可以考虑删的东西"混为一谈。 */
table.counts tr.zero td { color: var(--faint); }
.try-out { margin-top: 8px; padding: 9px 12px; border-left: 3px solid var(--quote-line);
           background: var(--quote-bg); color: var(--text-2); font-size: 13px; white-space: pre-wrap; }
.try-out.bad { border-left-color: var(--bad-line); color: var(--bad-text); }
"""


def _esc(text):
    import html as html_mod
    return html_mod.escape(str(text if text is not None else ""))


def _ago(stamp):
    """粗到"天"就够了——这一列是用来分辨"上周还灵"和"半年没动静"的。"""
    import time

    if not stamp:
        return "—"
    days = int((time.time() - int(stamp)) // 86400)
    if days <= 0:
        return "今天"
    if days == 1:
        return "昨天"
    if days < 60:
        return f"{days} 天前"
    return f"{days // 30} 个月前"


def _render_field(config, spec, stats):
    path, kind = spec["path"], spec["kind"]
    value = dig(config, path)
    hint = f'<p class="cfg-hint">{_esc(spec["hint"])}</p>' if spec.get("hint") else ""
    name = _esc(spec["label"])

    if kind == "bool":
        checked = " checked" if value else ""
        return (f'<div class="cfg-field"><label class="cfg-check">'
                f'<input type="checkbox" name="{path}"{checked}>{name}</label>{hint}</div>')

    head = f'<div class="cfg-field"><label class="name" for="{path}">{name}</label>{hint}'

    if kind == "int":
        return (head + f'<input class="narrow" type="text" id="{path}" name="{path}" '
                       f'value="{_esc(value)}"></div>')
    if kind == "text":
        return head + f'<input type="text" id="{path}" name="{path}" value="{_esc(value)}"></div>'
    if kind == "secret":
        filled = (f'已填（{len(value)} 位，{_esc(str(value)[:4])}…）。留空就是不改。'
                  if value else '还没填。')
        return (head + f'<p class="cfg-secret">{filled}</p>'
                       f'<input type="text" id="{path}" name="{path}" value="" '
                       f'placeholder="要换的话把新的整串粘在这里"></div>')
    if kind == "pairs":
        text = _pairs_to_text(value, spec.get("third"))
        return head + f'<textarea id="{path}" name="{path}">{_esc(text)}</textarea></div>'

    # lines
    text = "\n".join(str(v) for v in (value or []))
    counts = ""
    if stats is not None and path.endswith("keywords") and value:
        # 【表格，不是一长串】（2026-09-23 运营者定）：十几个词用"·"连成一行，
        # 要在里面找出哪几个是 0，得一个一个数过去。列成表就一眼看得到。
        # 【按命中数从多到少排】：这张表是拿来做"删哪个"这个决定的，0 全在底下
        # 最省事；表里的顺序和输入框里的顺序不一样，不影响任何功能。
        rows = sorted(((w, *stats.get(w, (0, 0))) for w in value),
                      key=lambda item: (-item[1], item[0]))
        cells = "".join(
            f'<tr class="{"zero" if not hit else ""}">'
            f'<td>{_esc(word)}</td><td class="num">{hit}</td>'
            f'<td class="when">{_esc(_ago(last) if hit else "—")}</td></tr>'
            for word, hit, last in rows)
        counts = (f'<table class="counts"><thead><tr><th>关键词</th>'
                  f'<th class="num">命中</th><th class="when">最近一次</th></tr></thead>'
                  f'<tbody>{cells}</tbody></table>')
    button = ""
    if path.endswith("keywords"):
        button = (f'<div class="cfg-bar" style="margin:8px 0 0">'
                  f'<button type="button" class="act try-btn" data-for="{path}" '
                  f'data-source="{spec.get("source") or ""}">试一下（不联网）</button></div>')
    return head + f'<textarea id="{path}" name="{path}">{_esc(text)}</textarea>{counts}{button}</div>'


def _render_usage(store, config):
    """额度块。【每一行都写清楚数字是哪儿来的】：估算和实测混在一起显示，
    人会拿估算当实测去做"还能不能再扫一轮"的决定。"""
    import usage

    rows = usage.summary(store, config)
    if not rows:
        return ""
    cells = []
    for row in rows:
        bars = "".join(
            f'<div class="bar-row">'
            f'<span class="bar-label">{_esc(b["label"])}</span>'
            f'<div class="bar" role="img" aria-label="{_esc(b["label"])} '
            f'{b["used"]}/{b["total"]}，{b["percent"]}%">'
            f'<div class="bar-fill {b["level"]}" style="width:{b["percent"]}%"></div></div>'
            f'<span class="bar-caption">{_esc(b["note"])}</span>'
            f'</div>' for b in row["bars"])
        use = (f'<span class="quota-use">{_esc(row["use"])}</span>'
               if row.get("use") else "")
        cells.append(f'<div class="quota-row"><b>{_esc(row["name"])}</b>{use}{bars}</div>')
    return f'<div class="quota">{"".join(cells)}</div>'


def render(store, config, message=None, errors=None, active=None):
    """整页。左边是分组，右边是表单——和榜单页同构，省一次学习。"""
    import radar

    stats = store.keyword_details()
    # 【表单要自报家门】：浏览器只提交勾上的复选框，没勾的压根不出现——所以
    # 光看"表单里有没有这个键"分不清"没勾"和"这一版表单根本没有这个字段"。
    # 页面在旧标签页里放了半天、代码又加了新字段时，后者就会发生：保存一下，
    # 新字段被当成"没勾"悄悄写成 false。带上这份清单，保存时就只动清单里有的。
    field_list = ",".join(spec["path"] for section in SECTIONS
                          for spec in section["fields"])
    side, panels = [], []
    active = active or SECTIONS[0]["key"]
    for section in SECTIONS:
        on = " on" if section["key"] == active else ""
        side.append(f'<div class="src-item{on}" data-source="cfg-{section["key"]}">'
                    f'<div class="src-name">{_esc(section["label"])}</div></div>')
        by_path = {spec["path"]: dict(spec, source=section.get("source"))
                   for spec in section["fields"]}
        body = []
        if section["key"] == "ai":
            body.append(_render_usage(store, config))
        used = set()
        for title, paths in GROUPS.get(section["key"], []):
            cells = [_render_field(config, by_path[path], stats)
                     for path in paths if path in by_path]
            used.update(paths)
            if cells:
                body.append(f'<h3 class="block-title">{_esc(title)}</h3>'
                            f'<div class="block">' + "".join(cells) + "</div>")
        # 【没分到组的也要出现】：往 SECTIONS 里加字段却忘了写进 GROUPS 时，
        # 它该露在最后一块里，而不是从页面上无声地消失。
        rest = [_render_field(config, by_path[spec["path"]], stats)
                for spec in section["fields"] if spec["path"] not in used]
        if rest:
            body.append('<h3 class="block-title">其它</h3>'
                        '<div class="block">' + "".join(rest) + "</div>")
        panels.append(f'<section class="panel{on}" data-source="cfg-{section["key"]}">'
                      f'<h2>{_esc(section["label"])}</h2>' + "".join(body) + "</section>")

    close = ('<button type="button" class="msg-x" aria-label="关闭" '
             'onclick="this.parentNode.remove()">&times;</button>')
    note = ""
    if errors:
        note = ('<div class="cfg-msg bad">' + close
                + "这些地方要先改掉，整份都还没保存：<ul>"
                + "".join(f"<li>{_esc(e)}</li>" for e in errors) + "</ul></div>")
    elif message:
        note = f'<div class="cfg-msg">{close}{_esc(message)}</div>'

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>anki-radar 设置</title><script>/* 【在渲染之前就定好主题】：放到 body 里的话，页面会先闪一下深色再变浅，比不切换还难受。 */try{{document.documentElement.dataset.theme=localStorage.getItem("radar-theme")||"light";}}catch(e){{document.documentElement.dataset.theme="light";}}</script>
<style>{radar.STYLE}{CONFIG_STYLE}</style></head>
<body class="app">
<form method="post" action="/config" id="cfg">
<input type="hidden" name="__fields" value="{field_list}">
<header class="topbar">
  <h1>设置</h1>
  <div class="cfg-actions">
    <button type="button" class="act" id="theme">浅色</button>
    <button type="button" class="act" id="restore">恢复上一版</button>
    <button type="submit" class="cfg-save">保存</button>
  </div>
</header>
<div class="wrap">
  <aside class="side">{"".join(side)}
    <div class="src-item" style="cursor:pointer" onclick="location.href='/'">
      <div class="src-name">← 回榜单</div></div>
  </aside>
  <main class="main">
    {note}
    {"".join(panels)}
  </main>
</div>
</form>
<script>

// 【开关记在本机】：白天浅色晚上深色，是随手换的东西，不该每次重开都回到默认。
(function () {{
  const btn = document.getElementById("theme");
  if (!btn) return;
  const label = () => {{
    btn.textContent = document.documentElement.dataset.theme === "light" ? "深色" : "浅色";
  }};
  label();
  btn.addEventListener("click", () => {{
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    try {{ localStorage.setItem("radar-theme", next); }} catch (e) {{}}
    label();
  }});
}})();

if (location.search.indexOf("saved=") >= 0) {{
  try {{ history.replaceState({{}}, "", location.pathname + location.hash); }} catch (e) {{}}
}}

const items = [...document.querySelectorAll(".src-item[data-source]")];
const panels = [...document.querySelectorAll(".panel")];
items.forEach(item => item.addEventListener("click", () => {{
  const key = item.dataset.source;
  items.forEach(i => i.classList.toggle("on", i.dataset.source === key));
  panels.forEach(p => p.classList.toggle("on", p.dataset.source === key));
  try {{ localStorage.setItem("radar-cfg-tab", key); }} catch (e) {{}}
}}));
try {{
  const want = localStorage.getItem("radar-cfg-tab");
  const found = items.find(i => i.dataset.source === want);
  if (found) found.click();
}} catch (e) {{}}

// 【试一下：拿库里已有的帖子当样本】。真扫一轮要几分钟又占限流配额，而
// "这个词写法对不对"用手上几十条就能回答。
document.querySelectorAll(".try-btn").forEach(btn => {{
  btn.addEventListener("click", async () => {{
    const area = document.getElementById(btn.dataset.for);
    const bar = btn.closest(".cfg-bar");
    let out = bar.nextElementSibling;
    if (!out || !out.classList.contains("try-out")) {{
      out = document.createElement("div");
      out.className = "try-out";
      bar.after(out);
    }}
    out.textContent = "试算中…";
    try {{
      const res = await fetch("/config/try", {{
        method: "POST", headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ source: btn.dataset.source, text: area.value }})
      }});
      const data = await res.json();
      if (data.error) {{ out.className = "try-out bad"; out.textContent = data.error; return; }}
      out.className = "try-out";
      let text = `库里 ${{data.total}} 条里会命中 ${{data.count}} 条。`;
      if (data.examples.length) {{
        text += "\\n" + data.examples.map(e => `  · [${{e.words.join(", ")}}] ${{e.text}}`).join("\\n");
      }} else {{
        text += " 一条都没命中——要么写法不对，要么库里还没有这类内容。";
      }}
      out.textContent = text;
    }} catch (e) {{
      out.className = "try-out bad";
      out.textContent = "试算失败：连不上本地服务";
    }}
  }});
}});

document.getElementById("restore").addEventListener("click", async () => {{
  if (!confirm("把配置换回上一版？现在这一版会被存成备份，可以再换回来。")) return;
  const res = await fetch("/config/restore", {{ method: "POST" }});
  const data = await res.json();
  if (data.ok) {{ location.reload(); }}
  else {{ alert(data.error || "没有可恢复的版本"); }}
}});
</script>
</body></html>"""
