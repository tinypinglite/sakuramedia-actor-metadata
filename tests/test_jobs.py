from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from sakuramedia_actor_metadata.jobs import process, run
from sakuramedia_actor_metadata.settings import Settings
from sakuramedia_actor_metadata.sources import Profile, SourceError
from sakuramedia_actor_metadata.sources import Sources as HttpSources
from sakuramedia_actor_metadata.state import State
from src.plugins.types import ActorPage, ActorSnapshot, MoviePage, MovieSnapshot


def actor(actor_id=1, **values):
    return ActorSnapshot(actor_id, 0, {
        "javdb_id": f"id{actor_id}", "name": "演员", "alias_name": "", "gender": 1,
        **values,
    }, {})


class Actors:
    def __init__(self, rows):
        self.rows = {r.actor_id: r for r in rows}
        self.patches = []
        self.conflict = False

    def list_page(self, *, after_id, limit):
        rows = sorted((r for r in self.rows.values() if r.actor_id > after_id), key=lambda r: r.actor_id)
        return ActorPage(tuple(rows[:limit]), rows[limit - 1].actor_id if len(rows) > limit else None)

    def get(self, actor_id):
        return self.rows.get(actor_id)

    def patch(self, actor_id, fields, expected_revision):
        row = self.rows[actor_id]
        if self.conflict or row.revision != expected_revision:
            return False
        assert all(row.values.get(k) in (None, "", 0) for k in fields)
        assert all(row.owners.get(k) in (None, "plugin:demo") for k in fields)
        self.patches.append(fields)
        self.rows[actor_id] = replace(row, revision=row.revision + 1,
                                      values={**row.values, **fields},
                                      owners={**row.owners, **dict.fromkeys(fields, "plugin:demo")})
        return True


class Movies:
    def __init__(self, rows=()):
        self.rows = rows
        self.calls = []

    def list_page(self, *, after_id, limit):
        self.calls.append(after_id)
        rows = sorted((r for r in self.rows if r.movie_id > after_id), key=lambda r: r.movie_id)
        # 小页验证插件会跨页汇总，而不是只统计第一页。
        limit = min(limit, 3)
        return MoviePage(tuple(rows[:limit]), rows[limit - 1].movie_id if len(rows) > limit else None)


def movie(movie_id, actors, **values):
    return MovieSnapshot(movie_id, 0, {
        "is_collection": False, "watched_count": 0, "want_watch_count": 0,
        "comment_count": 0, "score_number": 0, **values,
    }, {}, tuple(actors))


class Sources:
    def __init__(self, db=None, minnano=None):
        self.db = db or Profile()
        self.minnano = minnano or Profile(outcome="not_found")
        self.calls = []

    def javdb(self, jid):
        self.calls.append(("javdb", jid))
        if isinstance(self.db, Exception):
            raise self.db
        return self.db

    def minnanoav(self, names, ref, **kwargs):
        self.calls.append(("minnanoav", ref))
        if isinstance(self.minnano, Exception):
            raise self.minnano
        return self.minnano

    def close(self):
        pass


@pytest.fixture
def setup(tmp_path):
    ctx = SimpleNamespace(plugin_id="demo", actors=Actors([actor()]), movies=Movies())
    return ctx, State(tmp_path / "state.sqlite3"), Settings(target_fields=("height_cm", "cup"))


def test_complete_stops_forever_and_does_not_fetch_second_source(setup):
    ctx, state, settings = setup
    source = Sources(Profile({"height_cm": 160, "cup": "E"}, ref="javdb"))
    stats = process(ctx, settings, state, source, now=lambda: 0)
    assert stats["completed"] == 1
    assert source.calls == [("javdb", "id1")]
    assert state.get(1, "id1")["sources"]["cup"]["url"] == "javdb"
    ctx.actors.rows[1] = actor()  # 即使后来被清空，也不自动解除终态。
    process(ctx, settings, State(state.path), source, now=lambda: 9999999)
    assert len(source.calls) == 1


def test_unknown_gender_is_written_from_javdb(setup):
    ctx, state, _ = setup
    ctx.actors = Actors([actor(gender=0)])
    settings = Settings(target_fields=("gender",))
    source = Sources(Profile({"gender": 1}, ref="javdb"))

    stats = process(ctx, settings, state, source, now=lambda: 0)

    assert stats["updated"] == stats["completed"] == 1
    assert ctx.actors.get(1).values["gender"] == 1
    assert source.calls == [("javdb", "id1")]


