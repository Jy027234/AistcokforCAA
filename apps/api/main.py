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
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from aquant.application.workspace_view import build_data_status, build_research_card
from aquant.domain.data.db import apply_migrations, connect
from aquant.domain.data.ingest import SnapshotBuilder
from aquant.domain.data.reader import SnapshotReader
from aquant.domain.data.snapshot import SnapshotError, SnapshotStore
from aquant.domain.portfolio.construction import Candidate, ConstructionParams
from aquant.domain.portfolio.plan import PlanError, PlanService, confirmer_is_human
from aquant.domain.simulation.corporate_actions import CashDividend
from aquant.domain.simulation.fees import synthetic_fee_table
from aquant.domain.simulation.simulator import Bar, BoardRule, SimError

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

BOARD_RULES = [
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

    def __init__(self, con: sqlite3.Connection, root: Path) -> None:
        self.con = con
        self.root = root
        self.store = SnapshotStore(con, root)
        self.reader = SnapshotReader(self.store)
        self.snapshot_id = active_snapshot()
        self.fees = synthetic_fee_table()
        # 组合参数与研究配置保持一致（单票 10% / 行业 30%）。持仓上限取 4：
        # 研究池有 24 只（其中 18 只沪深主板可模拟），因此上限确实是紧的——
        # 池子只够填满上限时，"上限"就测不出任何东西。
        self.params = ConstructionParams(
            max_holdings=4, max_single_name_pct=Decimal("10"),
            max_single_industry_pct=Decimal("30"),
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


def active_snapshot() -> str:
    """当前生效的快照 ID。

    `AQUANT_SNAPSHOT_ID` 可以指向**已经发布过**的快照（例如真实数据快照
    `snap-real-61d`），使得同一套 API 与界面既能跑合成验收、也能跑真实验收，
    不必为真实数据再写一个服务。默认仍是合成快照。
    """

    return os.environ.get("AQUANT_SNAPSHOT_ID", "").strip() or SNAPSHOT_ID


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

    wanted = active_snapshot()
    try:
        # require_published 同时校验状态：只认已发布快照，
        # 半成品快照不能拿来跑决策（否则会读到一份"看起来有数据"的空壳）。
        store.require_published(wanted)
        print(f"[api] 使用已发布的快照 {wanted}")
    except SnapshotError:
        if wanted != SNAPSHOT_ID:
            raise RuntimeError(
                f"AQUANT_SNAPSHOT_ID={wanted} 指向的快照不存在或未发布；"
                "先用对应脚本建成并发布该快照，或去掉这个环境变量使用合成快照"
            ) from None
        _seed_synthetic(con, root)
    return AppState(con, root)


# ======================================================================
# 请求 / 响应模型
# ======================================================================
class PreviewRequest(BaseModel):
    portfolio_id: str = Field(min_length=1, max_length=64)
    snapshot_id: str = Field(default_factory=active_snapshot)
    trading_day: date
    cash_available_cents: int | None = Field(
        default=None, ge=0,
        description="留空则由服务端从账本读取；提供时若与账本不符将被拒绝",
    )


class FreezeRequest(BaseModel):
    plan_id: str = Field(min_length=1, max_length=64)
    confirmation_token: str = Field(min_length=8, max_length=512)


class DividendInput(BaseModel):
    """当日落在除权日或到账日的现金分红。"""

    action_id: str = Field(min_length=1, max_length=64)
    instrument_id: str = Field(min_length=1, max_length=64)
    record_date: date
    ex_date: date
    pay_date: date
    cash_per_share_cents: int = Field(gt=0)
    #: §12.6 未实现完整红利税处理前必须标注口径，默认税前
    tax_treatment: str = Field(default="PRE_TAX", pattern="^(PRE_TAX|CONSERVATIVE|VERIFIED)$")


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
    for inst in state.reader.instruments(snapshot_id, as_of=ref.as_of_time):
        iid = inst.get("instrument_id")
        code = inst.get("industry_code")
        # 没有权威行业分类就不参与横截面排名：S1 的排名是"同行业可比"，
        # 行业缺失时把它塞进某个行业会污染整个横截面。
        if not iid or not code:
            continue
        rows = state.reader.daily_quotes(snapshot_id, as_of=ref.as_of_time,
                                         instrument_id=iid)
        prices = [r.close_cents for r in rows]
        if len(prices) < S1_MIN_CLOSES:
            continue
        closes[iid] = prices
        industry[iid] = code
        boards[iid] = (inst.get("board") or "").upper()

    if not closes:
        return [], (f"快照内没有任何证券同时具备行业分类与 {S1_MIN_CLOSES} 根收盘价，"
                    "因此不产出候选")

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
        return build_data_status(s.reader, active_snapshot()).as_dict()

    @app.get("/api/v1/candidates")
    def candidates(s: AppState = Depends(svc)) -> dict:
        """候选来自**当前快照上的 S1 计算**，不是硬编码列表。

        `DEMO_CANDIDATES` 只在合成快照下使用；一旦指向真实快照，
        继续返回固定的三个演示标的，会让界面显示一份与数据无关的排名。
        """

        signals, note = _s1_candidates(s, active_snapshot())
        return {
            "snapshotId": active_snapshot(),
            "candidates": [
                {"instrumentId": sig.instrument_id,
                 "displayName": _display_name(s, active_snapshot(), sig.instrument_id),
                 "industryCode": sig.industry_code,
                 "signalRank": sig.signal_rank,
                 "simulatable": sig.simulatable}
                for sig in signals
            ],
            "note": note,
        }

    @app.get("/api/v1/instruments/{instrument_id}/research")
    def research(instrument_id: str, trading_day: date, s: AppState = Depends(svc)) -> dict:
        ref = s.reader.ref(active_snapshot())
        bars = s.service._bars(active_snapshot(), trading_day, ref.as_of_time, [instrument_id])
        try:
            card = build_research_card(
                s.reader, snapshot_id=active_snapshot(), as_of=ref.as_of_time,
                instrument_id=instrument_id, trading_day=trading_day,
                board_rules=BOARD_RULES, listings=LISTINGS,
                bar=bars.get(instrument_id),
            )
        except KeyError as exc:
            # 未覆盖的证券是"查无此物"，不是服务端故障；不得让 500 掩盖它
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return card.as_dict()

    @app.get("/api/v1/portfolios/{portfolio_id}/reconcile")
    def reconcile(portfolio_id: str, s: AppState = Depends(svc)) -> dict:
        return s.service.reconcile(portfolio_id=portfolio_id)

    # ---------------------------------------------------------------- 写
    @app.post("/api/v1/plans/preview")
    def preview(body: PreviewRequest, s: AppState = Depends(svc)) -> dict:
        """只算不冻。不写计划、不写账本（A08）。"""

        if body.snapshot_id != active_snapshot():
            raise HTTPException(status_code=404, detail=f"unknown snapshot {body.snapshot_id!r}")
        ref = s.reader.ref(active_snapshot())

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

        # 候选必须与当前快照一致：预览写进计划的目标权重会冻结，
        # 用另一份快照的排名去建仓，冻结的就是一个无据可查的组合。
        ranked, _ = _s1_candidates(s, active_snapshot())
        pv = s.service.preview(
            portfolio_id=body.portfolio_id, snapshot_id=active_snapshot(),
            trading_day=body.trading_day, as_of=ref.as_of_time,
            candidates=[Candidate(r.instrument_id, r.industry_code, r.signal_rank,
                                  simulatable=r.simulatable) for r in ranked],
            cash_available_cents=cash,
            lots=ledger_lots, confirm_subject="server:preview",
        )
        # 服务端保留本次预览：令牌签发与冻结都必须针对同一个预览，
        # 否则"绑定预览哈希"就无从谈起。
        s.previews[pv.plan_id] = pv
        out = pv.as_dict()
        # 领域契约用 snake_case；这里补 camelCase 呈现别名供前端使用。
        # 领域对象保持干净，改在 API 层做呈现映射（§14.1）。
        out["planId"] = pv.plan_id
        out["estimatedFeesCents"] = pv.estimated_fees_cents
        out["frozenLabel"] = ("未冻结 · 预览不产生成交" if not pv.frozen
                              else "已冻结")
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
        pv = s.previews.get(plan_id)
        if pv is None:
            raise HTTPException(status_code=404, detail="no live preview for this plan")
        lots = s.service._load_lots(pv.portfolio_id)
        # 构造 CashDividend 时就会校验日期顺序与股利符号；不合法即抛
        # SimError，由上面的处理器统一转成 409 错误信封。
        actions = [
            CashDividend(
                action_id=a.action_id, instrument_id=a.instrument_id,
                record_date=a.record_date, ex_date=a.ex_date, pay_date=a.pay_date,
                cash_per_share_cents_input=a.cash_per_share_cents,
                tax_treatment=a.tax_treatment,
            )
            for a in body.corporate_actions
        ]
        out = s.service.execute(plan_id=plan_id, lots=lots,
                                cash_available_cents=s.service._ledger_cash(pv.portfolio_id),
                                corporate_actions=actions)
        return out

    @app.post("/api/v1/valuations")
    def value(body: ValueRequest, s: AppState = Depends(svc)) -> dict:
        ref = s.reader.ref(active_snapshot())
        lots = s.service._load_lots(body.portfolio_id)
        return s.service.value(
            portfolio_id=body.portfolio_id, snapshot_id=active_snapshot(),
            trading_day=body.trading_day, as_of=ref.as_of_time,
            lots=lots, cash_available_cents=s.service._ledger_cash(body.portfolio_id),
        )

    return app


app = create_app()
