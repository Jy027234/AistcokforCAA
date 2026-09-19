"""A-Quant Lab HTTP API。

职责边界（主文档 §14.1：前端不复制计算逻辑）：
    本层**只做编排**——参数校验、调用领域服务、序列化结果。
    组合构建、模拟成交、费用、估值、冻结复核全部委托给已有领域模块，
    因此每条规则只有一处实现，不会在 API 层被"顺手简化"。

安全语义（§16.3、Q0 报告 §4.3）：
    * 确认主体由服务端从**已验证凭证**取得，不接受请求体自报。
      这是 Q0 实测教训的直接应用：客户端传入的身份不是身份。
    * 冻结令牌由服务端签发，前端只负责传递。
    * 令牌失效/过期不 panic：那是业务状态，返回 200 + TERMINAL 标记，
      避免把正常业务结果混进错误通道。
    * 现金与批次一律从账本读取；调用方谎报会被账本比对拒绝。

刻意不做的事：不向模型暴露任何写权限；没有"一键下单"；不自动交易。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from aquant.application.workspace_queries import events, portfolio_ledger
from aquant.adapters.models.deepseek import DeepSeekProvider  # noqa: E402
from aquant.application.assistant import (  # noqa: E402
    AssistantError, Material, ask_assistant, model_calls,
)
from aquant.application.research_cards import (
    get_card_payload, persist_card, research_cards,
)
from aquant.domain.ai.model import TextModelProvider  # noqa: E402
from aquant.domain.evidence.store import evidence_for  # noqa: E402
from aquant.operations.freshness import freshness  # noqa: E402
from aquant.operations.research_jobs import (  # noqa: E402
    run_research_job, submit_research_job,
)
from aquant.application.workspace_view import build_data_status, build_research_card
from aquant.domain.data.db import apply_migrations, connect
from aquant.domain.data.ingest import SnapshotBuilder
from aquant.domain.data.reader import SnapshotReader
from aquant.domain.data.snapshot import SnapshotError, SnapshotStore
from aquant.domain.portfolio.construction import Candidate, ConstructionParams
from aquant.domain.portfolio.plan import PlanError, PlanService, confirmer_is_human
from aquant.domain.research.experiments import (
    ExperimentError, ExperimentSpec, experiment, experiments,
    note_test_set_access, record_outcome, register_experiment,
)
from aquant.domain.research.f10 import compute_f10_for_snapshot
from aquant.domain.research.strategies import (  # noqa: E402
    KNOWN_FEATURE_SPECS, StrategyVersionError, ensure_strategy_version,
    seed_known_versions, strategy_versions,
)
from aquant.domain.research.runs import factor_values, factor_values_for_snapshot
from aquant.operations.scheduler import (
    ScheduleError,
    request_run,
    save_schedule,
)
from aquant.operations.scheduler import status as scheduler_status
from aquant.operations.snapshot_lifecycle import resolve_current_snapshot
from aquant.domain.simulation.corporate_actions import CashDividend
from aquant.domain.simulation.fees import synthetic_fee_table
from aquant.operations.jobs import Job, JobError, JobStore
from aquant.operations.workbench import (
    WorkbenchError, add_watchlist_item, decisions, record_decision,
    remove_watchlist_item, watchlist,
)
from aquant.domain.simulation.fees import FeeError
from aquant.domain.simulation.verified_fees import (  # noqa: E402
    fee_table_from_env, provenance as fee_provenance_source,
)
from aquant.domain.simulation.simulator import Bar, SimError

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_ID = "snap-syn-001"

#: 合成快照的交易所/板块清单；真实快照一律从快照内生证券读取。
#: 用运行时解析而不是写死：指向真实快照时，写死的清单会让真实标的
#: 落到"未知板块"，从而拿不到涨跌幅规则。
LISTINGS: dict[str, tuple[str, str]] = {
    "SYN.A.600519": ("SSE", "MAIN"),
    "SYN.A.000001": ("SZSE", "MAIN"),
    "SYN.A.600003": ("SSE", "MAIN"),
    "SYN.A.600002": ("SSE", "MAIN"),
    "SYN.A.300001": ("SZSE", "GEM"),
}


def _listings_for(state: "AppState", snapshot_id: str) -> dict[str, tuple[str, str]]:
    """从快照内生的证券记录构造 证券 -> (交易所, 板块)。

    必须来自快照而不是常量：涨跌幅规则、手数、可模拟性都按板块决定，
    拿不到板块就等于拿不到交易规则。
    """

    ref = state.reader.ref(snapshot_id)
    out: dict[str, tuple[str, str]] = {}
    for inst in state.reader.instruments(snapshot_id, as_of=ref.as_of_time):
        iid, exchange, board = (inst.get("instrument_id"), inst.get("exchange"),
                                inst.get("board"))
        if iid and exchange and board:
            out[iid] = (exchange, board)
    return out

# 规则表已移到域层：摄取层也要用它来判定 daily_quotes[].board_limit_up，
# 两份规则必然漂移（见 aquant.domain.simulation.board_rules 的说明）。
from aquant.domain.simulation.board_rules import BOARD_RULES  # noqa: E402

#: 合成快照下的演示候选。真实快照一律走 S1（见 _s1_candidates）。
DEMO_CANDIDATES = [
    Candidate("SYN.A.600519", "SW_SYN_01", 0.90),
    Candidate("SYN.A.000001", "SW_SYN_02", 0.70),
    Candidate("SYN.A.600003", "SW_SYN_05", 0.55),
]

#: 主文档 §4.1：首期只模拟沪深主板。其余板块可展示但不可模拟。
SIMULATABLE_BOARDS = {"MAIN"}

#: 计算 S1 所需的最少收盘价根数（F02 是 60 日动量跳过近 5 日，故需 61 根）
S1_MIN_CLOSES = 61


# ======================================================================
# 运行状态容器
# ======================================================================
class AppState:
    """一次进程生命周期内共享的连接与派生对象。

    刻意保持简单：单机、单库、单快照，与 §14.2 的模块化单体一致。
    """

    def __init__(self, con: sqlite3.Connection, root: Path, *,
                 data_dir: Path | None = None) -> None:
        self.con = con
        self.root = root
        #: 启动时解析好的数据目录。
        #:
        #: **不要在请求处理里重新解析它**：_data_dir_from_env() 有副作用——
        #: AQUANT_RESET_DATA=1 时它会 shutil.rmtree 数据目录。
        #: 把它放进请求路径的后果是**每个请求都在删自己的数据目录**
        #: （当时还开着 SQLite 连接）。这正是 /status 500 的原因。
        self.data_dir = data_dir or root.parent
        self.store = SnapshotStore(con, root)
        self.reader = SnapshotReader(self.store)
        self._snapshot_lock = threading.RLock()
        self.snapshot_id = active_snapshot(self.data_dir, con)
        self.fees = resolve_fee_table(self.snapshot_id)
        self.fee_provenance = fee_provenance()
        # 组合参数与研究配置保持一致（单票 10% / 行业 30%）。持仓上限取 4：
        # 研究池有 24 只（其中 18 只沪深主板可模拟），因此上限确实是紧的——
        # 池子只够填满上限时，"上限"就测不出任何东西。
        self.params = ConstructionParams(
            max_holdings=4, max_single_name_pct=Decimal("10"),
            max_single_industry_pct=Decimal("30"),
            # 与研究配置一致：近 20 日均成交额 >= 5,000 万元。
            # 该阈值此前因免费源**没有成交额**而保持 None（不筛）——
            # 给一个默认阈值会让所有标的被排除，而用"价格×成交量"估算
            # 是拿未观测的数字做准入判断。BaoStock 补齐成交额后（ADR-004），
            # 这条约束终于可以真正生效。
            liquidity_min_avg_amount_cents=5_000_000_000,
        )
        # 板块来自快照内生证券。指向真实快照时用写死的合成清单，
        # 会让真实标的落到"未知板块"，从而拿不到涨跌幅规则与手数。
        self.listings: dict[str, tuple[str, str]] = {}
        self.refresh_listings()
        self.service = PlanService(con, self.reader, self.fees, BOARD_RULES,
                                   self.listings, self.params)
        #: plan_id -> 最近一次预览。确认令牌与冻结都必须针对**同一个预览**，
        #: 因此服务端要保留它；进程重启后预览失效，必须重新预览（这是正确行为）。
        self.previews: dict[str, Any] = {}
        #: 文本模型提供方（ADR-012）。**惰性构造**：没有密钥时也能启动服务，
        #: 只是调用助手接口会明确报"模型不可用"，而不是启动就崩。
        #: 这样离线测试与行情相关的功能完全不依赖模型配置。
        self.model_provider: TextModelProvider | None = None

    def current_snapshot(self) -> str:
        """解析并切换到流水线刚发布的当前物理快照。

        一次请求只应调用一次并复用返回值。指针变化时同步刷新费率、
        证券板块映射和 PlanService，避免页面已经显示新快照而组合服务仍
        使用旧快照派生状态。
        """

        with self._snapshot_lock:
            snapshot_id = active_snapshot(self.data_dir, self.con)
            self.store.require_published(snapshot_id)
            if snapshot_id == self.snapshot_id:
                return snapshot_id

            # 先完整构造新快照所需依赖，再一次性替换内存状态。费率未配置等
            # 闸门可能在这里拒绝切换；若提前写 self.snapshot_id，下一次请求
            # 会误以为已经切换完成，实际却继续使用旧费率和旧 PlanService。
            fees = resolve_fee_table(snapshot_id)
            if snapshot_id == SNAPSHOT_ID:
                listings = dict(LISTINGS)
            else:
                try:
                    listings = _listings_for(self, snapshot_id)
                except (SnapshotError, KeyError):
                    listings = {}
            service = PlanService(
                self.con, self.reader, fees, BOARD_RULES, listings, self.params,
            )
            self.snapshot_id = snapshot_id
            self.fees = fees
            self.listings = listings
            self.service = service
            return snapshot_id

    def provider(self) -> TextModelProvider:
        if self.model_provider is None:
            self.model_provider = DeepSeekProvider()
        return self.model_provider

    def refresh_listings(self) -> None:
        """按当前快照刷新 证券 -> (交易所, 板块)。

        板块决定涨跌幅、手数与可模拟性，因此它必须来自**当前快照内生的
        证券记录**，不能来自写死的合成清单：指向真实快照时，
        写死清单会让真实标的落到"未知板块"，从而拿不到交易规则。
        """

        if self.snapshot_id == SNAPSHOT_ID:
            self.listings = dict(LISTINGS)
            return
        try:
            self.listings = _listings_for(self, self.snapshot_id)
        except (SnapshotError, KeyError):
            # 快照读不出来时保持为空字典：宁可在下单前报"无适用规则"，
            # 也不要拿一份猜出来的板块去算涨跌停。
            self.listings = {}


def daily_run_log_path(data_dir: Path | None = None) -> Path:
    """每日流水的运行留痕位置。

    默认与 tools/daily_run.py 一致（仓库里的 deploy/agentctl-q0/）。
    刻意不**必须**跟着 AQUANT_DATA_DIR 走：那个变量指向的是账本数据目录，
    而运行留痕是流水线自己的产物，两者可以不在同一处
    （容器里数据在 /data，代码与 deploy 在 /app）。

    **必须传入启动时解析好的 data_dir，不要在请求里重新解析。**
    _data_dir_from_env() 带副作用（AQUANT_RESET_DATA=1 时删目录），
    在请求路径里调用它等于每个请求都在删数据。
    用 AQUANT_DAILY_RUN_LOG 可显式覆盖。
    """

    override = os.environ.get("AQUANT_DAILY_RUN_LOG", "").strip()
    if override:
        return Path(override)
    candidates = [ROOT / "deploy" / "agentctl-q0" / "daily-runs.jsonl"]
    if data_dir is not None:
        candidates.append(data_dir / "daily-runs.jsonl")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _snapshot_last_day(state: "AppState") -> str | None:
    """快照覆盖的最后一个交易日。读不出就返回 None，不猜。"""

    try:
        ref = state.reader.ref(state.snapshot_id)
        rows = state.reader.daily_quotes(state.snapshot_id, as_of=ref.as_of_time)
    except Exception:                                    # noqa: BLE001
        return None
    days = {r.trading_day for r in rows}
    return max(days).isoformat() if days else None


def resolve_fee_table(snapshot_id: str):
    """按快照的数据模式选费率表（§12.6）。

    规则：
      * 配置了券商佣金（AQUANT_COMMISSION_RATE / _MIN_CENTS）-> 用**经验证**的费率表；
      * 没配置：使用带明确合成标记的占位费率表；只读研究仍可启动，
        preview/freeze/execute/value 会在领域闸门拒绝把它用于真实快照。

    费率缺失不应让候选、研究卡和证据也不可读；风险发生在模拟计算时，
    所以闸门放在依赖费用的领域入口，并由 API 返回明确 422。

    刻意不提供"跳过检查"的参数：要放行就给佣金，那是一个有记录的动作。
    """

    configured = bool(os.environ.get("AQUANT_COMMISSION_RATE", "").strip())

    if not configured:
        # 这张表带 synthetic_test_rate 标记；真实快照的模拟入口会拒绝它。
        return synthetic_fee_table()

    # 与验收脚本**同一条**取表路径（fee_table_from_env），
    # 因此"验收里跑的费率"与"真实用户跑的费率"不会分叉。
    table, _note = fee_table_from_env()
    return table


def fee_provenance() -> list[dict]:
    """费率出处的只读视图，供界面回答"这个数字哪来的"。"""

    return fee_provenance_source()


def _data_dir_from_env() -> Path | None:
    """从环境变量决定数据目录。

    `AQUANT_DATA_DIR` 指向固定目录时，账本与快照会**跨重启保留**；
    `AQUANT_RESET_DATA=1` 会在启动时清空该目录（显式清除、而非静默重建）。

    这两个开关是为验证服务的：界面写路径的检查需要一个**干净且隔离**的
    账本，否则上一次运行冻结的计划会留到下一次，让重复运行的结论不可信。
    默认（都不设）仍然是临时目录，不会碰到任何已有数据。
    """

    raw = os.environ.get("AQUANT_DATA_DIR")
    data_dir = Path(raw).expanduser() if raw else None

    if os.environ.get("AQUANT_RESET_DATA") == "1":
        if data_dir is None:
            # 没有指定目录就"重置"等于删掉一个随机临时目录，毫无意义，
            # 而且会让人误以为数据被清了。直接拒绝，避免静默的错误结论。
            raise RuntimeError(
                "AQUANT_RESET_DATA=1 需要同时设置 AQUANT_DATA_DIR，"
                "否则无法确定要清空哪个目录"
            )
        if data_dir.exists():
            shutil.rmtree(data_dir)
        print(f"[api] AQUANT_RESET_DATA=1：已清空 {data_dir}")

    return data_dir


def active_snapshot(data_dir: Path | None = None,
                    con: sqlite3.Connection | None = None) -> str:
    """当前生效的快照 ID。

    `AQUANT_SNAPSHOT_ID` 可以指向**已经发布过**的快照（例如真实数据快照
    `snap-real-61d`），使得同一套 API 与界面既能跑合成验收、也能跑真实验收，
    不必为真实数据再写一个服务。默认仍是合成快照。
    """

    configured = os.environ.get("AQUANT_SNAPSHOT_ID", "").strip()
    if configured:
        return configured
    resolved_root = data_dir
    if resolved_root is None:
        raw = os.environ.get("AQUANT_DATA_DIR", "").strip()
        resolved_root = Path(raw).expanduser() if raw else None
    if resolved_root is None:
        return SNAPSHOT_ID
    return resolve_current_snapshot(
        resolved_root, connection=con, legacy_default=SNAPSHOT_ID)


def _seed_synthetic(con: sqlite3.Connection, root: Path) -> None:
    """自举合成快照。仅在该快照尚不存在时执行。"""

    import sys

    # 复用资料包自带的合成样例，避免为此再维护一份数据
    sys.path.insert(0, str(ROOT / "tests" / "integration"))
    from test_m1_ingest_e2e import build_snapshot  # noqa: PLC0415

    builder = SnapshotBuilder(con, root / "datasets")
    store = SnapshotStore(con, root)
    build_snapshot(con, builder, store)


def build_state(data_dir: Path | None = None) -> AppState:
    """构建状态。默认在临时目录自举一份合成快照，使 API 可独立运行。

    若 `AQUANT_SNAPSHOT_ID` 指向一个已存在的快照，则**直接使用它**，
    不再重建合成数据——否则真实快照会被合成数据盖掉，
    而请求仍然拿着真实快照的 ID，界面就会读到一份对不上的账。
    """

    import tempfile

    if data_dir is None:
        data_dir = _data_dir_from_env()
    if data_dir is None:
        data_dir = Path(tempfile.mkdtemp(prefix="aquant-api-"))
    data_dir.mkdir(parents=True, exist_ok=True)
    # FastAPI 把同步端点放到线程池执行，因此复用同一连接时必须显式允许跨线程，
    # 由 db.write_tx 的连接锁串行化写事务（否则读-改-写会互相覆盖）。
    con = connect(data_dir / "meta.sqlite", allow_thread_sharing=True)
    apply_migrations(con)

    root = data_dir / "api"
    root.mkdir(exist_ok=True)
    store = SnapshotStore(con, root)

    # 策略版本必须在实验登记之前存在（外键）。这不是形式要求：
    # 登记一个从未冻结过的策略版本，实验就无法复现。
    seeded = seed_known_versions(con)
    if seeded["created"]:
        print(f"[api] 登记策略版本：{', '.join(seeded['created'])}")

    wanted = active_snapshot(data_dir, con)
    try:
        # require_published 同时校验状态：只认已发布快照，
        # 半成品快照不能拿来跑决策（否则会读到一份"看起来有数据"的空壳）。
        store.require_published(wanted)
        print(f"[api] 使用已发布的快照 {wanted}")
    except SnapshotError:
        if wanted != SNAPSHOT_ID:
            raise RuntimeError(
                f"当前快照 {wanted} 不存在或未发布；"
                "检查 AQUANT_SNAPSHOT_ID / current_snapshot.json，"
                "或先用发布脚本建成该快照"
            ) from None
        _seed_synthetic(con, root)
    return AppState(con, root, data_dir=data_dir)


# ======================================================================
# 请求 / 响应模型
# ======================================================================
class PreviewRequest(BaseModel):
    portfolio_id: str = Field(min_length=1, max_length=64)
    snapshot_id: str = Field(default_factory=active_snapshot)
    decision_snapshot_id: str | None = Field(default=None, min_length=1, max_length=128)
    decision_cutoff_at: datetime | None = None
    execution_snapshot_id: str | None = Field(default=None, min_length=1, max_length=128)
    trading_day: date
    cash_available_cents: int | None = Field(
        default=None, ge=0,
        description="留空则由服务端从账本读取；提供时若与账本不符将被拒绝",
    )


class FreezeRequest(BaseModel):
    plan_id: str = Field(min_length=1, max_length=64)
    confirmation_token: str = Field(min_length=8, max_length=512)


class StrategyVersionRequest(BaseModel):
    strategy_version: str = Field(min_length=1, max_length=64)
    family: str = Field(pattern="^(S1|S2|E1|CUSTOM)$")
    spec: dict
    parent_version: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=1000)


class ExperimentRequest(BaseModel):
    """实验登记输入（§13.2）。

    **不含结果字段**：登记发生在看结果之前，接口形状本身就在阻止"跑完再补"。
    """

    hypothesis: str = Field(min_length=1, max_length=2000)
    data_range_start: date
    data_range_end: date
    universe: list[str] = Field(min_length=1)
    feature_version: str = Field(min_length=1, max_length=64)
    strategy_version: str = Field(min_length=1, max_length=64)
    primary_metric: str = Field(min_length=1, max_length=200)
    stopping_condition: str = Field(min_length=1, max_length=500)
    train_start: date | None = None
    train_end: date | None = None
    valid_start: date | None = None
    valid_end: date | None = None
    test_start: date | None = None
    test_end: date | None = None
    preprocessing: dict = Field(default_factory=dict)
    label_window: str | None = Field(default=None, max_length=64)
    fee_version: str | None = Field(default=None, max_length=64)
    slippage_bps: int | None = Field(default=None, ge=0, le=10_000)
    comparison: dict = Field(default_factory=dict)

    def to_spec(self) -> "ExperimentSpec":
        def window(start: date | None, end: date | None) -> tuple[date, date] | None:
            if start is None and end is None:
                return None
            if start is None or end is None:
                raise WorkbenchError(
                    "DATA_NOT_READY",
                    "split window needs both start and end",
                    "window", "provide both dates or neither")
            return (start, end)

        return ExperimentSpec(
            hypothesis=self.hypothesis,
            data_range_start=self.data_range_start,
            data_range_end=self.data_range_end,
            universe=self.universe,
            feature_version=self.feature_version,
            strategy_version=self.strategy_version,
            primary_metric=self.primary_metric,
            stopping_condition=self.stopping_condition,
            train=window(self.train_start, self.train_end),
            valid=window(self.valid_start, self.valid_end),
            test=window(self.test_start, self.test_end),
            preprocessing=self.preprocessing,
            label_window=self.label_window,
            fee_version=self.fee_version,
            slippage_bps=self.slippage_bps,
            comparison=self.comparison)


class OutcomeRequest(BaseModel):
    status: str = Field(
        pattern="^(REGISTERED|RUNNING|COMPLETED|FAILED|ABANDONED)$")
    outcome_notes: str = Field(default="", max_length=4000)


class TestSetAccessRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class ResearchRunRequest(BaseModel):
    #: 限制参与计算的标的数（调试用）；0 = 不限。
    limit: int = Field(default=0, ge=0, le=10_000)


class ScheduleRequest(BaseModel):
    """每日任务的配置（§14.2）。

    `interpreter` 是**必填项而不是可选项**：这条流水线依赖 baostock 与
    pytest，而 API 进程自己的解释器未必装了它们。留空并启用会被拒绝
    （见 `save_schedule`），因为"用错解释器"每天都会失败，
    而失败信息看起来像数据源问题。
    """

    enabled: bool
    run_at_local: str = Field(default="20:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    weekdays_only: bool = True
    interpreter: str = Field(default="", max_length=400)
    data_dir: str = Field(default="", max_length=400,
                          description="快照数据目录。留空 = 用 worker 的默认目录")
    window_start: str = Field(default="2026-06-22", pattern=r"^\d{4}-\d{2}-\d{2}$")


class RunNowRequest(BaseModel):
    """请求立刻运行一次。**只是请求**：执行由独立 worker 负责。"""

    reason: str | None = Field(default=None, max_length=200)


class ResearchJobRequest(BaseModel):
    """研究作业提交（§8.4）。

    **不含作业 id 与幂等键**：幂等键由服务端按
    「作业类型 + 交易日 + 配置版本 + 输入快照」确定性算出，
    调用方若能自己指定，就能用两个不同的键跑同一件事。
    """

    job_type: str = Field(pattern="^(FACTOR_COMPUTE|EVIDENCE_RESEARCH)$")
    trading_day: date
    snapshot_id: str = Field(default_factory=active_snapshot,
                             min_length=1, max_length=64)
    config_version: str = Field(default="default", min_length=1, max_length=64)
    payload: dict = Field(default_factory=dict,
                          description="作业参数，例如「limit: 50」")


class AssistantMaterial(BaseModel):
    """一份交给模型的材料。**必须**声明来源——闸门是按来源判定的。"""

    source_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=200_000)
    #: 默认必须是 None（=未声明），**不能**是 False。
    #:
    #: §826 的不对称默认是"未声明等同于含个人信息"。这里的默认值
    #: 一旦写成 False，就等于服务端替调用方声明了"不含个人信息"，
    #: 闸门那道不对称默认会被这一行悄悄绕开——而且绕得完全没有痕迹。
    contains_personal_data: bool | None = None


class AssistantDraftRequest(BaseModel):
    """助手可附带的草稿请求（§5.5）。

    **只有算这一半**：服务端据此产出草稿与差异预览，
    不写计划、不写账本、不冻结。冻结必须走
    /plans/{id}/confirmation + /freeze 那条带一次性令牌的路径。
    """

    portfolio_id: str = Field(min_length=1, max_length=64)
    trading_day: date
    snapshot_id: str = Field(default_factory=active_snapshot,
                             min_length=1, max_length=64)


class AssistantMessageRequest(BaseModel):
    purpose: str = Field(min_length=1, max_length=200)
    materials: list[AssistantMaterial] = Field(min_length=1, max_length=50)
    #: 默认 8192：思考型模型会把上限全部用在思考上（见 domain/ai/model.py）
    max_output_tokens: int = Field(default=8192, ge=64, le=8192)
    #: 可选：顺带算一份草稿与差异预览。算完即弃，不落库。
    draft: AssistantDraftRequest | None = None


class WatchRequest(BaseModel):
    instrument_id: str = Field(min_length=1, max_length=64)
    #: 为什么关注。留一句话比只留一个代码有用得多——三个月后
    #: 没人知道当初为什么关注它。
    note: str | None = Field(default=None, max_length=500)


class DecisionRequest(BaseModel):
    """决策记录。**模型方案与人工方案分开提交**（§11.3）。

    刻意不接收事先算好的 diff：差异由服务端对两份方案现算，
    否则调用方可以提交一个"看起来没改"的 diff 来美化记录。
    """

    portfolio_id: str = Field(min_length=1, max_length=64)
    snapshot_id: str = Field(default_factory=active_snapshot)
    decision_type: str = Field(
        pattern="^(ACCEPT_MODEL|MODIFY_MODEL|NO_CHANGE|TIMEOUT|REJECT|CANCEL)$")
    plan_id: str | None = Field(default=None, max_length=128)
    model_proposed: dict | None = None
    human_final: dict | None = None
    reason_category: str | None = Field(default=None, max_length=64)
    reason_note: str | None = Field(default=None, max_length=1000)
    #: 用了模型上下文之外的信息会改变可复现性，事后无法从数据反推，
    #: 只能靠记录。因此必须显式声明，默认 False。
    external_information_used: bool = False
    rule_check: dict | None = None


class DividendInput(BaseModel):
    """当日落在除权日或到账日的现金分红。

    金额给两种口径之一，**优先用微元**：
      * `cash_per_share_micros` —— 权威单位。真实分红常常不是整数分
        （茅台 2025 年度每股 28.02423 元 = 2,802.423 分），
        按分传递会截断，误差随股数放大；
      * `cash_per_share_cents` —— 过渡口径，仅当金额确实是整数分时使用。
    """

    action_id: str = Field(min_length=1, max_length=64)
    instrument_id: str = Field(min_length=1, max_length=64)
    record_date: date
    ex_date: date
    pay_date: date
    cash_per_share_micros: int | None = Field(default=None, gt=0)
    cash_per_share_cents: int | None = Field(default=None, gt=0)
    #: §12.6 未实现完整红利税处理前必须标注口径，默认税前
    tax_treatment: str = Field(default="PRE_TAX", pattern="^(PRE_TAX|CONSERVATIVE|VERIFIED)$")

    @model_validator(mode="after")
    def _require_one_amount(self) -> "DividendInput":
        if (self.cash_per_share_micros is None) == (self.cash_per_share_cents is None):
            raise ValueError(
                "provide exactly one of cash_per_share_micros / cash_per_share_cents"
            )
        return self


class ExecuteRequest(BaseModel):
    plan_id: str = Field(min_length=1, max_length=64)
    #: 不传则视为当日无公司行为。权利由登记日持仓决定，服务端自行计算，
    #: 调用方不能指定股数或金额。
    corporate_actions: list[DividendInput] = Field(default_factory=list)


class ValueRequest(BaseModel):
    portfolio_id: str = Field(min_length=1, max_length=64)
    snapshot_id: str = Field(default_factory=active_snapshot)
    trading_day: date


# ======================================================================
# 应用
# ======================================================================
def _job_view(job: Job) -> dict:
    """把领域 Job 转成对外形状。

    字段名**显式访问**而不是 getattr(..., None)：
    我第一版用 getattr 加默认值，结果字段名写错（attempts 实际叫
    attempt_count、error 实际是 error_code/error_detail）
    却全部静默返回 None——接口看起来正常，值全是空的。
    AttributeError 比一个安静的空值好得多。
    """

    return {
        "jobId": job.job_id,
        "jobType": job.job_type,
        "status": job.status.value,
        "tradingDay": job.trading_day,
        "attemptCount": job.attempt_count,
        "leaseOwner": job.lease_owner,
        "leaseExpiresAt": (job.lease_expires_at.isoformat()
                           if job.lease_expires_at else None),
        "errorCode": job.error_code,
        "errorDetail": job.error_detail,
        "idempotencyKey": job.idempotency_key,
        # 作业"做了什么"必须能从接口回答：结果与参数原先只写不读，
        # 于是 /jobs 只能显示状态，看不出这条作业产出的是什么。
        "snapshotId": job.input_snapshot_id,
        "configVersion": job.config_version,
        "result": json.loads(job.result_json) if job.result_json else None,
    }


#: 引用不在原文中的标记。刻意写明"原文中定位不到"而不是"引用无效"：
#: 定位失败可能是模型改写，也可能是来源文本本身被截断——
#: 后者是数据问题，不是模型问题。界面不该替使用者下结论。
UNLOCATED_NOTE = "该引用在来源正文中定位不到（可能是改写，也可能是来源文本被截断）"


def _assistant_draft(state: "AppState", req: "AssistantDraftRequest") -> dict:
    """按助手的草稿请求算一份预览。**只算不写**（§5.5、A08）。

    刻意不复用 /plans/preview 的完整草稿视图：那个端点返回的是
    供界面渲染的完整结构，而这里只要能回答"会做什么、代价多少"。
    两者共用的底层计算是 plan.preview，所以不存在两套算法。
    """

    ref = state.reader.ref(req.snapshot_id)
    signals, _note = _s1_candidates(state, req.snapshot_id)
    candidates = [Candidate(sig.instrument_id, sig.industry_code, sig.signal_rank)
                  for sig in signals]
    lots = state.service._load_lots(req.portfolio_id)
    cash = state.service._ledger_cash(req.portfolio_id)
    preview = state.service.preview(
        portfolio_id=req.portfolio_id, snapshot_id=req.snapshot_id,
        trading_day=req.trading_day, as_of=ref.as_of_time,
        candidates=candidates, cash_available_cents=cash, lots=lots,
        confirm_subject="assistant:draft-preview")
    return {
        "portfolioId": req.portfolio_id,
        "snapshotId": req.snapshot_id,
        "tradingDay": req.trading_day.isoformat(),
        "planId": preview.plan_id,
        "orders": [{"instrumentId": o["instrument_id"], "side": o["side"],
                    "quantity": o["quantity"]} for o in preview.orders],
        "estimatedFeesCents": preview.estimated_fees_cents,
        "frozen": preview.frozen,
        "excluded": [{"instrumentId": e.get("instrument_id"),
                      "reason": e.get("reason")}
                     for e in (preview.excluded or [])][:20],
    }


def _card_evidence(con: sqlite3.Connection, instrument_id: str, *,
                   as_of: datetime | None = None) -> list[dict]:
    """把落库证据转成研究卡的 evidence 形状。

    一条引用一条记录，同一条公告的多个字段各自成条——
    界面上逐条可核对才有意义，合并成"某公告"会让引用失去作用。
    """

    out: list[dict] = []
    for row in evidence_for(con, instrument_id=instrument_id, as_of=as_of):
        if not row.get("citationId"):
            continue        # 只有事件没有引用：卡片的证据栏不显示它
        out.append({
            "statement": row["factSummary"],
            "citationId": row["citationId"],
            "documentId": row["documentId"],
            "quote": row["quote"],
            "note": (None if row["located"] else UNLOCATED_NOTE),
            "located": bool(row["located"]),
            "locatorKind": None if not row["located"] else row.get("locatorKind"),
            "availableAt": row["availableAt"],
        })
    return out


def _card_counter_evidence(con: sqlite3.Connection, instrument_id: str, *,
                           as_of: datetime | None = None) -> list[dict]:
    """从证据本身派生反证。

    两条规则，都是"我们不掌握的事实要自己说出来"：

      * 有引用在原文里定位不到 -> 那条证据不可核验，必须显式列出；
      * 同一份公告的模型抽取与解析结果**不一致** -> 这是最需要被看到的反证，
        两边都留着，由人判断以谁为准。

    这里不主动替使用者下"因此结论不成立"的判断——
    卡片的职责是把反证摆出来，不是替人下结论。
    """

    located_flags: dict[str, list[bool]] = {}
    for row in evidence_for(con, instrument_id=instrument_id, as_of=as_of):
        if row.get("citationId"):
            located_flags.setdefault(row["eventId"], []).append(bool(row["located"]))

    out: list[dict] = []
    for event_id, flags in located_flags.items():
        if flags and not all(flags):
            out.append({
                "statement": "部分引用无法在来源正文中定位",
                "note": (f"事件 {event_id} 中有 {sum(1 for f in flags if not f)}/"
                         f"{len(flags)} 条引用定位不到；该证据不可完全核验"),
                "noneFound": False,
            })
    unverified = con.execute(
        "SELECT e.event_id,e.fact_summary,e.available_at FROM event e "
        "JOIN event_subject s ON s.event_id=e.event_id AND s.subject_id=? "
        "WHERE e.verification_status='DISPUTED'", (instrument_id,)).fetchall()
    for row in unverified:
        if as_of is not None:
            available_at = row["available_at"]
            try:
                visible = (available_at is not None
                           and datetime.fromisoformat(available_at) <= as_of)
            except (TypeError, ValueError):
                visible = False
            if not visible:
                continue
        out.append({"statement": "存在被标记为争议的证据",
                    "note": row["fact_summary"], "noneFound": False})
    return out


def _display_name(state: "AppState", snapshot_id: str, instrument_id: str) -> str | None:
    """证券简称。取不到就返回 None，不编一个像名字的字符串。"""

    try:
        for inst in state.reader.instruments(snapshot_id,
                                             as_of=state.reader.ref(snapshot_id).as_of_time):
            if inst.get("instrument_id") == instrument_id:
                return inst.get("short_name")
    except Exception:                                   # noqa: BLE001
        return None
    return None


def _is_simulatable(board: str | None) -> bool:
    """§4.1：只有沪深主板进入可执行模拟池。"""

    return (board or "").upper() in SIMULATABLE_BOARDS


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """候选的对外形状：S1 信号 + 该板块能否进入可执行模拟池。

    单独定义而不是往 `S1Signal` 上挂字段：板块可模拟性是**交易规则**，
    不是策略输出。策略模块不该知道哪块板能交易，否则换一个策略或换一套
    市场规则时，两边都会被牵动。
    """

    instrument_id: str
    industry_code: str
    signal_rank: float
    simulatable: bool
    f01_rank: float | None = None
    f02_rank: float | None = None
    low_vol_rank: float | None = None


def _s1_candidates(state: "AppState", snapshot_id: str
                   ) -> tuple[list["RankedCandidate"], str]:
    """在当前快照上计算 S1，返回 (信号, 说明)。

    历史窗口用**执行日之前已知**的收盘价，因此这里按快照的 as_of 读取，
    与 §10.1 的"决策截止时可知输入"一致。

    历史不足 61 根时**不产出候选**，并说明原因：F02 需要 60 日动量，
    用不足的窗口算出来的排名是另一种口径，不能冒充 S1。
    """

    from aquant.domain.strategy.s1 import FactorDataError, build_s1_signals

    ref = state.reader.ref(snapshot_id)
    if snapshot_id == SNAPSHOT_ID:
        # 合成快照仍是固定的演示候选：它是契约样例，不是研究池
        return (
            [RankedCandidate(
                instrument_id=c.instrument_id, industry_code=c.industry_code,
                signal_rank=c.signal_rank,
                simulatable=_is_simulatable(
                    LISTINGS.get(c.instrument_id, ("", ""))[1]),
             ) for c in DEMO_CANDIDATES],
            "合成快照使用固定演示候选",
        )

    closes: dict[str, list[int]] = {}
    industry: dict[str, str] = {}
    boards: dict[str, str] = {}
    missing_adjusted: list[str] = []
    for inst in state.reader.instruments(snapshot_id, as_of=ref.as_of_time):
        iid = inst.get("instrument_id")
        code = inst.get("industry_code")
        # 没有权威行业分类就不参与横截面排名：S1 的排名是"同行业可比"，
        # 行业缺失时把它塞进某个行业会污染整个横截面。
        if not iid or not code:
            continue
        rows = state.reader.daily_quotes(snapshot_id, as_of=ref.as_of_time,
                                         instrument_id=iid)
        # S1 的收益与动量必须使用前复权收盘价；原始 OHLC 只服务于成交、
        # 涨跌停和账本。缺失复权价就排除该证券，不能把原始价冒充研究价。
        prices = [r.adjusted_close_cents for r in rows
                  if r.adjusted_close_cents is not None]
        # 新上市证券不足完整窗口可以正常排除；已经具备完整原始行情窗口、
        # 却缺少前复权价则是数据缺口。后者不能伪装成“没有候选”。
        if len(rows) >= S1_MIN_CLOSES and any(
                r.adjusted_close_cents is None for r in rows):
            missing_adjusted.append(iid)
            continue
        if len(prices) < S1_MIN_CLOSES:
            continue
        closes[iid] = prices
        industry[iid] = code
        boards[iid] = (inst.get("board") or "").upper()

    if missing_adjusted:
        sample = ", ".join(missing_adjusted[:5])
        raise PlanError(
            "DATA_NOT_READY",
            f"快照有 {len(missing_adjusted)} 只证券具备原始行情窗口但缺少完整前复权价"
            f"（示例：{sample}）",
            snapshot_id,
            "重新采集并发布包含 adjusted_close_cents 的快照后再计算 S1",
        )

    if not closes:
        raise PlanError(
            "DATA_NOT_READY",
            f"快照内没有证券同时具备行业分类与 {S1_MIN_CLOSES} 根前复权收盘价",
            snapshot_id,
            "补齐行业分类和前复权历史窗口后再计算 S1",
        )

    try:
        signals = build_s1_signals(adjusted_closes_by_instrument=closes,
                                   industry_by_instrument=industry)
    except FactorDataError as exc:
        return [], f"S1 无法计算：{exc}"

    ranked = [
        RankedCandidate(
            instrument_id=sig.instrument_id, industry_code=sig.industry_code,
            signal_rank=sig.signal_rank,
            simulatable=_is_simulatable(boards.get(sig.instrument_id)),
            f01_rank=sig.f01_rank, f02_rank=sig.f02_rank,
            low_vol_rank=sig.low_vol_rank,
        )
        for sig in signals
    ]
    note = (f"S1 在快照内 {len(closes)} 只证券上计算；"
            f"其中可模拟（沪深主板）{sum(1 for s in ranked if s.simulatable)} 只")
    return ranked, note


def _cors_origins_from_env() -> list[str]:
    """允许跨源的来源清单，默认**为空**（即不开启 CORS）。

    开发时前端通过 Vite 代理访问同源 `/api`，因此不需要 CORS。
    但把前端与 API 分开部署（或分别指向不同端口的实例做隔离验证）时，
    浏览器会先发预检请求——那时代理帮不上忙，必须由 API 明确放行。

    用清单而不是 `*`：这个服务的写端点会改变模拟账本，
    对任意来源开放等于让任何网页都能调用它。
    """

    raw = os.environ.get("AQUANT_CORS_ORIGINS", "")
    return [o.strip() for o in raw.split(",") if o.strip()]


def create_app(state: AppState | None = None) -> FastAPI:
    app = FastAPI(title="A-Quant Lab API", version="0.1.0",
                  description="研究与模拟决策工作台。模拟账户，不连接券商。")
    app.state.aquant = state or build_state()

    cors_origins = _cors_origins_from_env()
    if cors_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "X-Aquant-Subject"],
            max_age=600,
        )

    def svc(request: Request) -> AppState:
        return request.app.state.aquant

    # ---------------------------------------------------------------- 身份
    def current_subject(request: Request,
                        x_aquant_subject: str | None = Header(default=None)) -> str:
        """当前确认主体。**来自服务端信任边界，不接受请求体自报。**

        演示实现从受信任的头部取得（真实部署应由已验证凭证/签名上下文映射，
        见 Q0 报告 §4.3：tenant 与 user 必须来自令牌，而不是请求体）。
        这里额外校验人类主体，因为冻结是用户动作（§16.3）。
        """

        subject = (x_aquant_subject or "").strip()
        if not subject:
            raise HTTPException(status_code=401, detail="missing authenticated subject")
        if not confirmer_is_human(subject):
            raise HTTPException(
                status_code=403,
                detail="plan freeze is a user action; model or system principals are refused",
            )
        return subject

    # ---------------------------------------------------------------- 错误
    def conflict(message: str, code: str = "STALE_SNAPSHOT", object_id: str = "-",
                 repair: str = "reload the authoritative state and retry") -> HTTPException:
        """API 层自身抛出的冲突也走与领域相同的错误信封，避免两种错误形状。"""

        return HTTPException(status_code=409,
                             detail={"error": {"code": code, "message": message,
                                               "object_id": object_id,
                                               "retryable": False,
                                               "repair_action": repair}})

    @app.exception_handler(PlanError)
    async def plan_error_handler(_: Request, exc: PlanError) -> JSONResponse:
        """领域错误按其语义映射 HTTP 状态，并保留 §16.4 的错误码与修复动作。"""

        status_map = {
            "INSUFFICIENT_CASH": 409,
            "STALE_SNAPSHOT": 409,
            "DECISION_CUTOFF_PASSED": 409,
            "RULE_VERSION_MISSING": 422,
            "DATA_NOT_READY": 409,
            "PIT_UNVERIFIED": 422,
        }
        return JSONResponse(status_code=status_map.get(exc.code, 400),
                            content={"error": exc.as_error()})

    @app.exception_handler(SnapshotError)
    async def snapshot_error_handler(_: Request, exc: SnapshotError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": exc.as_error()})

    @app.exception_handler(StrategyVersionError)
    async def strategy_error_handler(_: Request,
                                     exc: StrategyVersionError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": exc.as_error()})

    @app.exception_handler(ExperimentError)
    async def experiment_error_handler(_: Request, exc: ExperimentError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": exc.as_error()})

    @app.exception_handler(WorkbenchError)
    async def workbench_error_handler(_: Request, exc: WorkbenchError) -> JSONResponse:
        """自选与决策的领域错误走同一信封。

        不在路由里手工 conflict(...)：那会多嵌一层
        {"detail": {"error": ...}}，与其它领域错误的形状不同，
        调用方得为同一个错误码写两套解析逻辑。
        """

        return JSONResponse(status_code=409, content={"error": exc.as_error()})

    @app.exception_handler(FeeError)
    async def fee_error_handler(_: Request, exc: FeeError) -> JSONResponse:
        """费率问题（§12.6）。

        这条处理器是补上的：FeeError 以前从未走到过 API 边界——
        合成费率的守卫只被一个测试调用过，生产路径根本不会抛它。
        把闸门接进 preview/freeze/execute 之后，它立刻会走到这里，
        没有处理器就会变成 500，而调用方看到的是"服务端故障"，
        不是"你的费率表没有依据"。
        """

        return JSONResponse(status_code=422, content={"error": {
            "code": exc.code, "message": exc.message, "object_id": "fee_table",
            "retryable": False, "repair_action": exc.repair_action}})

    @app.exception_handler(SimError)
    async def sim_error_handler(_: Request, exc: SimError) -> JSONResponse:
        """§12.1：不支持的情形必须显式报错，不能近似处理后报告成功。

        这些错误同样走统一的信封。**不能**在路由里手工包一层
        `HTTPException(detail=...)`：那样 detail 会多嵌一层
        `{"detail": {"error": ...}}`，与领域处理器产出的形状不一致，
        调用方就得为"同一个错误码"写两套解析逻辑。
        """

        return JSONResponse(status_code=409, content={"error": exc.as_error()})

    # ---------------------------------------------------------------- 读
    @app.get("/api/v1/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/v1/status")
    def status(s: AppState = Depends(svc)) -> dict:
        """数据状态。**含新鲜度**——快照落后了没有。

        这一项容易被忽略：界面一切正常、数字自洽、对账通过，
        而它们基于三天前的数据。光看"上次跑成功"也不够——
        天天跑成功但数据源没更新，快照照样停在几天前。
        因此比较的是快照覆盖的末日与采集实际拿到的末日。
        """

        snapshot_id = s.current_snapshot()
        out = build_data_status(s.reader, snapshot_id).as_dict()
        # 快照覆盖的最后一个交易日：取自交易数据集，而不是 as_of_time
        # （as_of_time 是快照的时点，可能与行情末日不同）。
        #
        # 新鲜度是**附加信息**，不能因为它让整个状态接口 500——
        # /status 是界面顶栏的命脉，读不出留痕应当是"不判断新鲜度"，
        # 而不是"状态不可用"。
        try:
            out["freshness"] = freshness(
                daily_run_log_path(s.data_dir),
                snapshot_day=_snapshot_last_day(s)).as_dict()
        except Exception as exc:                         # noqa: BLE001
            import traceback
            print("[api] 新鲜度计算失败（不影响状态本身）：", flush=True)
            traceback.print_exc()
            out["freshness"] = {
                "stale": False, "detail": f"新鲜度不可用：{type(exc).__name__}",
            }
        return out

    @app.get("/api/v1/candidates")
    def candidates(s: AppState = Depends(svc)) -> dict:
        """候选来自**当前快照上的 S1 计算**，不是硬编码列表。

        `DEMO_CANDIDATES` 只在合成快照下使用；一旦指向真实快照，
        继续返回固定的三个演示标的，会让界面显示一份与数据无关的排名。
        """

        snapshot_id = s.current_snapshot()
        signals, note = _s1_candidates(s, snapshot_id)
        return {
            "snapshotId": snapshot_id,
            "candidates": [
                {"instrumentId": sig.instrument_id,
                 "displayName": _display_name(s, snapshot_id, sig.instrument_id),
                 "industryCode": sig.industry_code,
                 "signalRank": sig.signal_rank,
                 "simulatable": sig.simulatable}
                for sig in signals
            ],
            "note": note,
        }

    @app.get("/api/v1/instruments/{instrument_id}/research")
    def research(instrument_id: str, trading_day: date, s: AppState = Depends(svc)) -> dict:
        snapshot_id = s.current_snapshot()
        ref = s.reader.ref(snapshot_id)
        bars = s.service._bars(snapshot_id, trading_day, ref.as_of_time, [instrument_id])
        # 因子值来自**已落库的研究运行**，不在这里现算。
        #
        # 这一段此前缺失，后果是：库里有因子、卡片上永远没有。两侧各自的
        # 测试都是绿的——卡片层默认 factor_values=None 就是"没有因子"，
        # 而那看起来与"这只没有因子值"完全一样。
        factor_values, factor_note = factor_values_for_snapshot(
            s.con, snapshot_id=snapshot_id, instrument_id=instrument_id)
        try:
            card = build_research_card(
                s.reader, snapshot_id=snapshot_id, as_of=ref.as_of_time,
                instrument_id=instrument_id, trading_day=trading_day,
                board_rules=BOARD_RULES, listings=s.listings,
                bar=bars.get(instrument_id),
                factor_values=factor_values,
                factor_note=factor_note,
                # 证据来自作业的产出（§9）。**不在这里过滤 located**：
                # 卡片要如实显示"这条引用在原文里定位不到"，
                # 过滤掉等于让这类问题永远不出现在任何界面上。
                evidence=_card_evidence(s.con, instrument_id, as_of=ref.as_of_time),
                counter_evidence=_card_counter_evidence(
                    s.con, instrument_id, as_of=ref.as_of_time),
            )
        except KeyError as exc:
            # 未覆盖的证券是"查无此物"，不是服务端故障；不得让 500 掩盖它
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        body = card.as_dict()
        # 卡片按 (标的, 快照, 交易日) 冻结留档：它是"当时看到的证据"。
        # 同一快照同一天重复打开得到同一张，**不刷新生成时刻**；
        # 现算即弃的话，事后无法还原"我那天看到的是什么"。
        stored = persist_card(s.con, snapshot_id=snapshot_id,
                              trading_day=trading_day, card=body,
                              data_mode=ref.data_mode)
        # 返回体必须读留档值，而不是顺手把刚算出来的时刻放回去：
        # 那样接口看起来"每次都是新卡片"，与留档语义矛盾。
        persisted_payload = get_card_payload(s.con, stored["card_id"])
        if persisted_payload is not None:
            return persisted_payload
        # Existing databases may contain cards written before the full-payload
        # table was introduced. Keep those rows readable while all new cards use
        # the immutable complete response above.
        body["cardId"] = stored["card_id"]
        body["generatedAt"] = stored["generated_at"]
        return body

    @app.get("/api/v1/research/cards")
    def research_card_history(instrument_id: str | None = None,
                              snapshot_id: str | None = None, limit: int = 50,
                              s: AppState = Depends(svc)) -> dict:
        """已留存的研究卡片。用于回答"我那天看到的是什么"。"""

        return {"cards": research_cards(s.con, instrument_id=instrument_id,
                                        snapshot_id=snapshot_id, limit=limit)}

    @app.get("/api/v1/portfolios/{portfolio_id}/reconcile")
    def reconcile(portfolio_id: str, s: AppState = Depends(svc)) -> dict:
        return s.service.reconcile(portfolio_id=portfolio_id)

    @app.get("/api/v1/portfolios/{portfolio_id}/ledger")
    def ledger(portfolio_id: str, s: AppState = Depends(svc)) -> dict:
        """账本明细：现金分录、批次、成交、费用、应收。

        与 /reconcile 的分工：reconcile 回答"对不对"（逐项不变量），
        ledger 回答"是什么"（逐条事实）。合在一起会让"对账不通过"时
        没有逐个分录可看。
        """

        try:
            return portfolio_ledger(s.con, portfolio_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ---------------------------------------------------------------- 调度
    @app.get("/api/v1/schedule")
    def get_schedule(s: AppState = Depends(svc)) -> dict:
        """每日任务的配置与运行状态（§14.2 的调度由独立 worker 执行）。

        这里只回答"配了什么、下次什么时候、上次跑成什么样"。
        **不在 API 里执行采集**：那要跑几十秒到几分钟，会与请求争用同一个
        数据库连接，而且 API 一重启当天那次就没了（见 scheduler.py 的说明）。
        """

        return scheduler_status(s.con)

    @app.post("/api/v1/schedule")
    def put_schedule(body: ScheduleRequest,
                     subject: str = Depends(current_subject),
                     s: AppState = Depends(svc)) -> dict:
        try:
            save_schedule(
                s.con, enabled=body.enabled, run_at_local=body.run_at_local,
                weekdays_only=body.weekdays_only, interpreter=body.interpreter,
                data_dir=body.data_dir or "", window_start=body.window_start,
                actor=subject)
        except ScheduleError as exc:
            raise HTTPException(status_code=422, detail={
                "error": {"code": "SCHEDULE_INVALID", "message": exc.message,
                          "object_id": "run_schedule", "retryable": False,
                          "repair_action": exc.repair}}) from exc
        return scheduler_status(s.con)

    @app.post("/api/v1/schedule/run")
    def trigger_run(body: RunNowRequest,
                    subject: str = Depends(current_subject),
                    s: AppState = Depends(svc)) -> dict:
        """请求**立刻运行一次**流水线。

        只登记请求、不执行：执行由 worker 负责，因此"点按钮"与"到点自动跑"
        走同一条路径。重复点击不会排队成一串运行——同一时刻至多一条未完成
        的请求，第二次点击返回同一个 request_id。
        """

        request_id = request_run(
            s.con, source="manual", requested_by=subject,
            reason=(body.reason or "界面请求立刻运行"))
        return {
            "requestId": request_id,
            "note": ("已登记运行请求。执行者是独立 worker"
                     "（python tools/scheduler_worker.py）；"
                     "它会先做预检（解释器、采集缓存、数据目录）再跑流水线。"),
            **scheduler_status(s.con),
        }

    @app.get("/api/v1/events")
    def list_events(request: Request, instrument_id: str | None = None,
                    category: str | None = None, limit: int = 100,
                    s: AppState = Depends(svc)) -> dict:
        """事件列表，**按决策时点门禁过滤**。

        as_of 默认取当前快照的时点，因此不会返回"数据库里有但当时不可知"
        的事件——PIT 在读取侧的落点。
        """

        snapshot_id = s.current_snapshot()
        ref = s.reader.ref(snapshot_id)
        as_of = ref.as_of_time
        rows = events(s.con, as_of=as_of, instrument_id=instrument_id,
                      category=category, limit=max(1, min(limit, 500)))
        return {
            "snapshotId": snapshot_id,
            "asOfTime": as_of.isoformat(),
            "count": len(rows),
            "note": ("已按 available_at <= as_of 过滤；"
                     "带 located 引用的才算证据"),
            "events": rows,
        }

    @app.get("/api/v1/readiness")
    def readiness(s: AppState = Depends(svc)) -> dict:
        """就绪状态：数据、账本、任务三个维度分开报。"""

        status = build_data_status(s.reader, s.current_snapshot()).as_dict()
        jobs = JobStore(s.con).counts_by_status()
        return {
            "ready": status.get("readiness") == "READY",
            "data": {
                "snapshotId": status.get("snapshotId"),
                "dataMode": status.get("dataMode"),
                "readiness": status.get("readiness"),
                "readinessLabel": status.get("readinessLabel"),
                "qualityStatus": status.get("qualityStatus"),
                "blockingIssues": status.get("blockingIssues") or [],
            },
            "jobs": jobs,
            "note": ("就绪是**分维度**的：数据就绪不代表任务积压已清空，"
                     "反之亦然"),
        }

    @app.get("/api/v1/jobs/{job_id}")
    def job_detail(job_id: str, s: AppState = Depends(svc)) -> dict:
        try:
            job = JobStore(s.con).get(job_id)
        except JobError as exc:
            raise HTTPException(status_code=404, detail=exc.message) from exc
        return _job_view(job)

    @app.get("/api/v1/jobs")
    def job_list(s: AppState = Depends(svc)) -> dict:
        store = JobStore(s.con)
        return {"counts": store.counts_by_status()}

    # ------------------------------------------------------- 自选与决策
    @app.get("/api/v1/watchlist")
    def list_watchlist(subject: str = Depends(current_subject),
                       s: AppState = Depends(svc)) -> dict:
        items = watchlist(s.con, subject_id=subject)
        return {"subjectId": subject, "count": len(items), "items": items,
                "note": "自选不产生订单，也不影响模拟持仓"}

    @app.post("/api/v1/watchlist/items")
    def add_watch(body: WatchRequest, subject: str = Depends(current_subject),
                  s: AppState = Depends(svc)) -> dict:
        return add_watchlist_item(s.con, subject_id=subject,
                                  instrument_id=body.instrument_id,
                                  note=body.note)

    @app.delete("/api/v1/watchlist/items/{instrument_id}")
    def remove_watch(instrument_id: str, subject: str = Depends(current_subject),
                     s: AppState = Depends(svc)) -> dict:
        return remove_watchlist_item(s.con, subject_id=subject,
                                     instrument_id=instrument_id)

    # -------------------------------------------------------- 策略版本
    @app.get("/api/v1/strategy-versions")
    def list_strategy_versions(s: AppState = Depends(svc)) -> dict:
        return {"strategyVersions": strategy_versions(s.con),
                "featureVersions": [
                    {"featureVersion": k, **v}
                    for k, v in KNOWN_FEATURE_SPECS.items()
                ],
                "note": ("策略版本不可改：变更须注册新版本名，"
                         "否则已登记的实验会指向从未跑过的参数")}

    @app.post("/api/v1/strategy-versions")
    def create_strategy_version(body: StrategyVersionRequest,
                                subject: str = Depends(current_subject),
                                s: AppState = Depends(svc)) -> dict:
        return ensure_strategy_version(
            s.con, strategy_version=body.strategy_version, family=body.family,
            spec=body.spec, parent_version=body.parent_version,
            notes=body.notes)

    # ------------------------------------------------------------ 实验
    @app.get("/api/v1/experiments")
    def list_experiments(limit: int = 100, s: AppState = Depends(svc)) -> dict:
        rows = experiments(s.con, limit=max(1, min(limit, 500)))
        return {"count": len(rows), "experiments": rows,
                "note": ("失败与负收益实验同样保留；"
                         "test_set_access_count > 1 表示测试集已被用于选择方案")}

    @app.post("/api/v1/experiments")
    def create_experiment(body: ExperimentRequest,
                          subject: str = Depends(current_subject),
                          s: AppState = Depends(svc)) -> dict:
        """登记实验。**先登记、后看结果**（§13.2）。

        这个端点不接收任何结果字段——登记发生在看结果之前。
        改变任一输入会产生新实验，旧记录原样保留。
        """

        return register_experiment(s.con, body.to_spec())

    @app.get("/api/v1/experiments/{experiment_id}")
    def get_experiment(experiment_id: str, s: AppState = Depends(svc)) -> dict:
        return experiment(s.con, experiment_id)

    @app.post("/api/v1/experiments/{experiment_id}/outcome")
    def close_experiment(experiment_id: str, body: OutcomeRequest,
                         subject: str = Depends(current_subject),
                         s: AppState = Depends(svc)) -> dict:
        """记录结论。失败与负收益同样保存（§13.2）。"""

        return record_outcome(s.con, experiment_id, status=body.status,
                              outcome_notes=body.outcome_notes)

    @app.post("/api/v1/experiments/{experiment_id}/test-set-access")
    def touch_test_set(experiment_id: str, body: TestSetAccessRequest,
                       subject: str = Depends(current_subject),
                       s: AppState = Depends(svc)) -> dict:
        """记录一次测试集访问并计数（§13.2）。"""

        return note_test_set_access(s.con, experiment_id, reason=body.reason)

    # ------------------------------------------------------ 因子与研究运行
    @app.post("/api/v1/research/runs")
    def run_factors(body: ResearchRunRequest,
                    subject: str = Depends(current_subject),
                    s: AppState = Depends(svc)) -> dict:
        """在当前快照上计算 F10 并落库（§10.2）。

        横截面排名**在每个因子内部**计算，且只对有值的标的算——
        把"算不出"的也放进排名等于给缺失值一个名次，
        那是最隐蔽的一种 0 填充。

        财报可用性由 PIT 闸门保证：只使用决策时点前已公布的财报。
        """

        snapshot_id = s.current_snapshot()
        ref = s.reader.ref(snapshot_id)
        return compute_f10_for_snapshot(
            con=s.con, reader=s.reader, snapshot_id=snapshot_id,
            as_of=ref.as_of_time, limit=body.limit)

    @app.get("/api/v1/research/runs/{research_run_id}/factors")
    def get_factor_values(research_run_id: str, factor_id: str | None = None,
                          s: AppState = Depends(svc)) -> dict:
        rows = factor_values(s.con, research_run_id=research_run_id,
                             factor_id=factor_id)
        return {"researchRunId": research_run_id, "count": len(rows),
                "factors": rows}

    @app.get("/api/v1/decisions")
    def list_decisions(portfolio_id: str | None = None, limit: int = 100,
                       s: AppState = Depends(svc)) -> dict:
        rows = decisions(s.con, portfolio_id=portfolio_id,
                         limit=max(1, min(limit, 500)))
        return {"count": len(rows), "decisions": rows,
                "note": ("模型原方案与人工方案分开保存，差异由服务端算；"
                         "external_information_used 会影响可复现性")}

    @app.post("/api/v1/decisions")
    def create_decision(body: DecisionRequest,
                        subject: str = Depends(current_subject),
                        s: AppState = Depends(svc)) -> dict:
        """记录一次决策。**模型方案与人工方案分开提交**（§11.3）。"""

        return record_decision(
            s.con, portfolio_id=body.portfolio_id,
            snapshot_id=body.snapshot_id, decision_type=body.decision_type,
            plan_id=body.plan_id, model_proposed=body.model_proposed,
            human_final=body.human_final,
            reason_category=body.reason_category, reason_note=body.reason_note,
            external_information_used=body.external_information_used,
            rule_check=body.rule_check)

    # ---------------------------------------------------------------- 助手
    @app.post("/api/v1/assistant/messages")
    def assistant_message(body: AssistantMessageRequest,
                          subject: str = Depends(current_subject),
                          s: AppState = Depends(svc)) -> dict:
        """把材料交给模型作答。**材料必须先过外发闸门**（§17.2）。

        接口刻意不接受"模型名"或"是否跳过检查"这类参数：
        能选的只有材料与用途。放行与否由权利登记表决定——
        那是一个有记录、可复核的动作，不是一次参数调用。
        """

        # §5.5：助手在回答系统状态前**先读已发布快照**。
        # ref() 会在快照未发布或读不出时直接抛错——那时不该继续调用模型：
        # 一份不知道时点的回答，看起来完整但无法判断是否已过期。
        snapshot_id = s.current_snapshot()
        ref = s.reader.ref(snapshot_id)
        # 草稿先算（只算不写），再连同材料一起交给助手。
        # 顺序如此是因为"助手看到的草稿"必须与返回给界面的**同一份**，
        # 各算一次会得到两个版本，而用户只看到其中一个。
        draft = None
        if body.draft is not None:
            draft = _assistant_draft(s, body.draft)
        try:
            answer = ask_assistant(
                s.con, s.provider(),
                materials=[Material(source_id=m.source_id, text=m.text,
                                    contains_personal_data=m.contains_personal_data)
                           for m in body.materials],
                purpose=body.purpose, data_mode=ref.data_mode,
                max_output_tokens=body.max_output_tokens,
                snapshot_context={
                    "snapshotId": snapshot_id,
                    "dataMode": ref.data_mode,
                    "asOfTime": ref.as_of_time.isoformat(),
                })
            if draft is not None:
                answer["draft"] = draft
                answer["draftNote"] = (
                    "草稿仅为预览：未写计划、未写账本。冻结必须由界面上的"
                    "显式确认触发，对话里的一句同意不构成授权（§5.5）。")
            return answer
        except AssistantError as exc:
            # 模型不可用是**上游依赖不可用**，不是客户端的错，也不是服务端 bug：
            # 用 503 让调用方知道"稍后重试可能有用"，而不是 500。
            status = 503 if exc.code == "DATA_NOT_READY" else 403
            detail = {"code": exc.code, "message": exc.message,
                      "repair_action": exc.repair_action}
            if exc.blockers:
                detail["blockers"] = exc.blockers
            raise HTTPException(status_code=status, detail=detail) from exc

    @app.get("/api/v1/fees")
    def fee_status(s: AppState = Depends(svc)) -> dict:
        """当前费率表的口径与出处（§12.6）。

        界面与报告要能回答"这个盈亏是按谁的费率算的"。缺了它，
        使用者只看到金额，看不到金额背后的假设。
        """

        snapshot_id = s.current_snapshot()
        sched = s.fees.schedule_for(date.today())
        return {
            "snapshotId": snapshot_id,
            "feeVersion": sched.fee_version,
            "syntheticTestRate": s.fees.is_synthetic,
            "commissionSource": s.fees.commission_source,
            "commissionRate": str(sched.commission_rate),
            "commissionMinCents": sched.commission_min_cents,
            "stampDutyRateSell": str(sched.stamp_duty_rate_sell),
            "transferFeeRate": str(sched.transfer_fee_rate),
            "provenance": s.fee_provenance,
            "note": ("syntheticTestRate=true 表示这张表**不得**用于真实数据；"
                     "commissionSource=UNCONFIGURED_DEFAULT 表示佣金是示例值，"
                     "是一个假设而不是你的券商费率。"),
        }

    @app.get("/api/v1/instruments/{instrument_id}/evidence")
    def instrument_evidence(instrument_id: str, located_only: bool = False,
                            s: AppState = Depends(svc)) -> dict:
        """某只标的已落库的证据与引用（§9、§15.3）。

        `located_only=true` 只返回能在原文里定位到的引用。
        默认**返回全部**：不可定位的引用是"模型引用了原文里没有的话"
        这一事实，默认藏起来会让它永远不会被看到。
        """

        snapshot_id = s.current_snapshot()
        ref = s.reader.ref(snapshot_id)
        rows = evidence_for(s.con, instrument_id=instrument_id,
                            located_only=located_only, as_of=ref.as_of_time)
        return {"instrumentId": instrument_id, "count": len(rows),
                "evidence": rows,
                "snapshotId": snapshot_id,
                "asOfTime": ref.as_of_time.isoformat(),
                "note": ("located=false 表示该引用在来源正文里**定位不到**；"
                         "locator_kind 区分逐字命中与只差空白两档。")}

    @app.get("/api/v1/assistant/calls")
    def list_model_calls(limit: int = 100, s: AppState = Depends(svc)) -> dict:
        """模型调用留档。**失败的也在里面**——否则预算与责任都无从核对。"""

        rows = model_calls(s.con, limit=max(1, min(limit, 500)))
        return {"count": len(rows), "calls": rows,
                "note": ("被拒（REJECTED）与出错（ERROR）同样留档；"
                         "contract_version 列存的是本次判定细节")}

    # ---------------------------------------------------------------- 作业
    @app.post("/api/v1/research/jobs")
    def submit_research_job_route(body: ResearchJobRequest,
                                  subject: str = Depends(current_subject),
                                  s: AppState = Depends(svc)) -> dict:
        """提交一条研究作业。**重复提交返回同一条**（§8.4）。

        接口只入队、不执行：执行由 worker 领取（§14.2 独立 worker +
        数据库任务租约，不引入消息中间件）。
        """

        try:
            return submit_research_job(
                s.con, job_type=body.job_type,
                trading_day=body.trading_day.isoformat(),
                snapshot_id=body.snapshot_id,
                config_version=body.config_version,
                payload=body.payload or None)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/research/jobs/{job_id}/run")
    def run_research_job_route(job_id: str, subject: str = Depends(current_subject),
                               s: AppState = Depends(svc)) -> dict:
        """就地执行一条待执行作业。

        **这是开发与验收用的便利入口，不是生产调度路径**：生产由一个
        独立的 worker 进程反复调用 operations.research_jobs.run_pending()。
        两条路径共用同一个执行器，因此这里跑通的行为与 worker 里一致。
        """

        jobs = JobStore(s.con)
        try:
            job = jobs.get(job_id)
        except JobError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        done = run_research_job(s.con, s.reader, job_id=job_id,
                                worker_id=f"api:{subject}")
        return done

    # ---------------------------------------------------------------- 写
    @app.post("/api/v1/plans/preview")
    def preview(body: PreviewRequest, s: AppState = Depends(svc)) -> dict:
        """只算不冻。不写计划、不写账本（A08）。"""

        current_id = s.current_snapshot()
        decision_id = body.decision_snapshot_id or body.snapshot_id
        explicit_timing = any((body.decision_snapshot_id,
                               body.decision_cutoff_at,
                               body.execution_snapshot_id))
        if not explicit_timing and body.snapshot_id != current_id:
            raise HTTPException(status_code=404, detail=f"unknown snapshot {body.snapshot_id!r}")
        s.store.require_published(decision_id)
        if body.execution_snapshot_id:
            s.store.require_published(body.execution_snapshot_id)
        ref = s.reader.ref(decision_id)

        # 账户必须先存在，否则预览记录的 account_version 会基于"空账本"，
        # 而随后的令牌签发会先建账户再读账本，两者版本不一致，冻结永远失败。
        # 因此这里先幂等建账，再统一从账本读取权威状态。
        opening = body.cash_available_cents or 100_000_000
        # 预览只要求账户存在，不声明账户状态，因此不做账本比对
        # （比对发生在冻结与执行，那时调用方必须与账本一致）。
        s.service._ensure_account(body.portfolio_id, initial_cash_cents=opening,
                                  initial_lots=[], now=datetime.now(timezone.utc),
                                  enforce_match=False)
        ledger_cash = s.service._ledger_cash(body.portfolio_id)
        ledger_lots = s.service._load_lots(body.portfolio_id)
        if body.cash_available_cents is not None and body.cash_available_cents != ledger_cash:
            raise conflict(
                "supplied cash differs from the ledger; account state must come from the server",
                repair="do not send cash; the server reads it from the ledger",
            )
        cash = ledger_cash

        # 候选只从显式决策快照计算；执行快照只能提供交易日行情，不能反向
        # 改写候选。决策快照不是 current 时，板块映射也必须随它切换。
        ranked, _ = _s1_candidates(s, decision_id)
        decision_listings = (dict(LISTINGS) if decision_id == SNAPSHOT_ID
                             else _listings_for(s, decision_id))
        plan_service = (s.service if decision_id == current_id else PlanService(
            s.con, s.reader, s.fees, BOARD_RULES, decision_listings, s.params))
        pv = plan_service.preview(
            portfolio_id=body.portfolio_id, snapshot_id=decision_id,
            trading_day=body.trading_day, as_of=ref.as_of_time,
            candidates=[Candidate(r.instrument_id, r.industry_code, r.signal_rank,
                                  simulatable=r.simulatable) for r in ranked],
            cash_available_cents=cash,
            lots=ledger_lots, confirm_subject="server:preview",
            decision_snapshot_id=body.decision_snapshot_id,
            decision_cutoff_at=body.decision_cutoff_at,
            execution_snapshot_id=body.execution_snapshot_id,
        )
        # 服务端保留本次预览：令牌签发与冻结都必须针对同一个预览，
        # 否则"绑定预览哈希"就无从谈起。
        s.previews[pv.plan_id] = pv
        out = pv.as_dict()
        # 领域契约用 snake_case；这里补 camelCase 呈现别名供前端使用。
        # 领域对象保持干净，改在 API 层做呈现映射（§14.1）。
        out["planId"] = pv.plan_id
        out["decisionSnapshotId"] = pv.decision_snapshot_id
        out["decisionCutoffAt"] = (pv.decision_cutoff_at.isoformat()
                                   if pv.decision_cutoff_at else None)
        out["executionSnapshotId"] = pv.execution_snapshot_id
        out["executionCutoffAt"] = (pv.execution_cutoff_at.isoformat()
                                    if pv.execution_cutoff_at else None)
        out["estimatedFeesCents"] = pv.estimated_fees_cents
        out["frozenLabel"] = ("未冻结 · 预览不产生成交" if not pv.frozen
                              else "已冻结")
        # 执行后可用现金：**服务端算**，不让前端拿预览里的数字自行推算。
        # 这不是洁癖——前端算第二遍就会出现"界面上的数字与账本不一样"
        # 这种最难查的问题，而且它违反项目自己的原则：
        # 只读夹具里恰好有这个字段，真实响应里却没有，于是界面会显示
        # 一个来自夹具的数字来填空。
        out["cashAfterCents"] = pv.cash_after_cents
        # 行业分布同样由**服务端**给出。
        #
        # 这一块原先在服务端态下仍然显示随前端分发的夹具数值——
        # 我把订单表改成服务端来源时漏了它，而它和订单表在同一个面板里。
        # 教训：改"这一块显示哪份数据"时，必须把同一面板里**所有**
        # 数字过一遍，漏掉的那块会以"看起来正常"的方式继续显示旧数据。
        # 权益口径用"执行后的组合价值"：现金 + 买入成交额 + 卖出成交额。
        # 它与构建目标时的 equity 是同一个量（组合总市值），
        # 因此行业占比与构建期的约束口径一致。
        equity_cents = cash + sum(
            int(o["quantity"] * o["price_cents"]) for o in pv.orders
            if o["side"] == "BUY") + sum(
            int(o["quantity"] * o["price_cents"]) for o in pv.orders
            if o["side"] == "SELL")
        industry_value: dict[str, int] = {}
        for t in pv.targets:
            value = int(Decimal(equity_cents) * t.weight_pct / Decimal(100))
            code = t.industry_code or "UNKNOWN"
            industry_value[code] = industry_value.get(code, 0) + value
        cap = s.params.max_single_industry_pct
        out["industry"] = [
            {"industryCode": code, "valueCents": value,
             "sharePct": (str((Decimal(value) * 100 / equity_cents).quantize(
                 Decimal("0.01"))) if equity_cents else None),
             "overCap": (Decimal(value) * 100 / equity_cents) > cap
                        if equity_cents else False}
            for code, value in sorted(industry_value.items())
        ]
        out["industryCapPct"] = str(cap)
        return out

    @app.post("/api/v1/plans/{plan_id}/confirmation")
    def issue_confirmation(plan_id: str, subject: str = Depends(current_subject),
                           s: AppState = Depends(svc)) -> dict:
        """签发一次性确认令牌。

        令牌绑定主体、计划、组合、快照、账户版本与完整预览哈希，
        由服务端生成并在冻结时消费（见 plan.py 的 freeze）。
        """

        pv = s.previews.get(plan_id)
        if pv is None:
            raise HTTPException(
                status_code=404,
                detail="no live preview for this plan; preview again before confirming",
            )
        token = s.service.issue_confirmation(
            preview=pv, subject=subject,
            current_lots=s.service._load_lots(pv.portfolio_id),
            current_cash_cents=s.service._ledger_cash(pv.portfolio_id),
        )
        return {
            "planId": plan_id,
            "confirmationToken": token,
            "subject": subject,
            "note": "single use; bound to this exact preview; expiry enforced on freeze",
        }

    @app.post("/api/v1/plans/{plan_id}/freeze")
    def freeze(plan_id: str, body: FreezeRequest,
               subject: str = Depends(current_subject), s: AppState = Depends(svc)) -> dict:
        """冻结计划。五项复核由领域层执行，本层不重复实现。"""

        if body.plan_id != plan_id:
            raise HTTPException(status_code=400, detail="plan_id mismatch between path and body")
        pv = s.previews.get(plan_id)
        if pv is None:
            raise HTTPException(status_code=404,
                                detail="no live preview for this plan; preview again")
        # 账户状态由服务端读取，客户端无法谎报（A09）
        result = s.service.freeze(
            preview=pv, confirm_subject=subject,
            confirmation_token=body.confirmation_token,
            expected_account_version=pv.account_version,
            current_lots=s.service._load_lots(pv.portfolio_id),
            current_cash_cents=s.service._ledger_cash(pv.portfolio_id),
        )
        return result

    @app.post("/api/v1/plans/{plan_id}/execute")
    def execute(plan_id: str, body: ExecuteRequest, subject: str = Depends(current_subject),
                s: AppState = Depends(svc)) -> dict:
        if body.plan_id != plan_id:
            raise HTTPException(status_code=400, detail="plan_id mismatch between path and body")
        if s.service.load_persisted_plan(plan_id) is None:
            raise HTTPException(status_code=404, detail=f"unknown plan {plan_id!r}")
        # 构造 CashDividend 时就会校验日期顺序与股利符号；不合法即抛
        # SimError，由上面的处理器统一转成 409 错误信封。
        actions = [
            CashDividend(
                action_id=a.action_id, instrument_id=a.instrument_id,
                record_date=a.record_date, ex_date=a.ex_date, pay_date=a.pay_date,
                # 微元优先：按分传递会把 28.02423 元截成 28.02 元，
                # 1000 股就少派 2,242.30 元，而账面完全自洽。
                cash_per_share_micros=a.cash_per_share_micros or 0,
                cash_per_share_cents_input=a.cash_per_share_cents,
                tax_treatment=a.tax_treatment,
            )
            for a in body.corporate_actions
        ]
        # 冻结计划的组合、快照、订单与状态都已持久化。执行必须从这些
        # 数据恢复，不能要求重启后的 AppState 仍持有未冻结的预览对象。
        # PlanService 还会用 simulation_plan.confirmed_by 校验主体，并从
        # 该计划绑定的组合账本加载现金与批次。
        out = s.service.execute(plan_id=plan_id, subject=subject,
                                corporate_actions=actions)
        return out

    @app.post("/api/v1/valuations")
    def value(body: ValueRequest, s: AppState = Depends(svc)) -> dict:
        snapshot_id = s.current_snapshot()
        ref = s.reader.ref(snapshot_id)
        lots = s.service._load_lots(body.portfolio_id)
        return s.service.value(
            portfolio_id=body.portfolio_id, snapshot_id=snapshot_id,
            trading_day=body.trading_day, as_of=ref.as_of_time,
            lots=lots, cash_available_cents=s.service._ledger_cash(body.portfolio_id),
        )

    # 静态挂载必须放在**最后**：它挂在 "/" 上，是个 catch-all，
    # 在它之后注册的路由不会被匹配到（FastAPI 先看显式路由，
    # 但 Mount 一旦先注册就会吃下所有未匹配路径）。
    # 测试里动态添加的探针路由正是这样被截走过一次。
    _mount_web(app)
    return app


def _mount_web(app: FastAPI) -> None:
    """把前端构建产物挂到同源根路径（部署用）。

    为什么在 API 进程里挂静态文件，而不是再起一个 nginx：

      * 前端用**相对路径**请求 /api，同源即可，因此不需要 CORS 配置，
        也不需要反向代理；
      * 试运行的目的是"一个容器起来就能看"，多一个进程就多一处会配错的地方。

    生产形态仍然是分开部署（ADR-013）：那时静态文件由 CDN/nginx 提供，
    API 只回答 /api。因此这里**不**做任何只有这一种部署才成立的事。

    产物不存在时**不挂载**，也不报错：开发时前端跑在 Vite 开发服务器上，
    API 只需要回答 /api。挂载一个空目录会让所有未知路径返回 404 页面，
    把"前端没构建"这件事伪装成"路由不存在"。
    """

    dist = ROOT / "apps" / "web" / "dist"
    index = dist / "index.html"
    if not index.exists():
        print(f"[api] 未找到前端产物 {index}；只提供 /api（开发时的正常状态）")
        return

    # 挂载顺序有讲究：/api 的路由先注册，因此 StaticFiles 挂在 "/" 上
    # 不会截走它们（FastAPI 按注册顺序匹配）。
    app.mount("/", StaticFiles(directory=str(dist), html=True), name="web")
    print(f"[api] 已挂载前端产物 {dist}")


app = create_app()
