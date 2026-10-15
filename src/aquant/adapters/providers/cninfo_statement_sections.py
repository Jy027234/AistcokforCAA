"""独立于候选提取器的第二抽取路径与会计恒等式核对。

``cninfo_financial_candidate`` 只读**合并当期列**的五项 S2 金额，供人工目视
复核。本模块回答另一个问题：**同样的金额能否在原文里被另一条独立路径证实？**

它做三件事，三件都不联网、也不产生正式事实：

1. 用与候选提取器**相同的页边界与列规则**，但**不同的行名规则与不同的表节**，
   定位第二条定位路径：母公司表节、少数股东损益/权益行、以及各报表自身的
   平衡行。同一文件内的这些行位于不同页、由不同单元格承载。
2. 按标准会计恒等式核对，例如

   * F07 分母端：归母权益 = 所有者权益合计 - 少数股东权益；
   * F07 分子端：归母净利润 = 净利润 - 少数股东损益；
   * 现金流量表：经营现金流 = 现金净增加额 - 投资净额 - 筹资净额。

3. 无法从主表证实收入时，改读**合并附注**的收入分解表（``主营业务`` +
   ``其他业务`` = ``合计``），见 ``extract_revenue_note_decomposition``。

这些恒等式只在**两个不同表节的同一实数值**同时成立时才通过；一个错位
单元格、错列或单位错误都会使其失衡。因此它是一次真实的独立核对，而不是
把同一个抽取函数跑两遍。

本模块只输出核对结果；任何金额都不得据此直接写入正式事实仓。
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

from .cninfo_financial_candidate import (
    _NUMBER,
    _UNIT,
    _UNIT_MULTIPLIER,
    _CURRENCY,
    _HEADING,
    CninfoFinancialPdfError,
    _compact,
    _expected_header,
    _field_matches,
    _page_headings,
    _visible_label,
)
from .cninfo_financial_pdf import CninfoS2CandidateFacts, PdfCandidateFact


#: 官方巨潮全文 PDF 的 URL 形状。公告 ID 必须与 URL 一致，防止张冠李戴。
_CNINFO_PDF_URL = re.compile(
    r"https://static\.cninfo\.com\.cn/finalpage/\d{4}-\d{2}-\d{2}/(\d+)\.PDF\Z",
    re.IGNORECASE,
)


STATEMENTS = ("资产负债表", "利润表", "现金流量表")
#: 表节角色。``合并`` 与 ``母公司`` 在主报告中位于不同页码区间。
ROLE_CONSOLIDATED = "合并"
ROLE_PARENT = "母公司"

#: 母公司表节中与合并数**必然相等**的字段。
#:
#: 只有归母权益满足这一条件：同一批股东在同一时点享有的权益，合并与母公司
#: 两张资产负债表都必须列示同一个数（实测 000651：母公司表节只列``所有者
#: 权益合计``，不单列归母权益，因此这条路径在该公司自然不成立，而不是被
#: 误判为相等）。
#:
#: **营业收入不在其列。** 拥有财务子公司等金融业务的公司，母公司利润表按
#: 金融口径列示，母公司营业收入与合并``其中：营业收入``可以相差很大（实测
#: 000651 2024H1：母公司 69,466,689,275.80 对合并 99,783,116,496.59）。把
#: 母公司收入当作合并收入的镜像会制造一条**会给出错误结论**的核对路径，
#: 因此这里明确排除。
PARENT_MATCHING_FIELDS = frozenset({"parent_equity"})

#: 收入恒等式的金融收入扣减项。``其中：营业收入`` = ``营业总收入`` 减去这些
#: 项目；一般工商企业没有这些行，恒等式退化为收入 = 营业总收入。
FINANCIAL_REVENUE_COMPONENTS = (
    "interest_income", "earned_premium", "fee_commission_income",
)

#: 恒等式组件行。值为按去空白、去编号后的可见行名匹配的正则元组。
#:
#: 规则刻意保持**窄**：匹配不到就是缺失，由核对层报告失败，绝不用相似行名
#: 顶替。母公司与合并表节在同一份原文中由不同页码承载，因此同一个词在
#: 两个表节里是两条独立证据。
_COMPONENT_LABELS: dict[str, tuple[str, ...]] = {
    "total_equity": (
        r"^所有者权益\(或股东权益\)合计$",
        r"^股东权益合计$",
        r"^所有者权益合计$",
    ),
    "minority_equity": (r"^少数股东权益$",),
    "net_profit": (r"^净利润\(净亏损以.*号填列\)$", r"^净利润$"),
    "attributable_profit": (
        # 无边框版式会把"（净亏损以"—"号填列）"截断成"（净亏损以"—"号填"，
        # 因此这里只固定行名主干，不要求括号部分完整。
        r"^归属于母公司(?:股东|所有者)的净利润.*$",
    ),
    "minority_profit": (
        r"^少数股东损益\(净亏损以.*号填列\)$",
        r"^少数股东损益$",
    ),
    # 经营/投资/筹资三项也可能带报表附注引用（实测 002415
    # ``经营活动产生的现金流量净额 (五)58(1) (189,636,040.90) …``），
    # 归一化后可能留下一个空格，因此同样以 ``\s*$`` 收尾。
    #
    # 措辞还有**词序颠倒**的写法，年报里很常见（实测 000333 各年报）：
    # ``经营活动产生/(使用)的现金流量净额``、``投资活动使用/(产生)的现金流量净额``、
    # ``筹资活动(使用)/产生的现金流量净额``。只认"产生的"会让这些年报的三项
    # 全部定位不到，现金恒等式随之整条无法建立。
    "operating_cashflow": (
        r"^经营活动产生(?:/\(使用\))?的现金流量净额\s*$",
        r"^经营活动使用(?:/\(产生\))?的现金流量净额\s*$",
        r"^经营活动\(使用\)/产生的现金流量净额\s*$",
    ),
    "investing_cashflow": (
        r"^投资活动产生的现金流量净额\s*$",
        r"^投资活动使用(?:/\(产生\))?的现金流量净额\s*$",
        r"^投资活动\(使用\)/产生的现金流量净额\s*$",
    ),
    "financing_cashflow": (
        r"^筹资活动产生的现金流量净额\s*$",
        r"^筹资活动使用(?:/\(产生\))?的现金流量净额\s*$",
        r"^筹资活动(?:产生)?(?:/\(使用\))?的现金流量净额\s*$",
        r"^筹资活动使用/产生的现金流量净额\s*$",
    ),
    # 汇率影响行的措辞有两种真实写法：``汇率变动对现金及现金等价物的影响``
    # （实测 000651）与 ``…的影响额``（实测 002415）。只认前者会让整条现金
    # 恒等式在 002415 上无法建立。
    "fx_effect": (r"^汇率变动对现金及现金等价物的影响额?\s*$",),
    # 现金流量表的收尾三项常带报表附注引用（``五、现金及现金等价物净增加额 七、79``）。
    # ``_normalize_label`` 会把附注剥掉但留下一个空格，因此这些模式必须以
    # ``\s*$`` 收尾，不能用 ``$``——否则整条现金恒等式都建立不起来
    # （实测 601012 年报只定位到经营/投资/筹资三项）。
    # 净额行的措辞在真实原文里有**一整族**写法，随当期是增是减以及披露习惯而变：
    #   * ``现金及现金等价物净增加额``（实测 000651）；
    #   * ``现金及现金等价物净减少额``（实测 002415 2024H1，当期净减少）；
    #   * ``现金及现金等价物净增加(减少)额``（实测 002415 2025H1）；
    #   * ``现金及现金等价物净增加/(减少)额``（实测 000333 2025 各期）；
    #   * ``现金及现金等价物净(减少)/增加额``（实测 000333 2024 年报，
    #     连**括号内的词序也与括号外相反**）。
    # 用一个式子覆盖全族，而不是逐个追加：``净`` 之后允许
    # "增加/减少" 的任意组合，可带一层括号与斜杠。
    "net_increase_cash": (
        r"^现金及现金等价物净\s*(?:增加|减少)"
        r"(?:\s*/?\s*[(（]\s*(?:增加|减少)\s*[)）])?"
        r"\s*额\s*$",
        r"^现金及现金等价物净\s*[(（]\s*(?:增加|减少)\s*[)）]"
        r"\s*/?\s*(?:增加|减少)\s*额\s*$",
    ),
    "ending_cash": (r"^期末现金及现金等价物余额\s*$",),
    "beginning_cash": (
        # ``_compact`` 只归一化括号，不归一化冒号；原文两种冒号都要接受。
        r"^加[:：]期初现金及现金等价物余额\s*$",
        r"^期初现金及现金等价物余额\s*$",
    ),
    # 收入恒等式：``其中：营业收入`` = ``营业总收入`` 减去金融收入项。
    #
    # 这三项必须**以术语结尾**。不加 ``$`` 时，``利息收入 5,999,412,762.36``
    # 这类附带了另一层含义的行（母公司表的利息收入）也会被匹配，用它去扣减
    # 合并营业总收入就会算出错误的差额（实测 000651 年报：85.8 亿的利息收入
    # 被当成 60 亿的扣减项）。
    "total_revenue": (r"^营业总收入$",),
    "interest_income": (r"^利息收入$",),
    "earned_premium": (r"^已赚保费$",),
    "fee_commission_income": (r"^手续费及佣金收入$",),
}

#: 行首编号前缀。必须是**非贪婪**的：``2.少数股东损益`` 只应剥掉 ``2.``，
#: 贪婪匹配会把 ``2.少数股东损益`` 整段当成重复前缀吃掉，行名随之丢失。
#: 同一行名在**一个表节内**出现多次时是否取第一条。
#:
#: 仅用于金融口径利润表：它把``利息收入``同时列在主表收入分解段与
#: ``利息净收入``小节，两处指向同一个当期累计数。其余组件一律要求唯一，
#: 出现重复即视为定位歧义并失败关闭。
_FIRST_OCCURRENCE_COMPONENTS = frozenset({"interest_income"})

_LEADING_NUMBER = re.compile(
    r"^(?:\d+[.、．，]|[一二三四五六七八九十]+[、.]|\([一二三四五六七八九十]+\))+?")

#: 报表附注交叉引用。三种真实写法都要覆盖：
#: ``五、54``（年报无边框版式）、``(五)46``（实测 002415）与
#: ``(五)58(1)`` / ``(五)57(2)``（**带子项编号**，实测 002415 现金流量表）。
#: 子项括号必须一起吃掉，否则会残留 ``(1)``，让``期末现金及现金等价物余额``
#: 之类模式匹配不上。行尾还可能只留下``五、``（编号在被拆开的下一行），
#: 因此编号后允许没有数字。
#:
#: **不要**在这里追加"裸数字括号"``(3)`` 这种分支（曾试过，为了 000333 的
#: ``十八(3)``）。它会把 ``2024年(3)`` 之类列头残片也吃掉，导致 35 份报告的
#: 列头判定失效——回归代价远大于收益。000333 的写法请走
#: ``_strip_note_chapter`` 的专用路径。
#: 单处附注引用（三种真实写法）。
_NOTE_REFERENCE_ONE = (
    r"(?:[一二三四五六七八九十]+、\d*(?:\.\d+)*"
    r"|[(（]\s*[一二三四五六七八九十]+\s*[)）]\s*\d*(?:\s*[(（]\s*\d+\s*[)）])*)"
)
_NOTE_REFERENCE = re.compile(
    # 同一科目可以挂**多个**附注（实测 000333 ``其中：营业收入 四(46),十七(3)``）。
    # 不吃掉逗号连接的第二处，行名会残留一个 ``,`` 而匹配不上收入行。
    # **必须把整个式子重复一遍再补 ``,`` 连接**：把逗号那支并进第一个括号里会被
    # 交替式的优先级吃掉，等于没写。
    _NOTE_REFERENCE_ONE + r"(?:\s*[,，]\s*" + _NOTE_REFERENCE_ONE + r")*"
)
#: 行名**以裸数字结尾**。行名不会以数字收尾，这种结尾只可能来自被误当金额的
#: 附注编号（实测 600519 ``其中：营业收入 41 …``）或残留编号。
_TRAILING_DIGITS = re.compile(r"\s*\d+\s*\Z")
#: 行名**以中文数字章节号结尾**。
#:
#: **绝不能放进 ``_normalize_label``。** 曾试过在那里统一剥离，结果把真正的
#: 行名词尾也剥掉了——``本期发生额`` 结尾的 ``十`` 属于词本身，剥离后变成
#: ``本期发``，全库 35 份报告的列头判定立即失效。中文数字既可能是章节号，
#: 也可能是词尾，**只看结尾无法区分**，所以只能在确知"这是附注残留"的
#: 上下文里使用（见 ``_strip_note_chapter``）。
_TRAILING_CHAPTER = re.compile(r"\s*[一二三四五六七八九十]+\s*\Z")
#: ``章节号 + 子项括号`` 的附注引用（实测 000333 ``十八(3)`` / ``四(49)``）。
#: **只能用在 combined 表节的行名归一化里**：裸数字括号 ``(3)`` 在别处可能
#: 是列头残片，全库启用会破坏列头判定。
_CHAPTER_WITH_ITEM = re.compile(
    r"[一二三四五六七八九十]+\s*[(（]\s*\d+\s*[)）](?:\s*[(（]\s*\d+\s*[)）])*")
#: 行名**尾部整段附注残留**。实测 000333 的两种写法：
#: ``其中：营业收入 十八(3)`` 与 ``其中：营业收入 四(46),十七(3)``——
#: 两处附注都排在行名之后，因此整段都是尾部残留。
#: 这里不解析"章节号/子项"的语义，只把**尾部的一串 ``(…数字…)``（可带逗号与
#: 数字）**整段吃掉：序列里必须至少出现一个方括号项，行名本身不会以括号数字
#: 结尾，所以判定是安全的。**只在 combined 分支使用**。
_CHAPTER_ITEM_TAIL = re.compile(
    r"[(（]\s*\d+\s*[)）]"
    # 之后可以再接任意"括号/数字/分隔符"组合，**中间允许出现短章节号**
    # （实测 ``四(46),十七(3)``：两处方括号之间隔着一个 ``十七``）。
    # 整段都必须落在行尾，且至少含一处方括号，因此不会误伤正常行名。
    r"(?:\s*[,，、]?\s*[一二三四五六七八九十]{0,3}\s*[(（]\s*\d+\s*[)）])*\s*\Z")
#: 行尾的**单字母子项括号**（实测 000333 ``经营活动产生的现金流量净额 四(62)(c)``）。
#: 附注子项不一定是数字，也可能用字母编号；数字式子项会被
#: ``_CHAPTER_ITEM_TAIL`` 吃掉，字母的不会，于是行名残留 ``(c)`` 而匹配不上。
#: 只在 combined 分支使用。
_LETTER_ITEM_TAIL = re.compile(r"\s*[(（]\s*[a-zA-Z]\s*[)）]\s*\Z")


def _strip_note_chapter(label: str) -> str:
    """剥掉行名尾部**确知是附注章节号**的残留。

    仅在某几家公司（实测 000333 ``其中：营业收入 十八(3)``）出现"章节号 +
    子项括号"的附注写法时使用：``_NOTE_REFERENCE`` 会先吃掉 ``(3)``，留下
    ``…营业收入十八``。这里再剥掉章节号。

    之所以不在 ``_normalize_label`` 里统一做：中文数字无法按位置区分章节号与
    词尾（``本期发生额`` 的 ``十`` 就是词尾），统一剥离会破坏正常行名。
    """

    return _TRAILING_CHAPTER.sub("", label).strip()

#: 行名**以附注引用结尾**。用于识别"本期为空"的科目（见 ``_carries_amount``）。
_NOTE_REFERENCE_TAIL = re.compile(
    r"(?:[一二三四五六七八九十]+、\d*(?:\.\d+)*"
    r"|[(（]\s*[一二三四五六七八九十]+\s*[)）]\s*\d*(?:\s*[(（]\s*\d+\s*[)）])*)\s*\Z"
)

#: 列头候选词。资产负债表用``期末/期初余额``，利润与现金流量表用期间词，
#: 年报无边框版式还会插入``附注``列。这里只负责切出候选词，
#: 由 ``_expected_header`` 决定哪一列是当期列。
#:
#: 还要接受紧跟日期列的``(已重述)``/``(经重述)``注记：实测 002415 的表头是
#: ``项目 附注 2024年06月30日`` 与下一行``(已重述)``，注记本身不是金额列，
#: 但它的存在会让调用方误判"只找到一列"。
_COLUMN_TOKEN = re.compile(
    r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"|\d{4}\s*年\s*0?1\s*[-－—–]\s*0?6\s*月"
    r"|\d{4}\s*年(?:上半年|半年度|年度|第一季度|第三季度|第[1-4]季度|一季度|三季度)"
    r"|\d{4}\s*年度"
    r"|本期发生额|上期发生额|上年同期发生额|本年发生额|上年发生额"
    r"|期末余额|期初余额|年末余额|年初余额"
    r"|[(（]\s*已?经?重述\s*[)）]"
)


def _column_header_labels(line: str) -> list[str] | None:
    """从一条表头行取出**金额列**标签。

    无边框年报的表头形如 ``项 目 附注 2024年12月31日 2024年1月1日``，其中最
    右两列才是金额列，``附注``列右边的内容是报表附注编号。因此这里只取最右
    两个候选列，避免把"五、1"这类附注编号当成金额列。

    还要求该行出现``项目``或``附注``这类报表列头标记。否则正文里任意一句
    含两个日期的描述（例如"2024年6月，美的集团…2023年度…"）都会被当成列头，
    从而凭空造出一个"表节"并给出看似合理的数值——这比报错危险得多。

    返回 ``None`` 表示这一行不提供任何金额列标签，由调用方继续累积下一行。
    """

    compact = _compact(line)
    labels = _COLUMN_TOKEN.findall(compact)
    if not any(marker in compact for marker in ("项目", "附注")):
        # 没有列头标记时，只有**本身就是金额列**的词才算列头：明确的期间词/
        # 余额词，或**整行就是**一个日期（资产负债表的日期列头）。
        # 页眉里的``杭州海康威视…2024年年度报告``（裸年份词）与
        # ``2024年12月31日止年度``（日期后接文字）都不符合，因此不会挤进
        # 候选窗口；无条件接受它们会让真正的表头被判成歧义
        # （实测 002415 2024 年报整份利润表因此定位失败）。
        return labels if any(
            _AMOUNT_COLUMN_ONLY.fullmatch(item) or _DATE_COLUMN_ONLY.fullmatch(item)
            for item in labels) or re.fullmatch(r"(?:20\d{2}年0?1[-－—–]0?6月){1,2}", compact) else None
    if not labels:
        return None
    # 先剔除**注记**再取最右两列。``(已重述)``/``(经重述)`` 这类括号注记也命中
    # 列头词，若直接取"最右两个"，它会把真正的金额列挤掉——实测 002415 2024
    # 年报 ``项目 附注 本年发生额 上年发生额(已重述)`` 因此只剩下
    # ``['上年发生额', '(已重述)']``，``本年发生额`` 丢失，整份利润表定位失败。
    #
    # 但**不能**改成"返回全部候选"：那样调用方会在错误的行上过早配对成功，
    # 列头判定整体失效（实测：全库 35 份报告立即从"全通过"变成
    # ``column header is ambiguous``）。因此保持"最右两列"这一收敛规则，
    # 只把注记排除在候选择之外。
    candidates = [item for item in labels if not _COLUMN_ANNOTATION_ONLY.fullmatch(item)]
    if len(candidates) < 2:
        candidates = labels
    return candidates[-2:]


@dataclass(frozen=True, slots=True)
class StatementRow:
    """一条已定位的报表行及其原文证据。"""

    role: str
    statement: str
    label: str
    amount: Decimal
    prior_amount: Decimal
    page: int
    unit: str
    current_cell: str
    prior_cell: str
    raw_line: str
    #: 该行金额单元格是"无此项"占位符（``-``/``——``），即本期为空。
    nil_amounts: bool = False

    def as_evidence(self) -> dict[str, object]:
        return {
            "role": self.role,
            "statement": self.statement,
            "label": self.label,
            "amount_yuan": str(self.amount * _UNIT_MULTIPLIER[self.unit]),
            "page": self.page,
            "amount_unit": self.unit,
            "current_cell": self.current_cell,
            "raw_line": self.raw_line,
        }


@dataclass(frozen=True, slots=True)
class StatementSection:
    """一个合并/母公司表节的定位与列头证据。"""

    role: str
    statement: str
    start_page: int
    end_page: int
    unit: str
    current_header: str
    prior_header: str
    rows: tuple[StatementRow, ...]
    #: 表节裁剪后的 ``(页码, 原文行)``，用于复原被换行拆开的行名与金额。
    lines: tuple[tuple[int, str], ...] = ()
    #: 表名是否写成"合并及公司…"（一张表同时列示合并与公司口径）。
    combined: bool = False
    #: 行金额按几个口径分组：2 = 当期/比较各一列；4 = 合并当期/合并上期/
    #: 公司当期/公司上期，此时只取最左两列。下游据此确认"确实按四列读了"，
    #: 而不是把公司列当成合并列。
    scope_columns: int = 2
    #: Only used by the literal cross-page CFO fallback. These values come
    #: from the same original PDF; ordinary direct rows do not depend on them.
    continuation_report_header: str = ""
    pdf_page_count: int = 0

    def normalize(self, label: str) -> str:
        """按本表节的版式归一化行名。

        ``合并及公司…`` 表的附注引用写法是"章节号 + 子项括号"
        （实测 000333 ``一、营业总收入`` 与 ``其中：营业收入 十八(3)``）。
        ``_NOTE_REFERENCE`` 只吃掉 ``(3)``，留下 ``…营业收入十八``，收入行随之
        定位不到。**这一剥离只在 combined 表节内进行**：中文数字无法按位置区分
        章节号与词尾（``本期发生额`` 的 ``十`` 就是词尾），全库统一剥离会让
        35 份报告的列头判定失效，因此不能放进 ``_normalize_label``。
        """

        # Actual 000680 prints a separate note cell as ``五）49``. Only
        # recognize this closed-parenthesis suffix after the exact revenue
        # label; keep the original row/cells in the source evidence. A bare
        # chapter, unmatched prose, or 营业总收入 must not become revenue.
        closed_note = re.fullmatch(
            r"((?:其中[:：])?营业收入)\s+[一二三四五六七八九十]{1,3}[)）]\s*[1-9]\d{0,2}",
            _LEADING_NUMBER.sub("", label.strip()),
        )
        if (closed_note is None and self.role == ROLE_CONSOLIDATED
                and self.statement == "利润表" and not self.combined
                and self.scope_columns == 2):
            # Actual 600489 prints 七、（61） as a complete note cell.
            # Match only the exact revenue label plus that closed token;
            # global note/number parsing must retain its old semantics.
            closed_note = re.fullmatch(
                r"((?:其中[:：])?营业收入)\s+[一二三四五六七八九十]{1,3}、"
                r"(?:\([1-9]\d{0,2}\)|（[1-9]\d{0,2}）)",
                _LEADING_NUMBER.sub("", label.strip()),
            )
        clean = _normalize_label(closed_note.group(1) if closed_note else label)
        if self.combined:
            # 顺序要紧：先按"整段尾部残留"吃掉 ``四(46),十七(3)`` 这一整串，
            # 再用 ``_CHAPTER_WITH_ITEM`` 处理剩余的单处写法。反过来先跑后者
            # 只会吃掉其中一处，留下一个孤立的逗号（实测 000333 收入行变成
            # ``其中:营业收入,``，仍然匹配不上）。
            clean = _CHAPTER_ITEM_TAIL.sub("", clean)
            clean = _LETTER_ITEM_TAIL.sub("", clean)
            clean = _CHAPTER_WITH_ITEM.sub("", clean)
            clean = _TRAILING_CHAPTER.sub("", clean).strip()
            clean = _strip_glued_section_header(clean)
        return clean

    def find(self, field: str) -> StatementRow | None:
        matches = [
            row for row in self.rows
            if _field_matches(field, self.normalize(row.label))
        ]
        if (not matches and field == "parent_equity"
                and self.role == ROLE_CONSOLIDATED
                and self.statement == "资产负债表"):
            # Actual 000680's original cell ends at 权益合; the final 计 is
            # absent from both the native text and the ruled cell. Recognize
            # this exact disclosed label without manufacturing a missing
            # character. Independent equity/source verification remains
            # required and the raw label is preserved verbatim.
            short_labels = [row for row in self.rows
                            if self.normalize(row.label) == "归属于母公司所有者权益合"
                            and not row.nil_amounts]
            matches = short_labels if len(short_labels) == 1 else []
        if len(matches) > 1:
            # 同一表节里同名行重复出现只可能是版式伪影（例如续表把标题行
            # 重印一次）。取先出现的一行——它就是当期列所在的主表行——
            # 而不是让整份报告因为伪影无法核验。
            matches = matches[:1]
        if matches:
            return matches[0]
        recovered = self._recover_wrapped(
            field, lambda clean: _field_matches(field, clean))
        if recovered is not None:
            return recovered
        return None

    def component(self, key: str) -> StatementRow | None:
        """按恒等式组件名定位唯一行；重复出现时保持缺失而不是猜一个。

        只接受**归一化后恰好等于术语**的行。``_normalize_label`` 会剥掉报表
        附注引用，因此"本期为空"的科目（原文 ``利息收入 七、66``，金额单元格
        为空、附注编号被当成金额）**无法**归一化成 ``利息收入``——它留下的是
        ``利息收入`` 加一个残留标记，于是被排除，恒等式随之报告"分量缺失"。
        这正是我们要的行为：不拿附注编号去算。
        """

        patterns = tuple(re.compile(pattern) for pattern in _COMPONENT_LABELS[key])
        first_wins = key in _FIRST_OCCURRENCE_COMPONENTS
        direct = []
        for row in self.rows:
            # 必须走 ``self.normalize``，不能直接调 ``_normalize_label``：
            # "合并及公司…"表的附注写法（``…净额 四(62)(c)``）只有前者会清理。
            # 早期这里绕过表节归一化，导致这类表的三项现金净额永远定位不到
            # （实测 000333 五个期间全部报"分量缺失"）。
            cleaned = _LEADING_NUMBER.sub("", self.normalize(row.label))
            if any(pattern.match(cleaned) for pattern in patterns):
                direct.append(row)
                if first_wins:
                    break
        # "本期为空"的科目（金额单元格写 ``-``/``——``）必须当作缺失，不能拿
        # 占位符当金额参与恒等式。
        direct = [row for row in direct if not row.nil_amounts]
        if len(direct) > 1:
            # 同一表节里同名行重复：先看有没有**带报表附注引用**的那些行。
            #
            # 合并利润表里可以同时出现带附注的 ``利息收入 五、55``（874,417,456.85）
            # 与一个不带附注的裸 ``利息收入``（5,999,412,762.36，另一层口径）。
            # 两者都匹配「利息收入」，但带附注的才是有编号的正式报表行；取裸行
            # 会拿 60 亿去扣减 1900 亿的营业总收入，恒等式随之被错误判负
            # （实测 000651 年报）。只在没有带附注行时才回退到版式伪影规则。
            annotated = [
                row for row in direct
                if _NOTE_REFERENCE_TAIL.search(_compact(row.label)) is None
                and _NOTE_REFERENCE.search(_normalize_label(row.label)) is not None
            ]
            direct = annotated[:1] if annotated else direct[:1]
        if direct:
            return direct[0]
        return self._recover_wrapped(
            key, lambda clean: any(p.match(clean) for p in patterns),
            first_wins=first_wins)

    def _recover_wrapped(self, key: str, matches: Any, *,
                         first_wins: bool = False) -> StatementRow | None:
        """复原被换行拆开的行名。

        不少年报把长行名与金额分到两行（``1.归属于母公司股东的净利润（净亏损以“—”号填``
        后接 ``14,136,119,367.60 12,673,238,816.55``）。此时行名行没有数字，
        金额行没有行名。只有**紧邻的下一行**才被接受，且该行必须只含两个数字。
        """

        recovered: list[StatementRow] = []
        # Actual 605499 H126/FY25 and 600236 H1: the complete CFO label
        # surrounds a separate note-reference/two-amount line. Only the three
        # observed exact split pairs are supported, never an arbitrary prefix.
        # Keep this recovery inside an already
        # located consolidated cashflow section; global dewrapping must not
        # turn an arbitrary note or another statement into a primary CFO row.
        if (key == "operating_cashflow" and self.role == ROLE_CONSOLIDATED
                and self.statement == "现金流量表" and not self.combined
                and self.scope_columns == 2 and self.unit in _UNIT_MULTIPLIER
                and self.current_header and self.prior_header):
            for index in range(len(self.lines) - 2):
                head, middle, tail = self.lines[index:index + 3]
                if (head[0] != middle[0] or head[0] != tail[0]
                        or (_compact(head[1]), _compact(tail[1])) not in (
                            ("经营活动产生的现金", "流量净额"),
                            ("经营活动产生的现金流量", "净额"),
                            ("经营活动产生的现金流", "量净额"),
                        )):
                    continue
                note = re.fullmatch(
                    r"[一二三四五六七八九十]{1,3}、"
                    r"(?:[1-9]\d{0,2}|\([1-9]\d{0,2}\)|（[1-9]\d{0,2}）)\s+(.+)",
                    middle[1].strip(),
                )
                if note is None:
                    continue
                amounts = _line_amounts_only(note.group(1))
                if amounts is None:
                    continue
                current_cell, current, prior = amounts
                cells = _amounts_in(note.group(1), skip_row_numbers=False)
                if re.fullmatch(re.escape(cells[0][1]) + r"\s+" +
                                re.escape(cells[1][1]), note.group(1)) is None:
                    continue
                recovered.append(StatementRow(
                    role=self.role, statement=self.statement,
                    label="经营活动产生的现金流量净额", amount=current,
                    prior_amount=prior, page=head[0], unit=self.unit,
                    current_cell=current_cell, prior_cell=cells[1][1],
                    raw_line="\n".join(item[1] for item in (head, middle, tail)),
                ))
            if recovered:
                return recovered[0] if len(recovered) == 1 else None
            # The other observed layout places both native amount cells on
            # the stem line, then the literal suffix on the next line.
            for index, head in enumerate(self.lines):
                match = re.fullmatch(r"经营活动产生的现金流\s+(.+)", head[1].strip())
                if match is None:
                    continue
                cells = _exact_native_amount_cells(match.group(1), 2)
                if cells is None:
                    continue
                cited = self.lines[index:index + 2]
                if not (len(cited) == 2 and cited[1][0] == head[0]
                        and _compact(cited[1][1]) == "量净额"):
                    # Actual601083 crosses exactly one page: last data row,
                    # numeric footer, same issuer/report header, first-data
                    # suffix. No arbitrary text/header skipping is allowed.
                    cited = self.lines[index:index + 4]
                    if not (len(cited) == 4
                            and self.continuation_report_header
                            and self.pdf_page_count >= head[0] + 1
                            and self.start_page <= head[0]
                            and head[0] + 1 <= self.end_page
                            and cited[1][0] == head[0]
                            and _compact(cited[1][1]) == f"{head[0]}/{self.pdf_page_count}"
                            and cited[2][0] == cited[3][0] == head[0] + 1
                            and _compact(cited[2][1]) == self.continuation_report_header
                            and _compact(cited[3][1]) == "量净额"):
                        continue
                recovered.append(StatementRow(
                    role=self.role, statement=self.statement,
                    label="经营活动产生的现金流量净额", amount=cells[0][2],
                    prior_amount=cells[1][2], page=head[0], unit=self.unit,
                    current_cell=cells[0][1], prior_cell=cells[1][1],
                    raw_line="\n".join(item[1] for item in cited),
                ))
            if recovered:
                return recovered[0] if len(recovered) == 1 else None
        if (key == "parent_equity" and self.role == ROLE_CONSOLIDATED
                and self.statement == "资产负债表" and not self.combined
                and self.scope_columns == 2 and self.unit in _UNIT_MULTIPLIER
                and self.current_header and self.prior_header):
            # Two literal BS layouts glue the end of the parent-equity label
            # to the following minority row. Its amounts are never the
            # parent's amounts; preserve the whole citation without moving
            # that following row into the parent's current/prior cells.
            for index, head in enumerate(self.lines):
                plain = re.fullmatch(r"归属于母公司所有者权益\s+(.+)", head[1].strip())
                split = re.fullmatch(r"归属于母公司所有者权益（或股\s+(.+)", head[1].strip())
                cells = None
                cited = ()
                if plain is not None:
                    cited = self.lines[index:index + 2]
                    if len(cited) != 2 or cited[1][0] != head[0]:
                        continue
                    tail = re.fullmatch(r"（或股东权益）合计少数股东权益\s+(.+)",
                                        cited[1][1].strip())
                    if tail is not None and _exact_native_amount_cells(tail.group(1), 2):
                        cells = _exact_native_amount_cells(plain.group(1), 2)
                elif split is not None:
                    cited = self.lines[index:index + 3]
                    if len(cited) != 3 or any(item[0] != head[0] for item in cited):
                        continue
                    tail = re.fullmatch(r"东权益）合计少数股东权益\s+(.+)",
                                        cited[2][1].strip())
                    current = _exact_native_amount_cells(split.group(1), 1)
                    prior = _exact_native_amount_cells(cited[1][1], 1)
                    if (tail is not None and _exact_native_amount_cells(tail.group(1), 2)
                            and current and prior):
                        cells = (current[0], prior[0])
                if cells is None:
                    continue
                recovered.append(StatementRow(
                    role=self.role, statement=self.statement,
                    label="归属于母公司所有者权益（或股东权益）合计",
                    amount=cells[0][2], prior_amount=cells[1][2],
                    page=head[0], unit=self.unit, current_cell=cells[0][1],
                    prior_cell=cells[1][1],
                    raw_line="\n".join(item[1] for item in cited),
                ))
            if recovered:
                return recovered[0] if len(recovered) == 1 else None
        for index, (page, line) in enumerate(self.lines):
            label = _label_only(line)
            if not label:
                continue
            clean = _LEADING_NUMBER.sub("", _normalize_label(label))
            if not matches(clean):
                continue
            # 金额可能紧邻下一行，也可能再隔一两个短行：无边框年报会把
            # "行名 + 附注引用" 单独放一行，金额在它下面，因此允许跳过
            # 不含金额且很短的续行，但不跳过任何含金额或含中文的行。
            located = None
            amount_position = None
            for offset in (1, 2, 3):
                position = index + offset
                if position >= len(self.lines):
                    break
                next_page, next_line = self.lines[position]
                amounts = _line_amounts_only(
                    next_line, scope_columns=self.scope_columns)
                if amounts is not None:
                    located = (*amounts, next_page, next_line)
                    amount_position = position
                    break
                loose = _line_amounts_loose(next_line)
                if loose is not None:
                    located = (loose[0], loose[1], loose[2], next_page, next_line)
                    amount_position = position
                    break
                # 续行允许是附注引用（``五、54``）这种短且无金额的片段。
                if re.search(r"[\u4e00-\u9fff]", next_line) and len(next_line) > 12:
                    break
            if located is None or amount_position is None:
                continue
            current_cell, current_value, prior_value, next_page, next_line = located
            # 金额之后可能还有该行名的括号收尾（``（或股东权益）合计``），
            # 它已经经由 ``_dewrapped`` 被接回行名；这里只在仍以括号开头时
            # 补一次，使证据行名与原文一致。
            trailing = ""
            closing = (self.lines[amount_position + 1][1].strip()
                       if amount_position + 1 < len(self.lines) else "")
            if closing.startswith(("（", "(")):
                trailing = closing
            recovered.append(StatementRow(
                role=self.role, statement=self.statement,
                label=(label + trailing) if trailing else label,
                amount=current_value,
                prior_amount=(Decimal(0) if prior_value is None else prior_value),
                page=page if page == next_page else next_page,
                unit=self.unit, current_cell=current_cell,
                prior_cell=(PRIOR_NOT_ON_LINE if prior_value is None
                            else str(prior_value)),
                raw_line=f"{label}\t{next_line.strip()}",
            ))
            if first_wins:
                break
        if recovered:
            return recovered[0] if len(recovered) == 1 else None
        # 最后一条路径：**本期为空**的科目。原文写作"科目名 + 附注 + 空金额"
        # （实测 601012 年报 ``利息收入 七、66`` 本期为"—"），这种行不该参与
        # 恒等式核对，因此只返回 None，让核对层报告"分量缺失"，而不是拿附注
        # 编号去算。``first_wins`` 的调用方（``component``）依赖这个 None。
        return None


@dataclass(frozen=True, slots=True)
class StatementSections:
    """一份报告的全部已定位表节。"""

    pdf_sha256: str
    sections: tuple[StatementSection, ...]

    def get(self, role: str, statement: str) -> StatementSection | None:
        for section in self.sections:
            if section.role == role and section.statement == statement:
                return section
        return None

    def require(self, role: str, statement: str) -> StatementSection:
        section = self.get(role, statement)
        if section is None:
            raise CninfoFinancialPdfError(
                f"{role}{statement} section is not uniquely locatable")
        return section


_NUMBER_TOKEN = re.compile(
    r"(?<![\d.,])"
    r"(?:-?(?:0|[1-9]\d{0,2}(?:,\d{3})*)(?:\.\d{1,2})?"
    r"|\(\s*(?:0|[1-9]\d{0,2}(?:,\d{3})*)(?:\.\d{1,2})?\s*\))"
    r"(?![\d.,])"
)


#: 行首编号词法。数字后紧跟这些分隔符时它是行号，不是金额列。
_ROW_NUMBER = re.compile(r"\d+[.、．，]|[一二三四五六七八九十]+、")


def _amounts_in(text: str, *, skip_row_numbers: bool = True,
                skip_note_references: bool = True,
                ) -> list[tuple[int, str, Decimal]]:
    """抽出文本中的金额单元格，连同它在原文中的起始下标。

    返回下标是必要的：调用方要靠它切出行名，而**不能**再用 ``text.index(token)``
    回查。行名里的附注编号可能与金额文本相同（实测 600519 的
    ``其中：营业收入 41 89,389,354,416.84``），回查会定位到 41 里的那个
    ``41``，行名随之被截成 ``其中：营业收入 41`` 而匹配失败。

    两类数字不是金额，必须排除：

    * **行首编号**（``2.少数股东损益`` 的 ``2``）——否则两列金额错位成三列；
    * **报表附注引用**（``五、54`` 的 ``54``）——无边框年报把它插在行名与
      金额列之间，误当金额会把收入读成附注编号。

    边界刻意收紧为"前后都不是数字、点或逗号"。``skip_row_numbers=False``
    用于"整行只含两个金额"的判定：那种行首尾不该出现行号，排除它会掩盖
    真正的版式歧义。
    """

    found: list[tuple[int, str, Decimal]] = []
    for match in _NUMBER_TOKEN.finditer(text):
        token = match.group()
        numeric_token = re.sub(r"\s+", "", token)
        if _NUMBER.fullmatch(numeric_token) is None:
            continue
        if skip_note_references and _is_note_reference(text, match):
            continue
        end = match.end()
        if skip_row_numbers and "," not in token and "." not in token:
            # 不含千分位与小数点的数字只可能是**编号**，不可能是金额：
            # 报表金额最小单位是元且按两位小数或千分位书写。这类编号有三种
            # 位置，都要排除：
            #   * 行首章节号 ``五、现金及现金等价物净减少额``；
            #   * 行首行号 ``2.少数股东损益``；
            #   * 附注子项 ``(五)58(1)`` 里的 ``1``（实测 002415 现金流量表：
            #     括号被换行截断，``(1)`` 里的 1 被当成当期金额）。
            # 早期只检查"后面紧跟分隔符"，会漏掉第三种，也会把``五``当成金额，
            # 导致整条现金恒等式定位失败。
            if len(token) == 1:
                continue
            if end < len(text) and text[end] in ".、．，":
                continue
            # 既不带千分位、又不是单位数、后面也不接分隔符时，只有**位数足够**
            # 才可能是金额。这里挡掉的是无边框版式里的**页码行**（实测 300502
            # 页面上单独一行的 ``3``／``40``）：它们含少量数字、又刚好落在
            # 数据行之间，被当成金额会让列头判定误以为"数据已经开始"。
            # 报表金额最小列示单位是元，三位数以上才可能是不带分隔符的真实金额。
            if len(token) < 3:
                continue
        try:
            value = Decimal(
                ("-" + numeric_token[1:-1] if numeric_token.startswith("(") else numeric_token).replace(",", ""))
        except InvalidOperation:
            continue
        found.append((match.start(), token, value))
    return found


def _is_note_reference(text: str, match: re.Match[str]) -> bool:
    """这个数字是否为**行名后的报表附注编号**。

    附注列位于行名与金额列之间，形如 ``其中：营业收入 41 89,389,354,416.84``；
    这个 ``41`` 不是金额。判定必须**窄**：只把"紧跟在行名之后、后面还有数字"
    的短整数当作附注编号。

    早期实现按"前文含 ``五、`` 就跳过下一个数字"判断，会误伤真正的金额列：
    实测 688981 的 ``其中：营业收入 4,412,132,088.76`` 被整列跳过，收入随之
    失配。因此这里不再依赖前文模式，只看位置与后续内容。
    """

    token = match.group()
    if "," in token or "." in token:
        return False
    before = text[:match.start()]
    if not before or before[-1].isdigit():
        return False
    if not re.search(r"\d", text[match.end():]):
        return False
    # 行名必须以汉字或中文标点收尾；否则更像行首编号，交给行号规则处理。
    # 必须容忍中间的空格：实测 600519 写作 ``其中：营业收入 41 89,389,354,416.84``，
    # 附注编号前有一个空格；只检查紧邻字符会把 41 当成金额，行名随之变成
    # ``其中:营业收入41`` 而匹配不上收入行。
    # 注意``、``是 U+3001，不在 ``\u4e00-\u9fff`` 里（实测 ``五、54`` 因此
    # 漏判），所以显式并入 CJK 标点区段。
    return bool(re.search(r"[\u4e00-\u9fff\u3000-\u303f:：)]\s*$", before))


def _exact_native_amount_cells(text: str, count: int
                               ) -> tuple[tuple[int, str, Decimal], ...] | None:
    """Literal whole-cell tokens only, with no note/prose/nil substitution."""
    text = text.strip()
    cells = _amounts_in(text, skip_row_numbers=False, skip_note_references=False)
    if len(cells) != count:
        return None
    if re.fullmatch(r"\s+".join(re.escape(cell[1]) for cell in cells), text) is None:
        return None
    return tuple(cells)


def _native_split_decimal_pair(page: Any, *, anchor: str,
                               heads: tuple[str, str], tails: tuple[str, str],
                               head_offset: float = 0.0,
                               ) -> tuple[tuple[str, Decimal], tuple[str, Decimal]]:
    """Join only two native, right-aligned cells across adjacent text baselines.

    This reads PDF words, never ruled-table cells or a candidate amount. The
    literal row recognizer supplies the original fragments; both columns must
    have one unambiguous word at each observed baseline and stay in their own
    ordered horizontal bands. No isolated decimal suffix is an amount.
    """
    words = page.extract_words()
    anchors = [word for word in words if _compact(word["text"]) == _compact(anchor)]
    if len(anchors) != 1:
        raise CninfoFinancialPdfError("split decimal row anchor is ambiguous")
    top = float(anchors[0]["top"]) + head_offset
    pairs = []
    for head, tail in zip(heads, tails):
        matches = [word for word in words if word["text"] == head
                   and abs(float(word["top"]) - top) <= 1.0]
        if len(matches) != 1:
            raise CninfoFinancialPdfError("split decimal head is not unique")
        first = matches[0]
        if tail:
            endings = [word for word in words if word["text"] == tail
                       and 10.0 <= float(word["top"]) - float(first["top"]) <= 18.0
                       and abs(float(word["x1"]) - float(first["x1"])) <= 0.5
                       and float(word["x0"]) >= float(first["x0"]) - 0.5]
            if len(endings) != 1:
                raise CninfoFinancialPdfError("split decimal suffix is not in the same native column")
            last = endings[0]
        else:
            last = first
        joined = head + tail
        cells = _exact_native_amount_cells(joined, 1)
        if cells is None or re.fullmatch(r"-?\d{1,3}(?:,\d{3})*\.\d{2}", joined) is None:
            raise CninfoFinancialPdfError("split decimal cell is incomplete")
        pairs.append((first, last, joined, cells[0][2]))
    current, prior = pairs
    if (float(current[0]["x1"]) >= float(prior[0]["x0"])
            or float(anchors[0]["x1"]) >= float(current[0]["x0"])
            or abs(float(current[0]["top"]) - float(prior[0]["top"])) > 1.0
            or (all(tails) and abs(float(current[1]["top"]) - float(prior[1]["top"])) > 1.0)):
        raise CninfoFinancialPdfError("split decimal columns are not ordered")
    return ((current[2], current[3]), (prior[2], prior[3]))


def _native_literal_section_rows(page: Any, lines: list[str], *, page_number: int,
                                 role: str, statement: str, unit: str,
                                 header: tuple[str, str] | None, combined: bool,
                                 scope_columns: int) -> tuple[StatementRow, ...]:
    """Finite raw-line layouts, before global dewrapping can glue other rows.

    Only the already identified consolidated two-period statement is eligible.
    Keep every original line in the citation, including both monetary columns.
    """
    if (role != ROLE_CONSOLIDATED or combined or scope_columns != 2
            or unit not in _UNIT_MULTIPLIER or not header or not all(header)):
        return ()
    found: list[StatementRow] = []

    def add(label: str, cells: Any, cited: list[str]) -> None:
        found.append(StatementRow(role=role, statement=statement, label=label,
            amount=cells[0][1], prior_amount=cells[1][1], page=page_number,
            unit=unit, current_cell=cells[0][0], prior_cell=cells[1][0],
            raw_line="\n".join(cited)))

    def ordinary(label: str, amounts: str, cited: list[str]) -> None:
        cells = _exact_native_amount_cells(amounts, 2)
        if cells is not None:
            add(label, tuple((cell[1], cell[2]) for cell in cells), cited)

    for index, line in enumerate(lines):
        following = lines[index + 1:index + 3]
        if statement == "利润表":
            # Actual FY25: the two cell heads precede the centred row label;
            # decimal tails follow it. Coordinates bind each tail to its own
            # native word, so 69 cannot become the current revenue cell.
            if line.strip() == "其中：营业收入" and index and following:
                heads = re.fullmatch(r"(-?\d{1,3}(?:,\d{3})+) (-?\d{1,3}(?:,\d{3})+\.)", lines[index - 1].strip())
                if heads:
                    tails = re.fullmatch(r"(\.\d{2}) (\d{2})", following[0].strip())
                    if tails is None:
                        raise CninfoFinancialPdfError("revenue decimal tails are incomplete")
                    cells = _native_split_decimal_pair(page, anchor=line.strip(),
                        heads=heads.groups(), tails=tails.groups(), head_offset=-7.8)
                    add("其中：营业收入", cells, lines[index - 1:index + 2])
            # Literal two-line profit rows with only the current decimal
            # fraction wrapped. A complete prior cell cannot stand in for it.
            for anchor, suffix, label in (
                ("五、净利润（净亏损以“－”号", "填列）", "五、净利润（净亏损以“－”号填列）"),
                ("1.归属于母公司股东的净", "利润（净亏损以“-”号填列）", "1.归属于母公司股东的净利润（净亏损以“-”号填列）"),
            ):
                match = re.fullmatch(re.escape(anchor) + r"\s+(-?\d{1,3}(?:,\d{3})+\.)\s+(-?\d{1,3}(?:,\d{3})*\.\d{2})", line.strip())
                if match:
                    tail = (re.fullmatch(re.escape(suffix) + r"\s+(\d{2})", following[0].strip())
                            if following else None)
                    if tail is None:
                        raise CninfoFinancialPdfError("profit decimal suffix is incomplete")
                    cells = _native_split_decimal_pair(page, anchor=anchor,
                        heads=match.groups(), tails=(tail.group(1), ""))
                    add(label, cells, lines[index:index + 2])
            match = re.fullmatch(r"1\.归属于母公司股东的\s+(.+)", line.strip())
            if (match and len(following) == 2
                    and following == ['净利润（净亏损以“-”号', '填列）']):
                ordinary("1.归属于母公司股东的净利润（净亏损以“-”号填列）",
                         match.group(1), lines[index:index + 3])
        elif statement == "现金流量表" and following:
            for stem, suffix in (("经营活动产生的现金", "流量净额"),
                                 ("经营活动产生的现", "金流量净额")):
                match = re.fullmatch(re.escape(stem) + r"\s+(.+)", line.strip())
                if match and following[0].strip() == suffix:
                    ordinary("经营活动产生的现金流量净额", match.group(1), lines[index:index + 2])
        elif statement == "资产负债表" and len(following) == 2:
            match = re.fullmatch(r"归属于母公司所有者权\s+(.+)", line.strip())
            minority = re.fullmatch(r"少数股东权益\s+(.+)", following[1].strip())
            if (match and following[0].strip() == "益（或股东权益）合计"
                    and minority and _exact_native_amount_cells(minority.group(1), 2)):
                ordinary("归属于母公司所有者权益（或股东权益）合计", match.group(1), lines[index:index + 3])
    labels = [row.label for row in found]
    if len(set(labels)) != len(labels):
        raise CninfoFinancialPdfError("literal native row is duplicated")
    return tuple(found)


def _merge_native_literal_rows(ordinary: list[StatementRow],
                               native: tuple[StatementRow, ...],
                               statement: str) -> list[StatementRow]:
    """Keep successful direct rows; only the proven decimal misread replaces one."""
    additions = []
    retained = list(ordinary)
    for row in native:
        fields = {field for field, kind in FIELD_STATEMENT.items() if kind == statement
                  and _field_matches(field, _normalize_label(row.label))}
        matches = [old for old in retained if any(
            _field_matches(field, _normalize_label(old.label)) for field in fields)]
        # A complete amount literally on an original line is an ordinary
        # fallback: never replace an already successful direct observation.
        # Decimal-fragment recovery is different: that exact complete current
        # token is absent from the raw lines, and the old direct value may be
        # only a complete prior cell. Its unique geometry has been checked.
        if matches:
            if row.current_cell in row.raw_line:
                continue
            # Only the exact original head line whose sole parsed amount
            # was this validated prior cell is the known error. A separate
            # successful direct row must retain its original observation.
            head = row.raw_line.splitlines()[0]
            parsed = _row_amounts(head)
            prior_misreads = [old for old in matches if
                old.role == row.role and old.statement == row.statement
                and old.page == row.page and old.unit == row.unit
                and not old.nil_amounts and old.raw_line == head
                and parsed == (old.label, old.current_cell, old.amount, None)
                and old.current_cell == row.prior_cell
                and old.amount == row.prior_amount
                and old.prior_amount == Decimal(0)
                and old.prior_cell == PRIOR_NOT_ON_LINE]
            if not prior_misreads:
                continue
            if len(prior_misreads) != 1 or len(matches) != 1:
                raise CninfoFinancialPdfError("split decimal row conflicts with a separate direct observation")
        retained = [old for old in retained if old not in matches]
        additions.append(row)
    return [*additions, *retained]


def _row_amounts(line: str, *, scope_columns: int = 2,
                 ) -> tuple[str, str, Decimal, Decimal | None] | None:
    """把一行原文解析为 (可见行名, 当期单元格, 当期值, 上期值)。

    与候选提取器使用同一套数字词法（千分位、两位小数、括号负数），但直接
    作用于原文行，因此**不依赖 pdfplumber 的表格切分**。

    ``scope_columns`` 指出该行的金额列**按几个口径分组**：

    * ``2``（默认）：当期列、比较列各一列；
    * ``4``：``合并当期 | 合并上期 | 公司当期 | 公司上期``——"合并及公司…"
      这类表把两个口径并排放在四列里（实测 000333）。此时**只取第 1、2 个
      金额**作为合并口径的当期/上期。早期按"前两个金额"取值，取到的其实是
      公司列，会把公司口径标成合并口径（000333 合并营业总收入 409,084,266
      被读成 407,149,600）。

    上期值可以是 ``None``：有些行只列本期。实测 300502 2026 半年报的
    ``2.少数股东损益（净亏损以"—"号填列） 37,146,656.75`` 只有一列金额，
    早期要求"至少两个金额"会让整行消失，利润恒等式随之报"少数股东损益缺失"。
    缺失就是缺失，**不能**用本期值或 0 顶上。
    """

    text = line.strip()
    if not text:
        return None
    found = _amounts_in(text)
    if not found:
        return None
    if scope_columns > 2:
        if len(found) < scope_columns:
            # 不足声明列数就不是同一版式的行（例如公司列为空的行只剩两列），
            # 这种行按两列处理即可，不能强行按四列取值。
            scope_columns = 2
        elif found[0][2] != found[2][2]:
            # 第 1、3 列本应是**同一期间的合并与公司**口径，数值几乎不可能相等；
            # 相等说明这不是"四列两口径"结构，退回两列读法，避免误取。
            scope_columns = 2
    (start, current, current_value) = found[0]
    if scope_columns > 2:
        prior_value = found[1][2]
    else:
        prior_value = found[1][2] if len(found) > 1 else None
    # 用匹配下标切行名，不能用 text.index(token) 回查：行名里的附注编号可能
    # 与金额文本相同，回查会定位到附注编号。
    label = text[:start].strip()
    if not label:
        return None
    return label, current, current_value, prior_value


def _line_amounts_only(line: str, *, scope_columns: int = 2
                       ) -> tuple[str, Decimal, Decimal] | None:
    """把只含金额的行解析为 (当期单元格, 当期值, 上期值)。

    ``scope_columns=4`` 时，行里有**四个**金额（``合并当期 合并上期 公司当期
    公司上期``），必须只取第 1、2 个。否则会把公司口径的当期值当成合并当期
    （实测 000333：``经营活动产生的现金流量净额 … 37,552,090 37,281,015
    35,163,272 34,159,720`` 被读成 35,163,272）。
    """

    text = line.strip()
    if not text:
        return None
    found = _amounts_in(text, skip_row_numbers=False)
    expected = 4 if scope_columns > 2 else 2
    if len(found) != expected:
        return None
    current, current_value = found[0][1], found[0][2]
    prior_value = found[1][2]
    if text[:found[0][0]].strip():
        return None
    last = found[1]
    if text[last[0] + len(last[1]):].strip():
        return None
    return current, current_value, prior_value


#: 单列金额行的哨兵：年报无边框版式下，长行名与金额被拆行，当期金额可能
#: 单独占一行而比较列被截断。此时当期值仍然可用，比较列明确记为缺失，
#: 绝不猜一个数。恒等式只使用当期值，因此哨兵不会污染核对。
PRIOR_NOT_ON_LINE = "not-on-line"


def _amounts_on_line(line: str) -> list[tuple[str, Decimal]]:
    """该行出现的全部金额单元格（无下标视图）。"""

    return [(token, value) for _, token, value in _amounts_in(line.strip())]


def _line_amounts_loose(line: str) -> tuple[str, Decimal, Decimal | None] | None:
    """行内只含 1~2 个金额、且没有汉字时按行尾金额解析。

    用于无边框年报版式：行名与金额被拆到相邻行，比较列还可能被截断。
    """

    text = line.strip()
    if not text or re.search(r"[\u4e00-\u9fff]", text):
        return None
    found = _amounts_in(text, skip_row_numbers=False)
    if not 1 <= len(found) <= 2:
        return None
    _, current, current_value = found[0]
    prior_value = found[1][2] if len(found) == 2 else None
    return current, current_value, prior_value


def _label_only(line: str) -> str | None:
    """取出一行原文中金额之前的可见行名。

    返回 ``None`` 表示这一行不是候选行名行：它要么本身就是一条纯金额行
    （换行拆开的金额部分），要么含有无法解释为行名的数字。

    注意行首编号（``1.归属于母公司股东的净利润…``）：金额词法刻意拒绝
    ``1.`` 这样"点后不接数字"的形式（否则它会把编号误当成一列金额），
    因此这里必须**先剥掉编号再找金额**，不能直接对整行做金额切分。
    """

    text = line.strip()
    if not text:
        return None
    stem = _LEADING_NUMBER.sub("", text)
    if stem != text:
        return stem if re.search(r"[\u4e00-\u9fff]", stem) else None
    parsed = _row_amounts(text)
    if parsed is not None:
        label = parsed[0]
        return label if re.search(r"[\u4e00-\u9fff]", label) else None
    if _line_amounts_only(text) is not None:
        return None
    if re.search(r"\d", text):
        return None
    return text if re.search(r"[\u4e00-\u9fff]", text) else None


def _scaled_amount(row: StatementRow) -> Decimal:
    if row.unit not in _UNIT_MULTIPLIER:
        raise CninfoFinancialPdfError(f"unsupported amount unit {row.unit!r}")
    return row.amount * _UNIT_MULTIPLIER[row.unit]


#: 标准字段到承载它的合并表节。
FIELD_STATEMENT = {
    "parent_equity": "资产负债表",
    "revenue": "利润表",
    "net_profit_consolidated": "利润表",
    "net_profit_attributable": "利润表",
    "operating_cashflow": "现金流量表",
}

#: 报告期对应的报告名称，用于封面身份核对。
_PERIOD_REPORT_NAME = {
    (3, 31): ("第一季度报告", "第一季度"),
    (6, 30): ("半年度报告", "半年度"),
    (9, 30): ("第三季度报告", "第三季度"),
    (12, 31): ("年度报告", "年度"),
}


def period_report_names(period_end: date) -> tuple[str, str]:
    """返回该报告期的 (报告名称, 期间词)。不支持的报告期失败关闭。"""

    key = (period_end.month, period_end.day)
    if key not in _PERIOD_REPORT_NAME:
        raise CninfoFinancialPdfError(
            f"{period_end.isoformat()} is not a supported report period")
    return _PERIOD_REPORT_NAME[key]


def check_report_cover(
    pdf_bytes: bytes, *, company: str, period_end: date, instrument_id: str | None = None,
) -> str:
    """核对前置身份页：公司全称、报告名称与年份必须同页出现。

    返回压缩后的封面文本供证据留存。身份不成立时失败关闭，绝不"取第一个
    看起来像公司的字符串"。
    """

    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - environment guard
        raise CninfoFinancialPdfError(
            "pdfplumber is required for PDF candidate extraction") from exc
    report_name, _ = period_report_names(period_end)
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # 图形封面/空白扉页不含文字（实采 001286、002608），但前三页的
        # 正式报告标题仍明确身份。只查这个有界前置区域，不到正文中召回
        # 另一家公司或另一年度。遇到已有文字公司身份的页必须立即核对，
        # 不能跳过错误封面，再靠后页“补齐”身份。
        for page in pdf.pages[:3]:
            cover = _compact(page.extract_text() or "")
            has_company = re.search(r"(?:股份有限公司|股份公司|有限公司)", cover)
            if not has_company:
                continue
            if (_compact(company) in cover and report_name in cover and
                    re.search(rf"(?<!\d){period_end.year}(?!\d)", cover)):
                return cover
            break
    if instrument_id is not None:
        from .cninfo_report_identity import compound_report_identity, IDENTITY_PAGE_LIMIT
        from pypdf import PdfReader
        reader=PdfReader(io.BytesIO(pdf_bytes))
        pages=[(i+1,(page.extract_text() or "").splitlines())
               for i,page in enumerate(reader.pages[:IDENTITY_PAGE_LIMIT])]
        chain=compound_report_identity(pages,instrument_id,company,period_end)
        if chain:
            # Source pages/lines remain explicit. Primary admission re-executes
            # this chain from receipt-bound bytes; geometry does likewise.
            return "\n".join(f"PDF page {page}: {line}" for group in chain for page,line in group)
    raise CninfoFinancialPdfError("PDF cover company or period mismatch")


def extract_section_facts(
    pdf_bytes: bytes, *,
    instrument_id: str, company: str, period_end: date, announcement_id: str,
    document_url: str, required_fields: tuple[str, ...] | None = None,
) -> CninfoS2CandidateFacts:
    """按表节路径提取 S2 候选，覆盖无边框年报版式。

    与 ``extract_cninfo_candidate_facts`` 的差别只在**定位方式**：本函数直接
    按表节页区间逐行取当期列，不依赖 pdfplumber 的表格切分，因此对"行名与
    金额被拆行、附注列插在金额列左侧"的无边框年报同样成立。两者都要求官方
    URL 身份、封面身份、唯一行名与当期列在前。
    """

    if not re.fullmatch(r"\d{6}", instrument_id):
        raise CninfoFinancialPdfError("instrument_id must be a six-digit code")
    url_match = _CNINFO_PDF_URL.fullmatch(document_url)
    if url_match is None or url_match.group(1) != announcement_id:
        raise CninfoFinancialPdfError("official announcement identity mismatch")
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise CninfoFinancialPdfError("input must be PDF bytes")
    fields = tuple(sorted(required_fields or tuple(FIELD_STATEMENT)))
    if len(fields) != len(set(fields)) or not set(fields) <= set(FIELD_STATEMENT):
        raise CninfoFinancialPdfError("required S2 fields are invalid")
    check_report_cover(pdf_bytes, company=company, period_end=period_end,instrument_id=instrument_id)
    sections = extract_statement_sections(
        pdf_bytes, period_end=period_end,
        statements=tuple(sorted({FIELD_STATEMENT[field] for field in fields})))
    digest = sections.pdf_sha256
    candidates = []
    for field in fields:
        section = sections.require(ROLE_CONSOLIDATED, FIELD_STATEMENT[field])
        row = section.find(field)
        if row is None:
            raise CninfoFinancialPdfError(
                f"{field}: consolidated row is not uniquely locatable")
        candidates.append(PdfCandidateFact(
            field=field, value_yuan=_scaled_amount(row), period_end=period_end,
            report_period_text=section.current_header,
            statement={"资产负债表": "balance", "利润表": "profit",
                       "现金流量表": "cashflow"}[section.statement],
            pdf_page=row.page, column_header=section.current_header,
            amount_unit=section.unit, currency="CNY",
            source_row=row.raw_line.replace("\t", " "),
            source_cells=(row.label, row.current_cell),
            source_current_cell_index=1, source_prior_cell_index=1,
            pdf_sha256=digest, display_row_label=row.label,
        ))
    return CninfoS2CandidateFacts(
        instrument_id=instrument_id, period_end=period_end,
        version_label="section_layout_extracted", document_url=document_url,
        pdf_sha256=digest, candidates=tuple(candidates),
    )


def _balance_sheet_header(labels: list[str], period: date) -> tuple[str, str] | None:
    """资产负债表的当期/比较列。

    资产负债表**只有两列金额且当期列在左**，所以列序本身就是证据：

    * ``期末余额 / 期初余额``（或 ``年末余额 / 年初余额``）；
    * 两个具体日期列。比较列有三种真实写法：**上年同日**（``2024年6月30日``）、
      **本年年初**（``2024年1月1日``，半年报）、**上年年末**
      （``2024年12月31日``，实测 600519 半年报）。三者都合法，因此这里只要求
      两列都是日期，不强制比较列等于某个特定日期；
    * 上面两支都不成立时，仍要求至少一列带报告期年份，再取前两列。

    这里比利润表宽松，是因为资产负债表没有"本期/上期发生额"那种会改变
    经济含义的歧义；利润表仍沿用原有严格判定，避免把比较列当当期列。
    """

    if len(labels) < 2:
        return None
    head = labels[:2]
    if head == ["期末余额", "期初余额"]:
        return head[0], head[1]
    if head == ["年末余额", "年初余额"]:
        return head[0], head[1]
    dated = re.compile(r"\d{4}年\d{1,2}月\d{1,2}日")
    if all(dated.fullmatch(label) for label in head):
        return head[0], head[1]
    if any(str(period.year) in label for label in head):
        return head[0], head[1]
    return None


#: 页眉字符串。无边框版式把页眉与紧随其后的报表行**粘成一行**，行名因此被
#: 污染。实测 300502 2026 半年报：
#: ``成都新易盛通信技术股份有限公司2026年半年度报告全文其中：营业收入 20,909,746,962.20 …``
#: 行名变成 ``…报告全文其中:营业收入``，收入行定位失败；同页还有单独的
#: ``3``／``40`` 页码行。
#: 这里只剥"公司名 + 年份 + 报告类型"这种**结构明确**的前缀，不做模糊匹配。
_PAGE_HEADER = re.compile(
    r"^[\u4e00-\u9fff（）()]{4,40}?"
    r"20\d{2}\s*年\s*(?:年度|半年度|第[一二三四]季度|中期|一季度|三季度)?\s*报告"
    r"(?:全文|摘要)?\s*"
)


def _strip_page_header(line: str) -> str:
    """剥掉粘在行首的页眉。只认结构明确的前缀，避免误伤正常行名。"""

    stripped = _PAGE_HEADER.sub("", line)
    return stripped if stripped else line


#: 单独一列的候选是否**本身像金额列**：期间词或余额词。用于在"列头按每行一列
#: 排布"时排除页眉年份与括号注记这类噪声。
#: 括号注记（``(已重述)``/``(经重述)``）。它们命中列头词但**不是金额列**，
#: 取"最右两列"时会把真正的金额列挤掉（实测 002415 2024 年报）。
_COLUMN_ANNOTATION_ONLY = re.compile(r"[(（]\s*(?:已|经)?重述\s*[)）]")
_AMOUNT_COLUMN_ONLY = re.compile(
    r"本期发生额|上期发生额|上年同期发生额|本年发生额|上年发生额"
    r"|期末余额|期初余额|年末余额|年初余额"
)
#: 单独一列的候选是否**整行就是一个日期**（资产负债表的日期列头）。
#: ``2024年12月31日`` 成立；``2024年12月31日止年度`` 不成立——后者是期间描述，
#: 不是列头，放进来会让候选窗口混入噪声。
_DATE_COLUMN_ONLY = re.compile(r"\d{4}年\d{1,2}月\d{1,2}日")


#: S2 五字段的**整词**模式。
#:
#: 判定"拼出来是不是一个报表术语"必须整词匹配，**不能**复用 ``_field_matches``：
#: 后者对归母权益只判断"以``归属于母公司所有者/股东权益``开头"且"行内出现
#: ``合计``"（``re.match(...) and "合计" in clean``），于是
#: ``…（或股东权益）合计``再往后接一个字**仍然算命中术语**。实测这条漏洞会把
#: 一条**本来完整**的归母权益行多切一个字、同时削掉下一行的首字——标准资产
#: 负债表里``少数股东权益``正是紧跟在归母权益之后的那一行（行名取自 600519
#: 实测原文，金额为示意）：
#:
#: .. code-block:: text
#:
#:     归属于母公司所有者权益（或股东权益）合计 238,646,536,390.48 …  → 被切掉"少"
#:     少数股东权益 2,000,000.00 …                                    → 变成"数股东权益"
#:
#: 同理 ``net_profit_consolidated`` 的 ``startswith("净利润")`` 与
#: ``net_profit_attributable`` 的前缀匹配也都不是整词判断。
_FIELD_WHOLE_LABEL = re.compile(
    r"(?:归属于母公司(?:所有者|股东)(?:权益|股东权益)(?:\(或股东权益\))?合计"
    r"|(?:其中:)?营业收入"
    r"|净利润(?:\(净亏损以.*号填列\))?"
    r"|归属于母公司(?:股东|所有者)的净利润(?:\(净亏损以.*号填列\))?"
    r"|经营活动产生(?:/\(使用\))?的现金流量净额)"
)


def _strip_glued_section_header(label: str) -> str:
    """剥掉粘在行名前的**分节标题**（实测 000333）。

    ``合并及公司…`` 利润表把分节标题与首行挤在同一行：

    ```
    (二)按所有权归属分类归属于母公司股东的净利润 26,446,037 …
    少数股东损益 136,837 …
    ```

    行名因此变成 ``按所有权归属分类归属于母公司股东的净利润``（``(二)`` 已被
    附注剥离规则吃掉），前缀式模式虽能命中，但真正的问题是**同一术语在
    归一化后仍不等词**，且这类前缀会让后续任何"整词"判定失败。

    **判定锚在"分类后面紧跟一个已知术语"上，而不是"含有分类"**：全库有 57 个
    行名含 ``分类``，其中绝大多数是**正当行名**（``不能重分类进损益的其他综合
    收益``、``金融资产重分类计入其他综合收益的金额``）。只有"分类"之后**恰好
    接着已知术语**时才是被粘住的分节标题，这样正当行名一个都不会被误伤。
    """

    position = label.find("分类")
    while position != -1:
        tail = label[position + len("分类"):]
        if tail and any(tail.startswith(term) for term in _GLUED_HEADER_TERMS):
            return tail
        position = label.find("分类", position + 1)
    return label


#: 分节标题粘连判定允许的后续术语。只列**会出现在分节标题之后**的科目主干，
#: 不求全：判定条件要求"分类"后**紧跟**其中一项，因此不会误伤正当行名。
_GLUED_HEADER_TERMS = (
    "归属于母公司股东的净利润",
    "归属于母公司所有者的净利润",
    "归属于母公司所有者的综合收益总额",
    "归属于母公司股东的综合收益总额",
    "归属于少数股东的综合收益总额",
    "少数股东损益",
)


def _matches_component_label(label: str) -> bool:
    """归一化后是否**恰好等于**某个已知恒等式术语或字段。

    "恰好等于"是这条判定的全部价值所在：它决定"某个字该接回上一行、还是留给
    下一行"，而两个方向都能拼出一个像模像样的行名，只有拼出来确实是术语的
    方向才有原文证据。因此这里既不能用前缀判断（见 ``_FIELD_WHOLE_LABEL``），
    也不能容忍行尾多出字来。

    一处例外要说明白：``_COMPONENT_LABELS`` 里归母净利润那一支结尾带 ``.*$``
    （刻意的，无边框版式会把"（净亏损以"—"号填列）"截断，见该常量的说明），
    所以它不算整词判断。这一处不会造成误切：调用方 ``_resplit_wrapped_tail``
    只在"本行行名**还**不是术语"时才动字，而带 ``.*$`` 的那一支能匹配的任何
    拼法，都要求本行已经含有完整词干——那种本行自己就命中了术语，判定会直接
    跳过（实测形态：``归属于母公司所有者的净利润（净亏损以"—"号填列）`` 后接
    ``少数股东损益…``，两条行都完整，不许动）。
    """

    cleaned = _LEADING_NUMBER.sub("", _normalize_label(label)).strip()
    if not cleaned:
        return False
    if _FIELD_WHOLE_LABEL.fullmatch(cleaned):
        return True
    return any(
        re.compile(pattern).match(cleaned)
        for patterns in _COMPONENT_LABELS.values() for pattern in patterns
    )


def _page_furniture(line: str) -> bool:
    """页码、页眉、页脚、项目符号等**不是报表内容**的行。

    用于"跨页续行"的宿主回看：实测 600519 2025 年报把一个行名与其尾行分到
    两页，中间夹着 ``58/143``（页码）、``贵州茅台酒股份有限公司2025年年度报告``
    （页眉）与一个项目符号。这些行必须被**跳过**而不是当作搜索边界，
    否则跨页的续行永远接不回宿主。
    """

    text = line.strip()
    if not text:
        return True
    # 页码：``58/143``、``第 58 页``、纯数字。
    if re.fullmatch(r"\d+\s*/\s*\d+", text) or re.fullmatch(r"第?\s*\d+\s*页?", text):
        return True
    if re.fullmatch(r"\d+", text):
        return True
    # 页眉：``公司名 + 年份 + 报告``（与 ``_PAGE_HEADER`` 同构）。
    if _PAGE_HEADER.match(text):
        return True
    # 纯符号/项目符号（无汉字无数字）。
    return not re.search(r"[\u4e00-\u9fff\d]", text)


def _resplit_wrapped_tail(
    merged: list[tuple[int, str]],
) -> list[tuple[int, str]]:
    """修复"行名末字被折到下一行行首"的版式。

    实测 600276 2026 半年报合并资产负债表：

    ```
    归属于母公司所有者权益（或股东权益）合 64,466,430,205.71 61,272,068,641.41
    计少数股东权益 523,310,211.35 524,754,772.84
    ```

    权益行的末字 ``计`` 折到了下一行，粘在 ``少数股东权益`` 前面。于是权益行的
    行名停在 ``…合``（匹配不上 ``…合计``），下一行又多出 ``计`` 前缀。

    **判定以"结果可验证"为准，而不是猜位置**：只有当"把下一行的前 1~2 个汉字
    接到本行行名末尾"能得到一个**已知术语**（``_matches_component_label``）时
    才重切。普通相邻两行几乎不可能凑巧命中，因此不会误合并。

    两条守卫是这一判定的**必要条件**，缺一不可：

    * 本行行名**本身还不是**已知术语——已经完整的行没有任何可补的字，而
      "完整行名 + 下一行首字"仍可能被前缀式模式判成术语（``_COMPONENT_LABELS``
      里归母净利润那一支就带 ``.*$``），把两条完好的行同时切坏；
    * 切走之后剩下的部分**仍像行名**（还有汉字）。
    """

    out: list[tuple[int, str]] = list(merged)
    for position in range(len(out) - 1):
        page, line = out[position]
        next_page, following = out[position + 1]
        if page != next_page:
            continue
        head = _row_amounts(line)
        tail = _row_amounts(following)
        if head is None or tail is None:
            continue
        head_label, rest = head[0], tail[0]
        # 本行与下一行都必须**自带金额**：本行是"行名被截断的数据行"，
        # 下一行是"被粘了前缀的数据行"。纯续行不适用本规则。
        if not head_label or len(rest) < 3:
            continue
        # 行名以句读/右括号收尾时本身就是完整行名，不该再接字。
        if head_label[-1] in "）：:。；，,、":
            continue
        # 已经是完整术语的行名不再接字。实测这条守卫拦下的是标准资产负债表
        # 版式：``归属于母公司所有者权益（或股东权益）合计`` 后面紧跟
        # ``少数股东权益``，没有它就会切成 ``…合计少`` / ``数股东权益``。
        if _matches_component_label(head_label):
            continue
        for take in (1, 2):
            if len(rest) <= take:
                break
            moved, remainder = rest[:take], rest[take:]
            if not _matches_component_label(head_label + moved):
                continue
            # 剩下的部分必须仍像行名，否则说明切错位置。
            if not remainder or not re.search(r"[\u4e00-\u9fff]", remainder):
                continue
            out[position] = (page, line.replace(head_label, head_label + moved, 1))
            out[position + 1] = (next_page, following.replace(rest, remainder, 1))
            break
    return out


def _dewrapped(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """合并被换行截断的行名。

    无边框版式会把一个长行名硬拆成两行，而且续行常常与下一行粘在一起：

    .. code-block:: text

        1、归属于母公司股东的净利润（净亏损以“-”号填
        列）2、 少数股东损益（净亏损以“-”号填列） 186,498,915.07 -1,298,364,690.59

    判定条件是"当前行没有金额，且下一行以当前行内容开头"。只有这种**可验证
    的拼接关系**才被接受；其余换行保持原样，避免把无关两行粘成一个假行名。
    """

    merged: list[tuple[int, str]] = []
    index = 0
    while index < len(lines):
        page, line = lines[index]
        text = line.strip()
        # Actual 000415: a classification subtitle is not a broken row label.
        if re.fullmatch(r"(?:[(（][一二三四五六七八九十\d]+[)）])?按(?:所有权归属|经营持续性)分类", _compact(text)):
            merged.append((page, text))
            index += 1
            continue
        header_labels = _column_header_labels(text)
        header_remainder = _COLUMN_TOKEN.sub("", _compact(text))
        if header_labels and re.fullmatch(
                r"(?:项目|附注[一二三四五六七八九十百\d]*|资产|负债(?:及|和)股东权益)*",
                header_remainder):
            # Actual 000415 has a standalone 附注五 + two explicit period
            # columns before 营业收入. Those headings cannot become its label.
            merged.append((page, text))
            index += 1
            continue
        if index + 1 < len(lines):
            following = lines[index + 1][1].strip()
            # 行名被拆开，且续行与下一行粘在一起（``列）2、 少数股东损益…``）。
            if (text and not _amounts_in(text)
                    and _line_amounts_only(text) is None
                    and following.startswith(text)):
                merged.append((page, text + following))
                index += 2
                continue
            # 行名的括号收尾单独占一行，真正的行首在下一行。
            if text in ("列）", "列)", "）") and following:
                merged.append((page, text + following))
                index += 2
                continue
            # 第三种拆行：行名、金额、**行名收尾**各占一行。
            # 实测 300750 现金流量表与 600276 利润表：
            #   ``四、汇率变动对现金及现金等价物的`` / ``-2,664,640 -1,596,552`` / ``影响``
            #   ``五、净利润（净亏损以“－”号填``   / ``3,428,132,358.04 …``    / ``列）（一）按经营持续性分类``
            # 收尾排在本行**之后**，所以"行名 + 紧随金额"的复原路径看不到它。
            # 判定点在**金额行**上：它只含金额（无汉字），下一行是短中文片段。
            # 早期把判定写在处理收尾行时、并错测了当前行长度，条件永不成立
            # （实测：600276 的 ``五、净利润`` 整行因此从未出现）。
            if (_line_amounts_only(text) is not None
                    and not re.search(r"[\u4e00-\u9fff]", text)
                    and merged and len(following) <= 24
                    and following
                    and not _amounts_in(following)
                    and re.search(r"[\u4e00-\u9fff]", following)
                    and not _looks_like_amount_row(following)
                    # **页眉/页脚/页码不能当"收尾片段"**：它们是页面装饰，不是
                    # 行名的一部分。把它们接进金额行会污染宿主行名，使后续的
                    # 括号尾行再也接不回去。实测 600519 2025 年报：
                    # ``归属于母公司所有者权益 244,637,811,032.18 …`` /
                    # ``58/143``（页脚） / ``贵州茅台酒股份有限公司2025年年度报告``
                    # （页眉，被误当收尾片段接了上去） /
                    # ``（或股东权益）合计少数股东权益 …``——宿主行名被污染成
                    # ``…股东权益 244,637,811,032.18 …贵州茅台酒股份有限公司2025年年度报告``，
                    # ``parent_equity`` 整期定位失败。
                    and not _page_furniture(following)):
                # 金额行本身的下标；它应当就是 next_index 指向的那一行。
                next_index = index + 1
                following_page = lines[next_index][0]
                if following_page == page:
                    previous_page, previous = merged[-1]
                    merged[-1] = (previous_page,
                                  f"{previous.strip()}{following}\t{text}")
                    index += 2
                    continue
            # 第四种拆行：行名被**在字中间**拆到相邻两行，金额夹在后面那一行
            # 的行首之后。实测 600276 2024 年报：
            #   ``1.归属于母公司股``
            #   ``东的净利润（净亏损以 6,336,527,014.75 4,302,435,930.05``
            #   ``“-”号填列）（一）归属母公司所``
            # 直接解析第二行只能得到 ``东的净利润（净亏损以``，于是归母净利润
            # 整行定位失败。
            #
            # 判定必须双向收敛，避免把两行无关内容粘成假行名：
            #   * 本行**没有金额**，且以**词中字符**收尾（不是句读/右括号/冒号）；
            #   * 下一行**有金额**，且以**词中字符**开头（不是编号、年份、括号）。
            # 只在这种情况下拼接；拼接后按原有词法解析。
            if (_amounts_in(text) == [] and text
                    and re.search(r"[\u4e00-\u9fff]$", text)
                    and text[-1] not in "）：:。；，,、"
                    and following and _amounts_in(following)
                    and re.match(r"[\u4e00-\u9fff]", following)
                    and not re.match(r"^[一二三四五六七八九十]+[、.]", following)):
                merged.append((page, text + following))
                index += 2
                continue
            # 行名被**右括号开头的续行**截断：``归属于母公司所有者`` 后接
            # ``（或股东权益）合计``。把续行接回最近一条"无金额且有汉字"的
            # 行即可复原完整行名。中间可能夹着表头（``项目 附注 …``）或页码，
            # 因此向后回看有限几行，而不是只看紧邻上一行。
            #
            # **含金额的行绝不能当宿主**：半年报的版式是
            # ``归属于母公司所有者权益`` / ``238,646,536,390.48 …`` /
            # ``（或股东权益）合计``，把尾行接回金额行会同时毁掉金额行名与
            # 尾行本身（实测 600519），因此必须跳过并另找宿主。
            if text.startswith(("（", "(")):
                host = None
                # 尾行自带的括号片段。
                tail_close = min(
                    (position for position in
                     (text.find("）"), text.find(")")) if position != -1),
                    default=-1)
                tail_fragment = text[:tail_close + 1] if tail_close != -1 else text
                for back in range(1, 7):
                    if len(merged) < back:
                        break
                    previous_page, previous = merged[-back]
                    # 页码/页眉/页脚不是宿主的候选，但**也不能当作边界**：
                    # 实测 600519 2025 年报的行名与尾行被**分页**隔开——
                    # ``归属于母公司所有者权益`` / ``58/143``（页脚）/
                    # ``贵州茅台酒股份有限公司2025年年度报告``（页眉）/
                    # ``（或股东权益）合计少数股东权益 …``。
                    # 回看窗口太小或把页眉页脚当边界，都会找不到宿主。
                    if _page_furniture(previous):
                        continue
                    carries = _amounts_in(previous)
                    if carries:
                        # **含金额的行一般不能当宿主**：半年报的版式是
                        # ``归属于母公司所有者权益`` / ``238,646,536,390.48 …`` /
                        # ``（或股东权益）合计``，把尾行接回金额行会同时毁掉
                        # 金额行名与尾行本身（实测 600519 半年报）。
                        #
                        # **但有一个可验证的例外**：如果该行的行名**本身还不是
                        # 完整术语**、而"行名 + 括号片段"**恰好是**完整术语，
                        # 那么括号片段就是缺失的那部分，接回去是复原而不是破坏。
                        # 实测 600519 2025 年报：``归属于母公司所有者权益
                        # 244,637,811,032.18 …`` 的金额是**完整**的，缺的只是行名
                        # 尾巴 ``（或股东权益）合计``。两条判据都用已知术语表，
                        # 因此不依赖版式猜测。
                        host_label = _LEADING_NUMBER.sub(
                            "", _normalize_label(_label_only(previous) or previous))
                        if not (_matches_component_label(host_label + tail_fragment)
                                and not _matches_component_label(host_label)):
                            continue
                    elif not re.search(r"[\u4e00-\u9fff]", previous):
                        continue
                    host = back
                    break
                if host is None:
                    # 没有可接续的宿主：它本身就是一条独立行名。
                    merged.append((page, line))
                    index += 1
                    continue
                previous_page, previous = merged[-host]
                # **尾行自己带金额**是常见形态（实测 600519 2025 年报：
                # ``归属于母公司所有者权益`` / ``（或股东权益）合计少数股东权益
                # 9,321,442,876.89`` / …）。此时不能把「尾行整行」接到宿主上——
                # 那会把**下一行的行名与金额**一起并进宿主的行名里，宿主的金额
                # 也随之丢失。
                # 正确做法：只把**括号片段**接回宿主，括号之后的内容（下一行的
                # 行名）留在尾行原来的位置上，金额不动。
                tail_close = min(
                    (position for position in
                     (text.find("）"), text.find(")")) if position != -1),
                    default=-1)
                if tail_close != -1 and _amounts_in(text):
                    merged[-host] = (previous_page,
                                     previous.strip() + text[:tail_close + 1])
                    merged.append((page, text[tail_close + 1:]))
                    index += 1
                    continue
                merged[-host] = (previous_page, previous.strip() + text)
                index += 1
                continue
        merged.append((page, line))
        index += 1
    # 最后再修"行名末字折到下一行行首"的形态（600276 的 ``…合`` / ``计少数股东权益``）。
    # 前面几条规则都在处理"续行接回本行"，这一条方向相反：字是从**本行尾**掉到
    # **下一行首**的，因此必须在所有"向前接续"规则之后再判定。
    return _resplit_wrapped_tail(merged)


#: 行名右括号的孤儿碎片。无边框版式把上一行的"（净亏损以"—"号填列）"截断，
#: 收尾的``列）``会粘到下一行行首，排在行号之前（``列）2、 少数股东损益…``）。
#: 它属于上一行，必须在归一化时丢掉，否则下一行的行号不再是行首。
_ORPHAN_FRAGMENT = re.compile(r"^[列项]?[)）]")


def _normalize_label(value: str) -> str:
    """行名归一化：空白、全角括号、全角冒号、报表附注交叉引用。

    全角冒号必须一起归一化：原文写``其中：营业收入``，``_field_matches`` 只
    认半角``:``，不归一化就会漏掉收入行（实测 000651 年报无边框版式）。
    附注引用（``五、54``）同理：它紧跟行名，留着会让行名不再是纯行名。

    最后再兜一次"行名以**裸数字**收尾"：附注编号不带章节号时（实测 600519
    ``其中：营业收入 41 89,389,354,416.84``），无论它是在抽取阶段被当成
    金额、还是在归一化阶段残留，都会让行名变成``其中:营业收入41``。
    行名本身不会以数字结尾，因此剥掉它是安全的。
    """

    clean = _compact(value).replace("：", ":")
    clean = _ORPHAN_FRAGMENT.sub("", clean)
    clean = _NOTE_REFERENCE.sub("", clean)
    clean = _TRAILING_DIGITS.sub("", clean)
    if clean == "归属母公司股东权益合计":
        clean = "归属于母公司股东权益合计"
    return clean.strip()


#: ``合并及公司资产负债表``（实测 000333）的列头证据。列头被拆成三行：
#:   ``资产 附注 2025年 2024年 2025年 2024年``
#:   ``12月31日 12月31日 12月31日 12月31日``
#:   ``合并 合并 公司 公司``
#: 四列依次是 合并当期 / 合并上期 / 公司当期 / 公司上期，因此**合并口径就是
#: 最左两列**，不能因为表名带"及公司"就取错列。
_COMBINED_FULL_DATE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_COMBINED_YEAR_ONLY = re.compile(r"(\d{4})\s*年")
_COMBINED_DAY_ONLY = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日")
#: 口径行。两种真实写法：
#:   * ``合并 合并 公司 公司``（实测 000333 资产负债表/利润表）；
#:   * ``项目 附注 合并 合并 公司 公司``（实测 000333 现金流量表——同一行里
#:     既有列头标记词 ``项目``/``附注``，也有口径词）。
#: 因此允许 ``项目``/``附注`` 与口径词混排，但**行内不得出现别的汉字**，
#: 否则表名的 ``及公司`` 之类也会被算进来（子串扫描会数错口径）。
_COMBINED_SCOPE_LINE = re.compile(
    r"^(?:(?:合并|母公司|公司|项目|附注)[*＊]?[\s　]*){4,}$")


def _combined_scope_column_count(window: list[str]) -> int | None:
    """该表节的行是否按**四列两口径**排布；是则返回 4。

    判据是同一页既有"整行只含口径词"的行、且口径为
    ``合并 合并 公司 公司``——这正是"合并及公司…"表的列结构标记。
    返回 4 后，行金额只取第 1、2 列（合并口径）。
    """

    columns = _combined_scope_columns(window)
    if columns is None:
        return None
    if columns[0] == "合并" and columns[1] == "合并" \
            and columns[2] in ("公司", "母公司"):
        return 4
    return None


def _combined_scope_columns(window: list[str]) -> list[str] | None:
    """取出"整行只含口径词/列头标记词"的那一行的四列口径，按出现顺序返回。"""

    for line in window[:8]:
        compact = _compact(line)
        if _COMBINED_SCOPE_LINE.match(compact):
            columns = re.findall(r"合并|母公司|公司", compact)
            if len(columns) == 4:
                return columns
    return None


#: 只含年份的列头行：``资产 附注 2025年 2024年 2025年 2024年``。
_YEAR_ONLY_LINE = re.compile(r"^(?=.*\d{4}\s*年)[^月日]*$")
#: 只含"年份+期间词"的列头行：``项目 附注 2024年度 2023年度 2024年度 2023年度``
#: （实测 000333 的合并及公司利润表：列头用"年度"而不是具体日期）。
_PERIOD_WORD_LINE = re.compile(
    r"^(?=[\s\S]*\d{4}\s*年(?:度|上半年|半年度|中期))"
    r"(?![\s\S]*\d{1,2}\s*月)"
    r"[\s\S]*$")
_PERIOD_WORD_TOKEN = re.compile(r"(\d{4})\s*年(度|上半年|半年度|中期)?")


def _combined_period_word_dates(window: list[str]) -> list[str] | None:
    """从"年度/半年度"式列头取四列日期。

    两种真实排布：
      * ``项目 附注 2024年度 2023年度 2024年度 2023年度`` + 下一行 ``合并 合并 公司 公司``（利润表）；
      * ``2024年度 2023年度 2024年度 2023年度`` + ``项目 附注 合并 合并 公司 公司``（现金流量表）。

    期间词只给到年，因此按报告期把年份规整成日期：年报→12月31日，
    半年报→6月30日。比较列同样按这个规则还原。
    """

    for line in window[:8]:
        compact = _compact(line)
        if "月" in compact:
            continue
        tokens = _PERIOD_WORD_TOKEN.findall(compact)
        if len(tokens) != 4:
            continue
        out: list[str] = []
        for year, word in tokens:
            month_day = "06月30日" if word in ("上半年", "半年度", "中期") else "12月31日"
            out.append(f"{int(year)}年{month_day}")
        return out
    return None
#: 只含"月日"的列头行：``12月31日 12月31日 12月31日 12月31日``。
_DAY_ONLY_LINE = re.compile(r"^(?:[\s　]*\d{1,2}\s*月\s*\d{1,2}\s*日[\s　]*)+$")


def _combined_column_dates(window: list[str]) -> list[str] | None:
    """把**按行拆开**的四列日期重新拼成四个完整日期，按列序返回。

    实测 000333 的列头是"年份行 + 月日行"两行；把两行直接拼接会让年份与月日
    交错（``2025年`` 后面紧跟的 ``2024年`` 会被误判成完整日期的一部分），
    所以必须**分行**取：年份行给出四个年，紧随的月日行给出四个月日，按位配对。
    """

    for index, line in enumerate(window):
        compact = _compact(line)
        if not (compact and compact.isprintable() and _YEAR_ONLY_LINE.match(compact)):
            continue
        years = _COMBINED_YEAR_ONLY.findall(compact)
        if len(years) != 4:
            continue
        for following in window[index + 1:index + 3]:
            day_line = _compact(following)
            if not _DAY_ONLY_LINE.match(day_line):
                continue
            days = _COMBINED_DAY_ONLY.findall(day_line)
            if len(days) != 4:
                continue
            return [f"{int(years[i])}年{int(days[i][0])}月{int(days[i][1])}日"
                    for i in range(4)]
    return None


def _combined_statement_header(
    window: list[str], period: date,
) -> tuple[str, str] | None:
    """识别"合并及公司"四列列头，返回**合并口径**的 (当期列, 比较列)。

    口径取自**单独成行**的口径行，日期取自"年份行 + 月日行"，都不从表名推断，
    也不把多行硬拼成一段（拼了就会把报表日那一行也算成列头）。
    """

    columns = _combined_scope_columns(window)
    if columns is None:
        return None
    if columns[0] != "合并" or columns[1] != "合并":
        return None
    if columns[2] not in ("公司", "母公司"):
        return None

    dates = _combined_column_dates(window)
    if dates is None:
        # 利润表/现金流量表用"年度"式列头，而不是具体日期（实测 000333）。
        dates = _combined_period_word_dates(window)
    if dates is None:
        # 四列日期也可能同处一行（``2025年12月31日 ×4``）。
        for line in window[:8]:
            compact = _compact(line)
            found = [f"{int(m.group(1))}年{int(m.group(2))}月{int(m.group(3))}日"
                     for m in _COMBINED_FULL_DATE.finditer(compact)]
            if len(found) >= 4:
                dates = found[:4]
                break
    if dates is None or len(dates) < 4:
        return None
    # 四列日期必须两两对应（合并当期=公司当期、合并上期=公司上期），
    # 否则说明这不是"合并+公司"的重复列结构，宁可不认。
    if dates[0] != dates[2] or dates[1] != dates[3]:
        return None
    return dates[0], dates[1]


def _header_for_statement(statement: str, labels: list[str],
                          period: date) -> tuple[str, str] | None:
    """按报表类型选择当期/比较列。

    资产负债表与部分半年报利润表直接用**两个日期**做列头
    （``2025年6月30日 2024年12月31日``），``_expected_header`` 只认"半年度/
    年度"这类期间词，对纯日期列会判成歧义（实测 002415 半年报利润表）。
    两张表都只有两列金额且当期列在左，因此列序本身就是证据：两个日期即可
    确定，不需要比较列等于某个特定日期。
    """

    head = labels[:2]
    dated = re.compile(r"\d{4}年\d{1,2}月\d{1,2}日")
    if len(head) == 2 and all(dated.fullmatch(label) for label in head):
        return head[0], head[1]
    if statement == "资产负债表":
        return _balance_sheet_header(labels, period)
    if (head == ["本期发生额", "上期发生额"]):
        return head[0], head[1]
    half_year = [re.fullmatch(r"(20\d{2})年0?1[-－—–]0?6月", label) for label in head]
    if (len(half_year) == 2 and all(half_year) and
            (period.month, period.day) == (6, 30) and
            [int(match[1]) for match in half_year] == [period.year, period.year - 1]):
        return head[0], head[1]
    return _expected_header(statement, period, ["项目", *labels])


def _carries_amount(row: StatementRow) -> bool:
    """该行的"金额"是否真的来自金额列，而不是被误读的附注编号。

    只看行名**是否以附注引用收尾**。带真实金额的行，其行名在附注引用之后
    还会有列头文字残留（``少数股东损益 五、30 本期发生额``），或附注引用
    后面还有别的内容；而本期为空的科目，行名恰好停在附注引用处
    （``利息收入 七、66``）。用整行匹配会误杀前者——实测 000651 的
    少数股东损益行就是这样被错误剔除的。
    """

    return _NOTE_REFERENCE_TAIL.search(row.label) is None


#: 单元格里的"无此项"占位符。中文财报用 ``-``/``——`` 表示本期为空，
#: **不是**金额 0 或任何数值。报表把 ``利息收入 - -`` 写成一行时，若把
#: ``-`` 当数字，恒等式就会拿一个不存在的金额去核对（实测 688981：
#: 利息收入被读成 912,210，收入恒等式因此被错误判负）。
_NIL_CELL = re.compile(r"[-—－–−]+|不适用|无")
#: 只由占位符与空白组成的行（``- -``、``— —``）。
_NIL_ONLY_LINE = re.compile(r"^(?:\s*(?:[-—－–−]+|不适用|无)\s*)+$")


def _is_nil_cell(cell: str | None) -> bool:
    return cell is None or _NIL_CELL.fullmatch(cell.strip()) is not None


def _line_is_nil_amounts(line: str) -> bool:
    """该行是否只由"无此项"占位符组成（可能带行首编号）。"""

    text = _LEADING_NUMBER.sub("", _compact(line))
    return bool(text) and _NIL_ONLY_LINE.fullmatch(text) is not None


def _looks_like_amount_row(line: str) -> bool:
    """该行是否是一条带两列金额的报表行。

    用于区分"表头还没凑齐"与"数据已经开始"：一旦出现数据行，再往后的同名
    列头就属于附注而不是本表节，必须停止累积，否则会把附注里的日期当成金额列
    （实测 002415 的 ``项目 附注 本期发生额`` 单独一行，比较列在下一行）。
    """

    return _row_amounts(line) is not None


def _looks_like_row_label(label: str) -> bool:
    """排除页眉、页脚、表头和叙述性文本，只保留可能的报表行名。"""

    clean = _compact(label)
    if not clean or len(clean) > 60:
        return False
    if re.search(r"第\s*\d+\s*页", label):
        return False
    if any(token in clean for token in (
        "报告全文", "年度报告", "半年度报告", "季度报告",
        "法定代表人", "主管会计工作负责人", "会计机构负责人",
        "项目", "附注",
    )):
        return False
    return bool(re.search(r"[\u4e00-\u9fff]", clean))


@lru_cache(maxsize=4)
def _pdf_statement_headings(pdf_bytes: bytes) -> tuple:
    """Reuse immutable geometry from identical archived PDF bytes only.

    Independent checks still read their separate rows and statement crops.
    A different payload cannot reuse a prior report's cached headings.
    """
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        headings = [heading for index, page in enumerate(pdf.pages)
                    for heading in _page_headings(page, index)]
        adjusted = []
        for heading in headings:
            page = pdf.pages[heading.page_index]
            if (heading.unit is None and heading.bottom > page.height - 120 and
                    heading.page_index + 1 < len(pdf.pages)):
                # Actual 603889 FY25: title at the foot of page 90, its unit
                # and column header begin page 91. Never cross a new title or
                # a data row to borrow a later table's unit.
                units = []
                for line in (pdf.pages[heading.page_index + 1].extract_text() or "").splitlines()[:10]:
                    if _HEADING.fullmatch(_compact(line)) or _looks_like_amount_row(line):
                        break
                    currency = _CURRENCY.search(line)
                    if currency and currency[1].upper() not in ("人民币", "CNY"):
                        raise CninfoFinancialPdfError("statement explicitly uses a non-CNY currency")
                    match = _UNIT.search(line)
                    if match: units.append(match[2])
                if len(set(units)) > 1:
                    raise CninfoFinancialPdfError("statement has conflicting amount units")
                if units: heading = replace(heading, unit=units[0])
            # Actual 000550: repeated unmarked balance title separates the
            # assets and liabilities halves. Only this adjacent, explicit
            # four-scope column layout is a continuation.
            if adjusted and heading.statement == "资产负债表" and heading.combined:
                previous = adjusted[-1]
                if (previous.role == heading.role and previous.statement == heading.statement and
                        previous.combined and previous.unit == heading.unit and
                        previous.page_index + 1 == heading.page_index):
                    current_text = page.crop((0,heading.bottom,page.width,min(page.height,heading.bottom+160))).extract_text() or ""
                    previous_page = pdf.pages[previous.page_index]
                    previous_text = previous_page.crop((0,previous.bottom,previous_page.width,min(previous_page.height,previous.bottom+160))).extract_text() or ""
                    if ("负债及股东权益" in _compact(current_text) and
                            re.search(r"(?:^|\n)\s*资产\s*附注", previous_text)):
                        heading = replace(heading, continuation=True)
            adjusted.append(heading)
        return tuple(adjusted)


def _split_six_month_header(lines: list[str], period: date) -> tuple[str, str] | None:
    """CN offshore H1 header, with years/dates/duration in three aligned rows."""
    if (period.month, period.day) != (6,30): return None
    for index, line in enumerate(lines[:8]):
        years = re.fullmatch(r"截至(20\d{2})年截至(20\d{2})年", _compact(line))
        if not years or tuple(map(int,years.groups())) != (period.year,period.year-1): continue
        following = [_compact(x) for x in lines[index+1:index+3]]
        if len(following) != 2: continue
        if (following[0] not in ("6月30日止6月30日止","6月30日6月30日") or
                following[1] not in ("项目附注6个月期间6个月期间","项目附注止6个月期间止6个月期间")): continue
        return tuple(f"截至{year}年6月30日止6个月期间" for year in years.groups())
    return None


@dataclass(frozen=True, slots=True)
class _UnscopedStatementEnd:
    """A source-located terminator only; it has no financial statement role."""

    page_index: int
    top: float


def _unscoped_statement_end(pdf: Any, start: Any, continuation: Any,
                            period_end: date) -> _UnscopedStatementEnd | None:
    """Close a final continuation before an explicit next unmarked statement.

    Actual 000415 H125 has a consolidated cash table on p69-70, followed by
    an unmarked balance sheet on p71. The latter may terminate the cash table
    but must never become a guessed parent/consolidated statement itself.
    """

    if start.role != ROLE_CONSOLIDATED or start.statement != "现金流量表":
        return None
    next_index = continuation.page_index + 1
    if next_index >= len(pdf.pages):
        return None
    origin_text = (pdf.pages[start.page_index].extract_text() or "").splitlines()
    issuer_pattern = re.compile(r"[\u4e00-\u9fffA-Za-z（）()]+(?:股份有限公司|有限责任公司|有限公司)")
    origin_issuers = {line for value in origin_text[:4]
                      if issuer_pattern.fullmatch(line := _compact(value))}
    if len(origin_issuers) != 1:
        return None
    page = pdf.pages[next_index]
    lines = (page.extract_text() or "").splitlines()
    candidates = [(index, line, _compact(line)) for index, line in enumerate(lines[:6])
                  if _compact(line) in STATEMENTS and _compact(line) != start.statement]
    if len(candidates) != 1:
        return None
    index, title_line, statement = candidates[0]
    # This bounded repair needs the following unmarked balance sheet only.
    # Do not add unmarked flow-table period semantics through this terminator.
    if statement != "资产负债表":
        return None
    before = [_compact(line) for line in lines[:index]]
    # Require the same explicitly printed legal issuer immediately before the
    # title. Notes, policy narratives and a foreign issuer do not close a table.
    if not before or before[-1] not in origin_issuers or any(
            token in line for line in before for token in ("附注", "说明", "会计政策")):
        return None
    positions = page.search(re.escape(title_line.strip()))
    if len(positions) != 1 or positions[0]["top"] > min(150, page.height * 0.25):
        return None
    window = lines[index + 1:index + 7]
    units, explicit_cny, explicit_period, headers = [], False, False, []
    report_date = re.compile(rf"^{period_end.year}年0?{period_end.month}月0?{period_end.day}日")
    for line in window:
        if _looks_like_amount_row(line):
            break
        compact = _compact(line)
        if any(token in compact for token in ("模板", "示例", "说明", "会计政策", "不代表", "财务报表附注")):
            return None
        currencies = list(_CURRENCY.finditer(line))
        if any(currency[1].upper() not in ("人民币", "CNY") for currency in currencies):
            return None
        explicit_cny |= bool(currencies)
        for unit in _UNIT.finditer(line):
            units.append(unit[2])
            explicit_cny |= unit[1] == "人民币"
            explicit_period |= bool(report_date.match(compact))
        labels = _column_header_labels(line)
        if (labels and len(labels) == 2 and
                any(marker in compact for marker in ("项目", "附注"))):
            try:
                header = _header_for_statement(statement, labels, period_end)
            except CninfoFinancialPdfError:
                return None
            if header is not None:
                headers.append(header)
    if (not explicit_cny or not explicit_period or set(units) != {start.unit} or
            len(headers) != 1):
        return None
    # The boundary is not a new financial statement. Still validate any dated
    # balance header's actual current date/order before using its position.
    current, prior = headers[0]
    if statement == "资产负债表":
        dated = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
        values = [dated.fullmatch(value) for value in (current, prior)]
        if not all(values):
            return None
        try:
            current_date, prior_date = (date(*map(int, value.groups())) for value in values)
        except ValueError:
            return None
        if current_date != period_end or prior_date >= period_end:
            return None
    return _UnscopedStatementEnd(next_index, float(positions[0]["top"]))


def _literal_continuation_report_header(pdf: Any, period_end: date) -> str:
    """Unique whole issuer/report line from this PDF's bounded cover region."""
    report_name, _ = period_report_names(period_end)
    pattern = re.compile(
        r"[\u4e00-\u9fffA-Za-z0-9()·]+(?:股份有限公司|股份公司|有限公司)"
        + re.escape(f"{period_end.year}年{report_name}")
    )
    headers = {
        _compact(line)
        for page in pdf.pages[:3]
        for line in (page.extract_text() or "").splitlines()
        if pattern.fullmatch(_compact(line))
    }
    return next(iter(headers)) if len(headers) == 1 else ""


