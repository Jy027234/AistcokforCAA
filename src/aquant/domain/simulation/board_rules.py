"""按 交易所+板块 匹配的涨跌幅/最小交易单位规则（主文档 §12.2、§7.1）。

为什么这份规则必须只有一份
--------------------------
规则原先写死在 `apps/api/main.py` 里，只有 API 进程用得到。但快照里还有一个
派生事实需要它——`daily_quotes[].board_limit_up`（当日**开盘**是否即涨停）。
快照由摄取层写出，摄取层拿不到 API 的常量表，于是这个字段在真实快照里
从来没人计算，读取器只能取默认值 False。

后果不是"少一个字段"，而是**模拟器里 S02 那条守卫在真实数据上从未生效**：
开盘一字涨停的股票照常被假定买入成交。合成夹具因为 YAML 里手写了
board_limit_up: true，反而测得出这个行为——测试全绿，真实路径失效。

因此规则表移到域层，由 API、摄取层共用同一份；谁再想要第二份，
就得先解释为什么两份可以不一致。

版本化（§7.1）
--------------
**必须保留历史版本**：主规格记录 2026-04-24 上交所交易规则修订、自
2026-07-06 起实施，因此该日期之前需要另一条覆盖区间。只写"现行版本"
会让 2026-07-06 之前的交易日无规则可匹配，预览被 RULE_VERSION_MISSING
拒绝——拒绝本身是对的（规则不能猜），缺的是历史版本。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from aquant.domain.simulation.simulator import BoardRule

#: 真实市场的板块规则。测试用的合成板块规则请各自显式构造，不要复用这一份——
#: 合成数据改一条规则就跟着改真实规则，是本项目已经踩过的漂移方式。
BOARD_RULES: list[BoardRule] = [
    BoardRule(exchange="SSE", board="MAIN", price_limit_pct=Decimal("10"),
              lot_size=100, effective_from=date(2020, 1, 1),
              effective_to=date(2026, 7, 6)),
    BoardRule(exchange="SZSE", board="MAIN", price_limit_pct=Decimal("10"),
              lot_size=100, effective_from=date(2020, 1, 1),
              effective_to=date(2026, 7, 6)),
    BoardRule(exchange="SSE", board="MAIN", price_limit_pct=Decimal("10"),
              lot_size=100, effective_from=date(2026, 7, 6)),
    BoardRule(exchange="SZSE", board="MAIN", price_limit_pct=Decimal("10"),
              lot_size=100, effective_from=date(2026, 7, 6)),
    # 创业板与科创板是 20% 涨跌幅。它们**可展示但默认不进入可执行模拟池**
    # （主文档 §4.1），但规则本身必须齐备：研究卡片要按
    # 交易所+板块+生效日匹配规则来回答"这个标的为什么不能模拟"。
    # 缺规则会显示成"无适用规则"，那是把"尚未接入"说成了"规则不存在"。
    #
    # 生效日按两板注册制改革时点：科创板 2019-07-22 开市即 20%，
    # 创业板 2020-08-24 起 20%。
    BoardRule(exchange="SSE", board="STAR", price_limit_pct=Decimal("20"),
              lot_size=200, effective_from=date(2019, 7, 22)),
    BoardRule(exchange="SZSE", board="GEM", price_limit_pct=Decimal("20"),
              lot_size=100, effective_from=date(2020, 8, 24)),
]
