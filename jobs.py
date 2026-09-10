"""每天筛选到期演员；完成、耗尽次数的演员不再访问来源。"""

import re
import time

import portalocker

from .sources import MINNANOAV, SourceError, Sources, normalize_fields
from .state import State

MAX_CONSECUTIVE_NETWORK_ERRORS = 3


def missing_fields(snapshot, settings):
    valid = normalize_fields(snapshot.values)
    return [key for key in settings.target_fields if key not in valid]


def is_empty_field_value(name, value):
    return value in (None, "") or (name == "gender" and value == 0)


def writable_fields(snapshot, missing, owner):
    # 非空但无效的历史值也不覆盖；需要用户自行清理后明确重新入队。
    return [key for key in missing if is_empty_field_value(key, snapshot.values.get(key))
            and snapshot.owners.get(key) in (None, owner)]


def iter_actor_snapshot_batches(context):
    after_id = 0
    while True:
        page = context.actors.list_page(after_id=after_id, limit=500)
        yield page.items
        if page.next_cursor is None:
            return
        after_id = page.next_cursor


def prioritize_actor_snapshots(context, snapshots, reporter):
    movie_counts = {snapshot.actor_id: 0 for snapshot in snapshots}
    scanned_movies = 0
    if reporter is not None:
        reporter.progress_callback({"text": "正在计算演员优先级，已扫描 0 部影片"})
    after_id = 0
    while True:
        page = context.movies.list_page(after_id=after_id, limit=500)
        for movie in page.items:
            values = movie.values
            if values["is_collection"]:
                continue
            for actor in movie.actors:
                if actor.actor_id not in movie_counts:
                    continue
                movie_counts[actor.actor_id] += 1
        scanned_movies += len(page.items)
        if reporter is not None:
            reporter.progress_callback({
                "text": f"正在计算演员优先级，已扫描 {scanned_movies} 部影片",
            })
        if page.next_cursor is None:
            break
        after_id = page.next_cursor
    return sorted(snapshots, key=lambda snapshot: (
        not snapshot.values.get("is_subscribed"),
        -movie_counts[snapshot.actor_id],
        snapshot.actor_id,
    ))