def extract_statement_sections(
    pdf_bytes: bytes,
    *,
    period_end: date,
    roles: tuple[str, ...] = (ROLE_CONSOLIDATED, ROLE_PARENT),
    statements: tuple[str, ...] = STATEMENTS,
) -> StatementSections:
    """定位表节并抽出每条两列金额行；任何歧义都失败关闭。"""

    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise CninfoFinancialPdfError("input must be PDF bytes")
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - environment guard
        raise CninfoFinancialPdfError(
            "pdfplumber is required for PDF candidate extraction") from exc
    digest = "sha256:" + hashlib.sha256(pdf_bytes).hexdigest()
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            headings = _pdf_statement_headings(pdf_bytes)
            sections = []
            for role in roles:
                for statement in statements:
                    matches = [item for item in headings if item.role == role and
                               item.statement == statement and item.unit is not None]
                    # "…（续）"是同一张表的下一页，不是第二个表节；把它的
                    # 页区间并入本表节，否则长表面临"多个表节"的误判。
                    starts = [item for item in matches if not item.continuation]
                    if len(starts) != 1:
                        # 缺失的母公司表节是合法情形（例如部分简明披露），
                        # 但重复出现意味着定位不可靠，必须失败关闭。
                        if len(starts) > 1:
                            raise CninfoFinancialPdfError(
                                f"{role}{statement}: multiple unit-bearing headings")
                        continue
                    start = starts[0]
                    following = [item for item in headings
                                 if (item.page_index, item.top) > (start.page_index, start.top)]
                    if not following:
                        raise CninfoFinancialPdfError(
                            f"{role}{statement}: no following statement boundary")
                    end = min(following, key=lambda item: (item.page_index, item.top))
                    index = headings.index(end)
                    while (index < len(headings) and headings[index].continuation
                           and headings[index].role == role
                           and headings[index].statement == statement):
                        index += 1
                        if index < len(headings):
                            end = headings[index]
                    if index >= len(headings) and end.continuation:
                        terminator = _unscoped_statement_end(pdf, start, end, period_end)
                        if terminator is not None:
                            end = terminator
                    if end.page_index - start.page_index > 12:
                        raise CninfoFinancialPdfError(
                            f"{role}{statement}: section boundary is too far away")
                    header: tuple[str, str] | None = None
                    rows: list[StatementRow] = []
                    section_lines: list[tuple[int, str]] = []
                    #: 行金额按几个口径分组。2 = 当期/比较各一列；
                    #: 4 = "合并及公司…"表的``合并当期|合并上期|公司当期|公司上期``。
                    #: 必须在进入循环前就有值：列头判定只在第一页执行。
                    scope_columns = 2
                    for page_index in range(start.page_index, end.page_index + 1):
                        page = pdf.pages[page_index]
                        top = start.bottom if page_index == start.page_index else 0
                        bottom = end.top if page_index == end.page_index else page.height
                        if bottom <= top:
                            continue
                        cropped_page = page.crop((0, top, page.width, bottom))
                        text = cropped_page.extract_text() or ""
                        section_lines.extend(
                            (page_index + 1, line) for line in text.splitlines())
                        page_text_lines = text.splitlines()
                        # 列结构在**每一页**都要重新判定，并把已知结果作为下限带入：
                        # "合并及公司…"表的续表页往往不再重印口径行（``合并 合并
                        # 公司 公司``），只看当前页会退回两列读法，从而把公司列
                        # 当成合并列。首版漏了这一点，被下游的兜底检查抓了出来。
                        page_scopes = _combined_scope_column_count(page_text_lines)
                        if page_scopes is not None:
                            scope_columns = page_scopes
                        if header is None:
                            # ``合并及公司…`` 的四列列头没有任何"项目/附注"标记行，
                            # 且日期本身跨行拆开，通用累积逻辑看不到它。先单独识别
                            # 这种版式，再走通用路径（实测 000333）。
                            combined = _combined_statement_header(
                                page_text_lines, period_end)
                            if combined is not None:
                                header = combined
                            elif not start.combined and statement != "资产负债表":
                                header = _split_six_month_header(page_text_lines, period_end)
                            # 两个日期列头可能落在**相邻两行**上（实测 002415
                            # 半年报：``项目 附注 2024年06月30日`` 与下一行的
                            # 上年日期）。因此把连续几行的候选列累积起来，
                            # 凑够两列再判定，而不是要求它们同处一行。
                            pending: list[str] = []
                            for line in (() if header is not None else page_text_lines):
                                if _looks_like_amount_row(line):
                                    # 数据已经开始，再往后的同名"列头"属于附注。
                                    break
                                labels = _column_header_labels(line)
                                if labels is not None:
                                    pending.extend(labels)
                                if len(pending) < 2:
                                    continue
                                # 两个列头可能被单位行或注解行隔开（实测 002415：
                                # ``上年同期发生额``、``单位：人民币元``、
                                # ``项目 附注 本期发生额``），因此在一个**短窗口**
                                # 内尝试所有配对，按"最近两列优先"取配对（reversed），
                                # 但每个配对内部**先试文档顺序再试反转**：文档顺序
                                # 就是"当期在前"，是最可能的正确读法。
                                #
                                # 早期在配对内部先试反转，使
                                # ``本年发生额 上年发生额`` 这种正常表头被当成
                                # ``上年发生额 本年发生额`` 而判歧义，整份利润表
                                # 定位失败（实测 002415 2024 年报）。
                                pairs = [
                                    (pending[left], pending[right])
                                    for right in range(len(pending))
                                    for left in range(right)
                                ]
                                for pair in reversed(pairs):
                                    # The new literal H1/relative pair is already printed
                                    # as two ordered columns. Do not reverse it while
                                    # the body reader still takes the first amount cell.
                                    exact_order = (set(pair) == {"本期发生额", "上期发生额"} or
                                                   any(re.fullmatch(r"20\d{2}年0?1[-－—–]0?6月", item)
                                                       for item in pair))
                                    orders = (list(pair),) if exact_order else (list(pair), list(reversed(pair)))
                                    for ordered in orders:
                                        try:
                                            header = _header_for_statement(
                                                statement, ordered, period_end)
                                        except CninfoFinancialPdfError:
                                            header = None
                                        if header is not None:
                                            break
                                    if header is not None:
                                        break
                                pending = pending[-2:]
                                if header is not None:
                                    break
                        # 行必须用**复原换行后**的行文本构建。早期这里直接遍历
                        # ``text.splitlines()``（原文行），于是 ``_dewrapped`` 的
                        # 复原只影响了 ``lines``，没影响 ``rows``——像 300750
                        # ``四、汇率变动对现金及现金等价物的`` / ``金额`` / ``影响``
                        # 这种三行拆法，``lines`` 里已经合成完整行名，``rows`` 里
                        # 却永远缺这一行，恒等式因此一直报"分量缺失"。
                        native_rows = _native_literal_section_rows(
                            cropped_page, page_text_lines, page_number=page_index + 1,
                            role=role, statement=statement, unit=start.unit or "",
                            header=header, combined=start.combined,
                            scope_columns=scope_columns,
                        )
                        page_row_start = len(rows)
                        page_lines = [_strip_page_header(line) for _, line in _dewrapped(
                            [(page_index + 1, line) for line in text.splitlines()])]
                        for offset, line in enumerate(page_lines):
                            parsed = _row_amounts(line, scope_columns=scope_columns)
                            if parsed is None:
                                continue
                            label, current_cell, current_value, prior_value = parsed
                            if not _looks_like_row_label(label):
                                continue
                            # 无边框版式把"无此项"写在**下一行**（``利息收入`` /
                            # ``- -``）。这种行的金额是缺失，不是占位符本身。
                            nil_row = _is_nil_cell(current_cell) or any(
                                _line_is_nil_amounts(following)
                                for following in page_lines[offset + 1:offset + 3]
                            )
                            rows.append(StatementRow(
                                role=role, statement=statement,
                                label=label, amount=current_value,
                                # 上期列可以整列不列示。按本仓已有约定记录缺失：
                                # ``prior_amount`` 归零且 ``prior_cell`` 标为
                                # ``PRIOR_NOT_ON_LINE``，第三方据此可判断"原文没有
                                # 这一列"，而不是把它读成 0。
                                prior_amount=(Decimal(0) if prior_value is None
                                              else prior_value),
                                page=page_index + 1,
                                unit=start.unit or "", current_cell=current_cell,
                                prior_cell=(PRIOR_NOT_ON_LINE if prior_value is None
                                            else str(prior_value)),
                                raw_line=line.strip(),
                                nil_amounts=nil_row,
                            ))
                        if native_rows:
                            # The ordinary parser may have taken a complete
                            # prior cell (or a bare decimal tail) as current.
                            # Replace only these exact recovered field rows on
                            # this page; unrelated direct rows stay byte exact.
                            rows[page_row_start:] = _merge_native_literal_rows(
                                rows[page_row_start:], native_rows, statement)
                    if header is None:
                        raise CninfoFinancialPdfError(
                            f"{role}{statement}: current/prior column header is ambiguous")
                    if not rows:
                        raise CninfoFinancialPdfError(
                            f"{role}{statement}: no two-column amount row was located")
                    wrapped_lines = tuple(_dewrapped(section_lines))
                    continuation_header = ""
                    if (role == ROLE_CONSOLIDATED and statement == "现金流量表"
                            and any(
                                index + 3 < len(wrapped_lines)
                                and line.startswith("经营活动产生的现金流 ")
                                and wrapped_lines[index + 3][0] == page_num + 1
                                and _compact(wrapped_lines[index + 3][1]) == "量净额"
                                for index, (page_num, line) in enumerate(wrapped_lines))):
                        continuation_header = _literal_continuation_report_header(pdf, period_end)
                    sections.append(StatementSection(
                        role=role, statement=statement, start_page=start.page_index + 1,
                        end_page=end.page_index + 1, unit=start.unit or "",
                        current_header=header[0], prior_header=header[1],
                        rows=tuple(rows), lines=wrapped_lines,
                        combined=getattr(start, "combined", False),
                        scope_columns=scope_columns,
                        continuation_report_header=continuation_header,
                        pdf_page_count=len(pdf.pages),
                    ))
    except CninfoFinancialPdfError:
        raise
    except Exception as exc:  # noqa: BLE001 - unknown parser behaviour cannot be promoted
        raise CninfoFinancialPdfError(
            f"statement section extraction failed: {type(exc).__name__}") from exc
    if not sections:
        raise CninfoFinancialPdfError("no statement section could be located")
    return StatementSections(pdf_sha256=digest, sections=tuple(sections))


