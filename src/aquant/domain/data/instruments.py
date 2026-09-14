"""证券身份与历史状态版本。

主文档 §15.2：内部 instrument_id 必须稳定，不以公司名称为主键；
供应商代码只是映射；交易所与证券类别独立保存；**更名不产生新证券**。

§18.1 D05 要求"历史证券池与历史分类不被当前列表替换"，
因此名称、状态、行业分类都按 valid_from/valid_to 版本化查询。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


class Exchange(str, Enum):
    SSE = "SSE"
    SZSE = "SZSE"
    BSE = "BSE"
    OTHER = "OTHER"


class Board(str, Enum):
    """§3.1 首期可模拟池默认仅沪深普通主板。"""

    MAIN = "MAIN"
    GEM = "GEM"
    STAR = "STAR"
    BSE = "BSE"
    OTHER = "OTHER"


class SecurityStatus(str, Enum):
    LISTED = "LISTED"
    SUSPENDED = "SUSPENDED"
    RISK_WARNING = "RISK_WARNING"
    DELISTING = "DELISTING"
    DELISTED = "DELISTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class StatusVersion:
    """某时间段内有效的名称/状态/行业。左闭右开（§7.1）。"""

    valid_from: date
    valid_to: date | None
    name: str
    status: SecurityStatus
    industry_code: str | None = None
    industry_name: str | None = None
    classification_version: str | None = None

    def covers(self, day: date) -> bool:
        if day < self.valid_from:
            return False
        return self.valid_to is None or day < self.valid_to


@dataclass(frozen=True, slots=True)
class Instrument:
    instrument_id: str
    exchange: Exchange
    board: Board
    security_class: str = "EQUITY"
    listed_on: date | None = None
    delisted_on: date | None = None
    status_history: tuple[StatusVersion, ...] = ()

    def __post_init__(self) -> None:
        if not self.instrument_id or "." not in self.instrument_id:
            raise ValueError(
                "instrument_id must be a stable internal id like <SOURCE>.<EXCHANGE>.<CODE>"
            )
        starts = [v.valid_from for v in self.status_history]
        if starts != sorted(starts):
            raise ValueError(f"{self.instrument_id}: status_history must be sorted by valid_from")
        if len(starts) != len(set(starts)):
            raise ValueError(f"{self.instrument_id}: duplicate valid_from in status_history")

    def status_on(self, day: date) -> StatusVersion | None:
        """历史时点视图。D05：不得用当前状态回答历史问题。"""

        for version in self.status_history:
            if version.covers(day):
                return version
        return None

    def name_on(self, day: date) -> str | None:
        version = self.status_on(day)
        return version.name if version else None

    def industry_on(self, day: date) -> str | None:
        version = self.status_on(day)
        return version.industry_code if version else None

    def is_simulatable(self, day: date) -> bool:
        """§3.1 默认可模拟池：沪深普通主板、正常上市、非停牌非退市。

        注意这是"默认规则"，不是永久常量；扩展必须按板块补齐规则与测试。
        """

        if self.exchange not in {Exchange.SSE, Exchange.SZSE}:
            return False
        if self.board is not Board.MAIN:
            return False
        version = self.status_on(day)
        if version is None or version.status is not SecurityStatus.LISTED:
            return False
        if self.listed_on is not None:
            return False  # 上市天数门槛由组合构建层按交易日计算
        return self.delisted_on is None or day < self.delisted_on


def assert_no_name_keyed_identity(ids: list[str]) -> None:
    """§15.2 不得以公司名称作为主键的守卫。

    真实不同证券不得因同名被合并；更名不产生新证券。
    这里只做形状检查：内部 ID 必须包含交易所段。
    """

    for i in ids:
        parts = i.split(".")
        if len(parts) != 3:
            raise ValueError(f"instrument_id {i!r} is not a stable internal id")
        if not parts[1].isupper():
            raise ValueError(f"instrument_id {i!r} must carry an uppercase exchange segment")
