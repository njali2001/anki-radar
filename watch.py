# -*- coding: utf-8 -*-
"""盯着几个官方页面上的数字，变了就在雷达首页上说一声。

【为什么需要它】：LeeAB 整个首页压在一个数字上——AnkiWeb 的 collection 上限
250 MB。2026 年 2 月起，Anki 的业务运营和开源治理由 Damien Elmes 移交给了
AnkiHub（一家有产品团队和商业路线的公司），而那份 FAQ 上本来就写着
"a pricing system may be introduced"。**这个数字第一次有了会被人改动的理由。**

它变了不会有任何人通知你：官网照常打开，Anki 照常同步，只有你的首页在替一个
过时的事实做广告。所以这里每隔半天去看一眼。

【为什么放在雷达里，不放在 leeab 的部署前检查里】（2026-09-29 运营者定）：
部署前检查拦的是"这次改动有毛病"，而这件事和你改没改代码毫无关系——它是外部
世界在变。挂在部署上意味着"不部署就永远不知道"，而且会在赶时间的时候拦住你。
放在雷达首页上，刷新就看见，该什么时候改文案由人来定。

【只读，而且很轻】：一次 HTTP GET，不动 AI，不写任何东西到外面。默认 12 小时
才会真的去取一次，页面刷新得再勤也不会多发请求。
"""

import difflib
import hashlib
import html
import json
import re
import threading
import time

import sources

# --- 盯什么 ----------------------------------------------------------------
#
# 【字段抓不到本身就是信号】：如果哪天某个正则匹配不上了，说明那句话被改写过——
# 这正是要知道的事，所以抓不到记成 "?"，并且当成一次变化报出来，而不是忽略。
WATCHES = [
    {
        "key": "ankiweb-limits",
        "label": "AnkiWeb 容量上限",
        "url": "https://faqs.ankiweb.net/are-there-limits-on-file-sizes-on-ankiweb.html",
        "fields": [
            ("collection_zip", "collection 压缩后", r"compressed size of\s*([\d.]+\s*[MG]B)"),
            ("collection_raw", "collection 解压后", r"uncompressed size of\s*([\d.]+\s*[MG]B)"),
            ("media_file", "单个媒体文件", r"individual media files is limited to\s*([\d.]+\s*[MG]B)"),
        ],
        # 有没有出现这句话，本身就是一条信息
        "flags": [
            ("media_total_free", "媒体总量无上限", r"no limits? on the size of your media"),
            ("pricing_hint", "提到可能收费", r"pricing system may be introduced"),
        ],
        "headline": ("collection_raw", "collection_zip"),
    },
]

_busy = threading.Lock()


def _text_of(page):
    """HTML 转成可比较的纯文本。脚本、样式、标签、多余空白全去掉。

    【只留 <main>】：这几页是 mdBook 生成的，模板里带着一整套导航和快捷键说明
    （"Press ← or → to navigate…"）。那些字和我们盯的事毫无关系，可 mdBook 哪天
    升一次级就会全变——于是报一次"这一页改过"，而正文一个字没动。
    """
    main = re.search(r"(?is)<main[^>]*>(.*?)</main>", page)
    if main:
        page = main.group(1)
    page = re.sub(r"(?is)<(script|style|nav|footer)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?s)<!--.*?-->", " ", page)
    page = re.sub(r"(?s)<[^>]+>", " ", page)
    page = html.unescape(page)
    return re.sub(r"[ \t\r\f\v]+", " ", page).strip()


def _extract(spec, text):
    values = {}
    for name, _label, pattern in spec["fields"]:
        found = re.search(pattern, text, re.I)
        values[name] = re.sub(r"\s+", "", found.group(1)) if found else "?"
    for name, _label, pattern in spec.get("flags", []):
        values[name] = "yes" if re.search(pattern, text, re.I) else "no"
    return values


def _label_of(spec, name):
    for key, label, _ in spec["fields"]:
        if key == name:
            return label
    for key, label, _ in spec.get("flags", []):
        if key == name:
            return label
    return name


def _describe(spec, before, after):
    """哪些字段变了，说人话。"""
    lines = []
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name, "（没有）"), after.get(name, "（没有）")
        if old != new:
            lines.append(f"{_label_of(spec, name)}：{old} → {new}")
    return lines


