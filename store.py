# -*- coding: utf-8 -*-
"""本地存储：SQLite 一个文件，没有服务、没有依赖。

【去重靠数据库的唯一约束，不靠"先查一下在不在"】：每小时扫一轮，相邻两轮会大量
重叠；先查后写在并发或中断重跑时会写进两行，而 INSERT OR IGNORE 一句话就完事。

【"报告过"和"看过"分开记】：报告过的不再出现在下一份报告里（否则每天都是昨天那几条），
但它仍然留在库里可以翻——`--stats` 要靠它算哪个关键词只带来噪音。
"""

import sqlite3
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
    verdict     TEXT                       -- 你自己标的：replied / ignored / junk
);
CREATE INDEX IF NOT EXISTS posts_reported ON posts (reported_at);
CREATE INDEX IF NOT EXISTS posts_posted   ON posts (posted_at DESC);
"""


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    def add(self, item, matched, now):
        """写一行。之前见过就返回 False。"""
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

    def unreported(self, limit):
        """还没进过报告的，新的在前。

        【新的在前，不是命中数多的在前】：越新的帖子越可能还没被别人回答过，
        也越可能得到原帖作者的回应。命中三个关键词但发了两周的帖子，价值很低。
        """
        # 【发帖的排在回帖的前面】：提问的人还在等答案，而一条回复下面通常
        # 已经有人在答了。同为发帖时，新的在前。
        rows = self.db.execute(
            "SELECT * FROM posts WHERE reported_at IS NULL"
            " ORDER BY (kind = 'post') DESC, posted_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_reported(self, ids, now):
        self.db.executemany(
            "UPDATE posts SET reported_at = ? WHERE external_id = ?",
            [(now, i) for i in ids],
        )
        self.db.commit()

    def last_report(self, limit):
        """上一次报告里的那几条（用于 --again）。"""
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
        counts = {}
        for row in self.db.execute("SELECT matched FROM posts"):
            for keyword in (row["matched"] or "").split(","):
                keyword = keyword.strip()
                if keyword:
                    counts[keyword] = counts.get(keyword, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def totals(self):
        row = self.db.execute(
            "SELECT COUNT(*) AS all_rows,"
            " SUM(CASE WHEN reported_at IS NULL THEN 1 ELSE 0 END) AS pending"
            " FROM posts"
        ).fetchone()
        return {"all": row["all_rows"] or 0, "pending": row["pending"] or 0}
