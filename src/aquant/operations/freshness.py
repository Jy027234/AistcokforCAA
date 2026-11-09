"""数据新鲜度：快照落后了没有（§5.4 顶栏的一个真实风险）。

为什么需要它
------------
没人会注意到快照落后了。界面一切正常：数字自洽、对账通过、
研究卡能打开——而它们基于的是三天前的数据。

光看"上次跑成功是什么时候"也不够：**天天跑成功但数据源没更新**，
快照照样停在几天前。因此这里比较的是
**快照覆盖的最后一个交易日**与**采集实际拿到的最后一个交易日**：
两者不一致就是落后了；若较晚目标日的日更明确失败，也会提示仍在使用旧快照。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

#: 落后多少个**自然日**才算需要提醒。
#:
#: 刻意给得宽松：跨周末本来就会有 3 个自然日的间隔，把正常状态做成提醒，
#: 真正的落后就没人看了。
#:
#: 口径说明：这里数的是自然日，不是交易日。数交易日更贴近"落后了几个
#: 交易日"的直觉，但需要一份权威日历（而快照里的日历只覆盖自己的窗口，
#: 过去的日期不在里面）。自然日偏保守——它会比交易日更早提醒，
#: 对"数据可能过期"这件事来说，早提醒比漏提醒好。
STALE_AFTER_DAYS = 4

# 仅在运行留痕明确给出受支持的失败类型时改写面向用户的原因。
# 原始 `reason` 仍保留在日志和 lastAttempt.reason 中供诊断。
_DISPLAY_FAILURE_REASONS = {
    "BAOSTOCK_LOGIN_TIMEOUT": "模拟行情数据源（BaoStock）登录超时",
}


@dataclass(slots=True)
class Freshness:
    snapshot_day: str | None          # 快照覆盖的最后一个交易日
    data_last_day: str | None         # 采集实际拿到的最后一个交易日
    last_published_day: str | None    # 上次成功发布的交易日
    last_attempt: dict | None         # 最近一次相关运行；已核验休市跳过不覆盖
    staleness_days: int | None        # 快照落后采集多少个自然日
    stale: bool
    detail: str

    def as_dict(self) -> dict:
        return {
            "snapshotDay": self.snapshot_day,
            "dataLastDay": self.data_last_day,
            "lastPublishedDay": self.last_published_day,
            "lastAttempt": self.last_attempt,
            "stalenessDays": self.staleness_days,
            "stale": self.stale,
            "detail": self.detail,
        }


def _entries(log_path: Path, limit: int = 200) -> list[dict]:
    if not log_path.exists():
        return []
    # 运行日志是 JSONL：**逐行读、坏行跳过**。整个文件解析会因为
    # 最后一行写到一半（进程被杀）而全盘失败，而那时恰恰最需要它。
    out: list[dict] = []
    for line in log_path.read_text(encoding="utf-8").splitlines()[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _trim_attempt(entry: dict | None) -> dict | None:
    """只留界面需要的字段。

    运行留痕里带着每一步的输出尾巴（给排查用），整个塞进 /status
    会让一个状态接口返回几十 KB 的日志。该给排查用的留在文件里，
    接口只回答"跑了没有、成没成、为什么"。
    """

    if not entry:
        return None
    attempt = {
        "tradingDay": entry.get("trading_day"),
        "outcome": entry.get("outcome"),
        "reason": entry.get("reason"),
        "finishedAt": entry.get("finished_at"),
        "steps": [{"step": s.get("step"), "ok": s.get("ok")}
                  for s in (entry.get("steps") or [])],
    }
    display_reason = _display_reason(entry)
    if display_reason != entry.get("reason"):
        attempt["displayReason"] = display_reason
    gate = entry.get("calendarGate")
    if isinstance(gate, dict) and isinstance(gate.get("isTradingDay"), bool):
        # `/status` 需要用权威日历结果区分休市跳过和运行异常；不透传整份
        # 归档日历证明，只保留决定状态所需的一位布尔值。
        attempt["calendarGate"] = {"isTradingDay": gate["isTradingDay"]}
    elif _is_closed_day_skip(entry):
        # 同时兼容较早 JSONL 形状；已由 _is_closed_day_skip 确认的休市，
        # 至少在摘要里保留可供 API/界面识别的确定事实。
        attempt["calendarGate"] = {"isTradingDay": False}
    return attempt


def _display_reason(entry: dict | None) -> str | None:
    """用明确的结构化失败类型给普通界面提供可读原因。"""

    if not entry:
        return None
    if entry.get("outcome") != "FAILED":
        return entry.get("reason")
    retryable_failure = entry.get("retryableFailure")
    kind = (retryable_failure.get("kind")
            if isinstance(retryable_failure, dict) else None)
    display_reason = (_DISPLAY_FAILURE_REASONS.get(kind)
                      if isinstance(kind, str) else None)
    return display_reason or entry.get("reason")


def _is_closed_day_skip(entry: dict) -> bool:
    """已核验为休市日的 SKIPPED 不是一次数据刷新结果。"""

    gate = entry.get("calendarGate")
    return (entry.get("outcome") == "SKIPPED"
            and isinstance(gate, dict) and gate.get("isTradingDay") is False)


def _date_gap(later: str | None, earlier: str | None) -> int | None:
    if not later or not earlier:
        return None
    try:
        return (date.fromisoformat(later) - date.fromisoformat(earlier)).days
    except ValueError:
        return None


def _unresolved_failure(entries: list[dict]) -> dict | None:
    """尚未被覆盖失败目标日的后续发布恢复的最近失败。

    旧日回补不会解除更晚目标日的失败。历史记录若缺少可比较日期，
    保留原有按日志顺序恢复的兼容行为。
    """

    for failure_index in range(len(entries) - 1, -1, -1):
        failure = entries[failure_index]
        if failure.get("outcome") != "FAILED":
            continue
        failure_day = failure.get("trading_day")
        resolved = False
        for entry in entries[failure_index + 1:]:
            if entry.get("outcome") != "PUBLISHED":
                continue
            publish_gap = _date_gap(entry.get("trading_day"), failure_day)
            if publish_gap is None or publish_gap >= 0:
                resolved = True
                break
        if not resolved:
            return failure
    return None


def age_from_snapshot(snapshot_day: str | None, *, today: date | None = None) -> int | None:
    """只凭快照末日算它有多旧（自然日）。

    这一项**不依赖任何文件**——快照自己就带着时点。因此它是兜底：
    没有运行留痕时（手工建的快照、换过数据目录、容器里没带留痕），
    仍然能回答"这份数据有多旧"。
    """

    if not snapshot_day:
        return None
    try:
        return ((today or date.today()) - date.fromisoformat(snapshot_day)).days
    except ValueError:
        return None


def freshness(log_path: Path, *, snapshot_day: str | None) -> Freshness:
    """按运行留痕判断快照的新鲜度。**读不出就如实说读不出**，不猜。

    两条判据，缺一不可：

      * **快照与采集的差距**（来自留痕）——能区分"数据源没更新"与"流水线没跑"；
      * **快照自身有多旧**（不依赖任何文件）——没有留痕时的兜底。

    只有前者会在换过数据目录或手工建快照时失效，而那时恰恰最需要提醒。
    """

    entries = _entries(log_path)
    if not entries:
        age = age_from_snapshot(snapshot_day)
        stale = age is not None and age >= STALE_AFTER_DAYS
        detail = ("没有运行留痕：无法判断采集是否已更新。"
                  + (f"快照覆盖到 {snapshot_day}，距今 {age} 个自然日。"
                     if age is not None else "快照也没记录覆盖到哪一天。")
                  + (f"（超过 {STALE_AFTER_DAYS} 个自然日即视为可能过期）"
                     if age is not None else "")
                  + ("**数据可能已经过期。**" if stale else ""))
        return Freshness(
            snapshot_day=snapshot_day, data_last_day=None,
            last_published_day=None, last_attempt=None,
            staleness_days=age, stale=stale, detail=detail)

    published = [e for e in entries if e.get("outcome") == "PUBLISHED"]
    last_pub = published[-1] if published else None
    # 休市日跳过不会采集或发布数据，不应覆盖最近一次有意义的发布/失败。
    # 其他 SKIPPED 仍保留为最近运行结果，避免推测其业务含义。
    attempt_index = next(
        (index for index in range(len(entries) - 1, -1, -1)
         if not _is_closed_day_skip(entries[index])),
        len(entries) - 1,
    )
    last_attempt = entries[attempt_index]
    latest_closed_skip = next(
        ((index, entry) for index, entry in reversed(list(enumerate(entries)))
         if _is_closed_day_skip(entry)),
        None,
    )
    closed_skip_after_attempt = (
        latest_closed_skip is not None and latest_closed_skip[0] > attempt_index
    )
    unresolved_failure = _unresolved_failure(entries)
    unresolved_failure_has_later_publish = False
    if unresolved_failure is not None:
        failure_index = next(
            index for index, entry in enumerate(entries)
            if entry is unresolved_failure
        )
        unresolved_failure_has_later_publish = any(
            entry.get("outcome") == "PUBLISHED"
            for entry in entries[failure_index + 1:]
        )

    def same_day_was_published(entry: dict) -> bool:
        """重复登记已发布目标日是幂等跳过，不代表刷新失败。"""

        day = entry.get("trading_day")
        return bool(day and any(
            published_entry.get("trading_day") == day
            for published_entry in published
        ))

    anomalous_skip = (
        last_attempt.get("outcome") == "SKIPPED"
        and not _is_closed_day_skip(last_attempt)
        and not same_day_was_published(last_attempt)
    )

    # 采集实际拿到的末日：优先取最近一次带该字段的记录。若最近一次
    # 尝试显式写了 null，它表示本次没有拿到末日，不能倒退到更早记录
    # 并把旧采集结果误当成最近一次运行的数据。
    data_last = None
    if (last_attempt.get("outcome") == "FAILED"
            and "data_last_day" in last_attempt):
        # 失败记录显式写 null 时，本轮确实没有取得采集末日；不能把更早
        # 的成功采集日期说成本次运行的结果。
        data_last = last_attempt.get("data_last_day")
    elif (unresolved_failure is not None
            and not unresolved_failure_has_later_publish
            and "data_last_day" in unresolved_failure
            and unresolved_failure.get("data_last_day") is None):
        # 之后的 SKIPPED 没有启动采集，也不能让此前失败的 null 被旧日期覆盖。
        data_last = None
    else:
        for entry in reversed(entries):
            if entry.get("data_last_day"):
                data_last = entry["data_last_day"]
                break

    failed_attempt = last_attempt.get("outcome") == "FAILED"
    attempt_day = last_attempt.get("trading_day")
    attempt_gap = _date_gap(attempt_day, snapshot_day)
    failed_attempt_gap = _date_gap(
        (unresolved_failure or {}).get("trading_day"), snapshot_day,
    )

    def failed_detail(*, old_snapshot: bool) -> str:
        failure = unresolved_failure if unresolved_failure is not None else last_attempt
        target_day = failure.get("trading_day")
        target = f"（目标日 {target_day}）" if target_day else ""
        reason = _display_reason(failure)
        cause = f"，原因：{reason}" if reason else ""
        if old_snapshot and snapshot_day:
            return (f"最近一次日更{target}失败{cause}；当前仍使用覆盖到 "
                    f"{snapshot_day} 的快照，无法确认该次运行拿到更新数据。")
        return f"最近一次日更{target}失败{cause}。"

    def skip_detail(*, old_snapshot: bool) -> str:
        reason = last_attempt.get("reason")
        cause = f"：{reason}" if reason else ""
        target = f"（目标日 {attempt_day}）" if attempt_day else ""
        if old_snapshot and snapshot_day:
            return (f"最近一次日更{target}因异常跳过{cause}；当前仍使用覆盖到 "
                    f"{snapshot_day} 的快照，本次未记录采集末日，数据可能已经过期。")
        return f"最近一次日更{target}因异常跳过{cause}。"

    def unresolved_failure_detail() -> str | None:
        if unresolved_failure is None or failed_attempt:
            return None
        day = unresolved_failure.get("trading_day")
        reason = _display_reason(unresolved_failure)
        target = f"（目标日 {day}）" if day else ""
        cause = f"：{reason}" if reason else ""
        return (f"此前日更{target}失败{cause}，尚未由后续成功发布恢复。")

    def add_later_closure(detail: str) -> str:
        if not closed_skip_after_attempt or latest_closed_skip is None:
            return detail
        closure_day = latest_closed_skip[1].get("trading_day")
        target = f" {closure_day} " if closure_day else "最近一次 "
        return f"{detail} 已核验{target}为休市日，未执行日更。"

    def closed_day_detail() -> str:
        closure_day = last_attempt.get("trading_day")
        target = f" {closure_day} " if closure_day else "最近目标日"
        if snapshot_day:
            return (f"已核验{target}为休市日，未执行日更；当前快照覆盖到 "
                    f"{snapshot_day}，无新的采集日期可供比较。")
        return f"已核验{target}为休市日，未执行日更；没有可比较的采集日期。"

    if not snapshot_day or not data_last:
        missed_target_gap = max(
            (gap for gap in (attempt_gap if anomalous_skip else None,
                             failed_attempt_gap if unresolved_failure else None)
             if gap is not None and gap > 0),
            default=None,
        )
        if failed_attempt:
            detail = failed_detail(old_snapshot=missed_target_gap is not None)
        elif anomalous_skip:
            detail = skip_detail(old_snapshot=missed_target_gap is not None)
        elif _is_closed_day_skip(last_attempt):
            detail = closed_day_detail()
        else:
            detail = "缺少可比对的日期（快照或采集窗口未记录），不判断新鲜度。"
        earlier_failure = unresolved_failure_detail()
        if earlier_failure:
            detail = f"{detail} {earlier_failure}"
        if not snapshot_day or not data_last:
            detail = add_later_closure(detail)
        return Freshness(
            snapshot_day=snapshot_day, data_last_day=data_last,
            last_published_day=(last_pub or {}).get("trading_day"),
            last_attempt=_trim_attempt(last_attempt),
            staleness_days=missed_target_gap,
            stale=missed_target_gap is not None, detail=detail)

    try:
        gap = (date.fromisoformat(data_last) - date.fromisoformat(snapshot_day)).days
    except ValueError:
        detail = "日期格式异常，不判断新鲜度。"
        if failed_attempt:
            detail = f"{detail} {failed_detail(old_snapshot=False)}"
        elif anomalous_skip:
            detail = f"{detail} {skip_detail(old_snapshot=False)}"
        earlier_failure = unresolved_failure_detail()
        if earlier_failure:
            detail = f"{detail} {earlier_failure}"
        return Freshness(
            snapshot_day=snapshot_day, data_last_day=data_last,
            last_published_day=(last_pub or {}).get("trading_day"),
            last_attempt=_trim_attempt(last_attempt), staleness_days=None, stale=False,
            detail=add_later_closure(detail))

    stale = gap >= STALE_AFTER_DAYS
    unresolved_failure_gap = failed_attempt_gap
    unresolved_target_is_ahead = (
        unresolved_failure is not None
        and unresolved_failure_gap is not None
        and unresolved_failure_gap > 0
    )
    if ((failed_attempt and failed_attempt_gap is not None
         and failed_attempt_gap > 0)
            or unresolved_target_is_ahead
            or (anomalous_skip and attempt_gap is not None and attempt_gap > 0)):
        # 明确的失败或异常跳过晚于快照目标日时，不能用“尚未超过 4 天”
        # 将其显示成新鲜；它没有被同日或更晚的发布恢复。
        stale = True
        relevant_gaps = [gap]
        if unresolved_target_is_ahead:
            relevant_gaps.append(unresolved_failure_gap)
        if failed_attempt_gap is not None and failed_attempt_gap > 0:
            relevant_gaps.append(failed_attempt_gap)
        if anomalous_skip and attempt_gap is not None and attempt_gap > 0:
            relevant_gaps.append(attempt_gap)
        gap = max(relevant_gaps)
        if failed_attempt:
            detail = failed_detail(old_snapshot=True)
        elif anomalous_skip:
            detail = skip_detail(old_snapshot=True)
        elif unresolved_target_is_ahead:
            detail = f"快照覆盖到 {snapshot_day}，而采集已拿到 {data_last}。"
        else:
            detail = (f"快照覆盖到 {snapshot_day}，而采集已拿到 {data_last}"
                      f"（落后 {gap} 天）。")
    elif gap <= 0:
        detail = "快照与采集窗口一致。"
    elif stale:
        detail = (f"快照覆盖到 {snapshot_day}，而采集已拿到 {data_last}"
                  f"（落后 {gap} 天）——界面上的数字基于**过期的数据**。")
    else:
        detail = (f"快照覆盖到 {snapshot_day}，采集中已有 {data_last}"
                  f"（{gap} 天）：其间没有交易日，或流水线尚未运行。")
    if failed_attempt and not (failed_attempt_gap is not None and failed_attempt_gap > 0):
        detail = f"{detail} {failed_detail(old_snapshot=False)}"
    elif anomalous_skip and not (attempt_gap is not None and attempt_gap > 0):
        detail = f"{detail} {skip_detail(old_snapshot=False)}"
    earlier_failure = unresolved_failure_detail()
    if earlier_failure:
        detail = f"{detail} {earlier_failure}"
    detail = add_later_closure(detail)
    return Freshness(
        snapshot_day=snapshot_day, data_last_day=data_last,
        last_published_day=(last_pub or {}).get("trading_day"),
        last_attempt=_trim_attempt(last_attempt), staleness_days=gap, stale=stale, detail=detail)