# ---------------------------------------------------------------------------
# 第三条定位路径：合并附注『营业收入和营业成本情况』的收入分解
# ---------------------------------------------------------------------------
#
# 为什么需要它——实测四份官方原文的主利润表都无法证实营业收入：
#
# * 601012 2024 年报：``营业总收入 = 82,582,273,118.72`` 与``其中：营业收入``
#   完全相等，而``利息收入 = 950,045,457.25``；两者不可能同时是收入的组成部分，
#   收入恒等式因此自相矛盾；
# * 300750 2025 年报（千元）：``营业总收入 = 423,701,834`` 也等于``其中：营业
#   收入``，而金融收入扣减项为 10,594,657；
# * 688981 2026 半年报（千元）：``营业总收入 = 38,635,115`` 亦然，扣减项 912,210；
# * 002415 2024 半年报：合并利润表**根本没有**``营业总收入``行，收入以``营业收入``
#   为首行，扣减恒等式无从建立。
#
# 这四份原文的**合并附注**都单列了收入分解表（``主营业务``/``其他业务``/``合计``）。
# 它与利润表既不是同一张表、也不在同一页，因此可以充当独立的第二处原文：
# 主营业务 + 其他业务 = 合计 必须等于候选营业收入。

#: 附注行在证据里的报表名。附注不是主表：它与利润表不在同一页，这正是本路径
#: 独立性的来源，因此证据里必须能与利润表行区分开。
REVENUE_NOTE_STATEMENT = "利润表附注"

