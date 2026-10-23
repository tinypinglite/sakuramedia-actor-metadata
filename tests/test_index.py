import json

import pytest
from sakuramedia_actor_metadata import index as index_module
from sakuramedia_actor_metadata.index import (
    INDEX_VERSION,
    ActressIndex,
    seed_bundled_index,
)
from sakuramedia_actor_metadata.sources import (
    ActressGroup,
    ActressListEntry,
    ActressListPage,
    SourceError,
)


def page(entries, last_page=1, total=None):
    items = tuple(
        ActressListEntry(ref, name, furi) for ref, name, furi in entries
    )
    return ActressListPage(items, last_page, len(items) if total is None else total)


def complete_index(path, *, built_at, entry):
    index = ActressIndex(path)
    index.upsert((entry,))
    index.set_meta("index_version", INDEX_VERSION)
    index.set_meta("full_build_id", "")
    index.set_meta("full_built_at", built_at)
    return index


def bundled_index(path, *, built_at, entries):
    path.write_text(json.dumps({
        "index_version": INDEX_VERSION,
        "full_built_at": built_at,
        "entries": entries,
    }, ensure_ascii=False), encoding="utf-8")


class FakeSources:
    def __init__(self, groups=(("a", "あ"),), pages=None, fail_pages=()):
        self.groups = tuple(ActressGroup(code, label) for code, label in groups)
        self.pages = dict(pages or {})
        self.fail_pages = set(fail_pages)
        self.calls = []

    def actress_groups(self):
        self.calls.append(("groups",))
        return self.groups

    def actress_list_page(self, page_no, *, sort="name", gojuon=""):
        self.calls.append((gojuon, page_no, sort))
        if (gojuon, page_no) in self.fail_pages:
            raise SourceError("minnanoav:http_403")
        return self.pages.get(
            (gojuon, page_no), ActressListPage(tuple(), 0, 0)
        )


def test_lookup_matches_name_kana_roman_and_unqualified(tmp_path):
    index = ActressIndex(tmp_path / "index.sqlite3")
    index.upsert((
        ActressListEntry("/actress1.html", "山手梨愛", "やまてりあ / Yamate Ria"),
        ActressListEntry("/actress2.html", "同名（かな）", "どうめい / Doumei"),
        ActressListEntry("/actress3.html", "同名（別）", "どうめい2 / Doumei2"),
    ))

    assert index.lookup(["山手梨愛"]) == ["/actress1.html"]
    assert index.lookup(["やまてりあ"]) == ["/actress1.html"]
    assert index.lookup(["Yamate Ria"]) == ["/actress1.html"]
    assert index.lookup(["同名"]) == ["/actress2.html", "/actress3.html"]
    assert index.lookup(["不存在"]) == []


