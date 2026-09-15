"""逐项数据权利登记（§17.2）。

与 source_registry 的关系
------------------------
source_registry 里的 SourceSpec 已经带 rights 字段与
can_enter_model_context()。本模块把它落成**可复核的登记表**：

* 每一项都写明判定依据（basis）与判定日期；
* 区分 UNKNOWN（没查过）与 PROHIBITED（查过说不行）——
  两者在准入上都返回 False，但含义不同：前者可以通过补充依据放开。
  混为一谈会让"哪些还能争取"变得无人知道。

判定依据与理由见 docs/data-rights-register.md。
**修改本文件必须同步修改那份登记表**，否则视为未确认。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

REGISTER_VERSION = "2026-09-15"

#: 使用者授权日期。与 REGISTER_VERSION 分开记录：
#: 登记表整体更新与某一项被授权是两件事，混在一起日后无法追溯。


class Rights(str, Enum):
    ALLOWED = "ALLOWED"
    PROHIBITED = "PROHIBITED"
    UNKNOWN = "UNKNOWN"


class Basis(str, Enum):
    """判定依据的类型。没有依据的判定不应写成 ALLOWED。"""

    PUBLIC_DISCLOSURE = "PUBLIC_DISCLOSURE"    # 法定公开披露信息
    TERMS_NOT_REVIEWED = "TERMS_NOT_REVIEWED"  # 条款未审阅
    TERMS_REVIEWED = "TERMS_REVIEWED"          # 条款已审阅并据此判定
    WRITTEN_LICENSE = "WRITTEN_LICENSE"        # 有书面授权
    #: **使用者本人授权**。记录的是"谁在什么时候做了这个决定"，
    #: **不是**法律结论。它之所以要单独一类：
    #: 把使用者的一句话记成 TERMS_REVIEWED，等于替使用者做了一次
    #: 并没有做过的法律审阅，而后来的人无法分辨两者。
    USER_AUTHORIZED = "USER_AUTHORIZED"


@dataclass(frozen=True, slots=True)
class RightsEntry:
    source_id: str
    rights: dict[str, Rights]
    basis: Basis
    decided_on: date
    note: str = ""

    def can_enter_model_context(self) -> bool:
        """外发到模型的唯一判据：明确 ALLOWED 才算开放。"""

        return self.rights.get("model_processing") is Rights.ALLOWED

    def open_for(self, purpose: str) -> bool:
        return self.rights.get(purpose) is Rights.ALLOWED


_FIELDS = ("research_use", "local_storage", "model_processing",
           "excerpt_display", "third_party_redistribution", "commercial_use")


def _entry(source_id: str, *, allowed: tuple[str, ...], basis: Basis,
           note: str = "", decided: str = REGISTER_VERSION) -> RightsEntry:
    rights = {f: (Rights.ALLOWED if f in allowed else Rights.UNKNOWN)
              for f in _FIELDS}
    return RightsEntry(source_id=source_id, rights=rights, basis=basis,
                       decided_on=date.fromisoformat(decided), note=note)


class RightsRegistry:
    def __init__(self, entries: list[RightsEntry]) -> None:
        self._by_id = {e.source_id: e for e in entries}

    def get(self, source_id: str) -> RightsEntry:
        if source_id not in self._by_id:
            # 未登记来源**不得**默认为开放。抛错而不是造一个空条目：
            # 空条目会让"忘了登记"看起来像"登记了但都没开"，
            # 而后者是一个可以通过的结论。
            raise KeyError(
                "source " + repr(source_id) + " has no rights entry; register it in "
                "src/aquant/domain/data/rights.py and docs/data-rights-register.md "
                "before using it")
        return self._by_id[source_id]

    def all(self) -> list[RightsEntry]:
        return list(self._by_id.values())

    def with_right(self, source_id: str, purpose: str,
                   value: Rights) -> "RightsRegistry":
        """返回一个改了某项权利的新登记表（不改原对象）。"""

        entry = self.get(source_id)
        updated = dict(entry.rights)
        updated[purpose] = value
        new_entry = RightsEntry(source_id=entry.source_id, rights=updated,
                                basis=Basis.TERMS_REVIEWED,
                                decided_on=entry.decided_on, note=entry.note)
        entries = [new_entry if e.source_id == source_id else e
                   for e in self.all()]
        return RightsRegistry(entries)


#: 公开披露信息源：数据本身是法定公开披露内容。
#: 研究使用、本地保存、片段展示放开；**再分发与商用不放开**——
#: 交易所对行情数据另有商业授权安排，原始信息公开不等于可以转售。
_DISCLOSURE_ALLOWED = ("research_use", "local_storage", "excerpt_display")

#: 消费级行情网站：受企业服务条款约束，不是公开披露制度。
#:
#: 2026-09-15 使用者确认放行研究使用与本地保存（basis=USER_AUTHORIZED）。
#: 片段展示仍不放开：展示片段涉及转载，属于另一类风险，
#: 使用者的"确认放行"不针对它，不应由我替他扩展。
#: **model_processing 一律不放开**，见下。
_TERMS_ALLOWED = ("research_use", "local_storage")


def default_rights() -> RightsRegistry:
    return RightsRegistry([
        _entry("baostock", allowed=_DISCLOSURE_ALLOWED,
               basis=Basis.PUBLIC_DISCLOSURE,
               note="上游为交易所披露数据；BSD 是代码许可，不等于数据许可"),
        _entry("cninfo", allowed=_DISCLOSURE_ALLOWED,
               basis=Basis.PUBLIC_DISCLOSURE, note="证监会指定披露平台"),
        _entry("sse-site", allowed=_DISCLOSURE_ALLOWED,
               basis=Basis.PUBLIC_DISCLOSURE, note="交易所官网"),
        _entry("szse-site", allowed=_DISCLOSURE_ALLOWED,
               basis=Basis.PUBLIC_DISCLOSURE, note="交易所官网"),
        _entry("tencent-ifzq", allowed=_TERMS_ALLOWED,
               basis=Basis.USER_AUTHORIZED,
               note="2026-09-15 使用者确认放行研究与本地保存；条款本身未做法律审阅"),
        _entry("tencent-qt", allowed=_TERMS_ALLOWED,
               basis=Basis.USER_AUTHORIZED,
               note="2026-09-15 使用者确认放行研究与本地保存；条款本身未做法律审阅"),
        _entry("sina-hq", allowed=_TERMS_ALLOWED,
               basis=Basis.USER_AUTHORIZED,
               note="2026-09-15 使用者确认放行研究与本地保存；条款本身未做法律审阅"),
        _entry("eastmoney-direct", allowed=_TERMS_ALLOWED,
               basis=Basis.USER_AUTHORIZED,
               note="2026-09-15 使用者确认放行研究与本地保存；条款本身未做法律审阅"),
        _entry("synthetic-fixture", allowed=_FIELDS,
               basis=Basis.WRITTEN_LICENSE,
               note="本资料包自带的合成数据，可用于任何用途包括模型处理"),
    ])