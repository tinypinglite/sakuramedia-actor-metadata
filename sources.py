"""JavBus 女优搜索与演员页解析，不保存登录态。"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import quote, urljoin, urlparse, urlsplit

import httpx
from bs4 import BeautifulSoup

from .settings import PROFILE_FIELDS, Settings

JAVBUS = "https://www.javbus.com"
JAVBUS_REF = re.compile(r"/star/[A-Za-z0-9_-]+")
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Cookie": "dv=1",
}
_LABEL_FIELDS = {
    "生日": "birthday",
    "身高": "height_cm",
    "罩杯": "cup",
    "胸圍": "bust_cm",
    "腰圍": "waist_cm",
    "臀圍": "hips_cm",
    "出生地": "birthplace",
}


class SourceError(RuntimeError):
    """站点故障、页面结构异常或站点要求人机验证。"""


@dataclass
class Profile:
    fields: dict = field(default_factory=dict)
    names: tuple[str, ...] = ()
    ref: str = ""
    outcome: str = "ok"


def normalize_name(value):
    return "".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def unqualified_name(value):
    return re.split(r"[（(]", value or "", maxsplit=1)[0].strip()


def normalize_fields(values):
    result = {}
    for key in PROFILE_FIELDS:
        raw = values.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        value = str(raw).strip()
        if value.casefold() in {
            "", "?", "未知", "不明", "非公開", "未公開", "－", "n/a", "none", "null", "-",
        }:
            continue
        if key == "birthday":
            try:
                birthday = date.fromisoformat(value)
            except ValueError:
                continue
            if not date(1900, 1, 1) <= birthday <= date.today():
                continue
            result[key] = birthday.isoformat()
        elif key.endswith("_cm"):
            match = re.fullmatch(r"([0-9]{1,3})\s*(?:cm|公分|厘米)?", value, re.I)
            if match and 1 <= int(match[1]) <= 250:
                result[key] = int(match[1])
        elif key == "cup":
            value = value.upper()
            if re.fullmatch(r"[A-Z]{1,3}", value):
                result[key] = value
        elif len(value) <= 255:
            result[key] = value
    return result


def star_ref(url: str) -> str:
    link = urlparse(urljoin(JAVBUS + "/", url))
    if link.scheme == "https" and link.netloc == "www.javbus.com":
        path = link.path.rstrip("/")
        if JAVBUS_REF.fullmatch(path):
            return path
    return ""


def _requires_verification(response: httpx.Response) -> bool:
    location = response.headers.get("location", "")
    return (
        "/doc/driver-verify" in response.url.path
        or "/doc/driver-verify" in urlsplit(location).path
        or "Age Verification JavBus" in response.text
    )


def parse_star_page(html: str, ref: str, names) -> Profile:
    soup = BeautifulSoup(html, "html.parser")
    profile = soup.select_one(".avatar-box .photo-info")
    if profile is None:
        return Profile(ref=ref, outcome="not_found")
    name_node = profile.select_one(".pb10")
    page_name = name_node.get_text(" ", strip=True) if name_node is not None else ""
    if not page_name:
        return Profile(ref=ref, outcome="not_found")

    expected = {
        normalize_name(unqualified_name(name))
        for name in names
        if isinstance(name, str) and name.strip()
    }
    if normalize_name(unqualified_name(page_name)) not in expected:
        return Profile(names=(page_name,), ref=ref, outcome="identity_mismatch")

    values = {}
    for item in profile.find_all("p"):
        text = unicodedata.normalize("NFKC", item.get_text(" ", strip=True))
        label, separator, value = text.partition(":")
        field_name = _LABEL_FIELDS.get(label.strip())
        if separator and field_name and value.strip():
            values[field_name] = value.strip()
    fields = normalize_fields(values)
    return Profile(
        fields=fields,
        names=(page_name,),
        ref=ref,
        outcome="ok" if fields else "empty_profile",
    )


def parse_search_page(html: str, names) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    expected = {
        normalize_name(unqualified_name(name))
        for name in names
        if isinstance(name, str) and name.strip()
    }
    found = {}
    for link in soup.select("a.avatar-box[href]"):
        name_node = link.select_one(".mleft")
        if name_node is None:
            continue
        for button in name_node.select("button"):
            button.decompose()
        candidate_name = name_node.get_text(" ", strip=True)
        ref = star_ref(link.get("href") or "")
        if (
            ref
            and normalize_name(unqualified_name(candidate_name)) in expected
        ):
            found[ref] = candidate_name
    return found


class Sources:
    def __init__(self, settings: Settings):
        self.interval = settings.request_interval_seconds
        self.client = httpx.Client(
            timeout=settings.timeout_seconds,
            follow_redirects=True,
        )
        self.last_request = None

    def close(self):
        self.client.close()

    def _wait(self):
        if self.last_request is not None:
            time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()

    def _get(self, url: str):
        self._wait()
        try:
            response = self.client.get(url, headers=_HEADERS)
        except httpx.HTTPError:
            raise SourceError("javbus:network_error") from None
        if _requires_verification(response):
            raise SourceError("javbus:verification_required")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise SourceError(f"javbus:http_{response.status_code}")
        return response

    def _search(self, names) -> tuple[str, str]:
        for name in names[:3]:
            response = self._get(
                f"{JAVBUS}/searchstar/{quote(unqualified_name(name), safe='')}"
            )
            if response is None:
                continue
            found = parse_search_page(response.text, names)
            if len(found) > 1:
                return "", "ambiguous"
            if found:
                ref = next(iter(found))
                return ref, "ok"
        return "", "not_found"

    def javbus(self, names, ref: str = "") -> Profile:
        names = tuple(
            dict.fromkeys(
                name.strip()
                for name in names
                if isinstance(name, str) and name.strip()
            )
        )
        if ref:
            if not JAVBUS_REF.fullmatch(ref):
                raise SourceError("javbus:invalid_ref")
            response = self._get(JAVBUS + ref)
            if response is not None:
                profile = parse_star_page(response.text, ref, names)
                if profile.outcome != "identity_mismatch":
                    return profile

        ref, outcome = self._search(names)
        if not ref:
            return Profile(outcome=outcome)
        response = self._get(JAVBUS + ref)
        if response is None:
            return Profile(ref=ref, outcome="not_found")
        return parse_star_page(response.text, ref, names)