#: 合并/母公司附注的节标题。"这一段附注属于谁"只能靠它判断：母公司附注里同样
#: 有『营业收入和营业成本』表，数值与合并数可以相差数倍（实测 002415 2024H1：
#: 母公司合计 10,889,673,181.86 对合并合计 41,209,096,206.36），按页码先后猜
#: "第一个就是合并"会在一份只披露母公司分解的报告上给出错误结论。
#:
#: 实测四种真实写法，缺一不可：
#:
#: * ``(五) 合并财务报表项目注释``（002415 半年报；续页写 ``… - 续``）；
#: * ``七、合并财务报表项目注释``（300750 年报、688981 半年报、601012 年报）；
#: * ``五、合并财务报表项目附注``（000651 年报——同一份报告里合并段写"附注"、
#:   母公司段写"注释"，只认一种写法会把合并段整个判成缺失）；
#: * ``十八、母公司财务报表主要项目注释``（000651 半年报）。
_NOTE_SCOPE_HEADING = re.compile(
    r"^[(（]?(?:\d+|[一二三四五六七八九十百]+)[)）、.．]?\s*"
    r"(合并|母公司|公司)财务报表(?:主要)?项目(?:注释|附注)"
    r"(?:\s*[-—－]\s*续|[(（]\s*续\s*[)）])?$"
)

