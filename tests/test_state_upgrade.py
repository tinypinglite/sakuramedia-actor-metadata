import sqlite3

import pytest
from sakuramedia_actor_metadata.state import State


def test_source_upgrade_requeues_unresolved_once_and_preserves_data(tmp_path):
    path = tmp_path / "state.sqlite3"
    state = State(path)
    cases = [
        ("waiting", "javdb:ok;javbus:not_found", True),
        ("stopped", "javbus:search_forbidden", True),
        ("stopped", "javbus:identity_mismatch", True),
        ("waiting", "javbus:http_403", True),
        ("stopped", "javbus:ok", True),
        ("completed", "complete", False),
        ("stopped", "fields_not_writable", False),
        ("stopped", "actor_changed_or_deleted", False),
    ]
    for actor_id, (status, reason, _) in enumerate(cases, 1):
        row = state.get(actor_id, str(actor_id))
        row.update(status=status, reason=reason, attempts=3, next_attempt_at=9999999999,
                   missing_fields=["cup"], sources={"height_cm": {"url": "old-source"}})
        state.save(row)
    # 还原上一发布版本的真实列布局与迁移版本。
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE actors RENAME COLUMN minnanoav_ref TO javbus_ref")
        db.execute("UPDATE actors SET javbus_ref='/star/abc'")
        db.execute("PRAGMA user_version=0")
    upgraded = State(path)
    assert upgraded.requeued_count == 5
    for actor_id, (status, reason, requeued) in enumerate(cases, 1):
        row = upgraded.get(actor_id, str(actor_id))
        assert row["status"] == ("pending" if requeued else status)
        assert row["attempts"] == (0 if requeued else 3)
        assert row["next_attempt_at"] == (None if requeued else 9999999999)
        assert row["missing_fields"] == ["cup"]
        assert row["sources"] == {"height_cm": {"url": "old-source"}}
        assert row["minnanoav_ref"] == ""
    row = upgraded.get(1, "1")
    row.update(status="waiting", attempts=1, next_attempt_at=123, reason="minnanoav:not_found")
    upgraded.save(row)
    second = State(path)
    assert second.requeued_count == 0
    restarted = second.get(1, "1")
    assert restarted["attempts"] == 1 and restarted["next_attempt_at"] == 123
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1


def test_failed_upgrade_rolls_back_schema_and_retry_state(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite3"
    state = State(path)
    row = state.get(1, "id1")
    row.update(status="stopped", attempts=3, reason="javbus:not_found")
    state.save(row)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE actors RENAME COLUMN minnanoav_ref TO javbus_ref")
        db.execute("PRAGMA user_version=0")
    real_connect = sqlite3.connect

    def fail_version_commit(*args, **kwargs):
        db = real_connect(*args, **kwargs)
        db.set_authorizer(lambda action, first, second, *_:
                          sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_PRAGMA
                          and first == "user_version" and second == "1" else sqlite3.SQLITE_OK)
        return db

    monkeypatch.setattr(sqlite3, "connect", fail_version_commit)
    with pytest.raises(sqlite3.DatabaseError):
        State(path)
    with real_connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("SELECT status, attempts FROM actors").fetchone() == ("stopped", 3)
        assert "minnanoav_ref" not in {r[1] for r in db.execute("PRAGMA table_info(actors)")}
