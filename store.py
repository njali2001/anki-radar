# -*- coding: utf-8 -*-
"""本地存储：SQLite 一个文件，没有服务、没有依赖。

【去重靠数据库的唯一约束，不靠"先查一下在不在"】：每小时扫一轮，相邻两轮会大量
重叠；先查后写在并发或中断重跑时会写进两行，而 INSERT OR IGNORE 一句话就完事。

【"报告过"和"看过"分开记】：报告过的不再出现在下一份报告里（否则每天都是昨天那几条），
但它仍然留在库里可以翻——`--stats` 要靠它算哪个关键词只带来噪音。
"""

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    external_id TEXT PRIMARY KEY,
    source      TEXT NOT NULL DEFAULT 'reddit',
    kind        TEXT NOT NULL DEFAULT 'post',
    community   TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    author      TEXT NOT NULL DEFAULT '',
    permalink   TEXT NOT NULL,
    posted_at   INTEGER NOT NULL,          -- unix 秒
    found_at    INTEGER NOT NULL,
    matched     TEXT NOT NULL DEFAULT '',  -- 逗号分隔
    reported_at INTEGER,                   -- 进过哪一次报告
    verdict     TEXT,                      -- 你自己标的：replied / ignored / junk
    ai_score    INTEGER,                   -- 0-3，AI 判断的相关度；NULL = 还没打过分
    ai_reason   TEXT                       -- AI 给的一句理由
);
CREATE INDEX IF NOT EXISTS posts_reported ON posts (reported_at);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- 【盯着别人页面上的数字】：见 watch.py。存的是"上一次看到的样子"，
-- 下一次核对拿它来比。text 整篇留着，是为了能说出"改的是哪一句"。
CREATE TABLE IF NOT EXISTS watch (
    key        TEXT PRIMARY KEY,
    values_json TEXT NOT NULL DEFAULT '{}',
    text       TEXT NOT NULL DEFAULT '',
    checked_at INTEGER NOT NULL DEFAULT 0,
    changed_at INTEGER,
    note       TEXT NOT NULL DEFAULT ''
);
-- 【我们自己那条片子的数字】：见 video.py。和 watch 不同，这里要的是趋势，
-- 所以一天一行；同一天重复取就覆盖，否则"趋势"会取决于你那天刷了几次页面。
-- 存的是 YouTube 给的【累计数】，日增量在读的时候算——存原始值才有可能
-- 事后重算，存算好的差值就没了。
CREATE TABLE IF NOT EXISTS video_stats (
    video_id   TEXT NOT NULL,
    day        TEXT NOT NULL,            -- YYYY-MM-DD，本地时区
    title      TEXT NOT NULL DEFAULT '',
    views      INTEGER NOT NULL DEFAULT 0,
    likes      INTEGER NOT NULL DEFAULT 0,
    comments   INTEGER NOT NULL DEFAULT 0,
    fetched_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (video_id, day)
);
-- 【频道主数据】：见 analytics.py。和 video_stats 分开存，因为口径不同——
-- video_stats 是接近实时的累计数，这里是滞后 24-48 小时的窗口汇总。
-- 混在一张表里，日后一定会有人把两个 views 相减。
CREATE TABLE IF NOT EXISTS video_analytics (
    video_id       TEXT PRIMARY KEY,
    window_days    INTEGER NOT NULL DEFAULT 30,
    views          INTEGER NOT NULL DEFAULT 0,
    minutes        INTEGER NOT NULL DEFAULT 0,
    avg_duration   INTEGER NOT NULL DEFAULT 0,   -- 秒
    avg_percentage REAL    NOT NULL DEFAULT 0,
    fetched_at     INTEGER NOT NULL DEFAULT 0
);
-- 流量来源 / 搜索词 / 国家。【整批替换，不累加】：这是"最近 30 天的排行"，
-- 累加的话窗口会越滑越长，而那不是要看的东西。
CREATE TABLE IF NOT EXISTS video_breakdown (
    video_id   TEXT NOT NULL,
    kind       TEXT NOT NULL,          -- traffic / search / country
    label      TEXT NOT NULL,
    views      INTEGER NOT NULL DEFAULT 0,
    fetched_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (video_id, kind, label)
);
CREATE INDEX IF NOT EXISTS posts_posted   ON posts (posted_at DESC);
"""


class Store:
    def __init__(self, path):
        self.path = Path(path)
        # 【check_same_thread=False + 一把锁】：网页版里 HTTP 线程在渲染页面，
        # 后台线程同时在写扫描结果。sqlite3 默认拒绝跨线程使用连接（报
        # "SQLite objects created in a thread can only be used in that same thread"），
        # 而为每个线程各开一个连接会让"刚写完却读不到"变成偶发问题。
        # 这个工具的并发量是个位数，一把锁最简单也最不会出错。
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # 【老库要补列】：这个文件在用户机器上，不能每加一个字段就让他删库重来。
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(posts)")}
        for column, ddl in (("ai_score", "INTEGER"), ("ai_reason", "TEXT"),
                            ("translation", "TEXT")):
            if column not in have:
                self.db.execute(f"ALTER TABLE posts ADD COLUMN {column} {ddl}")
        self.db.commit()

    def close(self):
        self.db.close()

    # --- 盯页面（watch.py 用）-------------------------------------------
    def get_watch(self, key):
        with self.lock:
            row = self.db.execute("SELECT * FROM watch WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None

    def save_watch(self, key, values_json, text, checked_at, changed_at, note):
        """【changed_at 只在真的变了的时候才动】。每次核对都刷新它的话，
        页面上那句"上次变化"就变成了"上次核对"，而这两件事的意义完全不同。"""
        with self.lock:
            self.db.execute(
                """INSERT INTO watch (key, values_json, text, checked_at, changed_at, note)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     values_json=excluded.values_json, text=excluded.text,
                     checked_at=excluded.checked_at, changed_at=excluded.changed_at,
                     note=excluded.note""",
                (key, values_json, text, checked_at, changed_at, note),
            )
            self.db.commit()

    # --- 自己那条片子的数字（video.py） -------------------------------

    def save_video_stats(self, video_id, day, title, views, likes, comments,
                         fetched_at):
        """一天一行，同一天再取就覆盖。"""
        with self.lock:
            self.db.execute(
                """INSERT INTO video_stats
                   (video_id, day, title, views, likes, comments, fetched_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(video_id, day) DO UPDATE SET
                     title=excluded.title, views=excluded.views,
                     likes=excluded.likes, comments=excluded.comments,
                     fetched_at=excluded.fetched_at""",
                (video_id, day, title, views, likes, comments, fetched_at),
            )
            self.db.commit()

    def video_series(self, video_id, limit=31):
        """按日期【从旧到新】，方便直接相邻相减算增量。"""
        with self.lock:
            rows = self.db.execute(
                """SELECT day, title, views, likes, comments, fetched_at
                   FROM video_stats WHERE video_id = ?
                   ORDER BY day DESC LIMIT ?""",
                (video_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def video_checked_at(self, video_id):
        with self.lock:
            row = self.db.execute(
                "SELECT MAX(fetched_at) AS t FROM video_stats WHERE video_id = ?",
                (video_id,),
            ).fetchone()
        return (row["t"] if row and row["t"] else 0) or 0

    # --- 频道主数据（analytics.py） -----------------------------------

    def save_video_analytics(self, video_id, window_days, views, minutes,
                             avg_duration, avg_percentage, fetched_at):
        with self.lock:
            self.db.execute(
                """INSERT INTO video_analytics
                   (video_id, window_days, views, minutes, avg_duration,
                    avg_percentage, fetched_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(video_id) DO UPDATE SET
                     window_days=excluded.window_days, views=excluded.views,
                     minutes=excluded.minutes, avg_duration=excluded.avg_duration,
                     avg_percentage=excluded.avg_percentage,
                     fetched_at=excluded.fetched_at""",
                (video_id, window_days, views, minutes, avg_duration,
                 avg_percentage, fetched_at),
            )
            self.db.commit()

    def get_video_analytics(self, video_id):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM video_analytics WHERE video_id = ?",
                (video_id,)).fetchone()
        return dict(row) if row else None

    def save_video_breakdown(self, video_id, rows, fetched_at):
        """【先删后插，一次事务】：排行会掉出榜单，只 upsert 的话掉下去的那些
        会永远留在表里，而页面上看不出它们是上一轮的。"""
        with self.lock:
            self.db.execute("DELETE FROM video_breakdown WHERE video_id = ?",
                            (video_id,))
            self.db.executemany(
                "INSERT INTO video_breakdown"
                " (video_id, kind, label, views, fetched_at) VALUES (?,?,?,?,?)",
                [(video_id, kind, label, views, fetched_at)
                 for kind, label, views in rows],
            )
            self.db.commit()

    def video_breakdown(self, video_id):
        with self.lock:
            rows = self.db.execute(
                "SELECT kind, label, views FROM video_breakdown"
                " WHERE video_id = ? ORDER BY kind, views DESC",
                (video_id,)).fetchall()
        return [dict(r) for r in rows]

    def add(self, item, matched, now):
        """写一行。之前见过就返回 False。

        【"见过"只按 external_id 算，一稿多投照样是多行】（2026-09-27 运营者定）。
        同一个人把同一篇贴到七八个版块，在榜单上就会出现七八条——这是有意的：
        按标题折叠固然干净，但两个人真的发了同名帖时会少看到一条，而看漏一个
        正卡着的人，比多划过几行重复的代价大。
        """
        with self.lock:
            cursor = self.db.execute(
                """INSERT OR IGNORE INTO posts
                   (external_id, source, kind, community, title, body, author,
                    permalink, posted_at, found_at, matched)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item["external_id"], item.get("source", "reddit"), item["kind"],
                    item["community"], item["title"][:500], item["body"][:20000],
                    item["author"][:128], item["permalink"], int(item["created_utc"]),
                    now, ",".join(matched),
                ),
            )
            self.db.commit()
            return cursor.rowcount == 1

    def set_meta(self, key, value):
        with self.lock:
            self.db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
            self.db.commit()

    def get_meta(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def unreported_by_source(self, source, limit):
        """某个源里还没进过报告的。【发帖的排在回帖的前面】，同类按时间新的在前。"""
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM posts WHERE reported_at IS NULL AND source = ?"
                " ORDER BY (kind = 'post') DESC, posted_at DESC LIMIT ?",
                (source, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def pending(self, source, limit):
        """某个源里还等着你处理的几条。

        【页面显示的是"还没处理的"，不是"最近扫到的"】（2026-09-20 运营者定）：
        点过【已处理】或【忽略】的就消失，没点的下次启动还在——所以关掉程序
        不会丢东西，也不会因为今天没看完就少了几条。

        【AI 刷掉的（verdict='ai_rejected'）不算】：它们留在库里给 --stats 用，
        但不该再占人的注意力。
        """
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM posts WHERE source = ? AND verdict IS NULL"
                " ORDER BY COALESCE(ai_score, -1) DESC, (kind = 'post') DESC, posted_at DESC"
                " LIMIT ?",
                (source, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def unscored_pending(self, source, limit):
        """还等着处理、而且还没打过分的。AI 只看这些。

        【判据是"有没有打过分"，不是"进没进过报告"】：页面显示的是待处理列表，
        两套标记各走各的，迟早会出现"它就在页面上，却永远轮不到打分"。
        """
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM posts WHERE source = ? AND verdict IS NULL AND ai_score IS NULL"
                " ORDER BY (kind = 'post') DESC, posted_at DESC LIMIT ?",
                (source, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get(self, external_id):
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM posts WHERE external_id = ?", (external_id,)
            ).fetchone()
            return dict(row) if row else None

    def all_for(self, source, limit=500):
        """库里这个源的全部记录（含已处理、已刷掉的）。

        【"试一下关键词"要的是全部，不是待处理的那几条】：被刷掉和已处理的同样
        是真实语料，拿它们试才看得出一个词到底逮不逮得住东西。
        """
        with self.lock:
            if source:
                rows = self.db.execute(
                    "SELECT title, body FROM posts WHERE source = ?"
                    " ORDER BY found_at DESC LIMIT ?", (source, limit)).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT title, body FROM posts ORDER BY found_at DESC LIMIT ?",
                    (limit,)).fetchall()
            return [dict(r) for r in rows]

    def set_verdict(self, external_id, value):
        """记下你对这一条的处理：done / ignored。"""
        with self.lock:
            self.db.execute(
                "UPDATE posts SET verdict = ? WHERE external_id = ?", (value, external_id)
            )
            self.db.commit()

    def pending_count(self, source):
        with self.lock:
            row = self.db.execute(
                "SELECT COUNT(*) AS n FROM posts WHERE source = ? AND verdict IS NULL",
                (source,),
            ).fetchone()
            return row["n"] or 0

    def last_batch(self, source, limit):
        """某个源最近一次报告里留下的那几条。

        【页面渲染的是"上一批"，不是"还没看过的"】：网页版上点一次 Reddit 刷新，
        整页会重画，如果按"还没看过的"去取，刚才那批论坛结果会因为已经标记过
        而整块消失——人会以为数据丢了。
        """
        with self.lock:
            row = self.db.execute(
                "SELECT MAX(reported_at) AS t FROM posts WHERE source = ? AND verdict IS NULL",
                (source,),
            ).fetchone()
            if not row or row["t"] is None:
                return []
            rows = self.db.execute(
                "SELECT * FROM posts WHERE source = ? AND reported_at = ? AND verdict IS NULL"
                " ORDER BY COALESCE(ai_score, -1) DESC, (kind = 'post') DESC, posted_at DESC"
                " LIMIT ?",
                (source, row["t"], limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def reconsider(self, source, minimum):
        """把分数够得上新门槛、却在旧门槛下被刷掉的放回待看。

        【放宽门槛要能追溯既往】：分数是当时就存下来的，一条 1 分的记录不会因为
        我们改了主意就变成 2 分；既然现在 1 分算数，它就该回到榜单里，而不是永远
        卡在"上一版的标准"里。重新打分要再花一次钱，也不会得到新东西。
        """
        with self.lock:
            cursor = self.db.execute(
                "UPDATE posts SET verdict = NULL, reported_at = NULL "
                "WHERE source = ? AND verdict = 'ai_rejected' AND ai_score >= ?",
                (source, minimum),
            )
            self.db.commit()
            return cursor.rowcount

    def mark_rejected(self, ids):
        """被 AI 判定不相关的：留在库里（--stats 要用），但不再出现在任何报告里。"""
        with self.lock:
            self.db.executemany(
                "UPDATE posts SET verdict = 'ai_rejected' WHERE external_id = ?",
                [(i,) for i in ids],
            )
            self.db.commit()

    def unreported(self, limit):
        """还没进过报告的，新的在前。

        【新的在前，不是命中数多的在前】：越新的帖子越可能还没被别人回答过，
        也越可能得到原帖作者的回应。命中三个关键词但发了两周的帖子，价值很低。
        """
        with self.lock:
            # 【发帖的排在回帖的前面】：提问的人还在等答案，而一条回复下面通常
            # 已经有人在答了。同为发帖时，新的在前。
            rows = self.db.execute(
                "SELECT * FROM posts WHERE reported_at IS NULL"
                " ORDER BY (kind = 'post') DESC, posted_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def untranslated_pending(self, source, limit):
        """还等着处理、还没翻译过的。translation 为 '' 表示"看过了，不用翻"。"""
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM posts WHERE source = ? AND verdict IS NULL AND translation IS NULL"
                " ORDER BY COALESCE(ai_score, -1) DESC, posted_at DESC LIMIT ?",
                (source, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def set_translations(self, translations):
        """{external_id: 中文译文}。【空串也要写】：它的意思是"本来就是中文或英文，
        不用翻"，不写的话下一轮又会拿去问一遍、白花额度。"""
        with self.lock:
            self.db.executemany(
                "UPDATE posts SET translation = ? WHERE external_id = ?",
                [(v, k) for k, v in translations.items()],
            )
            self.db.commit()

    def set_scores(self, scores):
        """写回 AI 的打分。{external_id: (score, reason)}"""
        with self.lock:
            self.db.executemany(
                "UPDATE posts SET ai_score = ?, ai_reason = ? WHERE external_id = ?",
                [(v[0], v[1], k) for k, v in scores.items()],
            )
            self.db.commit()

    def mark_reported(self, ids, now):
        with self.lock:
            self.db.executemany(
                "UPDATE posts SET reported_at = ? WHERE external_id = ?",
                [(now, i) for i in ids],
            )
            self.db.commit()

    def last_report(self, limit):
        """上一次报告里的那几条（用于 --again）。"""
        with self.lock:
            row = self.db.execute("SELECT MAX(reported_at) AS t FROM posts").fetchone()
            if not row or row["t"] is None:
                return []
            rows = self.db.execute(
                "SELECT * FROM posts WHERE reported_at = ? ORDER BY posted_at DESC LIMIT ?",
                (row["t"], limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def keyword_stats(self):
        """每个关键词带来了多少条。噪音大的那个该换掉。"""
        with self.lock:
            counts = {}
            for row in self.db.execute("SELECT matched FROM posts"):
                for keyword in (row["matched"] or "").split(","):
                    keyword = keyword.strip()
                    if keyword:
                        counts[keyword] = counts.get(keyword, 0) + 1
            return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def keyword_details(self):
        """每个关键词：命中多少条、最近一次是什么时候。

        【"0 次"不够用】：一个从没命中过的词，可能是写法不对（sincroniz 少了
        星号），也可能是确实没人这么说。加上"最近一次"，半年前还灵、现在哑了
        的词也能看出来——那多半是对面换了说法。
        """
        with self.lock:
            detail = {}
            for row in self.db.execute("SELECT matched, found_at FROM posts"):
                for keyword in (row["matched"] or "").split(","):
                    keyword = keyword.strip()
                    if not keyword:
                        continue
                    count, last = detail.get(keyword, (0, 0))
                    detail[keyword] = (count + 1, max(last, int(row["found_at"] or 0)))
            return detail

    def totals(self):
        with self.lock:
            row = self.db.execute(
                "SELECT COUNT(*) AS all_rows,"
                " SUM(CASE WHEN reported_at IS NULL THEN 1 ELSE 0 END) AS pending"
                " FROM posts"
            ).fetchone()
            return {"all": row["all_rows"] or 0, "pending": row["pending"] or 0}
