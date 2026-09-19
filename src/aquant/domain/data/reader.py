"""研究读取：在固定快照上按 PIT 门禁取数。

主文档 §16.2 要求：研究读取必须指定 snapshot_id，或由服务解析为一个明确快照，
并在结果中返回该 ID；一次页面的多张卡片不能各自读取变化中的"最新数据"。

本模块是研究侧**唯一**的数据入口。它强制四件事：

1. **快照必须已发布**：DRAFT / SUPERSEDED 一律不可读（§15.4）。
2. **读取前校验哈希**：已发布内容被替换必须被发现。
3. **as_of 不得晚于快照上界**：禁止用更晚的时点去读一个较早的快照——
   那样会让人误以为当时就知道后来的事。
4. **返回快照标识**：调用方拿到数据的同时拿到 snapshot_id 与 as_of_time，
   用于研究卡与研究运行的证据关联。

§14.3：研究模块不能直接修改现金，也不能在展示时临时抓供应商。因此本模块只读。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .snapshot import SnapshotError, SnapshotStore


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    """研究结果必须携带的快照标识，供证据关联（§7.1）。"""

    snapshot_id: str
    as_of_time: datetime
    data_mode: str
    watermark: str | None

    def as_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "as_of_time": self.as_of_time.isoformat(),
            "data_mode": self.data_mode,
            "watermark": self.watermark,
        }


@dataclass(frozen=True, slots=True)
class QuoteRow:
    instrument_id: str
    trading_day: date
    open_cents: int
    high_cents: int
    low_cents: int
    close_cents: int
    #: 前复权收盘价只用于收益/动量研究；成交模拟仍使用上面的原始价格。
    adjusted_close_cents: int | None
    volume_shares: int
    #: 成交额。免费源不提供成交额（腾讯日线只有量，没有额），
    #: 缺失时如实为 None，**不得用 价格×成交量 之类的方式补造**。
    amount_cents: int | None
    prev_close_cents: int | None
    board_limit_up: bool = False


class SnapshotReader:
    def __init__(self, store: SnapshotStore) -> None:
        self.store = store
        self._cache: dict[tuple[str, str], Any] = {}

    # ------------------------------------------------------------ plumbing
    def _load_dataset(self, snapshot_id: str, name: str) -> Any:
        """读取并校验数据集。缓存按 (snapshot, dataset) 键，因为快照不可变。"""

        key = (snapshot_id, name)
        if key in self._cache:
            return self._cache[key]
        # 读取即校验哈希：不可变的前提是内容真的没变
        self.store.verify_dataset(snapshot_id, name)
        for d in self.store.datasets(snapshot_id):
            if d["name"] == name:
                payload = json.loads((self.store.root / d["path"]).read_text(encoding="utf-8"))
                self._cache[key] = payload
                return payload
        raise SnapshotError(
            "DATA_NOT_READY", f"snapshot {snapshot_id!r} has no dataset {name!r}",
            snapshot_id, "check the snapshot manifest for available dataset names",
        )

    def ref(self, snapshot_id: str) -> SnapshotRef:
        snap = self.store.require_published(snapshot_id)
        as_of = snap["as_of_time"] or snap["input_cutoff_at"]
        return SnapshotRef(
            snapshot_id=snapshot_id,
            as_of_time=datetime.fromisoformat(as_of),
            data_mode=snap["data_mode"],
            watermark=snap["watermark"],
        )

    def _assert_as_of_within_snapshot(self, snapshot_id: str, as_of: datetime) -> None:
        """as_of 必须**恰好**是快照自身的时间点。

        只检查上界是不够的：如果允许 as_of 早于快照时点，调用方就能用一个
        更晚的快照去回答一个更早的问题——那就等于带着后来的信息回看过去，
        正是 D01 要防的事。因此要求精确相等：一个快照只回答它自己那个时刻的问题。
        """

        if as_of.tzinfo is None:
            raise SnapshotError("PIT_UNVERIFIED", "as_of must be timezone-aware (§7.1)",
                                snapshot_id, "pass a timezone-aware UTC datetime")
        snap = self.store.require_published(snapshot_id)
        as_of_time = datetime.fromisoformat(snap["as_of_time"])
        if as_of != as_of_time:
            relation = "later than" if as_of > as_of_time else "earlier than"
            raise SnapshotError(
                "PIT_UNVERIFIED",
                f"as_of {as_of.isoformat()} is {relation} snapshot {snapshot_id} "
                f"as_of_time {as_of_time.isoformat()}; a snapshot answers only for its own "
                "time point, otherwise later information leaks into an earlier question",
                snapshot_id,
                "use the snapshot whose as_of_time equals the requested time point",
            )

    # ------------------------------------------------------------ quotes
    @staticmethod
    def _as_day_boundary(value: date | str | None, *, name: str) -> date | None:
        """把区间边界规整为 date。

        传进来一个 ISO 字符串时，`day > end` 会抛
        "'>' not supported between instances of 'datetime.date' and 'str'"——
        错误信息完全不提"边界传成了字符串"，排查成本很高。
        这里显式规整并说明，把这类错误挡在入口。

        日历日期是**本地日期**，不带时区；字符串一律按 ISO 日期解析，
        不猜测其它格式（猜错日期会静默改变结果集，比报错危险得多）。
        """

        if value is None or isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError as exc:
                raise TypeError(
                    f"{name} must be a date or an ISO date string, got {value!r}"
                ) from exc
        raise TypeError(f"{name} must be a date or an ISO date string, "
                        f"got {type(value).__name__}")

    def daily_quotes(self, snapshot_id: str, *, as_of: datetime,
                     instrument_id: str | None = None,
                     start: date | str | None = None,
                     end: date | str | None = None) -> list[QuoteRow]:
        """固定快照上的日行情。

        停牌等情况**没有行**——缺失即缺失，不得用前收填充（§12.3、S08）。

        start/end 为闭区间边界，接受 date 或 ISO 日期字符串。
        """

        self._assert_as_of_within_snapshot(snapshot_id, as_of)
        start = self._as_day_boundary(start, name="start")
        end = self._as_day_boundary(end, name="end")
        raw = self._load_dataset(snapshot_id, "daily_quotes")
        out: list[QuoteRow] = []
        for q in raw:
            if instrument_id and q["instrument_id"] != instrument_id:
                continue
            day = date.fromisoformat(q["trading_day"])
            if start and day < start:
                continue
            if end and day > end:
                continue
            out.append(QuoteRow(
                instrument_id=q["instrument_id"], trading_day=day,
                open_cents=q["open_cents"], high_cents=q["high_cents"],
                low_cents=q["low_cents"], close_cents=q["close_cents"],
                adjusted_close_cents=q.get("adjusted_close_cents"),
                volume_shares=q["volume_shares"],
                amount_cents=q.get("amount_cents"),
                prev_close_cents=q.get("prev_close_cents"),
                board_limit_up=bool(q.get("board_limit_up")),
            ))
        return sorted(out, key=lambda r: (r.instrument_id, r.trading_day))

    def latest_quote(self, snapshot_id: str, instrument_id: str, *,
                     as_of: datetime) -> QuoteRow | None:
        rows = self.daily_quotes(snapshot_id, as_of=as_of, instrument_id=instrument_id)
        return rows[-1] if rows else None

    def is_suspended_on(self, snapshot_id: str, instrument_id: str, day: date, *,
                        as_of: datetime) -> bool:
        """该日无行情且此前有行情 -> 视为停牌（不制造成交，S08）。"""

        rows = self.daily_quotes(snapshot_id, as_of=as_of, instrument_id=instrument_id,
                                 end=day)
        if any(r.trading_day == day for r in rows):
            return False
        return bool(rows)

    # ------------------------------------------------------------ events
    def events(self, snapshot_id: str, *, as_of: datetime,
               instrument_id: str | None = None,
               only_available: bool = True) -> list[dict]:
        """事件读取。

        only_available=True（默认）只返回 available_at <= as_of 的事件——
        这是 PIT 门禁在证据域的落点：**当时还看不到的材料不得参与当时的判断**
        （D04、A11）。
        """

        self._assert_as_of_within_snapshot(snapshot_id, as_of)
        raw = self._load_dataset(snapshot_id, "events")
        out: list[dict] = []
        for ev in raw:
            if instrument_id:
                subjects = {s.get("subject_id") for s in (ev.get("subjects") or [])}
                if instrument_id not in subjects:
                    continue
            if only_available:
                avail = ev.get("available_at")
                if not avail or datetime.fromisoformat(avail) > as_of:
                    continue
            out.append(ev)
        return out

    def corporate_actions(self, snapshot_id: str, *, as_of: datetime,
                          instrument_id: str | None = None) -> list[dict]:
        self._assert_as_of_within_snapshot(snapshot_id, as_of)
        raw = self._load_dataset(snapshot_id, "corporate_actions")
        return [a for a in raw
                if (instrument_id is None or a["instrument_id"] == instrument_id)]

    def instruments(self, snapshot_id: str, *, as_of: datetime) -> list[dict]:
        self._assert_as_of_within_snapshot(snapshot_id, as_of)
        return list(self._load_dataset(snapshot_id, "instruments"))

    def trading_calendar(self, snapshot_id: str, *, as_of: datetime) -> list[str]:
        self._assert_as_of_within_snapshot(snapshot_id, as_of)
        return list(self._load_dataset(snapshot_id, "trading_calendar"))

    def financials(self, snapshot_id: str, *, as_of: datetime) -> dict:
        """财务数据（季频，含 pubDate）。**没有就返回空**，不报错。

        为什么返回空而不是抛错：很多快照本来就不含财务数据
        （行情快照、合成快照）。让调用方去区分"快照没有这类数据"与
        "读取失败"，只会逼出一堆 try/except；而"没有财务数据"本身
        是一个可读的缺失原因，由调用方写进 exclusion_reason。
        """

        self._assert_as_of_within_snapshot(snapshot_id, as_of)
        try:
            return dict(self._load_dataset(snapshot_id, "financials"))
        except SnapshotError as exc:
            # 只吞"这份快照本来就没带这个数据集"这一种。
            # **不能**笼统 except 掉所有 SnapshotError：
            # 数据集存在但哈希校验失败是另一回事，掩盖它会让
            # "快照被改过"看起来像"没有财务数据"。
            if "has no dataset" not in str(exc):
                raise
            return {}
