"""A-Quant Lab product-owned agentctl capability handlers.

These handlers are the product side of the agentctl integration. They receive
and return JSON-compatible mappings and must NOT import agentctl server
internals, so the same contract can later be hosted in-process or behind an
HTTP sidecar.

Design constraints inherited from the A-Quant Lab spec:
  * Read handlers never fetch a provider on the fly. They read an already
    published, immutable snapshot keyed by snapshot_id (main doc 15.4, 16.2).
  * Synthetic data is always marked SYNTHETIC and carries a watermark. The
    production research path refuses to mix synthetic and real series
    (main doc 15.4).
  * Unknown snapshot / unknown instrument return an explicit error code from
    the main doc 16.4 vocabulary instead of partial or invented data.
  * No handler here holds execute_order / write_ledger / raw_sql / run_shell /
    fetch_arbitrary_url authority (main doc 16.3). Those capabilities are
    deliberately absent from the manifest, so the model has no mechanism to
    reach them.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

# 这里原先有一份写死的合成快照与两个证券的卡片（Q0/Q1 的替身）。
# M1 已交付，替身随之删除：能力 handler 改为**接收注入的读取器**，
# 真实读取由 src/aquant/adapters/agentctl/snapshot_card_reader.py 提供。
#
# 删掉它的理由不只是"过时了"：写死的数据会让"接上了真实存储"这件事
# 无法验证——测试全绿，而读的是一份常量。


def _error(code: str, message: str, object_id: str, retryable: bool, repair: str) -> dict[str, Any]:
    """Main doc 16.4: every error carries an explanation, object id,
    retryability and a repair action -- never just 'analysis failed'."""
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "object_id": object_id,
            "retryable": retryable,
            "repair_action": repair,
        },
    }


def _exclusion_labels() -> dict[str, str]:
    """原因码 -> 说明。**取自产品的唯一取值表**，不在这里再抄一份。

    这里原先有一份只覆盖 `MISSING_*` 英文码的副本，而 F10 在真实快照上
    产出的中文原因（"TTM 不可得……"）不在其中：模型读到的卡片上，
    排除原因的说明是空的。同一个原因码在界面与模型两侧必须同一种说法。

    import 失败时返回空表：能力 handler 要能在没有 aquant 的进程里装载
    （见模块开头的说明），此时说明字段为空而不是整次调用崩掉。
    """

    try:
        from aquant.domain.research.exclusions import EXCLUSION_LABELS
    except ImportError:
        return {}
    return dict(EXCLUSION_LABELS)


def _read_limitations(card: dict[str, Any]) -> list[str]:
    """这张卡片的限制。**从数据本身推出来**，不是固定文案。"""

    out: list[str] = []
    if card.get("classification_version"):
        out.append(f"行业分类版本 {card['classification_version']}："
                   "免费源不提供分类变更历史，因此它无法回答历史时点的行业归属")
    if card["board"] in ("GEM", "STAR"):
        out.append("该板块 20% 涨跌幅，首期可展示但不进入可执行模拟池")
    if card.get("listed_on") is None:
        out.append("缺少上市日期：无法判断历史时点是否已上市")
    out.append("不构成投资建议；排名不是概率，也不表示预期收益")
    return out


def _quality_label(snapshot: dict[str, Any]) -> str:
    """数据完整度标签。**只根据算得出来的覆盖度**，不编造等级。"""

    quality = snapshot.get("quality") or {}
    coverage = quality.get("coverage")
    if coverage is None:
        return "覆盖度未知"
    missing = quality.get("missing_domains") or []
    label = f"行情覆盖 {coverage * 100:.1f}%"
    return label + ("（有数据域缺失）" if missing else "")


def _default_reader() -> Any | None:
    """未被注入读取器时，按产品的运行期配置自己找一个。

    为什么需要它：agentctl 的接入侧只保证「能 import、签名能 bind 一个
    invocation」，随后真实调用时**不会**替你注入任何东西。
    如果这里返回 None 而 handler 直接报错，那么「能力已注册」这句话
    在接入侧是真的、在运行期是假的——正是本项目反复出现的那类缺陷。

    延迟 import + 吞掉 ImportError：handler 只有在**真的被调用**时
    才需要 aquant，import 阶段不依赖它。
    """

    try:
        from aquant.adapters.agentctl.runtime import card_reader
    except ImportError:
        return None
    try:
        return card_reader()
    except Exception:                                    # noqa: BLE001
        # 数据目录存在但读不了（权限、半截快照、schema 版本不对……）：
        # 这时要说「读不到」而不是抛栈——调用方要的是可修复的错误码。
        return None


async def research_card_read(invocation: dict[str, Any], *,
                             reader: Any | None = None) -> dict[str, Any]:
    """aquant.research_card.read -- read-only research card for one instrument
    under one fixed snapshot. No side effects, no provider access.

    reader 由调用方注入（见 src/aquant/adapters/agentctl/snapshot_card_reader.py）。
    原先这里读的是模块内写死的字典——那份替身是在 M1 之前写的，
    现在换成真实读取。

    **只依赖读取器提供的两个方法**（snapshot / card），不 import 任何
    M1 或 agentctl 模块：能力契约要的是「给 snapshot_id 与 instrument_id
    返回一张卡片」，中间那层由适配器负责。

    reader 可省略。**省略时必须仍能被调用**：agentctl 装载 handler 时
    用 `inspect.signature(fn).bind({})` 校验，一个必填的关键字参数
    会让能力在接入侧「注册成功」，直到第一次真实调用才失败。
    """

    args = dict(invocation.get("validated_arguments") or {})
    instrument_id = str(args.get("instrument_id") or "").strip()
    snapshot_id = str(args.get("snapshot_id") or "").strip()

    if reader is None:
        reader = _default_reader()
    if reader is None:
        return _error(
            "DATA_NOT_READY",
            "no snapshot store is reachable from this process: neither an injected "
            "reader nor an on-disk data directory (AQUANT_DATA_DIR) was found",
            snapshot_id or "<empty>",
            True,
            "point AQUANT_DATA_DIR at a published data directory on the host that "
            "runs the capability, or call the handler with reader=<SnapshotCardReader>",
        )

    snapshot = reader.snapshot(snapshot_id) if snapshot_id else None
    if snapshot is None:
        return _error(
            "STALE_SNAPSHOT",
            f"snapshot {snapshot_id!r} is not published or does not exist",
            snapshot_id or "<empty>",
            True,
            "retry with a snapshot_id that is published "
            "(the list is available from the product's /api/v1/status)",
        )

    card = reader.card(snapshot_id, instrument_id) if instrument_id else None
    if card is None:
        return _error(
            "DATA_NOT_READY",
            f"instrument {instrument_id!r} is not covered by snapshot {snapshot_id!r}",
            instrument_id or "<empty>",
            False,
            "use an instrument_id that exists in that snapshot",
        )

    factors = [
        {
            "factor_id": f["factor_id"],
            "name": f["factor_id"],
            "value": f.get("value"),
            "rank_pct": f.get("rank_pct"),
            "coverage": f.get("coverage"),
            # §10.2 算不出时必须给原因，不能给 0 或省略
            "exclusion_reason": f.get("exclusion_reason"),
            "exclusion_label": _exclusion_labels().get(f.get("exclusion_reason") or ""),
        }
        for f in card["factors"]
    ]

    limitations = _read_limitations(card)
    if card.get("factor_note"):
        # 没算出因子时要**说出来**，否则一张没有数值的卡片看起来像"算过了，值为空"
        limitations.append(card["factor_note"])

    return {
        "ok": True,
        "snapshot_id": snapshot["snapshot_id"],
        "as_of_time": snapshot["as_of_time"],
        "data_mode": snapshot["data_mode"],
        "watermark": snapshot.get("watermark"),
        "instrument_id": card["instrument_id"],
        "exchange": card["exchange"],
        "board": card["board"],
        "display_name": card["display_name"],
        "industry_code": card.get("industry_code"),
        "industry_name": card.get("industry_name"),
        # 上市日期：卡片上的限制项由它推出（"缺少上市日期"），
        # 因此它必须一起返回——否则调用方看到一句关于某个字段的限制，
        # 却拿不到那个字段本身。
        "listed_on": card.get("listed_on"),
        "status": card.get("status"),
        "data_completeness": _quality_label(snapshot),
        "factors": factors,
        "limitations": limitations,
    }


# ---------------------------------------------------------------------------
# Q5 read/compute/job capabilities
#
# The functions below deliberately keep the same shape as the original Q1
# handler: a single invocation mapping is required and every dependency is
# optional keyword-only injection.  The agentctl provider therefore imports
# this module without importing ``aquant`` and resolves product storage only
# when a capability is actually invoked.


def _invocation_arguments(invocation: dict[str, Any]) -> dict[str, Any]:
    """Return the validated argument mapping used by the product handler."""

    validated = invocation.get("validated_arguments")
    if isinstance(validated, dict):
        return dict(validated)
    # A few product-side tests and older in-process callers use ``arguments``.
    # The platform always supplies validated_arguments, so this is only a
    # compatibility fallback and never a source of authority.
    arguments = invocation.get("arguments")
    return dict(arguments) if isinstance(arguments, dict) else {}


def _invocation_actor(invocation: dict[str, Any]) -> str | None:
    """Read the platform-verified actor metadata, never a model argument."""

    metadata = invocation.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    for key in ("actor_user_id", "auth_subject", "user_id"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    return None


def _as_datetime(value: Any, *, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed


def _as_date(value: Any, *, name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an ISO date") from exc


def _domain_reader(reader: Any) -> Any:
    """Unwrap SnapshotCardReader while accepting a directly injected reader."""

    candidate = getattr(reader, "reader", None)
    if candidate is not None and callable(getattr(candidate, "ref", None)):
        return candidate
    return reader


def _reader_connection(reader: Any) -> Any | None:
    connection = getattr(reader, "con", None)
    if connection is not None:
        return connection
    store = getattr(reader, "store", None)
    connection = getattr(store, "con", None)
    return connection if connection is not None else None


def _default_write_connection() -> tuple[Any | None, bool]:
    """Open the product's configured metadata store for a job submission.

    A write capability must never create a new, empty database as a side
    effect.  We therefore require the explicit product data directory and an
    existing ``meta.sqlite``.  Tests and an embedding product may inject a
    connection directly into the handler.
    """

    try:
        from aquant.adapters.agentctl.runtime import data_dir_from_env
        from aquant.domain.data.db import connect
    except ImportError:
        return None, False
    data_dir = data_dir_from_env()
    if data_dir is None:
        return None, False
    path = data_dir / "meta.sqlite"
    if not path.is_file():
        return None, False
    try:
        return connect(path, allow_thread_sharing=True), True
    except Exception:  # noqa: BLE001
        return None, False


def _domain_error(exc: Exception, *, object_id: str) -> dict[str, Any]:
    """Map product-domain errors to the shared §16.4 error envelope."""

    code = str(getattr(exc, "code", "DATA_NOT_READY"))
    message = str(getattr(exc, "message", str(exc)))
    error_object = str(getattr(exc, "object_id", object_id) or object_id)
    repair = str(getattr(exc, "repair_action", "check the published product state"))
    retryable = code in {"DATA_NOT_READY", "STALE_SNAPSHOT", "PIT_UNVERIFIED"}
    return _error(code, message, error_object, retryable, repair)


def _snapshot_from_reader(reader: Any, snapshot_id: str) -> dict[str, Any] | None:
    method = getattr(reader, "snapshot", None)
    if callable(method):
        try:
            snapshot = method(snapshot_id)
        except Exception:  # noqa: BLE001
            return None
        return dict(snapshot) if isinstance(snapshot, dict) else None
    # The product adapter normally injects SnapshotCardReader. Accepting a
    # direct SnapshotReader too keeps this handler usable inside the API
    # process without introducing a second storage implementation.
    ref = getattr(reader, "ref", None)
    if not callable(ref):
        return None
    try:
        snapshot_ref = ref(snapshot_id)
    except Exception:  # noqa: BLE001
        return None
    as_dict = getattr(snapshot_ref, "as_dict", None)
    if not callable(as_dict):
        return None
    try:
        value = as_dict()
    except Exception:  # noqa: BLE001
        return None
    return dict(value) if isinstance(value, dict) else None


async def event_evidence_read(invocation: dict[str, Any], *,
                              reader: Any | None = None) -> dict[str, Any]:
    """Read PIT-filtered event evidence from the product evidence store.

    This handler only reads the published snapshot and SQLite evidence index.
    It does not accept source text, URLs, model instructions, or write
    arguments, so an adversarial material can only be returned as data.
    """

    args = _invocation_arguments(invocation)
    instrument_id = str(args.get("instrument_id") or "").strip()
    snapshot_id = str(args.get("snapshot_id") or "").strip()
    object_id = instrument_id or snapshot_id or "<empty>"
    if not instrument_id or not snapshot_id:
        return _error(
            "DATA_NOT_READY",
            "instrument_id and snapshot_id are required",
            object_id,
            False,
            "provide an instrument covered by a published snapshot",
        )
    if reader is None:
        reader = _default_reader()
    if reader is None:
        return _error(
            "DATA_NOT_READY",
            "no published snapshot store is reachable from this process",
            snapshot_id,
            True,
            "point AQUANT_DATA_DIR at the product data directory",
        )
    snapshot = _snapshot_from_reader(reader, snapshot_id)
    if snapshot is None:
        return _error(
            "STALE_SNAPSHOT",
            f"snapshot {snapshot_id!r} is not published or does not exist",
            snapshot_id,
            True,
            "retry with a published snapshot_id",
        )
    card = getattr(reader, "card", None)
    if callable(card):
        try:
            if card(snapshot_id, instrument_id) is None:
                return _error(
                    "DATA_NOT_READY",
                    f"instrument {instrument_id!r} is not covered by snapshot {snapshot_id!r}",
                    instrument_id,
                    False,
                    "use an instrument_id present in the published snapshot",
                )
        except Exception as exc:  # noqa: BLE001
            return _domain_error(exc, object_id=instrument_id)

    con = _reader_connection(reader)
    if con is None:
        return _error(
            "DATA_NOT_READY",
            "the snapshot reader has no product evidence connection",
            instrument_id,
            True,
            "run the handler in the product process or inject its SQLite reader",
        )
    try:
        as_of = _as_datetime(snapshot.get("as_of_time"), name="snapshot.as_of_time")
        from aquant.domain.evidence.store import evidence_for

        rows = evidence_for(
            con,
            instrument_id=instrument_id,
            located_only=bool(args.get("located_only", False)),
            as_of=as_of,
        )
        requested_limit = args.get("limit", 100)
        try:
            limit = max(1, min(int(requested_limit), 100))
        except (TypeError, ValueError):
            return _error(
                "DATA_NOT_READY", "limit must be an integer", object_id,
                False, "use a limit between 1 and 100",
            )
        rows = rows[:limit]

        # Evidence output must carry the durable authorization fact.  The
        # existing evidence query intentionally stays general; the handler
        # enriches it without changing the domain query implementation.
        document_ids = sorted({str(row.get("documentId") or "") for row in rows if row.get("documentId")})
        authorization: dict[str, dict[str, Any]] = {}
        if document_ids:
            marks = ",".join("?" for _ in document_ids)
            doc_rows = con.execute(
                f"SELECT document_id,license_status,source_id,withdrawn_at "
                f"FROM document WHERE document_id IN ({marks})", document_ids,
            ).fetchall()
            authorization = {
                str(row["document_id"]): {
                    "license_status": row["license_status"],
                    "source_id": row["source_id"],
                    "withdrawn_at": row["withdrawn_at"],
                    "display_allowed": row["license_status"] in {"PERMITTED", "EXCERPT_ONLY"}
                    and row["withdrawn_at"] is None,
                }
                for row in doc_rows
            }
        enriched: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            auth = authorization.get(str(item.get("documentId") or ""))
            item["authorization"] = auth
            # Never expose an excerpt that the source registry says is index
            # only or has been withdrawn.
            if auth and auth.get("display_allowed") is False:
                item["quote"] = None
                item["authorization_blocked"] = True
            else:
                item["authorization_blocked"] = False
            enriched.append(item)
        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "as_of_time": as_of.isoformat(),
            "data_mode": snapshot.get("data_mode"),
            "watermark": snapshot.get("watermark"),
            "instrument_id": instrument_id,
            "count": len(enriched),
            "evidence": enriched,
            "pit_filter": "available_at <= as_of_time",
        }
    except (ValueError, TypeError) as exc:
        return _error("PIT_UNVERIFIED", str(exc), object_id, False,
                      "use the exact timezone-aware as_of_time from the snapshot")
    except Exception as exc:  # noqa: BLE001
        return _domain_error(exc, object_id=object_id)


async def portfolio_read(invocation: dict[str, Any], *,
                         con: Any | None = None) -> dict[str, Any]:
    """Read one simulated portfolio ledger from the product store."""

    args = _invocation_arguments(invocation)
    portfolio_id = str(args.get("portfolio_id") or "").strip()
    if not portfolio_id:
        return _error(
            "DATA_NOT_READY", "portfolio_id is required", "<empty>", False,
            "provide the portfolio id selected by the authenticated caller",
        )
    if con is None:
        reader = _default_reader()
        con = _reader_connection(reader) if reader is not None else None
    if con is None:
        return _error(
            "DATA_NOT_READY",
            "no product portfolio store is reachable from this process",
            portfolio_id,
            True,
            "point AQUANT_DATA_DIR at a published product data directory",
        )
    try:
        from aquant.application.workspace_queries import portfolio_ledger

        row = portfolio_ledger(con, portfolio_id)
        # The actor is read from verified invocation metadata.  The durable
        # schema currently has no portfolio_owner column, so expose the scope
        # explicitly rather than pretending the ledger query proved ownership.
        actor = _invocation_actor(invocation)
        return {
            "ok": True,
            **row,
            "subject_scope": "agentctl_verified_actor" if actor else "embedding_context",
            "actor_user_id": actor,
            "read_only": True,
        }
    except KeyError as exc:
        return _error(
            "DATA_NOT_READY", str(exc), portfolio_id, False,
            "use a portfolio id that exists in the product store",
        )
    except Exception as exc:  # noqa: BLE001
        return _domain_error(exc, object_id=portfolio_id)


async def experiment_submit(invocation: dict[str, Any], *,
                            con: Any | None = None) -> dict[str, Any]:
    """Submit a real product research job using the durable idempotency key."""

    args = _invocation_arguments(invocation)
    job_type = str(args.get("job_type") or "").strip()
    trading_day = str(args.get("trading_day") or "").strip()
    snapshot_id = str(args.get("snapshot_id") or "").strip()
    config_version = str(args.get("config_version") or "default").strip()
    if not job_type or not trading_day or not snapshot_id:
        return _error(
            "DATA_NOT_READY",
            "job_type, trading_day, and snapshot_id are required",
            snapshot_id or "<empty>",
            False,
            "submit a supported product research job with its input snapshot",
        )
    try:
        _as_date(trading_day, name="trading_day")
    except ValueError as exc:
        return _error("DATA_NOT_READY", str(exc), trading_day, False,
                      "use an ISO trading day")

    close_after = False
    if con is None:
        con, close_after = _default_write_connection()
    if con is None:
        return _error(
            "DATA_NOT_READY",
            "no writable product metadata store is configured",
            snapshot_id,
            True,
            "set AQUANT_DATA_DIR to an existing product data directory or inject its connection",
        )
    try:
        from aquant.operations.research_jobs import submit_research_job

        payload = args.get("payload")
        if payload is not None and not isinstance(payload, dict):
            return _error("DATA_NOT_READY", "payload must be an object", snapshot_id,
                          False, "send a JSON object as payload")
        out = submit_research_job(
            con,
            job_type=job_type,
            trading_day=trading_day,
            snapshot_id=snapshot_id,
            config_version=config_version,
            payload=payload or None,
        )
        return {
            "ok": True,
            **out,
            "job_id": out.get("jobId"),
            "idempotency_key": out.get("idempotencyKey"),
            "actor_user_id": _invocation_actor(invocation),
            # agentctl's async_mode=job contract consumes this stable product
            # reference; the legacy top-level fields remain for the product
            # API shape and human-readable receipts.
            "job_ref": {
                "job_id": out.get("jobId"),
                "status": str(out.get("status") or "").lower(),
                "owner": "aquant_lab",
                "submitted_at": out.get("submittedAt"),
            },
        }
    except Exception as exc:  # noqa: BLE001
        return _domain_error(exc, object_id=snapshot_id)
    finally:
        if close_after and con is not None:
            con.close()


def _build_preview_candidates(domain_reader: Any, snapshot_id: str,
                              as_of: datetime,
                              raw_candidates: Any) -> list[Any]:
    """Resolve candidate IDs to snapshot-backed S1 candidates.

    Explicit candidate objects are accepted only after their IDs are verified
    against the snapshot.  When omitted, the deterministic S1 price pipeline
    computes ranks from the same snapshot; no model-provided prices or names
    are used.
    """

    from aquant.domain.portfolio.construction import Candidate

    instruments = list(domain_reader.instruments(snapshot_id, as_of=as_of))
    by_id = {str(item.get("instrument_id")): item for item in instruments}
    if raw_candidates is None:
        raw_ids: list[str] | None = None
    elif isinstance(raw_candidates, list):
        raw_ids = []
        for item in raw_candidates:
            if isinstance(item, str):
                raw_ids.append(item.strip())
            elif isinstance(item, dict):
                raw_ids.append(str(item.get("instrument_id") or "").strip())
            else:
                raise ValueError("candidates must contain instrument ids or objects")
        raw_ids = [item for item in raw_ids if item]
    else:
        raise ValueError("candidates must be an array")

    if raw_ids is not None:
        if not raw_ids:
            raise ValueError("candidates must not be empty")
        unknown = sorted(set(raw_ids) - set(by_id))
        if unknown:
            raise ValueError("candidate is not covered by the snapshot: " + ", ".join(unknown[:5]))

    # Build S1 ranks from published adjusted closes where possible.  This is
    # the only accepted source for generated signal_rank values.
    from aquant.domain.strategy.s1 import FactorDataError, build_s1_signals

    pool = by_id if raw_ids is None else {iid: by_id[iid] for iid in raw_ids}
    closes: dict[str, list[int]] = {}
    industries: dict[str, str] = {}
    for iid, item in pool.items():
        industry = item.get("industry_code")
        if not industry:
            continue
        quotes = domain_reader.daily_quotes(snapshot_id, as_of=as_of, instrument_id=iid)
        prices = [q.adjusted_close_cents for q in quotes if q.adjusted_close_cents is not None]
        if len(prices) >= 61:
            closes[iid] = prices
            industries[iid] = str(industry)
    signals: dict[str, Any] = {}
    if closes:
        try:
            signals = {s.instrument_id: s for s in build_s1_signals(
                adjusted_closes_by_instrument=closes,
                industry_by_instrument=industries,
            )}
        except FactorDataError:
            signals = {}

    candidates: list[Candidate] = []
    if raw_ids is None and not signals:
        raise ValueError("snapshot does not contain 61 adjusted closes for S1 preview")
    ids = raw_ids if raw_ids is not None else list(signals)
    for iid in ids:
        item = by_id[iid]
        signal = signals.get(iid)
        if signal is None:
            raise ValueError(f"candidate {iid!r} has no snapshot-backed S1 rank")
        board = str(item.get("board") or "").upper()
        exchange = str(item.get("exchange") or "").upper()
        candidates.append(Candidate(
            iid,
            str(item.get("industry_code") or signal.industry_code),
            float(signal.signal_rank),
            simulatable=board == "MAIN" and exchange in {"SSE", "SZSE"},
        ))
    return candidates


async def simulation_plan_preview(invocation: dict[str, Any], *,
                                  service: Any | None = None,
                                  reader: Any | None = None,
                                  con: Any | None = None) -> dict[str, Any]:
    """Compute a plan draft through PlanService without persisting it."""

    args = _invocation_arguments(invocation)
    portfolio_id = str(args.get("portfolio_id") or "").strip()
    snapshot_id = str(args.get("snapshot_id") or "").strip()
    if not portfolio_id or not snapshot_id or not args.get("trading_day"):
        return _error(
            "DATA_NOT_READY",
            "portfolio_id, snapshot_id, and trading_day are required",
            portfolio_id or snapshot_id or "<empty>",
            False,
            "provide an existing simulated portfolio and published decision snapshot",
        )
    if reader is None:
        reader = _default_reader()
    if reader is None:
        return _error("DATA_NOT_READY", "no snapshot store is reachable", snapshot_id,
                      True, "point AQUANT_DATA_DIR at a published data directory")
    snapshot = _snapshot_from_reader(reader, snapshot_id)
    if snapshot is None:
        return _error("STALE_SNAPSHOT", f"snapshot {snapshot_id!r} is not published",
                      snapshot_id, True, "retry with a published snapshot_id")
    domain_reader = _domain_reader(reader)
    if con is None:
        con = _reader_connection(reader)
    if con is None:
        return _error("DATA_NOT_READY", "no product portfolio store is reachable",
                      portfolio_id, True, "inject the product metadata connection")
    try:
        from aquant.domain.data.reader import SnapshotReader
        from aquant.domain.data.snapshot import SnapshotStore
        from aquant.domain.portfolio.construction import ConstructionParams
        from aquant.domain.portfolio.plan import PlanService
        from aquant.domain.simulation.board_rules import BOARD_RULES
        from aquant.domain.simulation.verified_fees import fee_table_from_env

        if not callable(getattr(domain_reader, "ref", None)):
            raise ValueError("injected reader does not provide published snapshot data")
        # SnapshotCardReader already owns the domain SnapshotReader.  For a
        # direct reader injection use its store; this path remains read-only.
        if not callable(getattr(domain_reader, "daily_quotes", None)):
            domain_reader = SnapshotReader(SnapshotStore(con, getattr(reader, "root", "")))
        ref = domain_reader.ref(snapshot_id)
        trading_day = _as_date(args.get("trading_day"), name="trading_day")
        row = con.execute(
            "SELECT portfolio_id FROM portfolio WHERE portfolio_id=?", (portfolio_id,)
        ).fetchone()
        if row is None:
            return _error("DATA_NOT_READY", f"unknown portfolio {portfolio_id!r}",
                          portfolio_id, False, "create the simulated portfolio before previewing")

        if service is None:
            listings = {
                str(item["instrument_id"]): (
                    str(item.get("exchange") or "OTHER"),
                    str(item.get("board") or "OTHER"),
                )
                for item in domain_reader.instruments(snapshot_id, as_of=ref.as_of_time)
                if item.get("instrument_id")
            }
            # 与产品 API 使用同一条费率解析路径。未配置佣金时返回的费率表
            # 带 UNCONFIGURED_DEFAULT 标记，PlanService 会允许合成数据预览，
            # 但会拒绝真实快照，不能由 agentctl 绕过 FEE_VERSION_UNVERIFIED。
            fee_table, _fee_note = fee_table_from_env()
            service = PlanService(
                con, domain_reader, fee_table, BOARD_RULES, listings,
                ConstructionParams(),
            )
        lots = service._load_lots(portfolio_id)
        cash = service._ledger_cash(portfolio_id)
        supplied_cash = args.get("cash_available_cents")
        if supplied_cash is not None and int(supplied_cash) != cash:
            return _error(
                "DATA_NOT_READY",
                "supplied cash differs from the product ledger",
                portfolio_id,
                False,
                "omit cash_available_cents; the handler reads cash from the ledger",
            )
        candidates = _build_preview_candidates(
            domain_reader, snapshot_id, ref.as_of_time, args.get("candidates"),
        )
        preview = service.preview(
            portfolio_id=portfolio_id,
            snapshot_id=snapshot_id,
            trading_day=trading_day,
            as_of=ref.as_of_time,
            candidates=candidates,
            cash_available_cents=cash,
            lots=lots,
            confirm_subject="agentctl:preview",
            decision_snapshot_id=args.get("decision_snapshot_id"),
            decision_cutoff_at=(
                _as_datetime(args["decision_cutoff_at"], name="decision_cutoff_at")
                if args.get("decision_cutoff_at") else None
            ),
            execution_snapshot_id=args.get("execution_snapshot_id"),
        )
        out = preview.as_dict()
        out.update({
            "ok": True,
            "plan_id": preview.plan_id,
            "portfolio_id": portfolio_id,
            "snapshot_id": snapshot_id,
            "frozen": False,
            "frozen_label": "未冻结 · 预览不产生成交",
            "read_only": True,
            "actor_user_id": _invocation_actor(invocation),
            "targets": [
                {
                    "instrument_id": target.instrument_id,
                    "industry_code": target.industry_code,
                    "weight_pct": str(target.weight_pct),
                    "rationale": target.rationale,
                }
                for target in preview.targets
            ],
        })
        return out
    except Exception as exc:  # noqa: BLE001
        return _domain_error(exc, object_id=portfolio_id or snapshot_id)
