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

import sqlite3
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
from aquant.domain.simulation.fees import synthetic_fee_table
from aquant.domain.simulation.simulator import Bar, BoardRule

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_ID = "snap-syn-001"

#: 演示用行情与规则。真实部署应由 M1 存储与规则表提供。
LISTINGS: dict[str, tuple[str, str]] = {
    "SYN.A.600519": ("SSE", "MAIN"),
    "SYN.A.000001": ("SZSE", "MAIN"),
    "SYN.A.600003": ("SSE", "MAIN"),
    "SYN.A.600002": ("SSE", "MAIN"),
    "SYN.A.300001": ("SZSE", "GEM"),
}

BOARD_RULES = [
    BoardRule(exchange="SSE", board="MAIN", price_limit_pct=Decimal("10"),
              lot_size=100, effective_from=date(2026, 7, 6)),
    BoardRule(exchange="SZSE", board="MAIN", price_limit_pct=Decimal("10"),
              lot_size=100, effective_from=date(2026, 7, 6)),
]

#: 演示候选。真实部署应由 S1 在全池上产生。
DEMO_CANDIDATES = [
    Candidate("SYN.A.600519", "SW_SYN_01", 0.90),
    Candidate("SYN.A.000001", "SW_SYN_02", 0.70),
    Candidate("SYN.A.600003", "SW_SYN_05", 0.55),
]


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
        self.fees = synthetic_fee_table()
        self.params = ConstructionParams(
            max_holdings=4, max_single_name_pct=Decimal("20"),
            max_single_industry_pct=Decimal("50"),
        )
        self.service = PlanService(con, self.reader, self.fees, BOARD_RULES,
                                   LISTINGS, self.params)
        #: plan_id -> 最近一次预览。确认令牌与冻结都必须针对**同一个预览**，
        #: 因此服务端要保留它；进程重启后预览失效，必须重新预览（这是正确行为）。
        self.previews: dict[str, Any] = {}


def build_state(data_dir: Path | None = None) -> AppState:
    """构建状态。默认在临时目录自举一份合成快照，使 API 可独立运行。"""

    import sys
    import tempfile

    if data_dir is None:
        data_dir = Path(tempfile.mkdtemp(prefix="aquant-api-"))
    data_dir.mkdir(parents=True, exist_ok=True)
    # FastAPI 把同步端点放到线程池执行，因此复用同一连接时必须显式允许跨线程，
    # 由 db.write_tx 的连接锁串行化写事务（否则读-改-写会互相覆盖）。
    con = connect(data_dir / "meta.sqlite", allow_thread_sharing=True)
    apply_migrations(con)

    root = data_dir / "api"
    root.mkdir(exist_ok=True)
    builder = SnapshotBuilder(con, root / "datasets")
    # 复用资料包自带的合成样例，避免为此再维护一份数据
    sys.path.insert(0, str(ROOT / "tests" / "integration"))
    from test_m1_ingest_e2e import build_snapshot  # noqa: PLC0415

    store = SnapshotStore(con, root)
    build_snapshot(con, builder, store)
    return AppState(con, root)


# ======================================================================
# 请求 / 响应模型
# ======================================================================
class PreviewRequest(BaseModel):
    portfolio_id: str = Field(min_length=1, max_length=64)
    snapshot_id: str = SNAPSHOT_ID
    trading_day: date
    cash_available_cents: int | None = Field(
        default=None, ge=0,
        description="留空则由服务端从账本读取；提供时若与账本不符将被拒绝",
    )


class FreezeRequest(BaseModel):
    plan_id: str = Field(min_length=1, max_length=64)
    confirmation_token: str = Field(min_length=8, max_length=512)


class ExecuteRequest(BaseModel):
    plan_id: str = Field(min_length=1, max_length=64)


class ValueRequest(BaseModel):
    portfolio_id: str = Field(min_length=1, max_length=64)
    snapshot_id: str = SNAPSHOT_ID
    trading_day: date


# ======================================================================
# 应用
# ======================================================================
def create_app(state: AppState | None = None) -> FastAPI:
    app = FastAPI(title="A-Quant Lab API", version="0.1.0",
                  description="研究与模拟决策工作台。模拟账户，不连接券商。")
    app.state.aquant = state or build_state()

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

    # ---------------------------------------------------------------- 读
    @app.get("/api/v1/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/v1/status")
    def status(s: AppState = Depends(svc)) -> dict:
        return build_data_status(s.reader, SNAPSHOT_ID).as_dict()

    @app.get("/api/v1/candidates")
    def candidates(s: AppState = Depends(svc)) -> dict:
        return {"snapshotId": SNAPSHOT_ID,
                "candidates": [{"instrumentId": c.instrument_id,
                                "industryCode": c.industry_code,
                                "signalRank": c.signal_rank}
                               for c in DEMO_CANDIDATES]}

    @app.get("/api/v1/instruments/{instrument_id}/research")
    def research(instrument_id: str, trading_day: date, s: AppState = Depends(svc)) -> dict:
        ref = s.reader.ref(SNAPSHOT_ID)
        bars = s.service._bars(SNAPSHOT_ID, trading_day, ref.as_of_time, [instrument_id])
        try:
            card = build_research_card(
                s.reader, snapshot_id=SNAPSHOT_ID, as_of=ref.as_of_time,
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

        if body.snapshot_id != SNAPSHOT_ID:
            raise HTTPException(status_code=404, detail=f"unknown snapshot {body.snapshot_id!r}")
        ref = s.reader.ref(SNAPSHOT_ID)

        # 账户必须先存在，否则预览记录的 account_version 会基于"空账本"，
        # 而随后的令牌签发会先建账户再读账本，两者版本不一致，冻结永远失败。
        # 因此这里先幂等建账，再统一从账本读取权威状态。
        opening = body.cash_available_cents or 100_000_000
        s.service._ensure_account(body.portfolio_id, initial_cash_cents=opening,
                                  initial_lots=[], now=datetime.now(timezone.utc))
        ledger_cash = s.service._ledger_cash(body.portfolio_id)
        ledger_lots = s.service._load_lots(body.portfolio_id)
        if body.cash_available_cents is not None and body.cash_available_cents != ledger_cash:
            raise conflict(
                "supplied cash differs from the ledger; account state must come from the server",
                repair="do not send cash; the server reads it from the ledger",
            )
        cash = ledger_cash

        pv = s.service.preview(
            portfolio_id=body.portfolio_id, snapshot_id=SNAPSHOT_ID,
            trading_day=body.trading_day, as_of=ref.as_of_time,
            candidates=DEMO_CANDIDATES, cash_available_cents=cash,
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
    def execute(plan_id: str, _: ExecuteRequest, subject: str = Depends(current_subject),
                s: AppState = Depends(svc)) -> dict:
        pv = s.previews.get(plan_id)
        if pv is None:
            raise HTTPException(status_code=404, detail="no live preview for this plan")
        lots = s.service._load_lots(pv.portfolio_id)
        out = s.service.execute(plan_id=plan_id, lots=lots,
                                cash_available_cents=s.service._ledger_cash(pv.portfolio_id))
        return out

    @app.post("/api/v1/valuations")
    def value(body: ValueRequest, s: AppState = Depends(svc)) -> dict:
        ref = s.reader.ref(SNAPSHOT_ID)
        lots = s.service._load_lots(body.portfolio_id)
        return s.service.value(
            portfolio_id=body.portfolio_id, snapshot_id=SNAPSHOT_ID,
            trading_day=body.trading_day, as_of=ref.as_of_time,
            lots=lots, cash_available_cents=s.service._ledger_cash(body.portfolio_id),
        )

    return app


app = create_app()