#: 附注收入分解表的标题。必须**整行等于标题**（容许前面有附注编号 ``61.`` 或子表
#: 编号 ``(1)``）：附注里还有 ``营业收入、营业成本的分解信息`` 这类子表，以及
#: "…之'七、合并财务报表项目注释''50、营业收入和营业成本'。"这类交叉引用，
#: 宽松匹配会把它们也当成收入分解表。
_REVENUE_NOTE_HEADING = re.compile(
    r"^[(（]?(?:\d+|[一二三四五六七八九十百]+)[)）、.．]?\s*"
    r"营业收入(?:和|、|及)营业成本(?:情况)?$"
)

#: 分解表的行名。必须**整行等于**这些词：同一页里还有 ``(3) 主营业务(按业务类型)``
#: 这类子表标题与 ``主营业务收入`` 这类矩阵行名，宽松匹配会取到别的表。
_REVENUE_NOTE_MAIN_LABEL = "主营业务"
_REVENUE_NOTE_OTHER_LABEL = "其他业务"
_REVENUE_NOTE_TOTAL_LABEL = "合计"
#: 行名的等价写法。附注的分项表有两种标题习惯：
#: ``主营业务`` / ``其他业务``（配"收入/成本"列头），以及
#: ``主营业务收入`` / ``其他业务收入``（实测 000333 附注 (46)，**行名单独成行**）；
#: 合计行也有 ``合计`` 与 ``小计`` 两种。只认第一种会让 000333 整条路径失配。
_REVENUE_NOTE_MAIN_LABELS = frozenset({_REVENUE_NOTE_MAIN_LABEL, "主营业务收入"})
_REVENUE_NOTE_OTHER_LABELS = frozenset({_REVENUE_NOTE_OTHER_LABEL, "其他业务收入"})
_REVENUE_NOTE_TOTAL_LABELS = frozenset({_REVENUE_NOTE_TOTAL_LABEL, "小计"})
#: **成本表**的行名。附注把收入表与成本表并排（实测 000333 年报 (48)/(49)：
#: 收入表在前、成本表紧随），而两张表的行名在归一化后是**同一个**
#: （``主营业务收入`` 与 ``主营业务成本`` 都被归一成 ``主营业务``）。
#: 不把成本表切开，扫描就会读到第二组同名行，报
#: ``分解行不是『主营业务→其他业务→合计』（实际 ['主营业务','其他业务','其他业务']）``。
_REVENUE_NOTE_COST_LABELS = frozenset({
    "主营业务成本", "其他业务成本", "营业成本",
})
#: 表头区兜底扫描的行数上限（见 ``_parse_revenue_note_decomposition`` 里的兜底）。
_REVENUE_NOTE_PREAMBLE_SCAN = 8

