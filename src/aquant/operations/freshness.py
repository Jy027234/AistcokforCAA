"""数据新鲜度：快照落后了没有（§5.4 顶栏的一个真实风险）。

为什么需要它
------------
没人会注意到快照落后了。界面一切正常：数字自洽、对账通过、
研究卡能打开——而它们基于的是三天前的数据。

光看"上次跑成功是什么时候"也不够：**天天跑成功但数据源没更新**，
快照照样停在几天前。因此这里比较的是
**快照覆盖的最后一个交易日**与**采集实际拿到的最后一个交易日**：
两者不一致就是落后了，与"跑没跑成功"无关。
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


@dataclass(slots=True)
class Freshness:
    snapshot_day: str | None          # 快照覆盖的最后一个交易日
    data_last_day: str | None         # 采集实际拿到的最后一个交易日
    last_published_day: str | None    # 上次成功发布的交易日
    last_attempt: dict | None         # 最近一次运行（含结果与原因）
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
    return {
        "tradingDay": entry.get("trading_day"),
        "outcome": entry.get("outcome"),
        "reason": entry.get("reason"),
        "finishedAt": entry.get("finished_at"),
        "steps": [{"step": s.get("step"), "ok": s.get("ok")}
                  for s in (entry.get("steps") or [])],
    }


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
    last_attempt = entries[-1]

    # 采集实际拿到的末日：优先取最近一次带该字段的记录
    data_last = None
    for entry in reversed(entries):
        if entry.get("data_last_day"):
            data_last = entry["data_last_day"]
            break

    if not snapshot_day or not data_last:
        return Freshness(
            snapshot_day=snapshot_day, data_last_day=data_last,
            last_published_day=(last_pub or {}).get("trading_day"),
            last_attempt=_trim_attempt(last_attempt), staleness_days=None, stale=False,
            detail="缺少可比对的日期（快照或采集窗口未记录），不判断新鲜度。")

    try:
        gap = (date.fromisoformat(data_last) - date.fromisoformat(snapshot_day)).days
    except ValueError:
        return Freshness(
            snapshot_day=snapshot_day, data_last_day=data_last,
            last_published_day=(last_pub or {}).get("trading_day"),
            last_attempt=_trim_attempt(last_attempt), staleness_days=None, stale=False,
            detail="日期格式异常，不判断新鲜度。")

    stale = gap >= STALE_AFTER_DAYS
    if gap <= 0:
        detail = "快照与采集窗口一致。"
    elif stale:
        detail = (f"快照覆盖到 {snapshot_day}，而采集已拿到 {data_last}"
                  f"（落后 {gap} 天）——界面上的数字基于**过期的数据**。")
    else:
        detail = (f"快照覆盖到 {snapshot_day}，采集中已有 {data_last}"
                  f"（{gap} 天）：其间没有交易日，或流水线尚未运行。")
    return Freshness(
        snapshot_day=snapshot_day, data_last_day=data_last,
        last_published_day=(last_pub or {}).get("trading_day"),
        last_attempt=_trim_attempt(last_attempt), staleness_days=gap, stale=stale, detail=detail)
