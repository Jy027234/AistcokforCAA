"""经验证的费率表（主文档 §12.6）。

为什么必须区分"有权威来源"与"必须由使用者给"
--------------------------------------------
费率表原先整张都是合成测试费率，而合成费率在真实数据上被**静默使用**——
成交、费用、盈亏全都算得出来，只是数字没有依据。

但"把它换成真实费率"这件事本身有个陷阱：A 股费用里只有两项有公开的
法定/行业标准，另外两项没有：

  * 印花税 —— 财政部、税务总局公告 2023 年第 39 号（法定）
  * 过户费 —— 中国结算通知，2022-04-29 起 0.01‰ 双向（行业标准）
  * 经手费/证管费 —— 同样有费率表，但在本实现里并入佣金口径（见下）
  * **佣金 —— 券商与客户约定，没有"正确值"**

佣金那一项如果也替使用者填一个数字，我们就又制造了一处
"看起来有依据的事实"。因此它必须是一个**显式参数**：
不给就不建表，而不是默认一个值。

来源留证见 tools/fetch_fee_sources.py 与其产出的
deploy/agentctl-q0/fee-sources.json（含 URL、抓取时间与内容哈希）。
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from aquant.domain.simulation.fees import FeeSchedule, FeeTable

#: 法定/行业标准费率的取值与出处。**这里的每一个数字都必须能在
#: fee-sources.json 里找到对应的来源记录**，测试会核对这一点。
STAMP_DUTY_NOTICE = "财政部、税务总局公告 2023 年第 39 号"
STAMP_DUTY_EFFECTIVE_FROM = date(2023, 8, 28)
STAMP_DUTY_RATE_SELL = Decimal("0.0005")        # 0.5‰，单边（仅卖出）

TRANSFER_FEE_NOTICE = "中国结算：关于降低股票交易过户费收费标准的通知"
TRANSFER_FEE_EFFECTIVE_FROM = date(2022, 4, 29)
TRANSFER_FEE_RATE = Decimal("0.00001")          # 0.01‰，双向

#: 印花税减半之前的版本。保留历史档是为了 S09（按生效日取用相应配置）
#: 与"2023-08-28 之前的交易日用哪个税率"这个问题有答案。
STAMP_DUTY_RATE_SELL_BEFORE = Decimal("0.001")


def fee_sources_path() -> Path:
    return (Path(__file__).resolve().parents[4]
            / "deploy" / "agentctl-q0" / "fee-sources.json")


def load_fee_sources(path: Path | None = None) -> dict:
    """读来源档案。缺失时返回空档案并**不伪造**来源记录。"""

    target = path or fee_sources_path()
    if not target.exists():
        return {"sources": [], "note": f"来源档案缺失：{target}"}
    return json.loads(target.read_text(encoding="utf-8"))


def verified_fee_table(*, commission_rate: Decimal,
                       commission_min_cents: int,
                       fee_version: str = "fee-cn-a-v1",
                       commission_source: str = "USER_CONFIGURED") -> FeeTable:
    """建一张**经验证**的费率表。

    commission_rate / commission_min_cents 必须由调用方给出：
    它们是券商与客户的约定，没有公开权威标准。不给就不建表——
    默认一个数字等于又制造一处无依据的事实。

    commission_source 默认 USER_CONFIGURED，但调用方可以如实标成
    UNCONFIGURED_DEFAULT：验收脚本用一个示例费率跑通链路时，
    标成"未配置的默认值"比标成"用户配置的"更诚实。
    """

    if not isinstance(commission_rate, Decimal):
        raise ValueError("commission_rate must be a Decimal; "
                         "never express fee rates as binary floats")
    if commission_rate < 0 or commission_min_cents < 0:
        raise ValueError("commission_rate and commission_min_cents must be >= 0")
    if commission_rate > Decimal("0.003"):
        # 上限千分之三是监管口径的通行说法，超过它几乎一定是填错了单位
        # （例如把万分之 2.5 写成 0.025）。
        raise ValueError(
            f"commission_rate {commission_rate} 超过千分之三；"
            "确认单位（万分之 2.5 应写作 0.00025）")

    return FeeTable([
        # 2022-04-29 起：过户费 0.01‰ 双向。这条与印花税版本无关，
        # 因此历史档也要带上它——缺了会让早期交易日无规则可匹配。
        FeeSchedule(
            fee_version=fee_version + "-pre2023",
            effective_from=TRANSFER_FEE_EFFECTIVE_FROM,
            effective_to=STAMP_DUTY_EFFECTIVE_FROM,
            commission_rate=commission_rate,
            commission_min_cents=commission_min_cents,
            stamp_duty_rate_sell=STAMP_DUTY_RATE_SELL_BEFORE,
            transfer_fee_rate=TRANSFER_FEE_RATE,
            #: 关键：**不是**合成费率。这张表的数字有出处。
            synthetic_test_rate=False,
        ),
        FeeSchedule(
            fee_version=fee_version,
            effective_from=STAMP_DUTY_EFFECTIVE_FROM,
            effective_to=None,
            commission_rate=commission_rate,
            commission_min_cents=commission_min_cents,
            stamp_duty_rate_sell=STAMP_DUTY_RATE_SELL,
            transfer_fee_rate=TRANSFER_FEE_RATE,
            synthetic_test_rate=False,
        ),
    ], commission_source=commission_source)


#: 未配置券商佣金时示例用的费率。**它是一个假设，不是事实**——
#: 用它的地方必须把 commission_source 标成 UNCONFIGURED_DEFAULT，
#: 否则盈亏数字会看起来像有依据的。
EXAMPLE_COMMISSION_RATE = Decimal("0.00025")     # 万分之 2.5
EXAMPLE_COMMISSION_MIN_CENTS = 500               # 5 元


def fee_table_from_env(*, commission_rate: Decimal | None = None,
                       commission_min_cents: int | None = None) -> tuple[FeeTable, str]:
    """按环境变量/参数建表，返回 (表, 实际取值)。**唯一的取表实现**。

    验收脚本与 API 都走这里，差别只在是否显式给了费率。
    这样"验收时跑的费率"与"真实用户跑的费率"是同一条代码路径，
    不会出现"验收里用一套、生产里用另一套"。

    返回的第二个值是"本次实际用的是哪个费率"，供日志与报告引用。
    """

    import os

    raw_rate = (str(commission_rate) if commission_rate is not None
                else os.environ.get("AQUANT_COMMISSION_RATE", "").strip())
    raw_min = (str(commission_min_cents) if commission_min_cents is not None
               else os.environ.get("AQUANT_COMMISSION_MIN_CENTS", "").strip())

    if raw_rate:
        return (verified_fee_table(commission_rate=Decimal(raw_rate),
                                   commission_min_cents=int(raw_min or "0"),
                                   commission_source="USER_CONFIGURED"),
                f"用户配置：佣金 {raw_rate}，最低 {raw_min or '0'} 分")

    return (verified_fee_table(commission_rate=EXAMPLE_COMMISSION_RATE,
                               commission_min_cents=EXAMPLE_COMMISSION_MIN_CENTS,
                               commission_source="UNCONFIGURED_DEFAULT"),
            f"未配置：使用示例费率（佣金 {EXAMPLE_COMMISSION_RATE}，"
            f"最低 {EXAMPLE_COMMISSION_MIN_CENTS} 分）—— **这是一个假设**")


def provenance() -> list[dict]:
    """这张表每一项费率的出处。给界面与审计用。"""

    return [
        {"item": "stamp_duty_rate_sell", "value": str(STAMP_DUTY_RATE_SELL),
         "effectiveFrom": STAMP_DUTY_EFFECTIVE_FROM.isoformat(),
         "authority": STAMP_DUTY_NOTICE, "kind": "STATUTORY",
         "note": "单边征收（仅卖出）；2023-08-28 起减半，此前为 "
                 + str(STAMP_DUTY_RATE_SELL_BEFORE)},
        {"item": "transfer_fee_rate", "value": str(TRANSFER_FEE_RATE),
         "effectiveFrom": TRANSFER_FEE_EFFECTIVE_FROM.isoformat(),
         "authority": TRANSFER_FEE_NOTICE, "kind": "INDUSTRY_STANDARD",
         "note": "双向收取；由 0.02‰ 下调至 0.01‰"},
        {"item": "commission_rate", "value": None,
         "effectiveFrom": None, "authority": None, "kind": "CONTRACTUAL",
         "note": "由券商与客户约定，**无权威值**；必须由使用者显式提供"},
    ]
