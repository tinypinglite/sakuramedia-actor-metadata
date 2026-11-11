"""每位演员的有限重试状态与 JavBus 映射缓存。"""

import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

SCHEMA_VERSION = 2


class State:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version < SCHEMA_VERSION:
                # 资料来源和身份键都已变化，旧重试状态不再有复用价值。
                db.execute("DROP TABLE IF EXISTS actors")
            db.execute("""
                CREATE TABLE IF NOT EXISTS actors (
                    actor_id INTEGER PRIMARY KEY,
                    identity_key TEXT NOT NULL DEFAULT '',
                    javbus_ref TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at REAL,
                    next_attempt_at REAL,
                    missing_fields TEXT NOT NULL DEFAULT '[]',
                    reason TEXT NOT NULL DEFAULT '',
                    sources TEXT NOT NULL DEFAULT '{}'
                )
            """)
            if version < SCHEMA_VERSION:
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def get(self, actor_id: int, identity_key: str):
        with self.connect() as db:
            db.execute("""
                INSERT INTO actors (actor_id, identity_key) VALUES (?, ?)
                ON CONFLICT(actor_id) DO NOTHING
            """, (actor_id, identity_key))
            row = dict(db.execute(
                "SELECT * FROM actors WHERE actor_id = ?", (actor_id,)
            ).fetchone())
            if row["identity_key"] != identity_key:
                # 宿主恢复/重建后复用数字 ID，或姓名/别名变化时，不能复用旧终态。
                db.execute("DELETE FROM actors WHERE actor_id = ?", (actor_id,))
                db.execute(
                    "INSERT INTO actors (actor_id, identity_key) VALUES (?, ?)",
                    (actor_id, identity_key),
                )
                row = dict(db.execute(
                    "SELECT * FROM actors WHERE actor_id = ?", (actor_id,)
                ).fetchone())
        row["missing_fields"] = json.loads(row["missing_fields"])
        row["sources"] = json.loads(row["sources"])
        return row

    def save(self, row):
        with self.connect() as db:
            db.execute("""
                UPDATE actors SET identity_key=?, javbus_ref=?, status=?, attempts=?,
                    last_attempt_at=?, next_attempt_at=?, missing_fields=?, reason=?,
                    sources=? WHERE actor_id=?
            """, (
                row["identity_key"],
                row["javbus_ref"],
                row["status"],
                row["attempts"],
                row["last_attempt_at"],
                row["next_attempt_at"],
                json.dumps(row["missing_fields"]),
                row["reason"],
                json.dumps(row["sources"], ensure_ascii=False),
                row["actor_id"],
            ))
