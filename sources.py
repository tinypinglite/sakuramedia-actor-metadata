"""JavDB JSON 与 MinnanoAV HTML 适配，不保存登录态。"""

import hashlib
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .settings import PROFILE_FIELDS, Settings

JAVDB = "https://jdforrepam.com"
MINNANOAV = "https://www.minnano-av.com"
MINNANOAV_REF = re.compile(r"/actress[0-9]+\.html")
# App 协议固定签名常量，沿用已验证的 JavDB 客户端；不是用户账号凭据。
_APP_SIGNING_SEED = '71cf27bb3c0bcdf207b64abecddc970098c7421ee7203b9cdae54478478a199e7d5a6e1a57691123c1a931c057842fb73ba3b3c83bcd69c17ccf174081e3d8aa'


class SourceError(RuntimeError):
    """站点故障或解析异常：结束本轮，避免消耗后续演员的尝试次数。"""


@dataclass
class Profile:
    fields: dict = field(default_factory=dict)
    names: tuple[str, ...] = ()
    ref: str = ""
    outcome: str = "ok"


@dataclass(frozen=True)
class ActressListEntry:
    ref: str
    name: str
    furi: str = ""


@dataclass(frozen=True)
class ActressGroup:
    code: str
    label: str


@dataclass(frozen=True)
class ActressListPage:
    entries: tuple[ActressListEntry, ...]
    last_page: int
    total: int = 0


def normalize_name(value):
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def normalize_fields(values):
    result = {}
    for key in PROFILE_FIELDS:
        raw = values.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        value = str(raw).strip()
        if value.casefold() in {"", "?", "未知", "不明", "非公開", "－", "n/a", "none", "null", "-"}:
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
        elif key == "blood_type":
            value = value.upper().removesuffix("型")
            if value in {"A", "B", "AB", "O"}:
                result[key] = value
        elif key == "cup":
            value = value.upper()
            if re.fullmatch(r"[A-Z]{1,3}", value):
                result[key] = value
        elif len(value) <= 255:
            result[key] = value
    return result


def unqualified_name(value):
    return re.split(r"[（(]", value, maxsplit=1)[0].strip()


def minnanoav_ref(url):
    link = urlparse(urljoin(MINNANOAV + "/", url))
    if link.scheme == "https" and link.netloc == "www.minnano-av.com" and MINNANOAV_REF.fullmatch(link.path):
        return link.path
    return ""


def parse_minnanoav(html, ref):
    # 个别资料页会返回 200 但没有正文；这是该演员的可重试缺资料，
    # 不能把整批扫描当作站点故障中止。
    if not html.strip():
        return Profile(ref=ref, outcome="empty_profile")
    soup = BeautifulSoup(html, "html.parser")
    card, heading = soup.select_one("div.act-profile"), soup.select_one("h1")
    if card is None or heading is None:
        raise SourceError("minnanoav:profile_markup_missing")
    for span in heading.select("span"):
        span.decompose()
    names = [heading.get_text(" ", strip=True)]
    rows = {}
    for item in card.select("tr"):
        td = item.find("td", recursive=False)
        if td is None:
            continue
        label, value = td.find("span", recursive=False), td.find("p", recursive=False)
        if label is None or value is None:
            continue
        key, text = label.get_text(strip=True), value.get_text(" ", strip=True)
        if key == "別名":
            names.append(unqualified_name(text))
        rows[key] = unicodedata.normalize("NFKC", text)
    if not names[0]:
        raise SourceError("minnanoav:profile_name_missing")
    values = {}
    size = rows.get("サイズ", "")
    for letter, key in (("T", "height_cm"), ("B", "bust_cm"), ("W", "waist_cm"), ("H", "hips_cm")):
        match = re.search(letter + r"\s*([0-9]{2,3})", size)
        if match:
            values[key] = match[1]
    cup = re.search(r"B\s*[0-9]{2,3}\s*\(\s*([A-Z])\s*カップ", size)
    if cup:
        values["cup"] = cup[1]
    birthday = re.search(r"([0-9]{4})年\s*([0-9]{1,2})月\s*([0-9]{1,2})日", rows.get("生年月日", ""))
    if birthday:
        values["birthday"] = "{:04d}-{:02d}-{:02d}".format(*map(int, birthday.groups()))
    values["birthplace"] = rows.get("出身地")
    values["blood_type"] = rows.get("血液型")
    # 性别由宿主从影片元数据维护；MinnanoAV 只提供女优资料字段。
    return Profile(normalize_fields(values), tuple(n for n in names if n), ref)