def test_minnanoav_gender_fills_when_javdb_is_unknown(setup):
    ctx, state, _ = setup
    ctx.actors = Actors([actor(gender=0)])
    settings = Settings(target_fields=("gender",))
    source = Sources(Profile(), Profile({"gender": 1}, ref="/actress1.html"))

    stats = process(ctx, settings, state, source, now=lambda: 0)

    assert stats["updated"] == stats["completed"] == 1
    assert ctx.actors.get(1).values["gender"] == 1
    assert source.calls == [("javdb", "id1"), ("minnanoav", "")]


def test_javdb_gender_wins_over_minnanoav_inference(setup):
    ctx, state, _ = setup
    ctx.actors = Actors([actor(gender=0)])
    settings = Settings(target_fields=("gender",))
    source = Sources(Profile({"gender": 2}), Profile({"gender": 1}))

    process(ctx, settings, state, source, now=lambda: 0)

    assert ctx.actors.get(1).values["gender"] == 2


def test_javdb_male_does_not_use_minnanoav_fallback(setup):
    ctx, state, _ = setup
    ctx.actors = Actors([actor(gender=0)])
    settings = Settings(target_fields=("gender", "height_cm"))
    source = Sources(
        Profile({"gender": 2}),
        Profile({"gender": 1, "height_cm": 165}),
    )

    process(ctx, settings, state, source, now=lambda: 0)

    assert source.calls == [("javdb", "id1")]
    assert ctx.actors.get(1).values["gender"] == 2
    assert ctx.actors.get(1).values.get("height_cm") is None


def test_three_rounds_three_days_partial_progress_does_not_reset(setup):
    ctx, state, settings = setup
    source = Sources(Profile({"height_cm": 160}))
    process(ctx, settings, state, source, now=lambda: 0)
    assert state.get(1, "id1")["next_attempt_at"] == 3 * 86400
    process(ctx, settings, state, source, now=lambda: 3 * 86400 - 1)
    assert state.get(1, "id1")["attempts"] == 1
    process(ctx, settings, state, source, now=lambda: 3 * 86400)
    process(ctx, settings, state, source, now=lambda: 6 * 86400)
    row = state.get(1, "id1")
    assert (row["status"], row["attempts"], row["missing_fields"]) == ("stopped", 3, ["cup"])
    assert row["next_attempt_at"] is None
    count = len(source.calls)
    process(ctx, settings.model_copy(update={"max_attempts": 10}), state, source, now=lambda: 9999999)
    assert len(source.calls) == count


def test_third_round_completion_wins_over_limit(setup):
    ctx, state, settings = setup
    source = Sources()
    for day in [0, 3]:
        process(ctx, settings, state, source, now=lambda: day * 86400)
    source.minnano = Profile({"height_cm": 160, "cup": "E"}, ref="/actress123.html")
    process(ctx, settings, state, source, now=lambda: 6 * 86400)
    assert state.get(1, "id1")["status"] == "completed"


def test_interrupted_third_attempt_is_not_repeated(setup):
    ctx, state, settings = setup
    row = state.get(1, "id1")
    row.update(status="waiting", attempts=3, reason="interrupted", next_attempt_at=0)
    state.save(row)
    source = Sources()
    process(ctx, settings, State(state.path), source, now=lambda: 99999)
    assert not source.calls
    assert state.get(1, "id1")["status"] == "stopped"


def test_progress_reports_total_and_scanned_actors(setup):
    ctx, state, settings = setup
    ctx.actors = Actors([actor(), actor(2)])
    source = Sources(Profile({"height_cm": 160, "cup": "E"}))
    events = []

    stats = process(
        ctx,
        settings,
        state,
        source,
        reporter=SimpleNamespace(progress_callback=events.append),
        now=lambda: 0,
    )

    assert stats["completed"] == 2
    assert [(event["current"], event["total"]) for event in events if "current" in event] == [
        (0, 2), (1, 2), (2, 2),
    ]


