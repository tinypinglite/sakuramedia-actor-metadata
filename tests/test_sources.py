import httpx
import pytest
from sakuramedia_actor_metadata.settings import Settings
from sakuramedia_actor_metadata.sources import (
    Profile,
    SourceError,
    Sources,
    normalize_fields,
    parse_search_page,
    parse_star_page,
    star_ref,
)

SEARCH_HTML = """
<a class="avatar-box text-center" href="https://www.javbus.com/star/2xi">
  <img src="/pics/actress/2xi_a.jpg">
  <span class="mleft">葵つかさ<button class="btn btn-xs btn-info">有碼</button></span>
</a>
"""

STAR_HTML = """
<div class="avatar-box">
  <div class="photo-info">
    <span class="pb10">葵つかさ</span>
    <p>生日: 1990-08-14</p>
    <p>年齡: 36</p>
    <p>身高: 163cm</p>
    <p>罩杯: E</p>
    <p>胸圍: 88cm</p>
    <p>腰圍: 58cm</p>
    <p>臀圍: 86cm</p>
    <p>出生地: 大阪府</p>
    <p>愛好: ジョギング</p>
  </div>
</div>
"""


def client(handler):
    source = Sources(Settings())
    source.interval = 0
    source.client.close()
    source.client = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    return source


def test_normalization_only_returns_javbus_fields():
    assert normalize_fields({
        "birthday": "1990-08-14",
        "height_cm": "163cm",
        "bust_cm": 88,
        "waist_cm": "58",
        "hips_cm": "86cm",
        "cup": "e",
        "birthplace": "大阪府",
        "blood_type": "A",
    }) == {
        "birthday": "1990-08-14",
        "height_cm": 163,
        "bust_cm": 88,
        "waist_cm": 58,
        "hips_cm": 86,
        "cup": "E",
        "birthplace": "大阪府",
    }


def test_star_ref_only_accepts_javbus_star_urls():
    assert star_ref("https://www.javbus.com/star/2xi") == "/star/2xi"
    assert star_ref("/star/w5a") == "/star/w5a"
    assert star_ref("https://example.com/star/2xi") == ""
    assert star_ref("https://www.javbus.com/actor/2xi") == ""


def test_parse_star_page_extracts_profile_and_ignores_age_and_hobby():
    profile = parse_star_page(STAR_HTML, "/star/2xi", ["葵つかさ"])
    assert profile.outcome == "ok"
    assert profile.ref == "/star/2xi"
    assert profile.fields == {
        "birthday": "1990-08-14",
        "height_cm": 163,
        "bust_cm": 88,
        "waist_cm": 58,
        "hips_cm": 86,
        "cup": "E",
        "birthplace": "大阪府",
    }


def test_parse_star_page_rejects_identity_mismatch():
    profile = parse_star_page(STAR_HTML, "/star/2xi", ["別の女優"])
    assert profile.outcome == "identity_mismatch"
    assert profile.fields == {}


def test_parse_star_page_without_profile_is_empty():
    html = """
    <div class="avatar-box">
      <div class="photo-info"><span class="pb10">乙白さやか</span></div>
    </div>
    """
    profile = parse_star_page(html, "/star/w5a", ["乙白さやか"])
    assert profile == Profile(
        names=("乙白さやか",),
        ref="/star/w5a",
        outcome="empty_profile",
    )


def test_parse_search_page_requires_exact_normalized_name():
    html = """
    <a class="avatar-box" href="/star/2xi"><span class="mleft">葵 つかさ<button>有碼</button></span></a>
    <a class="avatar-box" href="/star/other"><span class="mleft">葵つかさ別</span></a>
    """
    assert parse_search_page(html, ["葵つかさ"]) == {"/star/2xi": "葵 つかさ"}


def test_search_then_fetch_profile():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path.startswith("/searchstar/"):
            return httpx.Response(200, text=SEARCH_HTML)
        return httpx.Response(200, text=STAR_HTML)

    source = client(handler)
    profile = source.javbus(["葵つかさ"])

    assert profile.outcome == "ok"
    assert profile.ref == "/star/2xi"
    assert profile.fields["height_cm"] == 163
    assert seen == ["/searchstar/葵つかさ", "/star/2xi"]


def test_cached_ref_skips_search():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, text=STAR_HTML)

    source = client(handler)
    profile = source.javbus(["葵つかさ"], "/star/2xi")

    assert profile.outcome == "ok"
    assert seen == ["/star/2xi"]


def test_stale_ref_falls_back_to_search():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/star/old":
            return httpx.Response(404)
        if request.url.path.startswith("/searchstar/"):
            return httpx.Response(200, text=SEARCH_HTML)
        return httpx.Response(200, text=STAR_HTML)

    source = client(handler)
    profile = source.javbus(["葵つかさ"], "/star/old")

    assert profile.outcome == "ok"
    assert profile.ref == "/star/2xi"
    assert seen == ["/star/old", "/searchstar/葵つかさ", "/star/2xi"]


def test_ambiguous_exact_results_are_not_guessed():
    html = """
    <a class="avatar-box" href="/star/one"><span class="mleft">同名</span></a>
    <a class="avatar-box" href="/star/two"><span class="mleft">同名</span></a>
    """

    source = client(lambda request: httpx.Response(200, text=html))
    profile = source.javbus(["同名"])

    assert profile.outcome == "ambiguous"
    assert profile.ref == ""


def test_missing_name_returns_not_found():
    source = client(lambda request: httpx.Response(200, text="<html></html>"))
    assert source.javbus(["三上悠亚"]).outcome == "not_found"


def test_human_verification_is_source_error():
    source = client(
        lambda request: httpx.Response(
            200,
            text="<title>Age Verification JavBus</title>",
        )
    )
    with pytest.raises(SourceError, match="verification_required"):
        source.javbus(["葵つかさ"])


def test_network_error_is_source_error():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    source = client(handler)
    with pytest.raises(SourceError, match="network_error"):
        source.javbus(["葵つかさ"])
