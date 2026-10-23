from pathlib import Path

import httpx
import pytest
from sakuramedia_actor_metadata.settings import Settings
from sakuramedia_actor_metadata.sources import (
    ActressGroup,
    SourceError,
    Sources,
    normalize_fields,
    parse_actress_groups,
    parse_minnanoav,
)


def card(name="山手梨愛", info='<span>サイズ</span><p>T170 / B99(Iカップ) / W58 / H88</p>'):
    return f'<h1>{name}<span>reading / roman</span></h1><div class="act-profile"><table><tr><td>{info}</td></tr></table></div>'


@pytest.fixture
def client():
    source = Sources(Settings())
    source.interval = 0
    source.client.close()
    yield source
    source.close()


def mock(client, handler):
    client.client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_normalization_rejects_unknown_and_invalid_values():
    assert normalize_fields({"birthday": "2025-02-30", "height_cm": 0,
                             "bust_cm": True, "cup": "?", "birthplace": "不明",
                             "blood_type": "未知"}) == {}
    assert normalize_fields({"height_cm": "163cm", "blood_type": "AB型",
                             "birthday": "1990-08-14", "cup": "e"}) == {
        "birthday": "1990-08-14", "height_cm": 163, "cup": "E", "blood_type": "AB",
    }


def test_javdb_business_error_on_http_200(client):
    mock(client, lambda req: httpx.Response(200, json={"success": 0, "data": None}))
    with pytest.raises(SourceError, match="api_rejected"):
        client.javdb("Av2e")


def test_javdb_fields_and_aliases(client):
    def handle(req):
        assert "jdsignature" in req.headers and "authorization" not in req.headers
        return httpx.Response(200, json={"success": 1, "data": {"actor": {
            "id": "Av2e", "name": "三上悠亜", "other_name": "三上悠亞 / 鬼头桃菜",
            "birthday": "1993-08-16", "height": 159, "cup": "F", "blood_type": None,
        }}})
    mock(client, handle)
    profile = client.javdb("Av2e")
    assert profile.fields == {"birthday": "1993-08-16", "height_cm": 159, "cup": "F"}
    assert "鬼头桃菜" in profile.names


def test_javdb_actor_payload_gender_is_ignored(client):
    mock(client, lambda req: httpx.Response(200, json={"success": 1, "data": {"actor": {
        "id": "male", "name": "男", "gender": 1,
    }}}))
    assert "gender" not in client.javdb("male").fields


def test_real_profile_fragment():
    html = (Path(__file__).parent / "fixtures/minnanoav_profile.html").read_text()
    result = parse_minnanoav(html, "/actress203126.html")
    assert result.names == ("山手梨愛",)
    assert result.fields == {"birthday": "2000-02-23", "height_cm": 170, "bust_cm": 99,
                             "waist_cm": 58, "hips_cm": 88, "cup": "I", "birthplace": "福岡県"}


def test_alias_and_optional_values():
    html = card(info='<span>別名</span><p>旧名（きゅうめい）</p>')
    result = parse_minnanoav(html, "/actress1.html")
    assert result.names == ("山手梨愛", "旧名") and result.fields == {}
    assert parse_minnanoav(card(info='<span>血液型</span><p>AB型</p>'), "").fields == {
        "blood_type": "AB",
    }
    assert parse_minnanoav(card(info='<span>生年月日</span><p>2000年02月30日</p>'), "").fields == {}
    assert parse_minnanoav(card(info='<span>出身地</span><p>非公開</p>'), "").fields == {}


def test_empty_profile_is_retryable_result(client):
    mock(client, lambda req: httpx.Response(200, text=""))
    profile = client.minnanoav(["山手梨愛"], "/actress1.html")
    assert profile.outcome == "empty_profile" and profile.ref == "/actress1.html"


def test_actress_list_parse(client):
    html = '''
    <h1>AV女優リスト<span>（五十音：あ）</span><span>2,519 名</span></h1>
    <table class="tbllist actress"><tr><td><a href="actress1.html"></a></td>
    <td class="details"><h2 class="ttl"><a href="actress1.html">山手梨愛</a></h2>
    <p class="furi">やまてりあ / Yamate Ria</p></td></tr>
    <tr><td><a href="actress2.html"></a></td>
    <td class="details"><h2 class="ttl"><a href="actress2.html">同名（かな）</a></h2>
    <p class="furi">どうめい / Doumei</p></td></tr></table>
    <a href="/actress_list.php?page=2">2</a>
    <a href="/actress_list.php?page=3">3</a>
    '''
    mock(client, lambda req: httpx.Response(200, text=html))
    page = client.actress_list_page(1)
    assert [entry.ref for entry in page.entries] == ["/actress1.html", "/actress2.html"]
    assert page.entries[0].name == "山手梨愛"
    assert page.entries[0].furi == "やまてりあ / Yamate Ria"
    assert page.last_page == 3
    assert page.total == 2519


