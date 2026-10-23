"""公开女优列表索引：姓名到资料页的映射。

搜索接口被 Cloudflare 的 JS 挑战拦截，列表页和资料页不受影响；索引只抓列表页。
索引是可丢弃缓存，放在独立 SQLite 文件里，不参与演员状态和重试记录。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from .sources import (
    ActressGroup,
    ActressListEntry,
    SourceError,
    Sources,
    normalize_name,
    unqualified_name,
)

# 老版本用 sort=date 建索引，会在站点约 5,000 条分页上限处截断。
# 版本号变化会触发一次完整的五十音分组重建，用户不需要手动删索引文件。
INDEX_VERSION = 2
INCREMENTAL_REFRESH_SECONDS = 86400
INCREMENTAL_PAGES = 3
LIST_PAGE_SIZE = 30
MAX_GROUP_PAGES = 500
FULL_INDEX_MIN_RATIO = 0.95
BUNDLED_INDEX = Path(__file__).with_name("actress_index.json")


def _index_meta(path: Path) -> dict[str, str]:
    try:
        with sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True) as db:
            return dict(db.execute("SELECT key, value FROM actress_index_meta"))
    except (OSError, sqlite3.DatabaseError):
        return {}


def _bundled_index() -> tuple[int, float, tuple[tuple[str, str, str], ...]] | None:
    try:
        payload = json.loads(BUNDLED_INDEX.read_text(encoding="utf-8"))
        version = int(payload["index_version"])
        built_at = float(payload["full_built_at"])
        entries = payload["entries"]
        if version != INDEX_VERSION or built_at <= 0 or not isinstance(entries, list):
            return None
        rows = tuple(tuple(entry) for entry in entries)
        if not rows or any(
            len(row) != 3 or not all(isinstance(value, str) for value in row)
            for row in rows
        ):
            return None
        if int(payload.get("rows", len(rows))) != len(rows):
            return None
        return version, built_at, rows
    except (KeyError, OSError, TypeError, ValueError):
        return None


def seed_bundled_index(path: Path) -> bool:
    """安装或升级到新内置索引时，把文本种子转成插件数据目录里的 SQLite。"""
    target = Path(path)
    bundled = _bundled_index()
    if target.resolve() == BUNDLED_INDEX.resolve() or bundled is None:
        return False
    bundled_version, bundled_built_at, rows = bundled
    current = _index_meta(target) if target.is_file() else {}
    if current and not current.get("full_build_id"):
        current_version = int(current.get("index_version", 0) or 0)
        if current_version > bundled_version:
            return False
        if (
            current_version == bundled_version
            and float(current.get("full_built_at", 0) or 0)
            >= bundled_built_at
        ):
            return False
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    try:
        temporary.unlink(missing_ok=True)
        index = ActressIndex(temporary)
        index.upsert(tuple(ActressListEntry(*row) for row in rows))
        index.set_meta("index_version", bundled_version)
        index.set_meta("full_built_at", bundled_built_at)
        index.set_meta("incremental_at", bundled_built_at)
        index.set_meta("incremental_requires_full", 0)
        index.set_meta("full_build_id", "")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _furi_parts(furi: str) -> tuple[str, str]:
    text = (furi or "").strip()
    if not text:
        return "", ""
    parts = [part.strip() for part in text.split("/", 1)]
    return normalize_name(parts[0]), normalize_name(parts[1]) if len(parts) > 1 else ""


class ActressIndex:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS actress_index (
                    ref TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    name_norm TEXT NOT NULL,
                    furi TEXT NOT NULL DEFAULT '',
                    kana_norm TEXT NOT NULL DEFAULT '',
                    roman_norm TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    last_build_id TEXT NOT NULL DEFAULT ''
                )
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(actress_index)")}
            if "last_build_id" not in columns:
                db.execute(
                    "ALTER TABLE actress_index ADD COLUMN last_build_id TEXT NOT NULL DEFAULT ''"
                )
            for column in ("name_norm", "kana_norm", "roman_norm"):
                db.execute(
                    f"CREATE INDEX IF NOT EXISTS actress_index_{column} "
                    f"ON actress_index ({column})"
                )
            db.execute("""
                CREATE TABLE IF NOT EXISTS actress_index_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def lookup(self, names) -> list[str]:
        keys = set()
        for name in names:
            if not isinstance(name, str) or not name.strip():
                continue
            keys.add(normalize_name(name))
            keys.add(normalize_name(unqualified_name(name)))
        keys.discard("")
        if not keys:
            return []
        placeholders = ",".join(["?"] * len(keys))
        params = (*keys, *keys, *keys)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT DISTINCT ref FROM actress_index "
                f"WHERE name_norm IN ({placeholders}) "
                f"OR kana_norm IN ({placeholders}) "
                f"OR roman_norm IN ({placeholders})",
                params,
            ).fetchall()
        return sorted(row["ref"] for row in rows)

    def upsert(self, entries: tuple[ActressListEntry, ...], *, build_id=None) -> int:
        now = time.time()
        rows = []
        for entry in entries:
            name = (entry.name or "").strip()
            name_norm = normalize_name(unqualified_name(name))
            if not name_norm:
                continue
            kana_norm, roman_norm = _furi_parts(entry.furi)
            rows.append((
                entry.ref, name, name_norm, (entry.furi or "").strip(),
                kana_norm, roman_norm, now,
            ))
        if not rows:
            return 0
        with self.connect() as db:
            if build_id is None:
                # 增量更新保留上一次全量构建的标记，避免被下一次清理误删。
                db.executemany(
                    """
                    INSERT INTO actress_index
                        (ref, name, name_norm, furi, kana_norm, roman_norm,
                         updated_at, last_build_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, '')
                    ON CONFLICT(ref) DO UPDATE SET
                        name = excluded.name,
                        name_norm = excluded.name_norm,
                        furi = excluded.furi,
                        kana_norm = excluded.kana_norm,
                        roman_norm = excluded.roman_norm,
                        updated_at = excluded.updated_at
                    """,
                    rows,
                )
            else:
                db.executemany(
                    """
                    INSERT INTO actress_index
                        (ref, name, name_norm, furi, kana_norm, roman_norm,
                         updated_at, last_build_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(ref) DO UPDATE SET
                        name = excluded.name,
                        name_norm = excluded.name_norm,
                        furi = excluded.furi,
                        kana_norm = excluded.kana_norm,
                        roman_norm = excluded.roman_norm,
                        updated_at = excluded.updated_at,
                        last_build_id = excluded.last_build_id
                    """,
                    [(*row, build_id) for row in rows],
                )
        return len(rows)

    def new_refs(self, entries) -> int:
        """这一页有多少个 ref 还不在索引里；增量用它判断是否已经追到旧数据。"""
        refs = {
            entry.ref for entry in entries
            if normalize_name(unqualified_name(entry.name or ""))
        }
        if not refs:
            return 0
        placeholders = ",".join(["?"] * len(refs))
        with self.connect() as db:
            rows = db.execute(
                f"SELECT ref FROM actress_index WHERE ref IN ({placeholders})",
                tuple(refs),
            ).fetchall()
        return len(refs) - len(rows)

    def count(self, build_id=None) -> int:
        with self.connect() as db:
            if build_id is None:
                return int(db.execute("SELECT count(*) FROM actress_index").fetchone()[0])
            return int(db.execute(
                "SELECT count(*) FROM actress_index WHERE last_build_id = ?",
                (build_id,),
            ).fetchone()[0])

    def prune(self, build_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "DELETE FROM actress_index WHERE last_build_id != ?", (build_id,)
            )

    def remove(self, ref: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM actress_index WHERE ref = ?", (ref,))

    def meta(self, key: str, default=None):
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM actress_index_meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO actress_index_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def ensure(self, sources: Sources, *, reporter=None, logger=None, now=None) -> None:
        """确保索引可用：缺失、格式升级或积压时全量，平时每天只补最新几页。"""
        current = time.time() if now is None else float(now)
        full_built_at_raw = self.meta("full_built_at")
        version = int(self.meta("index_version", 0) or 0)
        in_progress = bool(self.meta("full_build_id"))
        requires_full = bool(int(self.meta("incremental_requires_full", 0) or 0))
        incremental_raw = self.meta("incremental_at")
        incremental_at = float(incremental_raw or 0)
        needs_full = (
            version < INDEX_VERSION
            or full_built_at_raw is None
            or in_progress
            or requires_full
        )
        if needs_full:
            try:
                self._refresh_full(sources, reporter, logger, current)
            except SourceError as exc:
                # 首次构建或旧版索引升级失败时，不能带着 5k 索引继续跑演员；
                # 否则大量 not_found 会消耗演员的重试次数。
                if version < INDEX_VERSION or full_built_at_raw is None:
                    raise SourceError(f"minnanoav:index_build_failed:{exc}") from exc
                # 已经有一份完整索引时，全量刷新失败仍可继续使用旧索引。
                if logger is not None:
                    logger.warning("女优索引全量刷新失败，继续使用旧索引")
        elif incremental_raw is None or current - incremental_at >= INCREMENTAL_REFRESH_SECONDS:
            try:
                self._refresh_incremental(sources, reporter, logger, current)
            except SourceError:
                if logger is not None:
                    logger.warning("女优索引增量刷新失败，继续使用旧索引")

    def _stored_groups(self) -> tuple[ActressGroup, ...]:
        raw = self.meta("full_groups")
        if not raw:
            return ()
        try:
            return tuple(
                ActressGroup(item["code"], item.get("label") or item["code"])
                for item in json.loads(raw)
            )
        except (TypeError, ValueError, KeyError):
            return ()

    def _start_full_build(self, sources: Sources) -> tuple[str, tuple[ActressGroup, ...]]:
        groups = sources.actress_groups()
        if not groups:
            raise SourceError("minnanoav:group_list_empty")
        build_id = uuid.uuid4().hex
        self.set_meta("full_build_id", build_id)
        self.set_meta("full_groups", json.dumps(
            [{"code": group.code, "label": group.label} for group in groups],
            ensure_ascii=False,
        ))
        self.set_meta("full_group_index", 0)
        self.set_meta("full_group_page", 1)
        self.set_meta("full_group_code", "")
        return build_id, groups

    def _refresh_full(self, sources, reporter, logger, current) -> None:
        build_id = self.meta("full_build_id", "") or ""
        groups = self._stored_groups() if build_id else ()
        if not build_id or not groups:
            build_id, groups = self._start_full_build(sources)

        start_group = int(self.meta("full_group_index", 0) or 0)
        start_group = min(max(start_group, 0), len(groups))
        start_page = int(self.meta("full_group_page", 1) or 1)
        start_code = self.meta("full_group_code", "") or ""

        for group_index in range(start_group, len(groups)):
            group = groups[group_index]
            page_no = start_page if group_index == start_group else 1
            total_key = f"full_total:{build_id}:{group.code}"
            pages_key = f"full_pages:{build_id}:{group.code}"
            pages_total = int(self.meta(pages_key, 0) or 0)
            if page_no > 1 and start_code != group.code:
                page_no = 1
            if page_no > 1 and (
                self.meta(total_key) is None or pages_total <= 0
            ):
                # 断点落在组中间但没有记录该组总页数时，从头重抓这一组，
                # 不能用组内分页条上的局部页码推断总页数。
                page_no = 1
            if page_no == 1:
                self.set_meta("full_group_index", group_index)
                self.set_meta("full_group_page", 1)
                self.set_meta("full_group_code", group.code)
            previous_refs = None

            while True:
                if page_no > MAX_GROUP_PAGES:
                    raise SourceError("minnanoav:list_page_too_deep")
                page = sources.actress_list_page(
                    page_no, sort="name", gojuon=group.code
                )
                if page_no == 1:
                    total = page.total or len(page.entries)
                    self.set_meta(total_key, total)
                    if total == 0 and not page.entries:
                        if logger is not None:
                            logger.info("女优索引分组为空 code={}", group.code)
                        break
                    if not page.entries:
                        raise SourceError("minnanoav:list_page_empty")
                elif not page.entries:
                    if pages_total and page_no <= pages_total:
                        raise SourceError("minnanoav:list_page_empty")
                    break

                if page.last_page:
                    # 组内后续页的分页条只显示当前窗口，不能覆盖第一页的总页数。
                    pages_total = max(pages_total, page.last_page)
                    self.set_meta(pages_key, pages_total)
                page_refs = {entry.ref for entry in page.entries}
                if previous_refs is not None and page_refs == previous_refs:
                    if logger is not None:
                        logger.info(
                            "女优索引分组分页重复 code={} page={}", group.code, page_no
                        )
                    break
                previous_refs = page_refs
                self.upsert(page.entries, build_id=build_id)
                # 先写数据再写断点；断点记录“下一页”，中断后不会跳过当前页。
                self.set_meta("full_group_index", group_index)
                self.set_meta("full_group_page", page_no + 1)
                self.set_meta("full_group_code", group.code)
                if reporter is not None:
                    reporter.progress_callback({
                        "current": group_index + 1,
                        "total": len(groups),
                        "text": (
                            f"建立女优索引 五十音 {group.label} "
                            f"{page_no}/{pages_total or '?'}（{group_index + 1}/{len(groups)}）"
                        ),
                    })
                if logger is not None:
                    logger.info(
                        "女优索引建立进度 {}/{} page={}/{} code={}",
                        group_index + 1, len(groups), page_no,
                        pages_total or "?", group.code,
                    )

                if pages_total and page_no >= pages_total:
                    # 第一页记录的总页数可能滞后；末页仍是满页且总人数更多时再翻一页。
                    if (
                        page.total
                        and page.total > page_no * LIST_PAGE_SIZE
                        and len(page.entries) >= LIST_PAGE_SIZE
                    ):
                        page_no += 1
                        continue
                    break
                page_no += 1

            self.set_meta("full_group_index", group_index + 1)
            self.set_meta("full_group_page", 1)
            self.set_meta("full_group_code", "")
            start_page = 1

        expected = sum(
            int(self.meta(f"full_total:{build_id}:{group.code}", 0) or 0)
            for group in groups
        )
        # 只统计本次构建写入的行；旧索引尚未清理，不能把旧行算成新索引的完整性。
        rows = self.count(build_id)
        if rows == 0 or (expected and rows < expected * FULL_INDEX_MIN_RATIO):
            # 留一份可重试的断点：下次从头重建，而不是把不完整索引标成完成。
            self.set_meta("full_group_index", 0)
            self.set_meta("full_group_page", 1)
            self.set_meta("full_group_code", "")
            raise SourceError("minnanoav:index_incomplete")

        self.prune(build_id)
        self.set_meta("index_version", INDEX_VERSION)
        self.set_meta("full_built_at", current)
        self.set_meta("incremental_at", current)
        self.set_meta("incremental_requires_full", 0)
        self.set_meta("full_build_id", "")
        self.set_meta("full_group_index", 0)
        self.set_meta("full_group_page", 1)
        self.set_meta("full_group_code", "")
        if logger is not None:
            logger.info(
                "女优索引建立完成 groups={} rows={} expected={}",
                len(groups), rows, expected,
            )

    def _refresh_incremental(self, sources, reporter, logger, current) -> None:
        for page_no in range(1, INCREMENTAL_PAGES + 1):
            page = sources.actress_list_page(page_no, sort="date")
            if not page.entries:
                if page_no == 1:
                    raise SourceError("minnanoav:list_page_empty")
                break
            new_count = self.new_refs(page.entries)
            self.upsert(page.entries)
            if logger is not None:
                logger.info(
                    "女优索引增量进度 page={} new_refs={}", page_no, new_count
                )
            if reporter is not None:
                reporter.progress_callback({
                    "current": page_no,
                    "total": INCREMENTAL_PAGES,
                    "text": f"更新女优索引 {page_no}/{INCREMENTAL_PAGES}",
                })
            if new_count == 0:
                break
            if page_no == INCREMENTAL_PAGES:
                # 到封顶页仍有新 ref，说明积压超过一次增量能覆盖的范围；
                # 下一轮改走全量，避免每天都只补前 3 页。
                self.set_meta("incremental_requires_full", 1)
                if logger is not None:
                    logger.warning("女优索引增量达到页数上限，下一轮将执行全量重建")
        self.set_meta("incremental_at", current)
        if logger is not None:
            logger.info("女优索引增量更新完成")