#: 分解表的列数：当期收入、当期成本、上期收入、上期成本。四列是实测的唯一版式
#: （002415/300750/688981/601012/000651）；列数不符即失败关闭，绝不按"前两个
#: 数字大概是收入与成本"去猜。
_REVENUE_NOTE_COLUMNS = 4

#: 附注分解表的当期列词与比较列词。列序本身就是证据：五份实测原文都写
#: ``本期发生额 上期发生额``（半年报为``上年同期发生额``）。若原文把比较列放在
#: 左边，取到的第一个金额就是上期数——那时"主营业务 + 其他业务 = 合计"仍然自洽，
#: 只是核对的是另一期，属于**看不出错的错误结论**，因此必须失败关闭。
_CURRENT_PERIOD_MARKERS = ("本期发生额", "本期金额", "本期数", "本年发生额", "本年金额")
_PRIOR_PERIOD_MARKERS = (
    "上期发生额", "上期金额", "上期数", "上年同期发生额", "上年同期金额", "上年同期数",
    "上年发生额", "上年金额",
)

#: 附注扫描窗口（行数）。实测 000651 半年报把标题印在 163 页末尾、数据行印在
#: 164 页，中间隔着页脚与重复表头；窗口太小会漏掉真实表格。
_REVENUE_NOTE_WINDOW = 40


