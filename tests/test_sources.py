from pathlib import Path

import httpx
import pytest
from sakuramedia_actor_metadata.settings import Settings
from sakuramedia_actor_metadata.sources import (
    SourceError,
    Sources,
    normalize_fields,
    parse_minnanoav,
)


def card(name="山手梨愛", info='<span>サイズ</span><p>T170 / B99(Iカップ) / W58 / H88</p>'):
    return f'<h1>{name}<span>reading / roman</span></h1><div class="act-profile"><table><tr><td>{info}</td></tr></table></div>'


def search(*entries):
    return '<h1>AV女優検索結果</h1>' + ''.join(
        f'<h2 class="ttl"><a href="{ref}">{name}</a></h2>' for name, ref in entries
    )


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


def test_redirect_match_and_no_extra_queries(client):
    seen = []

    def handle(req):
        seen.append(req.url.path)
        if req.url.path == "/search_result.php":
            assert req.url.params["search_word"] == "山手梨愛"
            assert req.url.params["search_scope"] == "actress"
            return httpx.Response(302, headers={"location": "/actress203126.html"})
        return httpx.Response(200, text=card())

    mock(client, handle)
    result = client.minnanoav(["山手梨愛", "unused"])
    assert result.ref == "/actress203126.html" and result.fields["cup"] == "I"
    assert seen == ["/search_result.php", "/actress203126.html"]


def test_search_exact_match_and_alias_retry(client):
    seen = []

    def handle(req):
        seen.append(str(req.url))
        if req.url.path == "/search_result.php":
            if req.url.params["search_word"] == "traditional":
                return httpx.Response(200, text=search())
            return httpx.Response(200, text=search(("山手梨愛（やまてりあ）", "/actress1.html"),
                                                  ("山手梨愛別人", "/actress2.html"),
                                                  ("山手梨愛", "https://evil.example/actress3.html")))
        return httpx.Response(200, text=card())

    mock(client, handle)
    result = client.minnanoav(["traditional", "山手梨愛"])
    assert result.outcome == "ok" and result.ref == "/actress1.html" and len(seen) == 3


def test_ambiguous_search_does_not_pick_first(client):
    mock(client, lambda req: httpx.Response(200, text=search(("同名", "/actress1.html"), ("同名", "/actress2.html"))))
    assert client.minnanoav(["同名"]).outcome == "ambiguous"


def test_no_results_bounded_queries(client):
    seen = []

    def handle(req):
        seen.append(req.url.params["search_word"])
        return httpx.Response(200, text=search())

    mock(client, handle)
    assert client.minnanoav(["a", "a", "b", "c", "d"]).outcome == "not_found"
    assert seen == ["a", "b", "c"]


def test_cached_identity_mismatch_researches(client):
    def handle(req):
        if req.url.path == "/actress1.html":
            return httpx.Response(200, text=card(name="別人"))
        return httpx.Response(200, text=search())

    mock(client, handle)
    assert client.minnanoav(["山手梨愛"], "/actress1.html").fields == {}


def test_redirect_to_wrong_actor_never_writes(client):
    def handle(req):
        if req.url.path == "/search_result.php":
            return httpx.Response(302, headers={"location": "/actress1.html"})
        return httpx.Response(200, text=card(name="別人"))

    mock(client, handle)
    assert client.minnanoav(["山手梨愛"]).outcome == "identity_mismatch"


@pytest.mark.parametrize("status", [403, 429, 500])
def test_service_failures_are_not_empty_profiles(client, status):
    mock(client, lambda req: httpx.Response(status))
    with pytest.raises(SourceError, match=f"minnanoav:http_{status}"):
        client.minnanoav(["山手梨愛"])


@pytest.mark.parametrize("ref", ["", "/actress1.html"])
def test_challenge_markup_is_error(client, ref):
    mock(client, lambda req: httpx.Response(200, text='<title>Just a moment...</title>'))
    with pytest.raises(SourceError, match="markup_missing"):
        client.minnanoav(["山手梨愛"], ref)