def parse_actress_groups(html):
    """解析列表页侧栏的五十音分组链接；同组只保留一次。"""
    soup = BeautifulSoup(html, "html.parser")
    groups = []
    seen = set()
    for link in soup.select('a[href*="gojuon="]'):
        match = re.search(r"[?&]gojuon=([a-z]+)", link.get("href") or "")
        if match is None or match.group(1) in seen:
            continue
        code = match.group(1)
        seen.add(code)
        groups.append(ActressGroup(code, link.get_text(" ", strip=True) or code))
    if not groups:
        raise SourceError("minnanoav:group_markup_missing")
    return tuple(groups)


def _parse_list_total(soup):
    page_info = soup.select_one(".pagination .page_info")
    if page_info is not None:
        match = re.search(r"\(([\d,]+)\s*件\)", page_info.get_text(" ", strip=True))
        if match:
            return int(match.group(1).replace(",", ""))
    heading = soup.select_one("h1")
    if heading is not None:
        match = re.search(r"([\d,]+)\s*名", heading.get_text(" ", strip=True))
        if match:
            return int(match.group(1).replace(",", ""))
    return 0


def parse_actress_list(html):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.tbllist.actress")
    if table is None:
        raise SourceError("minnanoav:list_markup_missing")
    entries = []
    seen = set()
    for row in table.select("tr"):
        link = row.select_one("h2.ttl a[href]")
        if link is None:
            continue
        href = (link.get("href") or "").strip().lstrip("/")
        if not re.fullmatch(r"actress[0-9]+\.html", href):
            continue
        ref = "/" + href
        if ref in seen:
            continue
        seen.add(ref)
        furi_node = row.select_one("p.furi")
        entries.append(ActressListEntry(
            ref=ref,
            name=link.get_text(" ", strip=True),
            furi=furi_node.get_text(" ", strip=True) if furi_node else "",
        ))
    last_page = 1
    for link in soup.select('a[href*="page="]'):
        match = re.search(
            r"actress_list\.php\?[^\"']*?page=([0-9]+)", link.get("href") or ""
        )
        if match:
            last_page = max(last_page, int(match.group(1)))
    return ActressListPage(tuple(entries), last_page, _parse_list_total(soup))


