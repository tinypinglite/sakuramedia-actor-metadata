import sqlite3

from sakuramedia_actor_metadata.state import SCHEMA_VERSION, State


def test_source_upgrade_rebuilds_old_state_table(tmp_path):
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TABLE actors (
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
        db.execute("""
            INSERT INTO actors (actor_id, javdb_id, status, attempts)
            VALUES (1, 'old-id', 'stopped', 3)
        """)
        db.execute("PRAGMA user_version = 1")

    state = State(path)
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(actors)")}
        row_count = db.execute("SELECT count(*) FROM actors").fetchone()[0]
        version = db.execute("PRAGMA user_version").fetchone()[0]

    assert version == SCHEMA_VERSION
    assert {"identity_key", "javbus_ref"} <= columns
    assert not {"javdb_id", "minnanoav_ref"} & columns
    assert row_count == 0

    row = state.get(1, "葵つかさ")
    assert (row["identity_key"], row["javbus_ref"], row["status"]) == (
        "葵つかさ", "", "pending",
    )


def test_upgrade_requeues_rows_the_old_matching_rules_gave_up_on(tmp_path):
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TABLE actors (
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
        db.executemany(
            "INSERT INTO actors (actor_id, status, attempts, next_attempt_at, reason, javbus_ref)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, "stopped", 3, 999.0, "javbus:ambiguous", "/star/1ma"),
                (2, "stopped", 2, 999.0, "javbus:not_found", ""),
                (3, "stopped", 1, 999.0, "javbus:identity_mismatch;javbus:not_found", ""),
                (4, "stopped", 2, 999.0, "javbus:empty_profile", "/star/2xi"),
                (5, "completed", 0, None, "complete", "/star/2xi"),
                (6, "stopped", 1, None, "actor_changed_or_deleted", ""),
                (7, "waiting", 1, 100.0, "javbus:not_found", ""),
                (8, "stopped", 2, None, "javbus:ok;patch_conflict", ""),
            ],
        )
        db.execute("PRAGMA user_version = 2")

    State(path)

    with sqlite3.connect(path) as db:
        rows = {
            row[0]: row[1:]
            for row in db.execute(
                "SELECT actor_id, status, attempts, next_attempt_at, reason, javbus_ref FROM actors"
            )
        }
        version = db.execute("PRAGMA user_version").fetchone()[0]

    assert version == SCHEMA_VERSION
    assert rows[1] == ("waiting", 0, None, "", "/star/1ma")
    assert rows[2] == ("waiting", 0, None, "", "")
    assert rows[3] == ("waiting", 0, None, "", "")
    assert rows[4] == ("waiting", 0, None, "", "/star/2xi")
    # 完成、身份变更、非终态和不属于搜索结果的失败保持原样。
    assert rows[5] == ("completed", 0, None, "complete", "/star/2xi")
    assert rows[6] == ("stopped", 1, None, "actor_changed_or_deleted", "")
    assert rows[7] == ("waiting", 1, 100.0, "javbus:not_found", "")
    assert rows[8] == ("stopped", 2, None, "javbus:ok;patch_conflict", "")


def test_upgrade_requeue_runs_only_once(tmp_path):
    path = tmp_path / "state.sqlite3"
    State(path)

    with sqlite3.connect(path) as db:
        db.execute("""
            INSERT INTO actors (actor_id, status, attempts, reason)
            VALUES (1, 'stopped', 3, 'javbus:ambiguous')
        """)

    State(path)

    with sqlite3.connect(path) as db:
        status, attempts = db.execute(
            "SELECT status, attempts FROM actors WHERE actor_id = 1"
        ).fetchone()
    assert (status, attempts) == ("stopped", 3)


def test_identity_change_resets_terminal_state(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    row = state.get(1, "旧名字")
    row.update(
        status="stopped",
        attempts=3,
        javbus_ref="/star/old",
        missing_fields=["height_cm"],
        reason="javbus:not_found",
        sources={"height_cm": {"url": "old"}},
    )
    state.save(row)

    reset = state.get(1, "新名字")
    assert (reset["status"], reset["attempts"], reset["javbus_ref"]) == (
        "pending", 0, "",
    )
    assert reset["missing_fields"] == []
    assert reset["sources"] == {}


def test_save_persists_javbus_ref_and_field_provenance(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    row = state.get(1, "葵つかさ")
    row.update(
        status="waiting",
        attempts=1,
        javbus_ref="/star/2xi",
        next_attempt_at=123,
        missing_fields=["cup"],
        reason="javbus:ok",
        sources={"height_cm": {"url": "/star/2xi", "fetched_at": 1}},
    )
    state.save(row)

    saved = State(tmp_path / "state.sqlite3").get(1, "葵つかさ")
    assert saved["javbus_ref"] == "/star/2xi"
    assert saved["next_attempt_at"] == 123
    assert saved["sources"]["height_cm"]["url"] == "/star/2xi"