def test_run_writes_task_log_stages(monkeypatch, tmp_path):
    logs = []
    logger = SimpleNamespace(
        info=lambda *args: logs.append(args),
        warning=lambda *args: logs.append(args),
    )
    context = SimpleNamespace(
        plugin_id="demo",
        actors=Actors([actor()]),
        movies=Movies(),
        data_dir=tmp_path,
        get_task_logger=lambda _: logger,
    )
    source = Sources(Profile({"height_cm": 160, "cup": "E"}, ref="javdb"))
    monkeypatch.setattr("sakuramedia_actor_metadata.jobs.Sources", lambda _: source)

    run(context, Settings(target_fields=("height_cm", "cup")), None)

    assert any(args[0] == "女优资料补全开始" for args in logs)
    assert any(args[0] == "待扫描演员数 = {}" for args in logs)
    assert any(args[0] == "女优资料补全完成 stats={}" for args in logs)


def test_sources_fill_only_missing_and_respect_owners(setup):
    ctx, state, settings = setup
    ctx.actors.rows[1] = replace(actor(height_cm=170), owners={"cup": "plugin:other"})
    source = Sources(Profile({"height_cm": 160, "cup": "E"}))
    process(ctx, settings, state, source, now=lambda: 0)
    assert not source.calls and not ctx.actors.patches
    assert state.get(1, "id1")["reason"] == "fields_not_writable"


def test_minnanoav_fallback_and_cached_mapping(setup):
    ctx, state, settings = setup
    source = Sources(Profile({"height_cm": 160}), Profile({"height_cm": 165}, ref="/actress123.html"))
    process(ctx, settings, state, source, now=lambda: 0)
    assert ctx.actors.get(1).values["height_cm"] == 160
    process(ctx, settings, state, source, now=lambda: 3 * 86400)
    assert source.calls[-1] == ("minnanoav", "/actress123.html")


def test_patch_conflict_is_not_marked_complete(setup):
    ctx, state, settings = setup
    ctx.actors.conflict = True
    source = Sources(Profile({"height_cm": 160, "cup": "E"}))
    process(ctx, settings, state, source, now=lambda: 0)
    row = state.get(1, "id1")
    assert row["status"] == "waiting"
    assert "patch_conflict" in row["reason"]
    assert not row["sources"]


def test_three_consecutive_network_errors_abort_batch_and_keep_partial_data(setup):
    ctx, state, settings = setup
    ctx.actors = Actors([actor(1), actor(2), actor(3), actor(4)])
    source = Sources(Profile({"height_cm": 160}), SourceError("minnanoav:network_error"))

    stats = process(ctx, settings, state, source, now=lambda: 0)

    assert stats["aborted"] == 1 and stats["errors"] == stats["attempted"] == 3
    assert ctx.actors.get(1).values["height_cm"] == 160
    assert state.get(1, "id1")["attempts"] == 1
    assert state.get(3, "id3")["attempts"] == 1
    assert state.get(4, "id4")["attempts"] == 0


def test_normal_response_resets_network_error_streak(setup):
    ctx, state, settings = setup
    ctx.actors = Actors([actor(1), actor(2), actor(3), actor(4)])

    class FlakySources(Sources):
        def __init__(self):
            super().__init__(Profile())
            self.results = [
                SourceError("minnanoav:network_error"),
                SourceError("minnanoav:network_error"),
                Profile(outcome="not_found"),
                SourceError("minnanoav:network_error"),
            ]

        def minnanoav(self, names, ref, **kwargs):
            self.calls.append(("minnanoav", ref))
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    stats = process(ctx, settings, state, FlakySources(), now=lambda: 0)

    assert stats["aborted"] == 0 and stats["errors"] == 3 and stats["attempted"] == 4
    assert state.get(4, "id4")["attempts"] == 1


def test_markup_error_resets_network_error_streak(setup):
    ctx, state, settings = setup
    ctx.actors = Actors([actor(1), actor(2), actor(3), actor(4)])

    class FlakySources(Sources):
        def __init__(self):
            super().__init__(Profile())
            self.results = [
                SourceError("minnanoav:network_error"),
                SourceError("minnanoav:search_markup_missing"),
                SourceError("minnanoav:network_error"),
                SourceError("minnanoav:network_error"),
            ]

        def minnanoav(self, names, ref, **kwargs):
            self.calls.append(("minnanoav", ref))
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    stats = process(ctx, settings, state, FlakySources(), now=lambda: 0)

    assert stats["aborted"] == 0 and stats["errors"] == stats["attempted"] == 4


