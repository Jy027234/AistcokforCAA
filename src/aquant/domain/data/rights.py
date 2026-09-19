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

REGISTER_VERSION = "2026-09-19"

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
    #: **由使用者意图推论得出**。与 USER_AUTHORIZED 的区别是：
    #: 使用者没有就这一项直接表态，是我方按其已表达的意图做的推论。
    #: 分开记录的理由：推论可能是错的，而"谁说的"决定了复核时该问谁。
    #: 复核清单里必须能一眼看出哪些是推论。
    INFERRED_FROM_USER_INTENT = "INFERRED_FROM_USER_INTENT"


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
#:
#: 2026-09-15 加入 model_processing。依据是**推论**而非使用者的直接指令：
#: 使用者明确授权了消费级网站的模型处理，而公开披露源的风险层级更低
#: （法定公开披露 vs 企业服务条款），且它们正是行情、公告、分红的主来源——
#: 不让它们外发，等于真实数据根本无法进入模型，
#: 而"允许送进模型"这句话就落不了地。
#: 登记表与 docstring 均如实标注该依据为推论，便于日后区分核查。
#:
#: **再分发与商用仍不放开**：交易所对行情数据另有商业授权安排，
#: 原始信息公开不等于可以转售。
_DISCLOSURE_ALLOWED = ("research_use", "local_storage", "excerpt_display",
                       "model_processing")

#: 消费级行情网站：受企业服务条款约束，不是公开披露制度。
#:
#: 2026-09-15 使用者**明确授权全部四项**：研究使用、本地保存、
#: 片段展示、模型处理（basis=USER_AUTHORIZED）。
#:
#: 关于 model_processing 必须写清的事：
#: 这一项一旦执行就**无法收回**——数据已经离开本机、进入第三方。
#: 使用者是在了解这一点之后明确授权的，因此登记表记录该授权；
#: 但依据类型是 USER_AUTHORIZED 而不是 TERMS_REVIEWED，
#: 因为来源条款本身并未经过法律审阅。
#: 详见 docs/data-rights-register.md 与
#: tests/security/test_rights_registry_guard.py 的守卫说明。
_TERMS_ALLOWED = ("research_use", "local_storage", "excerpt_display",
                  "model_processing")


def default_rights() -> RightsRegistry:
    return RightsRegistry([
        _entry("baostock", allowed=_DISCLOSURE_ALLOWED,
               basis=Basis.PUBLIC_DISCLOSURE,
               note="上游为交易所披露数据；模型外发依据为推论（见 _DISCLOSURE_ALLOWED）"),
        _entry("cninfo", allowed=_DISCLOSURE_ALLOWED,
               basis=Basis.PUBLIC_DISCLOSURE,
               note="证监会指定披露平台；模型外发依据为推论"),
        _entry("sse-site", allowed=_DISCLOSURE_ALLOWED,
               basis=Basis.PUBLIC_DISCLOSURE, note="交易所官网；模型外发依据为推论"),
        _entry("szse-site", allowed=_DISCLOSURE_ALLOWED,
               basis=Basis.PUBLIC_DISCLOSURE, note="交易所官网；模型外发依据为推论"),
        _entry("tencent-ifzq", allowed=_TERMS_ALLOWED,
               basis=Basis.USER_AUTHORIZED,
               note="2026-09-15 使用者授权四项（含模型处理）；条款未做法律审阅"),
        _entry("tencent-qt", allowed=_TERMS_ALLOWED,
               basis=Basis.USER_AUTHORIZED,
               note="2026-09-15 使用者授权四项（含模型处理）；条款未做法律审阅"),
        _entry("sina-hq", allowed=_TERMS_ALLOWED,
               basis=Basis.USER_AUTHORIZED,
               note="2026-09-15 使用者授权四项（含模型处理）；条款未做法律审阅"),
        _entry("eastmoney-direct", allowed=_TERMS_ALLOWED,
               basis=Basis.USER_AUTHORIZED,
               note="2026-09-15 使用者授权四项（含模型处理）；条款未做法律审阅"),
        _entry("tushare-pro", allowed=(),
               basis=Basis.TERMS_NOT_REVIEWED,
               note=("付费权限不可接受，当前产品不采用；保留历史登记，"
                     "所有用途保持 UNKNOWN")),
        _entry("mootdx-tdx", allowed=(),
               basis=Basis.TERMS_NOT_REVIEWED,
               note=("免费 S2 结构化候选；项目声明仅供学习交流，"
                     "字段、PIT、修订、缺失语义与使用范围尚未完成验收")),
        _entry("sina-financial", allowed=(),
               basis=Basis.TERMS_NOT_REVIEWED,
               note=("新浪三表 HTML 免费候选；与既有 sina-hq 行情端点分开登记，"
                     "财务页面条款、PIT、修订和稳定性尚未验收")),
        _entry("synthetic-fixture", allowed=_FIELDS,
               basis=Basis.WRITTEN_LICENSE,
               note="本资料包自带的合成数据，可用于任何用途包括模型处理"),
    ])