def test_actress_list_empty_group_has_zero_total(client):
    html = '''
    <h1>AV女優リスト<span>（五十音：を）</span><span>0 名</span></h1>
    <table class="tbllist actress"></table>
    '''
    mock(client, lambda req: httpx.Response(200, text=html))
    page = client.actress_list_page(1, gojuon="wo")
    assert page.entries == () and page.last_page == 1 and page.total == 0


def test_actress_groups_parse_and_dedupe():
    html = '''
    <a href="actress_list.php?gojuon=a">あ</a> (2441)
    <a href="actress_list.php?gojuon=i">い</a> (1133)
    <a href="actress_list.php?gojuon=a">あ</a> (2441)
    <a href="actress_list.php?gojuon=wo">を</a> (0)
    '''
    assert parse_actress_groups(html) == (
        ActressGroup("a", "あ"),
        ActressGroup("i", "い"),
        ActressGroup("wo", "を"),
    )


def test_actress_groups_requires_markup():
    with pytest.raises(SourceError, match="group_markup_missing"):
        parse_actress_groups("<html><body>no groups</body></html>")


def test_actress_list_request_carries_gojuon(client):
    seen = []

    def handle(req):
        seen.append(req.url)
        return httpx.Response(200, text="<h1><span>0 名</span></h1>"
                                        "<table class=\"tbllist actress\"></table>")

    mock(client, handle)
    client.actress_list_page(2, sort="name", gojuon="a")
    assert seen[0].params["gojuon"] == "a"
    assert seen[0].params["page"] == "2"
    assert seen[0].params["sort"] == "name"


def test_actress_list_404_is_empty(client):
    mock(client, lambda req: httpx.Response(404))
    page = client.actress_list_page(999)
    assert page.entries == () and page.last_page == 0


class Index:
    def __init__(self, refs=(), removed=None):
        self.refs = list(refs)
        self.removed = removed if removed is not None else []

    def lookup(self, names):
        return self.refs

    def remove(self, ref):
        self.removed.append(ref)


def test_index_hit_fetches_profile_directly(client):
    seen = []

    def handle(req):
        seen.append(req.url.path)
        return httpx.Response(200, text=card())

    mock(client, handle)
    result = client.minnanoav(["山手梨愛"], index=Index(["/actress203126.html"]))
    assert result.outcome == "ok" and result.ref == "/actress203126.html"
    assert seen == ["/actress203126.html"]


def test_index_missing_does_not_fall_back_to_search(client):
    seen = []

    def handle(req):
        seen.append(req.url.path)
        return httpx.Response(404)

    mock(client, handle)
    result = client.minnanoav(["山手梨愛"], index=Index())
    assert result.outcome == "not_found"
    assert seen == []


def test_index_ambiguous_does_not_pick_first(client):
    result = client.minnanoav(["同名"], index=Index(["/actress1.html", "/actress2.html"]))
    assert result.outcome == "ambiguous"


def test_index_stale_ref_is_removed(client):
    removed = []
    mock(client, lambda req: httpx.Response(404))
    result = client.minnanoav(["山手梨愛"], index=Index(["/actress1.html"], removed))
    assert result.outcome == "not_found"
    assert removed == ["/actress1.html"]


def test_index_identity_mismatch_keeps_candidate(client):
    removed = []
    mock(client, lambda req: httpx.Response(200, text=card(name="別人")))
    result = client.minnanoav(
        ["山手梨愛"], index=Index(["/actress1.html"], removed)
    )
    assert result.outcome == "identity_mismatch"
    assert removed == []


def test_cached_identity_mismatch_without_index(client):
    mock(client, lambda req: httpx.Response(200, text=card(name="別人")))
    assert client.minnanoav(["山手梨愛"], "/actress1.html").outcome == "identity_mismatch"


def test_redirect_to_wrong_actor_never_writes(client):
    def handle(req):
        if req.url.path == "/actress1.html":
            return httpx.Response(302, headers={"location": "/actress2.html"})
        return httpx.Response(200, text=card(name="別人"))

    mock(client, handle)
    assert client.minnanoav(["山手梨愛"], "/actress1.html").outcome == "identity_mismatch"


@pytest.mark.parametrize("status", [403, 429, 500])
def test_service_failures_are_not_empty_profiles(client, status):
    mock(client, lambda req: httpx.Response(status))
    with pytest.raises(SourceError, match=f"minnanoav:http_{status}"):
        client.minnanoav(["山手梨愛"], "/actress1.html")


def test_challenge_markup_is_error(client):
    mock(client, lambda req: httpx.Response(200, text='<title>Just a moment...</title>'))
    with pytest.raises(SourceError, match="markup_missing"):
        client.minnanoav(["山手梨愛"], "/actress1.html")