def test_empty_profile_continues_batch(setup):
    ctx, state, settings = setup
    ctx.actors = Actors([actor(1), actor(2)])
    source = Sources(Profile({"height_cm": 160}), Profile(outcome="empty_profile"))

    stats = process(ctx, settings, state, source, now=lambda: 0)

    assert stats["aborted"] == stats["errors"] == 0
    assert stats["attempted"] == stats["updated"] == 2
    assert state.get(1, "id1")["reason"] == "javdb:ok;minnanoav:empty_profile"
    assert state.get(2, "id2")["attempts"] == 1


def test_subscription_filter_processes_all_eligible_actors(setup):
    ctx, state, settings = setup
    ctx.actors = Actors([actor(1, gender=2), actor(2), actor(3, is_subscribed=True), actor(4, is_subscribed=True)])
    settings = settings.model_copy(update={"subscribed_only": True})
    source = Sources()
    events = []
    process(
        ctx,
        settings,
        state,
        source,
        reporter=SimpleNamespace(progress_callback=events.append),
        now=lambda: 0,
    )
    assert source.calls == [
        ("javdb", "id3"), ("minnanoav", ""),
        ("javdb", "id4"), ("minnanoav", ""),
    ]
    assert (events[-1]["current"], events[-1]["total"]) == (4, 4)


def test_reused_host_id_does_not_reuse_terminal_state(setup):
    _, state, _ = setup
    row = state.get(1, "old")
    row.update(status="completed", attempts=3, minnanoav_ref="/actress999.html")
    state.save(row)
    row = state.get(1, "new")
    assert row["status"] == "pending" and row["attempts"] == 0 and not row["minnanoav_ref"]


def test_existing_complete_actor_uses_zero_attempts(setup):
    ctx, state, settings = setup
    ctx.actors.rows[1] = actor(height_cm=160, cup="E")
    source = Sources()
    process(ctx, settings, state, source, now=lambda: 0)
    assert not source.calls
    assert state.get(1, "id1")["status"] == "completed"
    assert state.get(1, "id1")["attempts"] == 0


def test_attempt_is_saved_before_request_and_restart_respects_interval(setup):
    ctx, state, settings = setup

    class Interrupted(Sources):
        def javdb(self, jid):
            assert State(state.path).get(1, jid)["attempts"] == 1
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        process(ctx, settings, state, Interrupted(), now=lambda: 0)
    source = Sources()
    process(ctx, settings, State(state.path), source, now=lambda: 1)
    assert not source.calls
    process(ctx, settings, State(state.path), source, now=lambda: 3 * 86400)
    assert state.get(1, "id1")["attempts"] == 2


def test_priority_subscription_then_movie_count_then_id(setup):
    ctx, state, settings = setup
    rows = [actor(1), actor(2, is_subscribed=True), actor(3, is_subscribed=True),
            actor(4), actor(5), actor(6)]
    ctx.actors = Actors(rows)
    ctx.movies = Movies([
        movie(1, [rows[0]]), movie(2, [rows[0]]),
        movie(3, [rows[1]]),
        movie(4, [rows[2]]), movie(5, [rows[2]]),
        movie(6, [rows[3], rows[4]]),
    ])
    source = Sources(Profile({"height_cm": 160, "cup": "E"}))
    process(ctx, settings, state, source, now=lambda: 0)
    assert source.calls == [("javdb", f"id{i}") for i in [3, 2, 1, 4, 5, 6]]
    assert ctx.movies.calls == [0, 3]


def test_priority_uses_movie_count_and_excludes_collections(setup):
    ctx, state, settings = setup
    a, b, c, d = actor(1), actor(2), actor(3), actor(4)
    ctx.actors = Actors([a, b, c, d])
    ctx.movies = Movies([
        movie(1, [a]),
        movie(2, [b]), movie(3, [b]),
        movie(4, [c], is_collection=True),
        movie(5, [c, actor(999)]),
        movie(6, [d]),
    ])
    source = Sources(Profile({"height_cm": 160, "cup": "E"}))
    process(ctx, settings, state, source, now=lambda: 0)
    assert source.calls == [("javdb", f"id{i}") for i in [2, 1, 3, 4]]


