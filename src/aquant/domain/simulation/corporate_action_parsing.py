"""公司行为公告解析：送转、配股、退市（§12.7）。

与现金分红解析的关系
------------------
现金分红解析在 cninfo.py 里，已经有真实公告验证。这里补另外三类，
它们与分红共用同一份公告文本，但**不能共用同一套处理**：

  * 送转 —— 必须提取比例。它改变股数与成本基准。
  * 配股 —— 提取关键参数，但**标记为未支持**：
    §12.7 要求"配股、复杂合并换股、分拆或无法正确核验的公司行为出现时，
    停止产生新的有效绩效结论并列为待处理"。
    提取参数是为了让人能处理，不是为了自动近似。
  * 退市 —— 记录事件。§12.7 要求保留历史与退出事件，
    不得在组合中永远以最后收盘价假装可变现。

设计原则：**能提取的都提取，能不能算由 supported 决定。**
把"提取"与"处理"分开，是因为提取是可核验的事实，
而处理需要模型与规则——两者混淆会导致"解析成功"被误当成"已正确处理"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

#: 公告标题特征。只认实施公告，方案类需股东会审议。
BONUS_TITLE_PATTERN = re.compile(r"(权益分派实施|分红派息实施)")
RIGHTS_TITLE_PATTERN = re.compile(r"配股(发行)?(实施)?公告|配股说明书")
DELIST_TITLE_PATTERN = re.compile(r"(终止上市|退市整理|摘牌)")

#: A 股实务里的换算：每 10 股为披露单位。
SHARES_PER_DISCLOSURE = Decimal(10)


@dataclass(frozen=True, slots=True)
class ParsedAction:
    """解析出的公司行为。**supported 表达的是"能否正确处理"，不是"是否解析成功"。**"""

    action_id: str
    instrument_id: str
    action_type: str
    supported: bool
    record_date: date | None
    ex_date: date | None
    pay_date: date | None
    #: 每股送转比例。每 10 股送转 5 股 -> 0.5
    bonus_ratio: Decimal | None
    rights_price_cents: int | None
    reason: str
    evidence: dict[str, str]

    def as_dict(self) -> dict:
        return {
            "action_id": self.action_id,
            "instrument_id": self.instrument_id,
            "action_type": self.action_type,
            "supported": self.supported,
            "record_date": self.record_date.isoformat() if self.record_date else None,
            "ex_date": self.ex_date.isoformat() if self.ex_date else None,
            "pay_date": self.pay_date.isoformat() if self.pay_date else None,
            "bonus_ratio": str(self.bonus_ratio) if self.bonus_ratio is not None else None,
            "rights_price_cents": self.rights_price_cents,
            "reason": self.reason,
            "evidence": self.evidence,
        }


def _flat(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _context(flat: str, start: int, end: int, *, span: int = 60) -> str:
    return flat[max(0, start - span):end + span]


def _cn_number(text: str) -> Decimal | None:
    """解析公告里的数字（阿拉伯或简单中文）。"""

    text = text.strip()
    table = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if re.fullmatch(r"[0-9]+(\.[0-9]+)?", text):
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    if text in table:
        return Decimal(table[text])
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = table.get(head, 1) if head else 1
        ones = table.get(tail, 0) if tail else 0
        if (head and head not in table) or (tail and tail not in table):
            return None
        return Decimal(tens * 10 + ones)
    return None


def _dates(text: str) -> list[date]:
    out: list[date] = []
    for m in re.finditer(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?", text):
        try:
            out.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            continue
    return out


def _first_date_after(flat: str, labels: list[str], *,
                      window: int = 40) -> tuple[date, str] | None:
    """取标签之后**紧邻**的日期（与分红解析同一规则）。

    不能用"标签 + 任意字符 + 第一个日期"：跨度会跨过后续标签，
    把别的字段的日期算进来。
    """

    best_end: int | None = None
    for label in labels:
        m = re.search(re.escape(label), flat)
        if m is None:
            continue
        if best_end is None or m.end() < best_end:
            best_end = m.end()
    if best_end is None:
        return None
    for size in (window, window * 3):
        tail = flat[best_end:best_end + size]
        found = _dates(tail)
        if found:
            return found[0], tail[:40]
    return None


def parse_bonus_and_transfer(*, text: str, title: str, instrument_id: str,
                             announcement_id: str) -> ParsedAction | None:
    """解析送转。返回 None 表示这份公告与送转无关。"""

    if not BONUS_TITLE_PATTERN.search(title or ""):
        return None
    flat = _flat(text)
    evidence: dict[str, str] = {}

    # 「每 10 股送 3 股转增 2 股」：送股与转增要相加，它们对股数的影响相同。
    # 分开写容易只取其中一个，股数少算一半。
    bonus = Decimal(0)
    matched = False
    # "派"是允许的前缀："每 10 股派送 5 股"就是送 5 股。
    # 不允许的前缀是"转"：否则"转增5股"会被送股模式再算一次。
    # 每一条匹配只算一次：用 (start, end) 去重。
    #
    # 我第一版把"派送"也列为一个模式，而"送…股"能匹配到"派送5股"里的
    # "送"，于是同一段文字被算了两遍（0.5 变成 1.0）。这种错误不会报错，
    # 只会让股数翻倍——而股数翻倍看起来完全正常。
    seen: set[tuple[int, int]] = set()
    for pattern in (r"送(?:红)?股?([0-9一二三四五六七八九十]+(?:\.[0-9]+)?)股",
                    r"转增([0-9一二三四五六七八九十]+(?:\.[0-9]+)?)股"):
        for m in re.finditer(pattern, flat):
            if any(m.start() >= s and m.end() <= e for s, e in seen):
                continue
            # "转增N股"不得被"送…股"再算一次
            if flat[m.start() - 1:m.start()] == "转":
                continue
            value = _cn_number(m.group(1))
            if value is None:
                continue
            # 只在"每 10 股"语境下接受，否则可能把"共送 5 亿股"当成比例
            head = flat[max(0, m.start() - 14):m.start()]
            if "每" not in head and "10股" not in head and "十股" not in head:
                continue
            bonus += value
            matched = True
            seen.add((m.start(), m.end()))
            evidence["bonus"] = _context(flat, m.start(), m.end())
    if not matched:
        return None

    ratio = (bonus / SHARES_PER_DISCLOSURE).quantize(Decimal("0.000001"))
    record = _first_date_after(flat, ["股权登记日", "登记日"])
    ex = _first_date_after(flat, ["除权除息日", "除权（息）日", "除息日"])
    if record:
        evidence["record_date"] = record[1]
    if ex:
        evidence["ex_date"] = ex[1]

    return ParsedAction(
        action_id="ca-" + announcement_id, instrument_id=instrument_id,
        action_type="BONUS_SHARE",
        # 送转改变股数与成本基准。**暂标记为未支持**：股数调整与成本
        # 基准调整尚未实现，按 §12.7 必须列为待处理而不是近似处理。
        supported=False,
        record_date=record[0] if record else None,
        ex_date=ex[0] if ex else None,
        pay_date=None,
        bonus_ratio=ratio,
        rights_price_cents=None,
        reason=("送转比例已解析，但股数调整与成本基准调整尚未实现；"
                "按 §12.7 列为待处理，不近似处理"),
        evidence=evidence,
    )


def parse_rights_issue(*, text: str, title: str, instrument_id: str,
                       announcement_id: str) -> ParsedAction | None:
    """解析配股。**一律标记未支持**——§12.7 明确要求列为待处理。"""

    if not RIGHTS_TITLE_PATTERN.search(title or ""):
        return None
    flat = _flat(text)
    evidence: dict[str, str] = {}

    price_cents: int | None = None
    m = re.search(r"配股价格(?:为|：|:)?(?:人民币)?([0-9]+(?:\.[0-9]+)?)元", flat)
    if m:
        price_cents = int((Decimal(m.group(1)) * 100).to_integral_value())
        evidence["rights_price"] = _context(flat, m.start(), m.end())

    record = _first_date_after(flat, ["股权登记日", "登记日"])
    if record:
        evidence["record_date"] = record[1]

    return ParsedAction(
        action_id="ca-" + announcement_id, instrument_id=instrument_id,
        action_type="RIGHTS_ISSUE",
        supported=False,
        record_date=record[0] if record else None,
        ex_date=None, pay_date=None, bonus_ratio=None,
        rights_price_cents=price_cents,
        reason=("配股需要认购决策与资金安排，无法自动核验；"
                "按 §12.7 停止产生新的绩效结论并列为待处理"),
        evidence=evidence,
    )


def parse_delisting(*, text: str, title: str, instrument_id: str,
                    announcement_id: str) -> ParsedAction | None:
    """解析退市。记录事件，**不改动历史**。"""

    if not DELIST_TITLE_PATTERN.search(title or ""):
        return None
    flat = _flat(text)
    evidence: dict[str, str] = {}
    dates = _dates(flat)
    last_day = dates[0] if dates else None
    if last_day:
        evidence["first_date"] = flat[:60]

    return ParsedAction(
        action_id="ca-" + announcement_id, instrument_id=instrument_id,
        action_type="DELISTING",
        # 退市事件可以记录，但"终值如何取"需要一个有依据的选择，
        # 不能默认按最后收盘价当可变现。
        supported=False,
        record_date=last_day, ex_date=None, pay_date=None,
        bonus_ratio=None, rights_price_cents=None,
        reason=("退市已记录；终值口径需要一个有依据的选择，"
                "不得按最后收盘价当作可清算（§12.7）"),
        evidence=evidence,
    )


def parse_corporate_action(*, text: str, title: str, instrument_id: str,
                           announcement_id: str) -> ParsedAction | None:
    """按标题分派到具体解析器。都不匹配时返回 None。"""

    for parser in (parse_rights_issue, parse_delisting, parse_bonus_and_transfer):
        result = parser(text=text, title=title, instrument_id=instrument_id,
                        announcement_id=announcement_id)
        if result is not None:
            return result
    return None
