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

    def add(self, item, matched, now):
        """写一行。之前见过就返回 False。"""
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

    def totals(self):
        with self.lock:
            row = self.db.execute(
                "SELECT COUNT(*) AS all_rows,"
                " SUM(CASE WHEN reported_at IS NULL THEN 1 ELSE 0 END) AS pending"
                " FROM posts"
            ).fetchone()
            return {"all": row["all_rows"] or 0, "pending": row["pending"] or 0}