class RevenueNoteNotLocatedError(CninfoFinancialPdfError):
    """合并附注收入分解无法唯一确定。

    失败关闭是刻意的：这条路径的全部价值在于"另一处原文独立证实"。定位不到
    时必须报告"未证实"，**不得**退回主利润表的数值——那正是待核对的候选本身。
    """


@dataclass(frozen=True, slots=True)
class RevenueNoteDecomposition:
    """合并附注『营业收入和营业成本情况』的当期收入分解与原文证据。"""

    pdf_sha256: str
    heading: str
    heading_page: int
    role: str
    unit: str
    #: 单位来源：``note`` 为附注自己印的 ``单位：``，``statement_section`` 为
    #: 同一份原文的合并报表单位（000651 整节不重印单位）。
    unit_source: str
    current_header: str
    prior_header: str
    main_business: StatementRow
    other_business: StatementRow
    total: StatementRow
    #: 原文行：标题、主营业务行、其他业务行、合计行。
    raw_lines: tuple[str, ...]

    @property
    def current_revenue_yuan(self) -> Decimal:
        """分解口径的当期营业收入（元）：主营业务 + 其他业务。"""

        return (self.main_business.amount + self.other_business.amount
                ) * _UNIT_MULTIPLIER[self.unit]

    def as_evidence(self) -> dict[str, object]:
        return {
            "note_section": {
                "role": self.role,
                "statement": REVENUE_NOTE_STATEMENT,
                "heading": self.heading,
                "heading_page": self.heading_page,
                "amount_unit": self.unit,
                "unit_source": self.unit_source,
                "current_column": self.current_header,
                "prior_column": self.prior_header,
                "pdf_sha256": self.pdf_sha256,
                "raw_lines": list(self.raw_lines),
            },
            "main_business": self.main_business.as_evidence(),
            "other_business": self.other_business.as_evidence(),
            "total": self.total.as_evidence(),
            "current_revenue_yuan": str(self.current_revenue_yuan),
        }


def _note_amount_row(line: str) -> tuple[str, tuple[tuple[int, str, Decimal], ...]] | None:
    """把附注表的一行解析为 ``(归一化行名, ((下标, 原文单元格, 值), ...))``。

    复用 ``_amounts_in`` 的金额词法（千分位、两位小数、括号负数、排除行号与
    附注编号），因此与主表抽取是同一套数字规则，只是行名规则不同。
    """

    text = line.strip()
    if not text:
        return None
    found = _amounts_in(text)
    if not found:
        return None
    label = _LEADING_NUMBER.sub("", _normalize_label(text[:found[0][0]]))
    if not label or not re.search(r"[\u4e00-\u9fff]", label):
        return None
    return label, tuple(found)


def _looks_like_note_heading(line: str) -> bool:
    """该行是否已经是**下一条附注**的标题（``56、 利息收入、利息支出``）。

    用它给扫描窗口收边：附注表格按"行名 + 金额"成行，只有换到下一条附注时才会
    出现"编号 + 中文行名 + 没有金额"的行。收入分解的子表标题（``(1)``/``(2)``）
    以括号开头，不会命中。
    """

    compact = _compact(line)
    if not compact or len(compact) > 30 or _amounts_in(line):
        return False
    return re.match(r"^\d+[.、．][\u4e00-\u9fff]", compact) is not None


def _consolidated_revenue_note_headings(
    lines: list[tuple[int, str]],
) -> list[tuple[int, str]]:
    """返回**合并附注区间内**的收入分解标题：``(行下标, 标题)``。

    角色由紧邻在前的附注节标题决定。母公司附注之前必然出现自己的节标题，因此
    母公司段里的同名表不会被当成合并数（实测 002415：合并段标题在 82 页，母公司
    段标题在 134 页，两张收入分解表分别在 109 与 139 页）。
    """

    found: list[tuple[int, str]] = []
    role = ""
    for index, (_, line) in enumerate(lines):
        compact = _compact(line)
        scope = _NOTE_SCOPE_HEADING.fullmatch(compact)
        if scope is not None:
            role = ROLE_PARENT if scope.group(1) == "公司" else scope.group(1)
            continue
        if role == ROLE_CONSOLIDATED and _REVENUE_NOTE_HEADING.fullmatch(compact):
            found.append((index, compact))
    return found


def _first_marker(text: str, markers: tuple[str, ...]) -> tuple[int, str] | None:
    found = [(text.find(marker), marker) for marker in markers]
    found = [item for item in found if item[0] >= 0]
    return min(found) if found else None


def _note_period_columns(
    preamble: list[str], *, heading: str, period_end: date,
) -> tuple[str, str]:
    """确认当期列在比较列之前，返回 ``(当期列词, 比较列词)``。

    找不到任何列词时失败关闭：无列词就无法区分"当期收入"与"上期收入"，而取错
    一期会得到一张自身完全自洽、只是核对了另一期的表。
    """

    text = "".join(_compact(line) for line in preamble)
    pairs = (
        (_CURRENT_PERIOD_MARKERS, _PRIOR_PERIOD_MARKERS),
        ((f"{period_end.year}年",), (f"{period_end.year - 1}年",)),
    )
    for current_markers, prior_markers in pairs:
        current = _first_marker(text, current_markers)
        prior = _first_marker(text, prior_markers)
        if current is None and prior is None:
            continue
        if current is None or (prior is not None and prior[0] < current[0]):
            raise RevenueNoteNotLocatedError(
                f"『{heading}』表头把比较列排在当期列之前（{text[:60]}），"
                "无法确定哪一列是当期数")
        return current[1], (prior[1] if prior is not None else "未列示")
    raise RevenueNoteNotLocatedError(
        f"『{heading}』表头没有『本期发生额/上期发生额』这类期间列词，"
        "无法确定哪一列是当期数")


def _require_note_column_titles(preamble: list[str], *, heading: str) -> None:
    """确认表头的列语义，使"第一个金额是当期收入"有原文依据。

    两种真实版式都要接受：

    * **四列**：表头成对列示 ``收入 成本 收入 成本``（实测五份原文都印有）；
    * **两列**：收入与成本**分成两张小表**，收入表表头只有期间列词
      （实测 000333 附注 (46)：``项 目 本期数 上年同期数`` 之后是
      ``主营业务收入`` / ``其他业务收入`` / ``小计``，全表没有 ``成本`` 字样）。
      这种版式下"第 0 列是当期收入"由两件事共同支撑：期间列词已由
      ``_note_period_columns`` 确认过顺序，且分解算术
      ``主营业务 + 其他业务 = 合计`` 必须成立——取错列时算术不会成立。

    两者都不满足时失败关闭：那时"第一个金额是收入"只是假设。
    """

    for line in preamble:
        compact = _compact(line)
        if compact.count("收入") >= 2 and compact.count("成本") >= 2:
            return
    text = "".join(_compact(line) for line in preamble)
    if _first_marker(text, _CURRENT_PERIOD_MARKERS) is not None:
        return
    raise RevenueNoteNotLocatedError(
        f"『{heading}』表头既没有成对的『收入 成本』列名，也没有期间列词，"
        "无法确认第一列是当期收入")


def _note_unit(
    preamble: list[str], *, heading: str, default_unit: str | None,
) -> tuple[str, str]:
    """附注表声明的金额单位及其来源。

    附注表通常自己印一行 ``单位：千元``（实测 002415/300750/688981/601012），
    以它为准；000651 这类整节不重印单位的报告回退到同一份原文的合并报表单位，
    并把来源记进证据。单位不是金额：无论取哪一个，两侧都会折算成元再比较，
    取错只会让核对失败，不会让它虚假通过。
    """

    units = {match.group(2) for line in preamble if (match := _UNIT.search(line))}
    if len(units) > 1:
        raise RevenueNoteNotLocatedError(
            f"『{heading}』表头出现多个金额单位：{'、'.join(sorted(units))}")
    if units:
        unit = units.pop()
        if unit not in _UNIT_MULTIPLIER:
            raise RevenueNoteNotLocatedError(f"『{heading}』使用了不支持的单位 {unit!r}")
        return unit, "note"
    if default_unit in _UNIT_MULTIPLIER:
        return default_unit, "statement_section"
    raise RevenueNoteNotLocatedError(
        f"『{heading}』表头没有『单位：』声明，同一份原文的合并报表单位也未知，"
        "无法把附注金额折算成元")


def _note_statement_row(
    *, label: str, cells: tuple[tuple[int, str, Decimal], ...], page: int,
    unit: str, raw_line: str,
) -> StatementRow:
    """把附注分解表的一行包装成 ``StatementRow``，复用同一套证据格式。

    列序是``当期收入 当期成本 上期收入 上期成本``：``amount`` 取当期收入列，
    ``prior_amount`` 取上期收入列；成本列只留在 ``raw_line`` 里供第三方复核，
    不参与核对（本路径核对的是收入分解）。
    """

    current = cells[0]
    prior = cells[2] if len(cells) > 2 else None
    return StatementRow(
        role=ROLE_CONSOLIDATED, statement=REVENUE_NOTE_STATEMENT, label=label,
        amount=current[2], prior_amount=(Decimal(0) if prior is None else prior[2]),
        page=page, unit=unit, current_cell=current[1],
        prior_cell=(PRIOR_NOT_ON_LINE if prior is None else prior[1]),
        raw_line=raw_line,
    )


def _note_label_only(line: str) -> str | None:
    """附注里**单独成行的行名**（该行没有金额）。

    实测 000333 2026 半年报附注 (46)：行名与金额分处相邻两行，且只有两列::

        主营业务收入
        241,006,725 231,739,452
        其他业务收入
        19,035,765 19,384,262
        小 计
        260,042,490 251,123,714

    只接受**很短的纯行名**（无数字、无金额、含汉字），避免把叙述性段落
    当成行名。
    """

    text = line.strip()
    if not text or len(text) > 12:
        return None
    if _amounts_in(text) or re.search(r"\d", text):
        return None
    if not re.search(r"[\u4e00-\u9fff]", text):
        return None
    return _LEADING_NUMBER.sub("", _normalize_label(text))


def _note_split_row(
    label_line: str, amounts_line: str,
) -> tuple[str, tuple[tuple[int, str, Decimal], ...]] | None:
    """把"行名一行、金额在下一行"的附注行拼成与 ``_note_amount_row`` 同形的结果。

    ``cells`` 的下标为 **0**：这是跨行拼出来的行，原文里没有单一列位置可引，
    下游只按"不同单元格位"判独立，0/1 已足够区分两列。
    """

    label = _note_label_only(label_line)
    if not label:
        return None
    text = amounts_line.strip()
    if not text:
        return None
    found = _amounts_in(text)
    if len(found) != 2:
        # 只接受**恰好两列**（实测 000333 的"本期数 / 上年同期数"）。
        # 列数不符即放弃，交给原有路径失败关闭，不猜列。
        return None
    if text[:found[0][0]].strip():
        return None
    return label, tuple(
        (position, token, value)
        for position, (_, token, value) in enumerate(found))


