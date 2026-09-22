"""生产用全市场快照发布服务。

旧集成验收脚本仍可单独运行，但生产路径不能依赖测试模块。这个服务只消费 collector 已经写好的 JSON 缓存和
研究池，向既有数据根目录追加一个唯一的物理 ID：

    <data-root>/meta.sqlite                 # 共享业务库，保留不删
    <data-root>/api/datasets/<snapshot-id>/ # 每次发布一个新目录
    <data-root>/current_snapshot.json       # 当前逻辑指针

发布失败不会清理任何既有目录或数据库。新目录即使留下未发布的半成品，
也不会被当前指针解析，便于运维检查；下一次运行仍会使用另一个唯一 ID。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from aquant.domain.data.db import apply_migrations, connect
from aquant.domain.data.ingest import SnapshotBuilder
from aquant.domain.data.reader import SnapshotReader
from aquant.domain.data.snapshot import (
    DataMode,
    DatasetRef,
    SnapshotDraft,
    SnapshotStore,
)
from .decision_market_caps import (
    DecisionMarketCapError,
    inject_sidecar,
    load_sidecar,
)

from .snapshot_lifecycle import (
    new_snapshot_id,
    record_current_snapshot,
    validate_snapshot_id,
    write_current_pointer,
)


class UniverseSnapshotError(RuntimeError):
    """生产快照不能发布时抛出的可操作错误。"""


# 研究配置的默认上市年龄门槛。生产快照必须至少带这么多天的权威日历，
# 否则门槛对“早于行情窗口、但上市仍未满 120 日”的标的无法生效。
MIN_LISTING_CALENDAR_DAYS = 120

# 这三项只影响可选财务因子；其余质量失败意味着价格型 S1 也不完整。
_OPTIONAL_FINANCIAL_CHECKS = frozenset({
    "财务缓存存在",
    "财务数据覆盖池内标的 >= 90%",
    "决策日总市值覆盖率 >= 90%",
})


@dataclass(frozen=True, slots=True)
class SnapshotCheck:
    name: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(slots=True)
class UniverseSnapshotResult:
    snapshot_id: str
    data_root: Path
    last_day: str
    first_day: str
    trading_days: int
    instruments: int
    quotes: int
    dataset_dir: Path
    promoted: bool = False
    quality_status: str = "OK"
    checks: list[SnapshotCheck] = field(default_factory=list)

    @property
    def pointer_path(self) -> Path:
        return self.data_root / "current_snapshot.json"

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "data_root": str(self.data_root),
            "dataset_dir": str(self.dataset_dir),
            "current_pointer": str(self.pointer_path),
            "promoted": self.promoted,
            "quality_status": self.quality_status,
            "window": {
                "first_day": self.first_day,
                "last_day": self.last_day,
                "trading_days": self.trading_days,
            },
            "instruments": self.instruments,
            "quotes": self.quotes,
            "checks": [c.as_dict() for c in self.checks],
        }


def _load_json(path: Path, *, label: str) -> dict:
    if not path.is_file():
        raise UniverseSnapshotError(f"缺少{label}：{path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UniverseSnapshotError(f"无法读取{label}：{path}: {exc}") from exc
    if not isinstance(value, dict):
        raise UniverseSnapshotError(f"{label}必须是 JSON object：{path}")
    return value


def _cache_days(bars_all: dict[str, dict], *, window_start: str | None,
                window_end: str | None, window: int | None) -> list[str]:
    # 任意单只证券都可能停牌、晚上市或采集暂缺，不能拿字典中的第一只
    # 当作交易日历。取全池并集后再裁窗，避免第一只缺少末日时让整份快照
    # 静默退回前一个交易日。
    days = sorted({
        str(row["trading_day"])
        for entry in bars_all.values()
        for row in (entry.get("rows") or [])
        if isinstance(row, dict) and row.get("trading_day")
    })
    if window_start:
        days = [day for day in days if day >= window_start]
    if window_end:
        days = [day for day in days if day <= window_end]
    if window:
        days = days[-window:]
    return days


def _cache_calendar(cache: dict, *, quote_days: list[str],
                    window_end: str | None) -> list[str]:
    """读取独立长日历，并把它钉在本次快照末日以内。

    旧缓存没有 ``trading_calendar`` 时退回行情日期，便于诊断和显式的
    degraded replay；生产严格发布会由覆盖率检查拒绝短日历。
    """

    upper = window_end or quote_days[-1]
    raw = cache.get("trading_calendar")
    source = raw if isinstance(raw, list) else quote_days
    days: set[str] = set()
    for value in source:
        text = str(value)
        try:
            date.fromisoformat(text)
        except ValueError:
            continue
        if text <= upper:
            days.add(text)
    # 行情实际出现的日期也是已证实的交易日。并集可避免指数源偶发漏行
    # 导致行情日在自己的快照日历中消失。
    days.update(day for day in quote_days if day <= upper)
    return sorted(days)


def _mark(checks: list[SnapshotCheck], name: str, ok: bool, detail: str = "") -> None:
    checks.append(SnapshotCheck(name=name, ok=bool(ok), detail=detail))


def _build_document(
    *,
    cache: dict,
    pool: dict,
    window_start: str | None,
    window_end: str | None,
    window: int | None,
    financials_path: Path,
    actions_path: Path,
    market_caps_path: Path,
    check_market_caps: bool,
    checks: list[SnapshotCheck],
    market_caps_archive_root: Path | None = None,
) -> tuple[dict, list[dict], list[dict], list[str]]:
    bars_all = cache.get("bars") or {}
    if not isinstance(bars_all, dict) or not bars_all:
        raise UniverseSnapshotError("采集缓存没有 bars")
    days = _cache_days(
        bars_all, window_start=window_start, window_end=window_end, window=window)
    if not days:
        raise UniverseSnapshotError("快照窗口为空：检查 --window-start / --window 与缓存")
    market_caps = None
    if market_caps_path.is_file():
        try:
            market_caps = load_sidecar(
                market_caps_path, expected_as_of=days[-1],
                archive_root=market_caps_archive_root)
        except DecisionMarketCapError as exc:
            raise UniverseSnapshotError(str(exc)) from exc
    kept = set(days)
    calendar_days = _cache_calendar(
        cache, quote_days=days, window_end=window_end or days[-1])
    _mark(
        checks,
        f"交易日历覆盖 >= {MIN_LISTING_CALENDAR_DAYS} 日",
        len(calendar_days) >= MIN_LISTING_CALENDAR_DAYS,
        f"{len(calendar_days)} 个交易日（行情窗口 {len(days)} 日）",
    )
    picked = pool.get("instruments") or []
    if not isinstance(picked, list) or not picked:
        raise UniverseSnapshotError("研究池没有 instruments")

    instruments: list[dict] = []
    quotes: list[dict] = []
    missing = 0
    for entry in picked:
        iid = str(entry.get("instrument_id") or "").strip()
        info = bars_all.get(iid)
        if not iid or not info:
            missing += 1
            continue
        instruments.append({
            "instrument_id": iid,
            "exchange": entry.get("exchange", info.get("exchange", "OTHER")),
            "board": entry.get("board", info.get("board", "OTHER")),
            "security_class": "EQUITY",
            "short_name": entry.get("name") or entry.get("short_name"),
            "listed_on": entry.get("listed_on"),
            "industry_code": (entry.get("industry") or "")[:3] or None,
            "industry_name": entry.get("industry") or None,
            "classification_version": pool.get("classification_version"),
            "status_history": [{
                "valid_from": days[0], "valid_to": None,
                "name": entry.get("name") or entry.get("short_name"),
                "status": "LISTED",
                "industry_code": (entry.get("industry") or "")[:3] or None,
                "industry_name": entry.get("industry") or None,
                "classification_version": pool.get("classification_version"),
            }],
        })
        prev = info.get("prev_close_before_window")
        for bar in info.get("rows") or []:
            day = bar.get("trading_day")
            if day not in kept:
                prev = bar.get("close_cents")
                continue
            quotes.append({
                "instrument_id": iid,
                "trading_day": day,
                "open_cents": bar.get("open_cents"),
                "high_cents": bar.get("high_cents"),
                "low_cents": bar.get("low_cents"),
                "close_cents": bar.get("close_cents"),
                "adjusted_close_cents": bar.get("adjusted_close_cents"),
                "volume_shares": bar.get("volume_shares"),
                "amount_cents": bar.get("amount_cents"),
                "prev_close_cents": prev,
            })
            prev = bar.get("close_cents")

    _mark(checks, "池内标的都有行情", missing == 0, f"缺失 {missing} 只")
    expected_rows = len(instruments) * len(days)
    enough = len(quotes) >= expected_rows * 0.95 if expected_rows else False
    _mark(checks, "行情条数充足", enough,
          f"{len(quotes)} 条（期望 {expected_rows}，允许 5% 停牌缺失）")
    with_amount = sum(1 for q in quotes if q.get("amount_cents") is not None)
    coverage = with_amount / max(len(quotes), 1)
    _mark(checks, "成交额覆盖率 >= 99%", coverage >= 0.99,
          f"{with_amount}/{len(quotes)}")
    with_adjusted = sum(
        1 for q in quotes if q.get("adjusted_close_cents") is not None)
    adjusted_coverage = with_adjusted / max(len(quotes), 1)
    _mark(checks, "前复权收盘价覆盖率 >= 99%", adjusted_coverage >= 0.99,
          f"{with_adjusted}/{len(quotes)}")
    seeded = sum(1 for i in instruments
                 if bars_all.get(i["instrument_id"], {}).get("prev_close_before_window") is not None)
    _mark(checks, "窗口首行前收已从行情补齐", seeded >= len(instruments) * 0.99,
          f"{seeded}/{len(instruments)}")
    no_prev = sum(1 for q in quotes if q.get("prev_close_cents") is None)
    _mark(checks, "前收缺失只可能出现在窗口首行", no_prev <= len(instruments),
          f"{no_prev} 行无前收")
    industry_ok = all(i.get("industry_code") for i in instruments)
    _mark(checks, "行业齐全", industry_ok,
          f"{sum(1 for i in instruments if i.get('industry_code'))}/{len(instruments)}")
    boards: dict[str, int] = {}
    for item in instruments:
        board = item.get("board", "OTHER")
        boards[board] = boards.get(board, 0) + 1
    _mark(checks, "覆盖主板（可模拟）", boards.get("MAIN", 0) >= 100,
          str(boards))
    matched_caps, missing_caps = inject_sidecar(instruments, market_caps)
    if check_market_caps:
        _mark(
            checks,
            "决策日总市值覆盖率 >= 90%",
            market_caps is not None and matched_caps >= len(instruments) * 0.9,
            (f"{matched_caps}/{len(instruments)}；"
             + ("sidecar 缺失" if market_caps is None else
                f"缺失 {missing_caps} 只")),
        )

    actions: list[dict] = []
    if actions_path.is_file():
        payload = _load_json(actions_path, label="公司行为缓存")
        wanted = {i["instrument_id"] for i in instruments}
        for action in payload.get("corporate_actions") or []:
            record_day = action.get("record_date")
            if action.get("instrument_id") in wanted and record_day and days[0] <= record_day <= days[-1]:
                actions.append(action)
    _mark(checks, "公司行为带来源公告",
          all(a.get("source_announcement_id") for a in actions), f"{len(actions)} 条")

    doc: dict[str, Any] = {
        "schema_version": "aquant.real_dataset.v1",
        "data_mode": "PRODUCTION",
        "watermark": ("REAL MARKET DATA, RECONSTRUCTED POINT-IN-TIME -- "
                       "NOT VALID FOR FORMAL PIT BACKTESTS"),
        "disclaimer": ("行情与行业分类来自 BaoStock 免费接口，抓取发生在历史之后；"
                       "可得时点为重建而非当时观察，不得用于正式时点回测。"),
        "as_of_time": days[-1] + "T15:00:00+08:00",
        "input_cutoff_at": days[-1] + "T07:00:00Z",
        "published_at": datetime.now(timezone.utc).isoformat(),
        "trading_days": calendar_days,
        "instruments": instruments,
        "daily_quotes": quotes,
        "corporate_actions": actions,
        "events": [],
    }
    if financials_path.is_file():
        try:
            fin_cache = _load_json(financials_path, label="财务缓存")
            # The collector writes an object here.  Treat a valid JSON value
            # with a malformed statements member like any other unusable
            # financial cache, rather than letting .items() abort snapshot
            # publication before the optional quality gate can run.
            raw_statements = fin_cache.get("statements", {})
            if not isinstance(raw_statements, dict):
                raise UniverseSnapshotError(
                    f"财务缓存的 statements 必须是 JSON object：{financials_path}")
        except UniverseSnapshotError as exc:
            _mark(checks, "财务缓存存在", False,
                  f"无法读取或解析财务缓存：{exc}")
        else:
            pool_ids = {i["instrument_id"] for i in instruments}
            statements = {iid: periods for iid, periods in raw_statements.items()
                          if iid in pool_ids}
            doc["financials"] = {
                "created_at": fin_cache.get("created_at"),
                "updated_at": fin_cache.get("updated_at"),
                "source_id": "baostock",
                "unit_notes": "netProfit 单位为元；totalShare 为股；均由 records.py 归一",
                "statements": statements,
            }
            _mark(checks, "财务数据覆盖池内标的 >= 90%",
                  len(statements) >= len(pool_ids) * 0.9,
                  f"{len(statements)}/{len(pool_ids)}")
    else:
        _mark(checks, "财务缓存存在", False, f"缺少 {financials_path}")
    return doc, instruments, quotes, days


def _refs_to_dataset_refs(refs: list[dict]) -> list[DatasetRef]:
    def parse(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    return [DatasetRef(name=r["name"], path=r["path"], sha256=r["sha256"],
                       record_count=r["record_count"],
                       as_of_upper_bound=parse(r["as_of_upper_bound"]))
            for r in refs]


def publish_universe_snapshot(
    *,
    data_root: str | Path,
    cache_path: str | Path,
    pool_path: str | Path,
    snapshot_id: str | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    window: int | None = None,
    financials_path: str | Path | None = None,
    actions_path: str | Path | None = None,
    market_caps_path: str | Path | None = None,
    check_market_caps: bool = False,
    strict_quality: bool = True,
    promote: bool = True,
) -> UniverseSnapshotResult:
    """把 collector 缓存发布成不可变物理快照，并可选择切换当前指针。"""

    root = Path(data_root)
    cache_file = Path(cache_path)
    pool_file = Path(pool_path)
    financials_file = Path(financials_path) if financials_path else root.parent / "agentctl-q0" / "financials-cache.json"
    actions_file = Path(actions_path) if actions_path else root.parent / "agentctl-q0" / "dividend-actions.json"
    market_caps_file = (Path(market_caps_path) if market_caps_path else
                        root.parent / "agentctl-q0" / "decision-market-caps.json")
    cache = _load_json(cache_file, label="采集缓存")
    pool = _load_json(pool_file, label="研究池")
    days_for_id = _cache_days(
        cache.get("bars") or {}, window_start=window_start,
        window_end=window_end, window=window)
    if not days_for_id:
        raise UniverseSnapshotError("快照窗口为空")
    sid = validate_snapshot_id(snapshot_id or new_snapshot_id(days_for_id[-1]))
    # An explicit ID is useful for a one-off migration/replay, but it must still
    # obey the same no-overwrite rule as generated IDs.
    if (root / "api" / "datasets" / sid).exists():
        raise UniverseSnapshotError(f"物理快照目录已存在，拒绝覆盖：{sid}")

    checks: list[SnapshotCheck] = []
    doc, instruments, quotes, days = _build_document(
        cache=cache, pool=pool, window_start=window_start,
        window_end=window_end, window=window,
        financials_path=financials_file, actions_path=actions_file,
        market_caps_path=market_caps_file, check_market_caps=check_market_caps,
        checks=checks,
        market_caps_archive_root=(market_caps_file.parent / "forward-archive"
                                  if check_market_caps else None),
    )
    failed = [c for c in checks if not c.ok]
    # Financials and amount coverage are quality gates for the production command;
    # callers doing a controlled replay can explicitly request a degraded publish.
    if strict_quality and failed:
        detail = "; ".join(f"{c.name}: {c.detail}" for c in failed)
        raise UniverseSnapshotError("快照质量检查未通过：" + detail)

    root.mkdir(parents=True, exist_ok=True)
    api_root = root / "api"
    api_root.mkdir(parents=True, exist_ok=True)
    dataset_dir = api_root / "datasets" / sid
    # Creating only this new ID is safe. Existing IDs and meta.sqlite are never
    # removed or replaced by this function.
    dataset_dir.mkdir(parents=True, exist_ok=False)
    con = connect(root / "meta.sqlite")
    try:
        apply_migrations(con)
        builder = SnapshotBuilder(con, api_root / "datasets")
        builder.ensure_source(
            "baostock", display_name="BaoStock（免费公开接口）",
            domains=["DAILY_QUOTES", "CALENDAR_IDENTITY", "INDUSTRY_CONSTITUENTS", "FINANCIALS"],
            integration_state="TEST_PASSED", pit_available="NO", pit_basis="RECONSTRUCTED", rights={},
        )
        builder.ensure_source(
            "cninfo", display_name="巨潮资讯（法定信息披露平台）",
            domains=["ANNOUNCEMENTS", "CORPORATE_ACTIONS"],
            integration_state="TEST_PASSED", pit_available="NO", pit_basis="RECONSTRUCTED", rights={},
        )
        if any(i.get("market_cap_source_id") == "eastmoney-direct"
               for i in instruments):
            builder.ensure_source(
                "eastmoney-direct", display_name="东方财富免费行情接口",
                domains=["DAILY_QUOTES"], integration_state="TEST_PASSED",
                pit_available="NO", pit_basis="OBSERVED", rights={},
            )
        if any(i.get("market_cap_source_id") == "tencent-qt"
               for i in instruments):
            builder.ensure_source(
                "tencent-qt", display_name="腾讯证券收盘快照",
                # 现有持久化枚举把快照型市值归入 DAILY_QUOTES；应用层
                # SourceRegistry 已单列 MARKET_CAPS 能力。待统一迁移数据库
                # 枚举前，不在发布路径写入数据库尚不认识的新值。
                domains=["DAILY_QUOTES"],
                integration_state="TEST_PASSED",
                pit_available="NO", pit_basis="OBSERVED", rights={},
            )
        actions = doc.get("corporate_actions") or []
        registered = {row[0] for row in con.execute("SELECT source_id FROM source_registry")}
        referenced = {a.get("source_id") for a in actions if a.get("source_id")}
        _mark(checks, "公司行为的来源已登记", referenced <= registered,
              f"已登记 {sorted(registered)}；被引用 {sorted(referenced)}")
        if strict_quality and referenced - registered:
            raise UniverseSnapshotError("公司行为引用了未登记的数据源")
        final_failures = [c for c in checks if not c.ok]
        quality_status = (
            "OK" if not final_failures else
            "DEGRADED" if all(c.name in _OPTIONAL_FINANCIAL_CHECKS
                              for c in final_failures) else
            "BLOCKING"
        )
        builder.ingest(doc, source_id="baostock", data_version=f"universe:{sid}")
        refs = builder.write_datasets(doc, snapshot_id=sid)
        store = SnapshotStore(con, api_root)
        parsed = datetime.fromisoformat
        store.publish(SnapshotDraft(
            snapshot_id=sid, kind="EOD", data_mode=DataMode.PRODUCTION,
            input_cutoff_at=parsed(doc["input_cutoff_at"].replace("Z", "+00:00")),
            as_of_time=parsed(doc["as_of_time"].replace("Z", "+00:00")),
            created_at=datetime.now(timezone.utc),
            published_at=parsed(doc["published_at"].replace("Z", "+00:00")),
            code_version="0.1.0", data_version=f"universe:{sid}", watermark=doc["watermark"],
            pool_hash="sha256:" + hashlib.sha256(pool_file.read_bytes()).hexdigest(),
            datasets=_refs_to_dataset_refs(refs),
            quality_status=quality_status,
        ))
        # Verify the new immutable object before making it current. This also
        # ensures a malformed path cannot become the API's current input.
        reader = SnapshotReader(store)
        ref = reader.ref(sid)
        reader.instruments(sid, as_of=ref.as_of_time)
        if promote:
            if quality_status == "BLOCKING":
                raise UniverseSnapshotError(
                    f"快照 {sid} 存在 S1 阻断质量问题，拒绝切换 current")
            # DB 记录先写，文件指针最后原子替换。运行中的 API 以文件为优先，
            # 因此任何中途失败都不会把它暴露到半完成的新快照。
            db_pointer = record_current_snapshot(con, sid)
            write_current_pointer(
                root, sid, updated_at=datetime.fromisoformat(db_pointer.updated_at))
    except UniverseSnapshotError:
        raise
    except Exception as exc:  # 生产 CLI 边界：保留异常链并返回可操作错误
        raise UniverseSnapshotError(str(exc)) from exc
    finally:
        con.close()

    return UniverseSnapshotResult(
        snapshot_id=sid, data_root=root, last_day=days[-1], first_day=days[0],
        trading_days=len(days), instruments=len(instruments), quotes=len(quotes),
        dataset_dir=dataset_dir, promoted=promote, checks=checks,
        quality_status=quality_status,
    )


def promote_universe_snapshot(
    *, data_root: str | Path, snapshot_id: str, require_factors: bool = False,
) -> None:
    """验证已发布对象后原子切换 current；可要求因子已成功持久化。"""

    root = Path(data_root)
    con = connect(root / "meta.sqlite")
    try:
        apply_migrations(con)
        store = SnapshotStore(con, root / "api")
        snap = store.require_published(snapshot_id)
        if snap["quality_status"] == "BLOCKING":
            raise UniverseSnapshotError(
                f"快照 {snapshot_id} 的质量状态为 BLOCKING，拒绝切换 current")
        if require_factors:
            row = con.execute(
                "SELECT COUNT(*) FROM research_run r "
                "JOIN research_run_feature_validity rv "
                "  ON rv.research_run_id=r.research_run_id "
                "JOIN feature_version fv ON fv.feature_version=rv.feature_version "
                "JOIN feature_value f ON f.research_run_id=r.research_run_id "
                "WHERE r.snapshot_id=? AND r.status='SUCCEEDED' "
                "AND rv.validity_status='VALID' AND fv.status='ACTIVE' "
                "AND f.raw_value IS NOT NULL",
                (snapshot_id,),
            ).fetchone()
            if row is None or int(row[0]) <= 0:
                raise UniverseSnapshotError(
                    f"快照 {snapshot_id} 尚无 ACTIVE/VALID 且有值的因子，拒绝切换 current")
        db_pointer = record_current_snapshot(con, snapshot_id)
        write_current_pointer(
            root, snapshot_id,
            updated_at=datetime.fromisoformat(db_pointer.updated_at),
        )
    except UniverseSnapshotError:
        raise
    except Exception as exc:  # 同上，避免 SQLite/摄取错误直接泄漏堆栈
        raise UniverseSnapshotError(str(exc)) from exc
    finally:
        con.close()


__all__ = [
    "SnapshotCheck",
    "UniverseSnapshotError",
    "UniverseSnapshotResult",
    "promote_universe_snapshot",
    "publish_universe_snapshot",
]
