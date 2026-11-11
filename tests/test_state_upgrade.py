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
