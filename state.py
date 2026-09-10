"""每位演员的有限重试状态与一次性资料来源迁移。"""

import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


class State:
    def __init__(self, path: Path):
        self.path = path
        self.requeued_count = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                CREATE TABLE IF NOT EXISTS actors (
                    actor_id INTEGER PRIMARY KEY,
                    javdb_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at REAL,
                    next_attempt_at REAL,
                    missing_fields TEXT NOT NULL DEFAULT '[]',
                    reason TEXT NOT NULL DEFAULT '',
                    minnanoav_ref TEXT NOT NULL DEFAULT '',
                    sources TEXT NOT NULL DEFAULT '{}'
                )
            """)
            # user_version 记录资料来源迁移版本，与插件发版号独立。
            if db.execute("PRAGMA user_version").fetchone()[0] < 1:
                columns = {row["name"] for row in db.execute("PRAGMA table_info(actors)")}
                if "minnanoav_ref" not in columns:
                    db.execute("ALTER TABLE actors ADD COLUMN minnanoav_ref TEXT NOT NULL DEFAULT ''")
                # 换源后给尚未补齐的旧来源记录重新尝试的机会，保留资料来源审计。
                self.requeued_count = db.execute("""
                    UPDATE actors SET status='pending', attempts=0, last_attempt_at=NULL,
                        next_attempt_at=NULL, reason='source_upgraded'
                    WHERE status IN ('waiting', 'stopped') AND (
                        reason LIKE '%javbus:%' OR reason LIKE '%javdb:%'
                        OR reason IN ('no_data', 'interrupted', 'attempt_limit')
                    )
                """).rowcount
                db.execute("PRAGMA user_version = 1")

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def get(self, actor_id, javdb_id):
        with self.connect() as db:
            db.execute("""
                INSERT INTO actors (actor_id, javdb_id) VALUES (?, ?)
                ON CONFLICT(actor_id) DO NOTHING
            """, (actor_id, javdb_id))
            row = dict(db.execute("SELECT * FROM actors WHERE actor_id = ?", (actor_id,)).fetchone())
            if row["javdb_id"] != javdb_id:
                # 宿主恢复/重建后复用数字 ID 时，不能复用其他演员的终态和来源映射。
                db.execute("DELETE FROM actors WHERE actor_id = ?", (actor_id,))
                db.execute("INSERT INTO actors (actor_id, javdb_id) VALUES (?, ?)", (actor_id, javdb_id))
                row = dict(db.execute("SELECT * FROM actors WHERE actor_id = ?", (actor_id,)).fetchone())
        row["missing_fields"] = json.loads(row["missing_fields"])
        row["sources"] = json.loads(row["sources"])
        return row

    def save(self, row):
        with self.connect() as db:
            db.execute("""
                UPDATE actors SET status=?, attempts=?, last_attempt_at=?, next_attempt_at=?,
                    missing_fields=?, reason=?, minnanoav_ref=?, sources=? WHERE actor_id=?
            """, (
                row["status"], row["attempts"], row["last_attempt_at"], row["next_attempt_at"],
                json.dumps(row["missing_fields"]), row["reason"], row["minnanoav_ref"],
                json.dumps(row["sources"], ensure_ascii=False), row["actor_id"],
            ))