def _sentence_diff(old_text, new_text, limit=3):
    """正文里增删了哪几句。用来判断"数字没变但话改了"到底要紧不要紧。"""
    def split(text):
        return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 25]

    out = []
    for line in difflib.unified_diff(split(old_text), split(new_text), n=0, lineterm=""):
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line[:1] in "+-":
            out.append(("加了：" if line[0] == "+" else "删了：") + line[1:].strip()[:160])
        if len(out) >= limit:
            break
    return out


def check_one(store, spec, user_agent, now=None):
    """真的去取一次，和上次比。返回 (state, note)。

    state: first 第一次记下 / same 没变 / text 只有措辞变 / values 数字变了 / error
    """
    now = int(now or time.time())
    try:
        raw = sources._get(spec["url"], user_agent, "text/html")
    except Exception as exc:  # noqa: BLE001 —— 网络什么都可能抛，都不该弄崩页面
        return "error", f"取不到：{exc}"

    text = _text_of(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
    values = _extract(spec, text)
    previous = store.get_watch(spec["key"])

    if not previous:
        store.save_watch(spec["key"], json.dumps(values, ensure_ascii=False), text,
                         now, None, "第一次记下")
        return "first", "第一次记下这一页的样子"

    old_values = json.loads(previous["values_json"] or "{}")
    changed = _describe(spec, old_values, values)
    if changed:
        note = "；".join(changed)
        store.save_watch(spec["key"], json.dumps(values, ensure_ascii=False), text,
                         now, now, note)
        return "values", note

    if previous["text"] != text:
        diff = _sentence_diff(previous["text"], text)
        note = "数字没变，但这一页改过：" + ("；".join(diff) if diff else "（改的是空白或格式）")
        store.save_watch(spec["key"], json.dumps(values, ensure_ascii=False), text,
                         now, now, note)
        return "text", note

    store.save_watch(spec["key"], previous["values_json"], text, now,
                     previous["changed_at"], previous["note"])
    return "same", "没变"


def status(store):
    """给页面用：每一项现在什么样，上次什么时候核对的，上次变化是什么。"""
    out = []
    for spec in WATCHES:
        row = store.get_watch(spec["key"]) or {}
        values = json.loads(row.get("values_json") or "{}")
        headline = " / ".join(values.get(k, "?") for k in spec["headline"]) if values else "还没核对过"
        # 【页面上要显示人话】：values 的键是代码里的字段名（collection_zip 之类），
        # 给人看的是"collection 压缩后"。yes/no 同理——它在代码里是标记，在页面上
        # 是一句判断。
        detail = [{"label": _label_of(spec, k),
                   "value": {"yes": "是", "no": "否"}.get(v, v)}
                  for k, v in values.items()]
        out.append({
            "key": spec["key"],
            "label": spec["label"],
            "url": spec["url"],
            "headline": headline,
            "values": values,
            "detail": detail,
            "checked_at": row.get("checked_at") or 0,
            "changed_at": row.get("changed_at"),
            "note": row.get("note") or "",
        })
    return out


def _interval(config):
    cfg = config.get("watch", {})
    return int(cfg.get("min_interval_minutes", 720)) * 60


def is_stale(store, config):
    if not config.get("watch", {}).get("enabled", True):
        return False
    oldest = min((store.get_watch(w["key"]) or {}).get("checked_at") or 0 for w in WATCHES)
    return time.time() - oldest > _interval(config)


def refresh_if_stale(store, config):
    """页面渲染时调这一下：过期了就在后台去核对，**立刻返回**。

    【不能在渲染里同步等网络】：那会让整页卡住好几秒，而这一项的新鲜度根本不值
    那个代价。人下次刷新就看见结果了——这也正是运营者要的用法。
    """
    if not is_stale(store, config):
        return False
    if not _busy.acquire(blocking=False):
        return False  # 已经有一个在跑

    def run():
        try:
            user_agent = config.get("user_agent", "anki-radar/1.0")
            for spec in WATCHES:
                check_one(store, spec, user_agent)
        finally:
            _busy.release()

    threading.Thread(target=run, daemon=True).start()
    return True


def busy():
    locked = _busy.locked()
    return locked