def test_priority_does_not_override_retry_time(setup):
    ctx, state, settings = setup
    ctx.actors = Actors([actor(1, is_subscribed=True), actor(2)])
    row = state.get(1, "id1")
    row.update(status="waiting", attempts=1, next_attempt_at=100)
    state.save(row)
    source = Sources()
    process(ctx, settings, state, source, now=lambda: 0)
    assert [call for call in source.calls if call[0] == "javdb"] == [("javdb", "id2")]


@pytest.mark.parametrize("movie_count", [0, 4])
def test_priority_reports_each_page_before_actor_progress(setup, movie_count):
    ctx, state, settings = setup
    events = []

    class ReportingMovies(Movies):
        def list_page(self, **kwargs):
            assert events[-1]["text"].startswith("正在计算演员优先级，已扫描 ")
            return super().list_page(**kwargs)

    class ReportingSources(Sources):
        def javdb(self, jid):
            assert events[-1] == {"current": 0, "total": 1, "text": "开始扫描演员，共 1 位"}
            return super().javdb(jid)

    ctx.movies = ReportingMovies([
        movie(i + 1, [actor()], is_collection=(i == 0)) for i in range(movie_count)
    ])
    process(ctx, settings, state, ReportingSources(),
            reporter=SimpleNamespace(progress_callback=events.append), now=lambda: 0)
    counts = [0, 0] if movie_count == 0 else [0, 3, 4]
    assert events[:-2] == [
        {"text": f"正在计算演员优先级，已扫描 {count} 部影片"} for count in counts
    ]
    assert events[-2]["current"] == 0 and events[-2]["total"] == 1
    assert events[-1]["current"] == 1 and events[-1]["total"] == 1


@pytest.mark.parametrize("max_attempts", [1, 3])
def test_run_not_found_saves_partial_data_and_continues(
    setup, monkeypatch, tmp_path, max_attempts,
):
    ctx, state, settings = setup
    ctx.data_dir = tmp_path
    ctx.actors = Actors([actor(1), actor(2)])
    seen, events = [], []
    ctx.get_task_logger = lambda _: SimpleNamespace(info=lambda *a: None, warning=lambda *a: None)

    def handle(req):
        seen.append(req.url.path)
        if req.url.path.startswith("/api/v1/actors/"):
            jid = req.url.path.rsplit("/", 1)[-1]
            data = {"id": jid, "name": "未找到", "height": 160}
            if jid == "id2":
                data["cup"] = "E"
            return httpx.Response(200, json={"success": 1, "data": {"actor": data}})
        return httpx.Response(200, text='<h1>AV女優検索結果<span>0 名</span></h1>')

    def make_sources(settings):
        source = HttpSources(settings)
        source.interval = 0
        source.client.close()
        source.client = httpx.Client(transport=httpx.MockTransport(handle))
        return source

    monkeypatch.setattr("sakuramedia_actor_metadata.jobs.Sources", make_sources)
    settings = settings.model_copy(update={"max_attempts": max_attempts})
    stats = run(ctx, settings, SimpleNamespace(progress_callback=events.append))
    assert stats["aborted"] == 0 and stats["errors"] == 0
    assert stats["attempted"] == 2 and stats["updated"] == 2 and stats["completed"] == 1
    saved = State(tmp_path / "actor_metadata.sqlite3").get(1, "id1")
    assert saved["reason"] == "javdb:ok;minnanoav:not_found"
    assert saved["attempts"] == 1 and saved["missing_fields"] == ["cup"]
    assert saved["status"] == ("stopped" if max_attempts == 1 else "waiting")
    assert "height_cm" in saved["sources"]
    assert events[-1]["current"] == events[-1]["total"] == 2
    seen.clear()
    assert run(ctx, settings, None)["attempted"] == 0
    assert seen == []


def test_local_slash_aliases_reach_source(setup):
    ctx, state, settings = setup
    ctx.actors.rows[1] = actor(name="三上悠亞", alias_name="三上悠亞 / 三上悠亜 / 鬼头桃菜")

    class CheckingSources(Sources):
        def minnanoav(self, names, ref):
            assert [n.strip() for n in names] == ["三上悠亞", "三上悠亞", "三上悠亜", "鬼头桃菜"]
            return Profile({"cup": "F"})

    assert process(ctx, settings, state, CheckingSources(), now=lambda: 0)["updated"] == 1