def process(
    context,
    settings,
    state,
    sources,
    reporter=None,
    *,
    now=None,
    logger=None,
):
    now = time.time if now is None else now
    owner = f"plugin:{context.plugin_id}"
    stats = dict(scanned=0, attempted=0, updated=0, completed=0, stopped=0,
                 waiting=0, skipped=0, errors=0, aborted=0)
    consecutive_network_errors = 0
    actor_snapshots = tuple(
        snapshot
        for snapshot_batch in iter_actor_snapshot_batches(context)
        for snapshot in snapshot_batch
    )
    total = len(actor_snapshots)
    if logger is not None:
        logger.info("待扫描演员数 = {}", total)

    def report_progress():
        if reporter is not None:
            reporter.progress_callback({
                "current": stats["scanned"],
                "total": total,
                "text": (
                    f"已扫描 {stats['scanned']}/{total} 位，"
                    f"尝试 {stats['attempted']} 位，补齐 {stats['completed']} 位，"
                    f"停止 {stats['stopped']} 位"
                ),
            })

    if actor_snapshots:
        actor_snapshots = prioritize_actor_snapshots(context, actor_snapshots, reporter)

    if reporter is not None:
        reporter.progress_callback({
            "current": 0,
            "total": total,
            "text": f"开始扫描演员，共 {total} 位",
        })

    for snapshot in actor_snapshots:
        stats["scanned"] += 1
        if snapshot.values.get("gender") == 2 or (
            settings.subscribed_only and not snapshot.values.get("is_subscribed")
        ):
            stats["skipped"] += 1
            report_progress()
            continue
        javdb_id = snapshot.values.get("javdb_id") or ""
        row = state.get(snapshot.actor_id, javdb_id)
        if row["status"] in {"completed", "stopped"}:
            stats["skipped"] += 1
            report_progress()
            continue
        missing = missing_fields(snapshot, settings)
        row["missing_fields"] = missing
        if not missing:
            row.update(status="completed", next_attempt_at=None, reason="complete")
            state.save(row)
            stats["completed"] += 1
            report_progress()
            continue
        if row["attempts"] >= settings.max_attempts:
            row.update(status="stopped", next_attempt_at=None,
                       reason=row["reason"] or "attempt_limit")
            state.save(row)
            stats["stopped"] += 1
            report_progress()
            continue
        started_at = now()
        if row["next_attempt_at"] is not None and row["next_attempt_at"] > started_at:
            stats["skipped"] += 1
            report_progress()
            continue
        writable = writable_fields(snapshot, missing, owner)
        if not writable:
            row.update(status="stopped", next_attempt_at=None, reason="fields_not_writable")
            state.save(row)
            stats["stopped"] += 1
            report_progress()
            continue
        # 请求前提交次数，进程被终止后也不能绕开上限。
        row.update(status="waiting", attempts=row["attempts"] + 1,
                   last_attempt_at=started_at,
                   next_attempt_at=started_at + settings.retry_days * 86400,
                   reason="interrupted")
        state.save(row)
        stats["attempted"] += 1
        updates, provenance, outcomes = {}, {}, []
        javdb_male = False
        aborted = False
        names = [snapshot.values.get("name") or ""]
        names += re.split(r"[,，、;；/／]", snapshot.values.get("alias_name") or "")
        try:
            if javdb_id:
                profile = sources.javdb(javdb_id)
                javdb_male = profile.fields.get("gender") == 2
                names = [*profile.names, *names]
                outcomes.append("javdb:" + profile.outcome)
                for key, value in profile.fields.items():
                    if key in writable:
                        updates[key] = value
                        provenance[key] = {"url": profile.ref, "fetched_at": started_at}
            if not javdb_male and any(key not in updates for key in writable):
                profile = sources.minnanoav(
                    names, row["minnanoav_ref"],
                )
                outcomes.append("minnanoav:" + profile.outcome)
                if profile.outcome == "ok":
                    row["minnanoav_ref"] = profile.ref
                    for key, value in profile.fields.items():
                        if key in writable and key not in updates:
                            updates[key] = value
                            provenance[key] = {"url": MINNANOAV + profile.ref,
                                               "fetched_at": started_at}
                elif profile.outcome in {"not_found", "identity_mismatch"}:
                    row["minnanoav_ref"] = ""
        except SourceError as exc:
            outcomes.append(str(exc))
            stats["errors"] += 1
            if str(exc).endswith(":network_error"):
                consecutive_network_errors += 1
                aborted = consecutive_network_errors >= MAX_CONSECUTIVE_NETWORK_ERRORS
            else:
                consecutive_network_errors = 0
            if logger is not None and str(exc).endswith(":network_error"):
                logger.warning(
                    "网络请求异常 actor_id={} reason={} consecutive_network_errors={}/{}",
                    snapshot.actor_id, exc, consecutive_network_errors,
                    MAX_CONSECUTIVE_NETWORK_ERRORS,
                )
            elif logger is not None:
                logger.warning("来源响应异常 actor_id={} reason={}，继续下一位演员", snapshot.actor_id, exc)
        else:
            consecutive_network_errors = 0

        # 网络请求期间宿主资料可能被其他任务更新，重新读取再执行乐观写入。
        latest = context.actors.get(snapshot.actor_id)
        if latest is None or latest.values.get("javdb_id") != javdb_id:
            row.update(status="stopped", reason="actor_changed_or_deleted", next_attempt_at=None)
            stats["stopped"] += 1
        else:
            allowed = writable_fields(latest, missing_fields(latest, settings), owner)
            patch = {key: value for key, value in updates.items() if key in allowed}
            if patch:
                if context.actors.patch(latest.actor_id, patch, expected_revision=latest.revision):
                    stats["updated"] += 1
                    row["sources"].update({key: provenance[key] for key in patch})
                else:
                    outcomes.append("patch_conflict")
                latest = context.actors.get(snapshot.actor_id)
            row["missing_fields"] = missing_fields(latest, settings) if latest else missing
            row["reason"] = ";".join(outcomes) or "no_data"
            if latest and not row["missing_fields"]:
                row.update(status="completed", next_attempt_at=None, reason="complete")
                stats["completed"] += 1
            elif row["attempts"] >= settings.max_attempts:
                row.update(status="stopped", next_attempt_at=None)
                stats["stopped"] += 1
            else:
                row["next_attempt_at"] = now() + settings.retry_days * 86400
                stats["waiting"] += 1
        state.save(row)
        report_progress()
        if aborted:
            stats["aborted"] = 1
            return stats
    return stats


def run(context, settings, reporter):
    logger = context.get_task_logger("actor-metadata")
    logger.info("女优资料补全开始")
    with portalocker.Lock(str(context.data_dir / "actor_metadata.lock"), mode="a", timeout=0):
        state = State(context.data_dir / "actor_metadata.sqlite3")
        if state.requeued_count:
            logger.info("资料来源升级，已重新入队 {} 位未补齐演员", state.requeued_count)
        sources = Sources(settings)
        try:
            stats = process(
                context, settings, state, sources, reporter,
                logger=logger,
            )
        finally:
            sources.close()
    logger.info("女优资料补全完成 stats={}", stats)
    if stats["aborted"]:
        raise SourceError(
            f"连续 {MAX_CONSECUTIVE_NETWORK_ERRORS} 位演员网络请求异常，本轮已停止；"
            "当前演员状态已保存，未开始的演员不计次数"
        )
    return stats
