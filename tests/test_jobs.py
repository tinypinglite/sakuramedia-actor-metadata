from dataclasses import replace
from types import SimpleNamespace

import pytest
from sakuramedia_actor_metadata.jobs import actor_identity_key, process
from sakuramedia_actor_metadata.settings import Settings
from sakuramedia_actor_metadata.sources import Profile, SourceError
from sakuramedia_actor_metadata.state import State
from src.plugins.types import ActorPage, ActorSnapshot, MoviePage, MovieSnapshot

FULL_VALUES = {
    "birthday": "1990-08-14",
    "height_cm": 163,
    "bust_cm": 88,
    "waist_cm": 58,
    "hips_cm": 86,
    "cup": "E",
    "birthplace": "大阪府",
}


def actor(actor_id=1, **values):
    return ActorSnapshot(actor_id, 0, {
        "name": "葵つかさ",
        "alias_name": "",
        "gender": 1,
        "is_subscribed": False,
        **FULL_VALUES,
        **values,
    }, {})


class Actors:
    def __init__(self, rows):
        self.rows = {row.actor_id: row for row in rows}
        self.patches = []
        self.conflict = False

    def list_page(self, *, after_id, limit):
        rows = sorted(
            (row for row in self.rows.values() if row.actor_id > after_id),
            key=lambda row: row.actor_id,
        )
        return ActorPage(
            tuple(rows[:limit]),
            rows[limit - 1].actor_id if len(rows) > limit else None,
        )

    def get(self, actor_id):
        return self.rows.get(actor_id)

    def patch(self, actor_id, fields, expected_revision):
        row = self.rows[actor_id]
        if self.conflict or row.revision != expected_revision:
            return False
        assert all(row.values.get(key) in (None, "") for key in fields)
        assert all(row.owners.get(key) in (None, "plugin:demo") for key in fields)
        self.patches.append((actor_id, fields))
        self.rows[actor_id] = replace(
            row,
            revision=row.revision + 1,
            values={**row.values, **fields},
            owners={**row.owners, **dict.fromkeys(fields, "plugin:demo")},
        )
        return True


class Movies:
    def __init__(self, rows=()):
        self.rows = tuple(rows)

    def list_page(self, *, after_id, limit):
        rows = sorted(
            (row for row in self.rows if row.movie_id > after_id),
            key=lambda row: row.movie_id,
        )
        return MoviePage(
            tuple(rows[:limit]),
            rows[limit - 1].movie_id if len(rows) > limit else None,
        )


class Source:
    def __init__(self, profile=None, error=None):
        self.profile = profile or Profile()
        self.error = error
        self.calls = []

    def javbus(self, names, ref=""):
        self.calls.append((tuple(names), ref))
        if self.error is not None:
            raise self.error
        return self.profile

    def close(self):
        pass


@pytest.fixture
def setup(tmp_path):
    context = SimpleNamespace(
        plugin_id="demo",
        actors=Actors([actor()]),
        movies=Movies(),
    )
    return context, State(tmp_path / "state.sqlite3"), Settings()


def missing(actor_id=1, **values):
    empty = {
        key: None
        for key in (
            "birthday", "height_cm", "bust_cm", "waist_cm",
            "hips_cm", "cup", "birthplace",
        )
    }
    return actor(actor_id, **empty, **values)


def test_complete_actor_is_skipped_without_source(setup):
    context, state, settings = setup
    source = Source()

    stats = process(context, settings, state, source, now=lambda: 0)

    assert stats["completed"] == 1
    assert source.calls == []
    assert state.get(1, actor_identity_key(context.actors.get(1)))["status"] == "completed"


def test_only_empty_unowned_profile_fields_are_written(setup):
    context, state, settings = setup
    context.actors = Actors([missing()])
    context.actors.rows[1] = replace(
        context.actors.rows[1],
        values={**context.actors.rows[1].values, "bust_cm": 90},
        owners={"bust_cm": "plugin:other"},
    )
    source = Source(Profile({
        "height_cm": 163,
        "bust_cm": 99,
        "cup": "F",
        "blood_type": "A",
        "gender": 2,
    }, ref="/star/2xi"))

    stats = process(context, settings, state, source, now=lambda: 0)

    assert stats["updated"] == 1
    assert context.actors.patches == [(1, {"height_cm": 163, "cup": "F"})]
    assert context.actors.get(1).values["gender"] == 1
    assert context.actors.get(1).values["bust_cm"] == 90


def test_partial_profile_retries_with_backoff_then_stops(setup):
    context, state, settings = setup
    context.actors = Actors([missing()])
    source = Source(Profile({"height_cm": 163}, ref="/star/2xi"))

    process(context, settings, state, source, now=lambda: 0)
    row = state.get(1, actor_identity_key(context.actors.get(1)))
    assert (row["status"], row["attempts"], row["next_attempt_at"]) == (
        "waiting", 1, 3 * 86400,
    )

    process(context, settings, state, source, now=lambda: 3 * 86400)
    process(context, settings, state, source, now=lambda: 6 * 86400)
    row = state.get(1, actor_identity_key(context.actors.get(1)))
    assert (row["status"], row["attempts"]) == ("stopped", 3)
    assert len(source.calls) == 3