def test_seed_bundled_index_copies_missing_index(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled.json"
    bundled_index(
        bundled,
        built_at=100,
        entries=[["/actress1.html", "A", "a / A"]],
    )
    monkeypatch.setattr(index_module, "BUNDLED_INDEX", bundled)
    target = tmp_path / "data" / "actress_index.sqlite3"

    assert seed_bundled_index(target) is True
    assert ActressIndex(target).lookup(["A"]) == ["/actress1.html"]


def test_seed_bundled_index_replaces_older_index_only(tmp_path, monkeypatch):
    target = tmp_path / "actress_index.sqlite3"
    complete_index(
        target,
        built_at=100,
        entry=ActressListEntry("/old.html", "Old", "old / Old"),
    )
    bundled = tmp_path / "bundled.json"
    bundled_index(
        bundled,
        built_at=200,
        entries=[["/new.html", "New", "new / New"]],
    )
    monkeypatch.setattr(index_module, "BUNDLED_INDEX", bundled)

    assert seed_bundled_index(target) is True
    assert ActressIndex(target).lookup(["New"]) == ["/new.html"]
    assert ActressIndex(target).lookup(["Old"]) == []
    assert seed_bundled_index(target) is False


def test_full_build_uses_gojuon_groups_and_does_not_refresh_again(tmp_path):
    index = ActressIndex(tmp_path / "index.sqlite3")
    sources = FakeSources(
        groups=(("a", "あ"), ("i", "い")),
        pages={
            ("a", 1): page([("/actress1.html", "A", "a / A")], 1, 1),
            ("i", 1): page([("/actress2.html", "B", "b / B")], 1, 1),
        },
    )

    index.ensure(sources, now=100)

    assert index.lookup(["A"]) == ["/actress1.html"]
    assert index.lookup(["B"]) == ["/actress2.html"]
    assert sources.calls == [
        ("groups",), ("a", 1, "name"), ("i", 1, "name"),
    ]
    assert int(index.meta("index_version")) == INDEX_VERSION
    assert float(index.meta("full_built_at")) == 100
    assert index.meta("full_build_id") == ""
    assert index.count() == 2

    sources.calls.clear()
    index.ensure(sources, now=200)
    assert sources.calls == []


def test_full_build_skips_empty_group(tmp_path):
    index = ActressIndex(tmp_path / "index.sqlite3")
    sources = FakeSources(
        groups=(("wo", "を"), ("a", "あ")),
        pages={
            ("wo", 1): page([], 1, 0),
            ("a", 1): page([("/actress1.html", "A", "a / A")], 1, 1),
        },
    )

    index.ensure(sources, now=100)

    assert index.count() == 1
    assert index.lookup(["A"]) == ["/actress1.html"]


def test_full_build_resumes_from_group_page(tmp_path):
    index = ActressIndex(tmp_path / "index.sqlite3")
    sources = FakeSources(
        groups=(("a", "あ"), ("i", "い")),
        pages={
            ("a", 1): page([("/actress1.html", "A", "a / A")], 2, 2),
            ("a", 2): page([("/actress2.html", "B", "b / B")], 2, 2),
            ("i", 1): page([("/actress3.html", "C", "c / C")], 1, 1),
        },
        fail_pages={("a", 2)},
    )

    with pytest.raises(SourceError):
        index.ensure(sources, now=100)
    assert int(index.meta("full_group_index")) == 0
    assert int(index.meta("full_group_page")) == 2
    assert index.lookup(["B"]) == []

    sources.fail_pages.clear()
    sources.calls.clear()
    index.ensure(sources, now=200)

    assert sources.calls == [("a", 2, "name"), ("i", 1, "name")]
    assert index.lookup(["B"]) == ["/actress2.html"]
    assert index.lookup(["C"]) == ["/actress3.html"]
    assert index.meta("full_build_id") == ""
    assert int(index.meta("full_group_index")) == 0
    assert int(index.meta("full_group_page")) == 1


def test_old_index_is_rebuilt_and_stale_rows_are_pruned(tmp_path):
    index = ActressIndex(tmp_path / "index.sqlite3")
    index.upsert((ActressListEntry("/old.html", "Old", ""),))
    index.set_meta("full_built_at", 1)
    sources = FakeSources(pages={
        ("a", 1): page([("/actress1.html", "A", "a / A")], 1, 1),
    })

    index.ensure(sources, now=2)

    assert index.lookup(["Old"]) == []
    assert index.lookup(["A"]) == ["/actress1.html"]
    assert int(index.meta("index_version")) == INDEX_VERSION
    assert index.count() == 1


def test_incremental_failure_keeps_complete_old_index(tmp_path):
    index = ActressIndex(tmp_path / "index.sqlite3")
    sources = FakeSources(pages={
        ("a", 1): page([("/actress1.html", "A", "a / A")], 1, 1),
    })
    index.ensure(sources, now=1)

    index.ensure(sources, now=181 * 86400)

    assert index.lookup(["A"]) == ["/actress1.html"]
    assert float(index.meta("full_built_at")) == 1


def test_incremental_refresh_stops_on_first_page_with_no_new_refs(tmp_path):
    index = ActressIndex(tmp_path / "index.sqlite3")
    sources = FakeSources(pages={
        ("a", 1): page(
            [("/actress1.html", "A", "a / A"), ("/actress2.html", "B", "b / B")],
            1, 2,
        ),
    })
    index.ensure(sources, now=1)
    sources.calls.clear()
    sources.pages.update({
        ("", 1): page(
            [("/actress3.html", "C", "c / C"), ("/actress1.html", "A", "a / A")],
            2, 3,
        ),
        ("", 2): page([("/actress2.html", "B", "b / B")], 2, 3),
        ("", 3): page([("/actress4.html", "D", "d / D")], 3, 4),
    })

    index.ensure(sources, now=86401)

    assert sources.calls == [("", 1, "date"), ("", 2, "date")]
    assert index.lookup(["C"]) == ["/actress3.html"]
    assert index.lookup(["D"]) == []


def test_long_gap_still_uses_incremental_refresh(tmp_path):
    index = ActressIndex(tmp_path / "index.sqlite3")
    sources = FakeSources(pages={
        ("a", 1): page([("/actress1.html", "A", "a / A")], 1, 1),
        ("", 1): page([("/actress1.html", "A", "a / A")], 1, 1),
    })
    index.ensure(sources, now=1)
    sources.calls.clear()

    index.ensure(sources, now=8 * 86400)

    assert sources.calls == [("", 1, "date")]


def test_incremental_page_cap_requests_full_rebuild_next_time(tmp_path):
    index = ActressIndex(tmp_path / "index.sqlite3")
    sources = FakeSources(pages={
        ("a", 1): page([("/actress0.html", "Start", "start / Start")], 1, 1),
    })
    index.ensure(sources, now=1)
    sources.calls.clear()
    for page_no in range(1, 4):
        sources.pages[("", page_no)] = page(
            [(f"/actress{page_no}.html", f"New{page_no}", f"new{page_no} / New{page_no}")],
            3, 3,
        )

    index.ensure(sources, now=86401)

    assert int(index.meta("incremental_requires_full")) == 1
    assert sources.calls == [
        ("", 1, "date"), ("", 2, "date"), ("", 3, "date"),
    ]
    sources.calls.clear()
    sources.pages[("a", 1)] = page(
        [("/actress0.html", "Start", "start / Start")]
        + [
            (f"/actress{page_no}.html", f"New{page_no}", f"new{page_no} / New{page_no}")
            for page_no in range(1, 4)
        ],
        1, 4,
    )
    index.ensure(sources, now=86402)

    assert ("groups",) in sources.calls
    assert index.count() == 4
    assert int(index.meta("incremental_requires_full")) == 0


def test_incomplete_full_build_is_not_marked_complete(tmp_path):
    index = ActressIndex(tmp_path / "index.sqlite3")
    sources = FakeSources(pages={
        ("a", 1): page([("/actress1.html", "A", "a / A")], 1, 100),
    })

    with pytest.raises(SourceError, match="index_incomplete"):
        index.ensure(sources, now=1)

    assert index.meta("full_built_at") is None
    assert int(index.meta("full_group_index")) == 0
    assert int(index.meta("full_group_page")) == 1


def test_empty_first_page_does_not_mark_index_complete(tmp_path):
    index = ActressIndex(tmp_path / "index.sqlite3")
    sources = FakeSources(pages={("a", 1): page([], 1, 0)})

    with pytest.raises(SourceError, match="index_incomplete"):
        index.ensure(sources, now=1)

    assert index.meta("full_built_at") is None


def test_later_page_pager_does_not_shrink_total(tmp_path):
    index = ActressIndex(tmp_path / "index.sqlite3")
    sources = FakeSources(pages={
        ("a", 1): page([("/actress1.html", "A", "a / A")], 3, 3),
        ("a", 2): page([("/actress2.html", "B", "b / B")], 2, 3),
        ("a", 3): page([("/actress3.html", "C", "c / C")], 3, 3),
    })

    index.ensure(sources, now=1)

    assert sources.calls == [
        ("groups",), ("a", 1, "name"), ("a", 2, "name"), ("a", 3, "name"),
    ]
    assert index.lookup(["C"]) == ["/actress3.html"]


def test_repeated_page_stops_full_build(tmp_path):
    index = ActressIndex(tmp_path / "index.sqlite3")
    same = [("/actress2.html", "B", "b / B")]
    sources = FakeSources(pages={
        ("a", 1): page([("/actress1.html", "A", "a / A")], 4, 2),
        ("a", 2): page(same, 4, 2),
        ("a", 3): page(same, 4, 2),
    })

    index.ensure(sources, now=1)

    assert sources.calls == [
        ("groups",), ("a", 1, "name"), ("a", 2, "name"), ("a", 3, "name"),
    ]
    assert index.lookup(["B"]) == ["/actress2.html"]


def test_upsert_is_idempotent(tmp_path):
    index = ActressIndex(tmp_path / "index.sqlite3")
    entry = ActressListEntry("/actress1.html", "A", "a / A")
    index.upsert((entry,))
    index.upsert((entry,))
    assert index.count() == 1