class Sources:
    def __init__(self, settings: Settings):
        self.interval = settings.request_interval_seconds
        self.client = httpx.Client(timeout=settings.timeout_seconds, follow_redirects=True)
        self.last_request = None

    def close(self):
        self.client.close()

    def _get(self, url, *, headers=None):
        if self.last_request is not None:
            time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        site = "minnanoav" if url.startswith(MINNANOAV + "/") else "javdb"
        try:
            response = self.client.get(url, headers=headers)
        except httpx.HTTPError:
            # 不把异常中的 URL/请求头写进状态文件或日志。
            raise SourceError(f"{site}:network_error") from None
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise SourceError(f"{site}:http_{response.status_code}")
        return response

    def javdb(self, javdb_id):
        url = f"{JAVDB}/api/v1/actors/{quote(javdb_id, safe='')}"
        ts = str(int(time.time()))
        digest = hashlib.md5((ts + _APP_SIGNING_SEED).encode()).hexdigest()
        response = self._get(url, headers={
            "jdsignature": f"{ts}.lpw6vgqzsp.{digest}",
            "user-agent": "okhttp/4.9.0", "accept-language": "zh-TW",
        })
        if response is None:
            return Profile(ref=url, outcome="not_found")
        try:
            payload = response.json()
        except ValueError:
            raise SourceError("javdb:invalid_json") from None
        if not isinstance(payload, dict) or payload.get("success") != 1:
            raise SourceError("javdb:api_rejected")
        data = payload.get("data")
        actor = data.get("actor") if isinstance(data, dict) else None
        if not isinstance(actor, dict) or str(actor.get("id")) != javdb_id:
            raise SourceError("javdb:actor_mismatch_or_missing")
        names = [actor.get("name"), actor.get("name_zht")]
        names += re.split(r"[,，、;；/／]", actor.get("other_name") or "")
        names = tuple(dict.fromkeys(n.strip() for n in names if isinstance(n, str) and n.strip()))
        values = {key: actor.get(key) for key in PROFILE_FIELDS}
        for upstream, local in (
            ("height", "height_cm"), ("bust", "bust_cm"),
            ("waist", "waist_cm"), ("hips", "hips_cm"),
        ):
            values[local] = actor.get(upstream)
        return Profile(normalize_fields(values), names, url)

    @staticmethod
    def _minnanoav_headers():
        return {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "accept-language": "ja-JP,ja;q=0.9,en;q=0.7",
            "referer": MINNANOAV + "/",
        }

    def actress_groups(self):
        response = self._get(
            f"{MINNANOAV}/actress_list.php", headers=self._minnanoav_headers()
        )
        if response is None:
            raise SourceError("minnanoav:group_list_missing")
        return parse_actress_groups(response.text)

    def actress_list_page(self, page, *, sort="name", gojuon=""):
        params = []
        if gojuon:
            params.append(f"gojuon={quote(str(gojuon), safe='')}")
        params.append(f"page={int(page)}")
        params.append(f"sort={quote(str(sort), safe='')}")
        url = f"{MINNANOAV}/actress_list.php?" + "&".join(params)
        response = self._get(url, headers=self._minnanoav_headers())
        if response is None:
            return ActressListPage(tuple(), 0)
        return parse_actress_list(response.text)

    def minnanoav(self, names, ref="", index=None):
        """资料页直连；没有缓存 ref 或 ref 失效时，只查本地列表索引，不再请求搜索页。"""
        names = tuple(dict.fromkeys(n.strip() for n in names if n and n.strip()))
        expected = {normalize_name(n) for n in names}
        headers = self._minnanoav_headers()

        def checked(response):
            actual_ref = minnanoav_ref(str(response.url))
            if not actual_ref:
                raise SourceError("minnanoav:unexpected_redirect")
            profile = parse_minnanoav(response.text, actual_ref)
            if profile.outcome != "ok":
                return profile
            if not any(normalize_name(n) in expected for n in profile.names):
                return Profile(outcome="identity_mismatch")
            return profile

        outcome = "not_found"
        if ref:
            if not MINNANOAV_REF.fullmatch(ref):
                raise SourceError("minnanoav:invalid_mapping")
            response = self._get(MINNANOAV + ref, headers=headers)
            if response is not None:
                profile = checked(response)
                if profile.outcome in {"ok", "empty_profile"}:
                    return profile
                outcome = profile.outcome
        if index is None:
            return Profile(outcome=outcome)
        refs = index.lookup(names)
        if len(refs) > 1:
            return Profile(outcome="ambiguous")
        if not refs:
            return Profile(outcome=outcome)
        candidate = refs[0]
        if candidate == ref:
            return Profile(outcome=outcome)
        response = self._get(MINNANOAV + candidate, headers=headers)
        if response is None:
            index.remove(candidate)
            return Profile(outcome="not_found")
        profile = checked(response)
        if profile.outcome not in {"ok", "empty_profile"}:
            outcome = profile.outcome
            # 名字不一致可能是 JavDB 用了罗马字/中文名，不能据此删掉正确的
            # 索引 ref；只有资料页 404 才说明映射失效。
            if profile.outcome != "identity_mismatch":
                index.remove(candidate)
        return profile if profile.outcome in {"ok", "empty_profile"} else Profile(outcome=outcome)