@pytest.mark.parametrize("outcome", ["not_found", "ambiguous", "identity_mismatch", "empty_profile"])
def test_terminal_profile_outcomes_stop_immediately(setup, outcome):
    context, state, settings = setup
    context.actors = Actors([missing()])
    source = Source(Profile(outcome=outcome))

    stats = process(context, settings, state, source, now=lambda: 0)

    row = state.get(1, actor_identity_key(context.actors.get(1)))
    assert stats["stopped"] == 1
    assert (row["status"], row["attempts"]) == ("stopped", 1)
    assert row["reason"] == f"javbus:{outcome}"
    process(context, settings, state, source, now=lambda: 9999999)
    assert len(source.calls) == 1


def test_network_error_waits_and_does_not_stop_immediately(setup):
    context, state, settings = setup
    context.actors = Actors([missing()])
    source = Source(error=SourceError("javbus:network_error"))

    process(context, settings, state, source, now=lambda: 0)
    row = state.get(1, actor_identity_key(context.actors.get(1)))
    assert (row["status"], row["attempts"]) == ("waiting", 1)
    assert row["next_attempt_at"] == 3 * 86400

    process(context, settings, state, source, now=lambda: 3 * 86400 - 1)
    assert len(source.calls) == 1


def test_identity_change_resets_terminal_state_and_searches_again(setup):
    context, state, settings = setup
    context.actors = Actors([missing()])
    source = Source(Profile(outcome="not_found"))
    process(context, settings, state, source, now=lambda: 0)

    context.actors.rows[1] = replace(
        context.actors.rows[1],
        values={**context.actors.rows[1].values, "alias_name": "三上悠亜"},
    )
    source.profile = Profile({"height_cm": 163}, ref="/star/2xi")
    process(context, settings, state, source, now=lambda: 1)

    assert len(source.calls) == 2
    assert state.get(1, actor_identity_key(context.actors.get(1)))["status"] == "waiting"


def test_patch_conflict_is_reported_without_counting_update(setup):
    context, state, settings = setup
    context.actors = Actors([missing()])
    context.actors.conflict = True
    source = Source(Profile({"height_cm": 163}, ref="/star/2xi"))

    stats = process(context, settings, state, source, now=lambda: 0)

    assert stats["updated"] == 0
    row = state.get(1, actor_identity_key(context.actors.get(1)))
    assert row["status"] == "waiting"
    assert "patch_conflict" in row["reason"]


def test_manual_owner_blocks_write_without_fetching(setup):
    context, state, settings = setup
    context.actors = Actors([missing()])
    context.actors.rows[1] = replace(
        context.actors.rows[1],
        values={
            **{key: None for key in FULL_VALUES},
            "name": "葵つかさ",
            "alias_name": "",
            "gender": 1,
            "is_subscribed": False,
        },
        owners={key: "plugin:other" for key in FULL_VALUES},
    )
    source = Source()

    stats = process(context, settings, state, source, now=lambda: 0)

    assert stats["stopped"] == 1
    assert source.calls == []


def test_subscribed_only_and_male_actors_are_not_fetched(setup):
    context, state, settings = setup
    context.actors = Actors([
        missing(1, is_subscribed=False),
        missing(2, is_subscribed=True),
        missing(3, gender=2),
    ])
    source = Source(Profile({
        **FULL_VALUES,
    }, ref="/star/2xi"))

    stats = process(
        context,
        settings.model_copy(update={"subscribed_only": True}),
        state,
        source,
        now=lambda: 0,
    )

    assert stats["skipped"] == 2
    assert len(source.calls) == 1
    assert context.actors.get(2).values["height_cm"] == 163


def test_subscription_then_movie_count_priority(setup):
    context, state, settings = setup
    first = missing(1)
    second = missing(2, is_subscribed=True)
    third = missing(3)
    context.actors = Actors([first, second, third])
    context.movies = Movies([
        MovieSnapshot(1, 0, {"is_collection": False}, {}, actors=(first,)),
        MovieSnapshot(2, 0, {"is_collection": False}, {}, actors=(third, first)),
        MovieSnapshot(3, 0, {"is_collection": False}, {}, actors=(first,)),
    ])
    source = Source(Profile({"height_cm": 163}, ref="/star/2xi"))

    process(context, settings, state, source, now=lambda: 0)

    assert [actor_id for actor_id, _ in context.actors.patches] == [2, 1, 3]


def test_three_consecutive_network_errors_abort_round(setup):
    context, state, settings = setup
    context.actors = Actors([missing(1), missing(2), missing(3)])
    source = Source(error=SourceError("javbus:network_error"))

    stats = process(context, settings, state, source, now=lambda: 0)

    assert stats["aborted"] == 1
    assert len(source.calls) == 3


def test_verification_aborts_immediately(setup):
    context, state, settings = setup
    context.actors = Actors([missing(1), missing(2)])
    source = Source(error=SourceError("javbus:verification_required"))

    stats = process(context, settings, state, source, now=lambda: 0)

    assert stats["aborted"] == 1
    assert len(source.calls) == 1
