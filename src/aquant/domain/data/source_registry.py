"""数据源注册表：能力、优先级与健康状态。

为什么需要它（T4.3 实测教训）：
   东方财富接口在约十余次请求后**整体拒绝**了本机出口 IP，
   且三十分钟以上未恢复——而同一时刻 GitHub / PyPI / 百度均正常。
   这证明"免费源会无预警地切断你"，而不是偶发抖动。

因此本项目**不把任何单一免费源当作唯一管道**：
  * 每个能力域声明多个候选源与优先级；
  * 每个源记录独立的能力卡与健康状态；
  * 抓取失败按优先级自动降级到下一个源。

这与主文档 §6.3 一致："一个字段一个主源；备用源不得在主源失败时静默混入"
——注意：**降级是显式的、有记录的，不是静默混接**。切换来源必须产生新的
数据版本与差异报告（§6.3、D07）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Domain(str, Enum):
    """主文档 §6.1 的数据源矩阵域。"""

    CALENDAR_IDENTITY = "CALENDAR_IDENTITY"
    DAILY_QUOTES = "DAILY_QUOTES"
    ADJUSTMENTS = "ADJUSTMENTS"
    CORPORATE_ACTIONS = "CORPORATE_ACTIONS"
    INDUSTRY_CONSTITUENTS = "INDUSTRY_CONSTITUENTS"
    FINANCIALS = "FINANCIALS"
    ANNOUNCEMENTS = "ANNOUNCEMENTS"
    MACRO = "MACRO"
    NEWS = "NEWS"
    SOCIAL_HEAT = "SOCIAL_HEAT"


class Rights(str, Enum):
    """§17.2 逐项权利登记。未知默认不开放。"""

    ALLOWED = "ALLOWED"
    PROHIBITED = "PROHIBITED"
    UNKNOWN = "UNKNOWN"


class SourceHealth(str, Enum):
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"


class IntegrationState(str, Enum):
    """§6.2 M0 四态。"""

    PLANNED = "PLANNED"
    CREDENTIALS_CONFIGURED = "CREDENTIALS_CONFIGURED"
    TEST_PASSED = "TEST_PASSED"
    IN_PRODUCTION_USE = "IN_PRODUCTION_USE"


@dataclass(slots=True)
class SourceSpec:
    source_id: str
    display_name: str
    domains: frozenset[Domain]
    #: 越小越优先。备用源用于降级，不用于静默混接。
    priority: int = 100
    cost_model: str = "FREE"
    requires_credentials: bool = False
    #: §7.2 是否提供历史时点版本
    pit_available: str = "NO"          # YES / PARTIAL / NO / UNKNOWN
    pit_basis: str = "UNKNOWN"
    integration_state: IntegrationState = IntegrationState.PLANNED
    health: SourceHealth = SourceHealth.UNKNOWN
    rights: dict[str, Rights] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    last_checked_at: datetime | None = None

    def rights_open_for(self, purpose: str) -> bool:
        """未知权限默认不开放（§17.2）。"""

        return self.rights.get(purpose) is Rights.ALLOWED

    def can_enter_model_context(self) -> bool:
        """外发到模型的唯一判据：明确允许模型处理。"""

        return self.rights_open_for("model_processing")


_UNKNOWN_RIGHTS = {k: Rights.UNKNOWN for k in (
    "research_use", "local_storage", "model_processing",
    "excerpt_display", "third_party_redistribution", "commercial_use",
)}


class SourceRegistry:
    def __init__(self, specs: list[SourceSpec] | None = None) -> None:
        self._specs: dict[str, SourceSpec] = {}
        for spec in specs or []:
            self.register(spec)

    def register(self, spec: SourceSpec) -> None:
        self._specs[spec.source_id] = spec

    def get(self, source_id: str) -> SourceSpec:
        if source_id not in self._specs:
            raise KeyError(f"unknown source {source_id!r}")
        return self._specs[source_id]

    def all(self) -> list[SourceSpec]:
        return list(self._specs.values())

    def candidates(self, domain: Domain, *,
                   require_test_passed: bool = True) -> list[SourceSpec]:
        """按优先级返回该域的可用候选。

        只返回测试通过的源——"计划接入"不等于可用（§6.2 四态）。
        """

        # 注意：这里**不按 health 过滤**。健康状态是运行时判断，
        # 由 FetchChain 在连续失败后标记；若在此处预先排除 BLOCKED，
        # 降级链就永远不会尝试失败过的源，也就无法在源恢复后自动回归。
        # 已知封锁的源应通过 integration_state 或运维配置排除，而不是靠过滤。
        out = [
            s for s in self._specs.values()
            if domain in s.domains
            and (not require_test_passed
                 or s.integration_state in (IntegrationState.TEST_PASSED,
                                            IntegrationState.IN_PRODUCTION_USE))
        ]
        return sorted(out, key=lambda s: (s.priority, s.source_id))

    def mark_health(self, source_id: str, health: SourceHealth,
                    *, at: datetime | None = None) -> None:
        spec = self.get(source_id)
        spec.health = health
        spec.last_checked_at = at or datetime.now(timezone.utc)

    def model_safe_sources(self, domain: Domain) -> list[SourceSpec]:
        """可用于模型外发链路的源。未确认模型处理权利的一律排除（§17.2）。"""

        return [s for s in self.candidates(domain) if s.can_enter_model_context()]


#: 本机实测（2026-09-13）得到的源清单。权利状态一律 UNKNOWN，等待人工确认。
def default_registry() -> SourceRegistry:
    return SourceRegistry([
        SourceSpec(
            source_id="eastmoney-direct",
            display_name="东方财富行情接口（直连）",
            domains=frozenset({
                Domain.DAILY_QUOTES, Domain.CALENDAR_IDENTITY,
                Domain.INDUSTRY_CONSTITUENTS, Domain.FINANCIALS,
            }),
            priority=10,
            pit_available="NO",
            integration_state=IntegrationState.TEST_PASSED,
            # 实测：约十余次请求后整体拒绝本机出口 IP，30+ 分钟未恢复
            health=SourceHealth.BLOCKED,
            rights=dict(_UNKNOWN_RIGHTS),
            notes=[
                "字段最全（含行业、市值、上市日期），但已知会对出口 IP 实施长时段封锁",
                "前复权序列随分红重算，历史值非不变量",
            ],
        ),
        SourceSpec(
            source_id="tencent-ifzq",
            display_name="腾讯证券行情（web.ifzq.gtimg.cn）",
            domains=frozenset({Domain.DAILY_QUOTES, Domain.ADJUSTMENTS,
                               Domain.CALENDAR_IDENTITY}),
            priority=20,
            pit_available="NO",
            integration_state=IntegrationState.TEST_PASSED,
            health=SourceHealth.HEALTHY,
            rights=dict(_UNKNOWN_RIGHTS),
            notes=[
                "实测可用：日线含不复权/qfq/hfq，字段与东财可比对",
                "实测可用作为东财的降级备用源",
            ],
        ),
        SourceSpec(
            source_id="tencent-qt",
            display_name="腾讯证券实时快照（qt.gtimg.cn）",
            domains=frozenset({Domain.DAILY_QUOTES, Domain.CALENDAR_IDENTITY}),
            priority=30,
            pit_available="NO",
            integration_state=IntegrationState.TEST_PASSED,
            health=SourceHealth.HEALTHY,
            rights=dict(_UNKNOWN_RIGHTS),
            notes=["轻量快照，适合逐日观察而非历史序列"],
        ),
        SourceSpec(
            source_id="sina-hq",
            display_name="新浪财经行情（hq.sinajs.cn）",
            domains=frozenset({Domain.DAILY_QUOTES, Domain.CALENDAR_IDENTITY}),
            priority=40,
            pit_available="NO",
            integration_state=IntegrationState.TEST_PASSED,
            health=SourceHealth.HEALTHY,
            rights=dict(_UNKNOWN_RIGHTS),
            notes=["需 Referer 头；字段为 GBK 编码，须显式解码"],
        ),
        SourceSpec(
            source_id="sse-site",
            display_name="上海证券交易所网站",
            domains=frozenset({Domain.CALENDAR_IDENTITY, Domain.ANNOUNCEMENTS,
                               Domain.CORPORATE_ACTIONS}),
            priority=50,
            pit_available="PARTIAL",     # 公告带原始发布页，具备可证明时点
            pit_basis="OBSERVED",
            integration_state=IntegrationState.TEST_PASSED,
            health=SourceHealth.HEALTHY,
            rights=dict(_UNKNOWN_RIGHTS),
            notes=["官方来源；公告页面自带发布时间，是时点证据的首选"],
        ),
        SourceSpec(
            source_id="szse-site",
            display_name="深圳证券交易所网站",
            domains=frozenset({Domain.CALENDAR_IDENTITY, Domain.ANNOUNCEMENTS,
                               Domain.CORPORATE_ACTIONS}),
            priority=50,
            pit_available="PARTIAL",
            pit_basis="OBSERVED",
            integration_state=IntegrationState.TEST_PASSED,
            health=SourceHealth.HEALTHY,
            rights=dict(_UNKNOWN_RIGHTS),
            notes=["与上交所并列的官方来源"],
        ),
        SourceSpec(
            source_id="cninfo",
            display_name="巨潮资讯网",
            domains=frozenset({Domain.ANNOUNCEMENTS, Domain.CORPORATE_ACTIONS,
                               Domain.FINANCIALS}),
            priority=45,
            pit_available="PARTIAL",
            pit_basis="OBSERVED",
            integration_state=IntegrationState.TEST_PASSED,
            health=SourceHealth.HEALTHY,
            rights=dict(_UNKNOWN_RIGHTS),
            notes=["证监会指定披露平台；公告原文与时间的权威来源"],
        ),
        SourceSpec(
            source_id="tushare-pro",
            display_name="Tushare Pro 财务数据",
            domains=frozenset({Domain.FINANCIALS}),
            priority=35,
            cost_model="POINTS_AND_TOKEN",
            requires_credentials=True,
            # 官方文档提供 ann_date / f_ann_date、report_type 与
            # update_flag，但历史修订记录是否足以恢复任一过去时点，
            # 仍须用真实账户和样本报告验证，因此不能先写成 YES。
            pit_available="PARTIAL",
            pit_basis="RECONSTRUCTED",
            integration_state=IntegrationState.CREDENTIALS_CONFIGURED,
            health=SourceHealth.UNKNOWN,
            rights=dict(_UNKNOWN_RIGHTS),
            notes=[
                "Token 已验证有效，但 income/balancesheet/cashflow 均返回 40203 无权限",
                "文档字段可覆盖 F07-F10；尚未完成真实字段与覆盖率验收",
                "必须保留公告日、报表类型和修订版本；不得与 BaoStock 字段静默拼接",
            ],
        ),
    ])