def _parse_revenue_note_decomposition(
    lines: list[tuple[int, str]], index: int, *, heading: str,
    period_end: date, default_unit: str | None, pdf_sha256: str,
) -> RevenueNoteDecomposition:
    """解析标题之后的那张分解表；任何歧义都抛 ``RevenueNoteNotLocatedError``。"""

    window = lines[index + 1:index + 1 + _REVENUE_NOTE_WINDOW]
    rows: list[tuple[str, tuple[tuple[int, str, Decimal], ...], int, str]] = []
    preamble: list[str] = []

    def _accept(label: str, cells: tuple[tuple[int, str, Decimal], ...],
                page: int, raw: str) -> None:
        if label in _REVENUE_NOTE_MAIN_LABELS:
            if rows:
                raise RevenueNoteNotLocatedError(
                    f"『{heading}』表在『合计』之前重复出现『主营业务』行，"
                    "无法确定唯一的收入分解")
            rows.append((_REVENUE_NOTE_MAIN_LABEL, cells, page, raw))
            return
        if label in _REVENUE_NOTE_OTHER_LABELS:
            if not rows:
                raise RevenueNoteNotLocatedError(
                    f"『{heading}』表的『其他业务』行出现在『主营业务』之前，"
                    "版式与本路径已知的列式不符")
            rows.append((_REVENUE_NOTE_OTHER_LABEL, cells, page, raw))
            return
        if label in _REVENUE_NOTE_TOTAL_LABELS and rows:
            rows.append((_REVENUE_NOTE_TOTAL_LABEL, cells, page, raw))
            return
        # 成本表是**硬边界**：两张表的行名归一化后同名（``主营业务收入`` 与
        # ``主营业务成本`` 都是 ``主营业务``），越过它就会读到第二组同名行。
        # 只有在已经找到收入行之后才据此截断，避免把表头区的词误当边界。
        if label in _REVENUE_NOTE_COST_LABELS and rows:
            rows.append(("__cost_boundary__", cells, page, raw))

    # 表头区：**只在还没出现数据行时**收集。曾经试过独立扫描（为了让"行名单独
    # 成行"的版式也能拿到期间列词），但那会让 ``_require_note_column_titles``
    # 的成对列名要求作用到"收入/成本分成两张小表"的版式上，进而**破坏既有用例**
    # （实测 3 个既有测试变红）。因此这里保持原行为；要支持两表版式，应当**单独
    # 放宽** ``_require_note_column_titles``，而不是改变表头区的收集时机。
    #
    # 行名可能**单独成行**、金额在紧邻的下一行（实测 000333 附注 (46)）。
    # 先做一次"行名行 + 金额行"的配对扫描；命中就按规范化后的行名入表。
    #
    # **两道扫描必须互斥**：配对扫描消费掉的原文行要记下来，后续直读扫描跳过它们。
    # 否则同一批行会被写进 ``rows`` 两次，表现为
    # ``分解行不是『主营业务→其他业务→合计』（实际 ['主营业务','其他业务','其他业务']）``
    # ——实测 000333 年报即因此失败。
    consumed: set[int] = set()
    split_rows: dict[int, tuple[str, tuple[tuple[int, str, Decimal], ...], int, str]] = {}
    for position, (page, line) in enumerate(window):
        if _looks_like_note_heading(line):
            break
        if _note_amount_row(line) is not None:
            continue
        if position + 1 >= len(window):
            break
        next_page, next_line = window[position + 1]
        split = _note_split_row(line, next_line)
        if split is None:
            continue
        # 预扫描只识别拆行配对，不改变原文顺序。原先在此 _accept 会把
        # 后面另一张表的拆行“其他业务”插到本表直读“主营业务”之前，
        # 实采 000589 2024H1 因此误拒了第137页完整的四列分解表。
        split_rows[position] = (split[0], split[1], page,
                                f"{line.strip()} {next_line.strip()}")
        consumed.add(position)
        consumed.add(position + 1)

    # **没有行名的合计行**（实测 000333 年报 (48)/(49)）：
    #   ``主营业务收入 374,028,606 344,132,712``
    #   ``其他业务收入  33,120,994  27,904,568``
    #   ``407,149,600 372,037,280``          ← 只有两个金额，**没有任何行名**
    # 靠行名（``合计``/``小计``）永远找不到它。这里按**位置**识别，并用两道约束
    # 把误判挡住：必须**紧跟在"其他业务"行之后**，且**当期列等于前两行之和**。
    # 求和校验是这条路径可复核的关键——位置本身只是版式线索。
    if len(rows) == 2 and rows[0][0] == _REVENUE_NOTE_MAIN_LABEL \
            and rows[1][0] == _REVENUE_NOTE_OTHER_LABEL:
        last_position = max(consumed) if consumed else -1
        if last_position < 0:
            # 走的是直读扫描：找到"其他业务"所在行的下标。
            for position, (_, line) in enumerate(window):
                parsed = _note_amount_row(line)
                if parsed is not None and parsed[0] in _REVENUE_NOTE_OTHER_LABELS:
                    last_position = position
                    break
        if last_position >= 0:
            for offset in (1, 2, 3):
                position = last_position + offset
                if position >= len(window):
                    break
                page, line = window[position]
                text = line.strip()
                if not text or re.search(r"[\u4e00-\u9fff]", text):
                    # 出现汉字说明已经是下一张表/下一行行名，停止寻找。
                    break
                found = _amounts_in(text)
                if len(found) != 2:
                    continue
                if text[:found[0][0]].strip():
                    continue
                main_value = rows[0][1][0][2]
                other_value = rows[1][1][0][2]
                if found[0][2] == main_value + other_value:
                    rows.append((_REVENUE_NOTE_TOTAL_LABEL, tuple(
                        (index, token, value)
                        for index, (_, token, value) in enumerate(found)),
                        page, text))
                break

    for position, (page, line) in enumerate(window):
        if position in consumed:
            split_row = split_rows.get(position)
            if split_row is not None:
                _accept(*split_row)
            continue
        parsed = _note_amount_row(line)
        if parsed is None:
            # 下一条附注的标题是硬边界：越过它就可能把下一条附注里的同名行
            # （实测 000651 ``56、 利息收入、利息支出`` 小节同样有金额行）
            # 接到本表上，拼出一张原文里并不存在的分解表。
            if _looks_like_note_heading(line):
                break
            # 无标签合计仍须紧随已定位的两项，且当期精确等于两项之和；
            # 拆行记录现在按原文顺序加入，故在当前位置核对这个合计。
            if (len(rows) == 2 and
                    [row[0] for row in rows] == [_REVENUE_NOTE_MAIN_LABEL, _REVENUE_NOTE_OTHER_LABEL] and
                    not re.search(r"[\u4e00-\u9fff]", line)):
                found = _amounts_in(line)
                if (found and not line[:found[0][0]].strip() and
                        found[0][2] == rows[0][1][0][2] + rows[1][1][0][2]):
                    rows.append((_REVENUE_NOTE_TOTAL_LABEL, tuple(found), page, line.strip()))
                    break
            if not rows:
                preamble.append(line)
            continue
        _accept(parsed[0], parsed[1], page, line.strip())
        # 撞到成本表就停止本表扫描（收入表已完整）；边界标记本身不入表。
        if rows and rows[-1][0] == "__cost_boundary__":
            rows.pop()
            # 成本表起点之前可能有一行**没有行名的合计行**（实测 000333 年报
            # (48)/(49)：``407,149,600 372,037,28 0``，零个汉字、且比较列被
            # 换行切碎，词法只认得出当期那一个金额）。它不会被
            # ``_note_amount_row`` 收下（没有行名），因此靠**回看**补上。
            #
            # 判定用"位置 + 求和校验"两道约束；求和校验是这条路径可复核的依据。
            #
            # **注意**：收入两行可能是**配对扫描**收下的，此时它们已被
            # ``consumed`` 标记，本循环根本看不到它们——所以回看必须以
            # ``rows`` 的当前组成为准（两个收入标签），**不能**再要求
            # "本循环刚收下这两行"。早期把它写在"本循环收到成本边界"的
            # 分支里，于是配对扫描路径永远补不上合计行（实测 000333 年报
            # 两期即因此一直报"没有『合计』行"）。
            if (len(rows) == 2 and rows[0][0] == _REVENUE_NOTE_MAIN_LABEL
                    and rows[1][0] == _REVENUE_NOTE_OTHER_LABEL):
                for offset in (1, 2, 3):
                    back = position - offset
                    if back < 0:
                        break
                    back_page, back_line = window[back]
                    text = back_line.strip()
                    if not text or re.search(r"[\u4e00-\u9fff]", text):
                        break
                    found = _amounts_in(text)
                    if not found or text[:found[0][0]].strip():
                        continue
                    if found[0][2] == rows[0][1][0][2] + rows[1][1][0][2]:
                        rows.append((_REVENUE_NOTE_TOTAL_LABEL, tuple(
                            (index, token, value)
                            for index, (_, token, value) in enumerate(found)),
                            back_page, text))
                    break
            break
        if rows and rows[-1][0] == _REVENUE_NOTE_TOTAL_LABEL:
            break
    labels = [row[0] for row in rows]
    if labels[:1] != [_REVENUE_NOTE_MAIN_LABEL]:
        raise RevenueNoteNotLocatedError(
            f"『{heading}』之后 {_REVENUE_NOTE_WINDOW} 行内没有『主营业务』分解行")
    if labels[:2] != [_REVENUE_NOTE_MAIN_LABEL, _REVENUE_NOTE_OTHER_LABEL]:
        raise RevenueNoteNotLocatedError(
            f"『{heading}』表在『主营业务』之后没有单列『其他业务』行（实测 601012 "
            "2024 年报即此版式：只有主营业务与合计两行，合计与主营业务相等）；"
            "缺少一列就无法建立『主营业务 + 其他业务 = 合计』，本路径失败关闭")
    if len(rows) < 3:
        raise RevenueNoteNotLocatedError(
            f"『{heading}』表没有在『其他业务』之后给出『合计』行，"
            "分解明细无法与合计交叉核对")
    if labels[:3] != [_REVENUE_NOTE_MAIN_LABEL, _REVENUE_NOTE_OTHER_LABEL,
                      _REVENUE_NOTE_TOTAL_LABEL]:
        raise RevenueNoteNotLocatedError(
            f"『{heading}』表的分解行不是『主营业务→其他业务→合计』"
            f"（实际 {labels[:3]}），无法确定唯一的分解")
    # 两种真实列数：**四列**（当期收入/当期成本/上期收入/上期成本）与
    # **两列**（本期数/上年同期数，实测 000333 附注 (46)：收入与成本**分成
    # 两张小表**，收入表只有两列）。两种情况下收入都在**第 0 列**，
    # 因此下游取 ``cells[0]`` 是安全的；其它列数一律失败关闭，不猜哪列是收入。
    for label, cells, _, _ in rows[:2]:
        if len(cells) not in (2, _REVENUE_NOTE_COLUMNS):
            raise RevenueNoteNotLocatedError(
                f"『{heading}』表的『{label}』行有 {len(cells)} 个金额，既不是四列"
                "（当期收入/当期成本/上期收入/上期成本）也不是两列（本期/上期）；"
                "列数不符时不猜哪列是收入")
    main, other, total = (row[1] for row in rows[:3])
    # 合计行**只要求当期列**。早期还要求"至少两个金额（当期+比较）"，但比较列
    # 在原文里可能被换行切碎而根本无法解析（实测 000333 年报印成
    # ``407,149,600 372,037,28 0``，``_amounts_in`` 只认得出当期那一个）。
    # 要求比较列存在，等于让**比较列的版式问题**否掉当期列的核对。
    # 恒等式只用当期列，且下面的求和校验会验证它，因此这里不设列数门槛。
    if not total:
        raise RevenueNoteNotLocatedError(
            f"『{heading}』表的『合计』行没有可用的金额")
    computed = main[0][2] + other[0][2]
    if computed != total[0][2]:
        raise RevenueNoteNotLocatedError(
            f"『{heading}』表自身不成立：主营业务 {main[0][1]} + 其他业务 "
            f"{other[0][1]} = {computed}，而合计列示 {total[0][1]}")
    # 表头区**兜底**：多数版式的前几行就是表头，``preamble`` 已经够用。
    # 但"行名单独成行"的版式（实测 000333 半年报 (46)）会让数据行很早出现，
    # 常规收集随即停止，期间列词整段落在后面找不到。
    # 这里**只在常规表头区取不到期间列词时**才向前多找几行，属于纯兜底：
    # 既有版式仍走原路径，因此不会改变它们的判定（第 32 轮曾把"表头区收集时机"
    # 整体改掉，结果 3 个既有测试变红，那次教训就是这条兜底要**有条件**触发）。
    effective_preamble = preamble
    joined = "".join(_compact(line) for line in preamble)
    if _first_marker(joined, _CURRENT_PERIOD_MARKERS) is None:
        extended: list[str] = []
        for page, line in window[:_REVENUE_NOTE_PREAMBLE_SCAN]:
            if _note_amount_row(line) is not None and extended:
                break
            extended.append(line)
        candidates = preamble + extended
        extended_joined = "".join(_compact(line) for line in candidates)
        if _first_marker(extended_joined, _CURRENT_PERIOD_MARKERS) is not None:
            effective_preamble = candidates
    current_header, prior_header = _note_period_columns(
        effective_preamble, heading=heading, period_end=period_end)
    _require_note_column_titles(effective_preamble, heading=heading)
    unit, unit_source = _note_unit(effective_preamble, heading=heading,
                                   default_unit=default_unit)
    return RevenueNoteDecomposition(
        pdf_sha256=pdf_sha256, heading=heading, heading_page=lines[index][0],
        role=ROLE_CONSOLIDATED, unit=unit, unit_source=unit_source,
        current_header=current_header, prior_header=prior_header,
        main_business=_note_statement_row(
            label=_REVENUE_NOTE_MAIN_LABEL, cells=main, page=rows[0][2],
            unit=unit, raw_line=rows[0][3]),
        other_business=_note_statement_row(
            label=_REVENUE_NOTE_OTHER_LABEL, cells=other, page=rows[1][2],
            unit=unit, raw_line=rows[1][3]),
        total=_note_statement_row(
            label=_REVENUE_NOTE_TOTAL_LABEL, cells=total, page=rows[2][2],
            unit=unit, raw_line=rows[2][3]),
        raw_lines=(heading, rows[0][3], rows[1][3], rows[2][3]),
    )


def _select_revenue_note_decomposition(
    lines: list[tuple[int, str]], *, pdf_sha256: str, period_end: date,
    default_unit: str | None,
) -> RevenueNoteDecomposition:
    """从全部原文行里选出**唯一的**合并附注收入分解；0 张或 2 张都失败关闭。"""

    headings = _consolidated_revenue_note_headings(lines)
    if not headings:
        raise RevenueNoteNotLocatedError(
            "未在合并附注区间定位到『营业收入和营业成本』标题：没有合并附注节标题"
            "时无法与母公司附注区分（实测 002415 两张分解表分别在 109 与 139 页），"
            "因此不做任何猜测，也不回退到利润表数值")
    found: dict[tuple[object, ...], RevenueNoteDecomposition] = {}
    problems: list[str] = []
    for index, heading in headings:
        try:
            item = _parse_revenue_note_decomposition(
                lines, index, heading=heading, period_end=period_end,
                default_unit=default_unit, pdf_sha256=pdf_sha256)
        except RevenueNoteNotLocatedError as exc:
            problems.append(str(exc))
            continue
        # 同一条附注可以有**两个**标题行：附注本身的 ``46、 营业收入和营业成本``
        # 与紧随其后的子表 ``(1) 营业收入和营业成本情况``。它们解析出同一张表
        # （同一页、同样的三个单元格），因此按表格内容去重；保留先出现的那条
        # 附注标题作为证据，读起来才是"哪一条附注"。
        found.setdefault(
            (item.main_business.page, item.main_business.current_cell,
             item.other_business.current_cell, item.total.current_cell), item)
    if len(found) == 1:
        return next(iter(found.values()))
    if not found:
        raise RevenueNoteNotLocatedError("；".join(problems[:3]))
    raise RevenueNoteNotLocatedError(
        f"合并附注区间内定位到 {len(found)} 张数值不同的收入分解表，无法确定唯一来源")


def extract_revenue_note_decomposition(
    pdf_bytes: bytes, *, period_end: date, default_unit: str | None = None,
) -> RevenueNoteDecomposition:
    """取**合并**附注『营业收入和营业成本情况』的当期收入分解。

    存在的理由：主利润表在 601012 2024 年报、300750 2025 年报、688981 2026 半年报
    里都自相矛盾（``营业总收入`` 与``其中：营业收入``完全相等，却另有金融收入
    扣减项），002415 2024 半年报干脆没有``营业总收入``行；收入恒等式因此无法
    成立。合并附注的收入分解表是原文里另一张表、另一个页面，可以独立证实
    ``主营业务 + 其他业务 = 合计 = 营业收入``。

    失败关闭的边界（每一条都对应实测原文）：

    * 合并附注节标题缺失、或区间内出现两张数值不同的分解表 → 不猜；
    * 『其他业务』行缺失（601012 2024 年报）→ 不把缺失当 0；
    * 行名不是整行等于『主营业务』/『其他业务』/『合计』→ 不用相似行顶替；
    * 金额列不是四列、表头没有成对的『收入 成本』列名、期间列序不是当期在前
      → 不猜列；
    * 表自身不成立（主营业务 + 其他业务 ≠ 合计）→ 不采用；
    * 定位不到时抛 ``RevenueNoteNotLocatedError``，**绝不**返回利润表数值。

    ``default_unit`` 是同一份原文合并报表的单位，仅在附注表头没有``单位：``时
    兜底（实测 000651 整节不重印单位）；来源记入证据的 ``unit_source``。
    """

    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise CninfoFinancialPdfError("input must be PDF bytes")
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - environment guard
        raise CninfoFinancialPdfError(
            "pdfplumber is required for PDF candidate extraction") from exc
    digest = "sha256:" + hashlib.sha256(pdf_bytes).hexdigest()
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            lines = [(index + 1, line)
                     for index, page in enumerate(pdf.pages)
                     for line in (page.extract_text() or "").splitlines()]
    except CninfoFinancialPdfError:
        raise
    except Exception as exc:  # noqa: BLE001 - unknown parser behaviour cannot be promoted
        raise CninfoFinancialPdfError(
            f"revenue note extraction failed: {type(exc).__name__}") from exc
    return _select_revenue_note_decomposition(
        lines, pdf_sha256=digest, period_end=period_end, default_unit=default_unit)


__all__ = [
    "FIELD_STATEMENT",
    "PARENT_MATCHING_FIELDS",
    "REVENUE_NOTE_STATEMENT",
    "ROLE_CONSOLIDATED",
    "ROLE_PARENT",
    "STATEMENTS",
    "RevenueNoteDecomposition",
    "RevenueNoteNotLocatedError",
    "StatementRow",
    "StatementSection",
    "StatementSections",
    "check_report_cover",
    "extract_revenue_note_decomposition",
    "extract_section_facts",
    "extract_statement_sections",
    "period_report_names",
]
