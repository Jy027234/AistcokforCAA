"""决策日总市值的前向观察 sidecar。

东方财富的 ``f20`` 只表示抓取时看到的当前值，没有供应商历史版本。
本模块因此只接受带有抓取收据的 sidecar，并把它限定为「当日收盘后观察到的
最近行情日总市值」。缺少、过期或时点不成立时返回缺失/抛出可操作错误，绝不
用财报期股本和收盘价拼出一个看似完整的值。
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "aquant.decision_market_caps.v1"
DEFAULT_FILENAME = "decision-market-caps.json"
DEFAULT_SOURCE_ID = "eastmoney-direct"
SUPPORTED_SOURCE_IDS = frozenset({"eastmoney-direct", "tencent-qt"})
MARKET_CLOSE = time(15, 0)
_MARKET_TZ = ZoneInfo("Asia/Shanghai")


class DecisionMarketCapError(ValueError):
    """sidecar 结构或时点不满足生产使用条件。"""


def market_cap_items_hash(items: Mapping[str, int]) -> str:
    """归一后的证券 ID 与总市值分的规范摘要，供归档清单绑定 sidecar。"""

    body = json.dumps(sorted((iid, int(cents)) for iid, cents in items.items()),
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


@dataclass(frozen=True, slots=True)
class DecisionMarketCap:
    instrument_id: str
    market_cap_cents: int
    market_cap_as_of: str


@dataclass(frozen=True, slots=True)
class DecisionMarketCapSidecar:
    source_id: str
    observed_at: str
    market_cap_as_of: str
    receipt_id: str
    content_hash: str
    items: Mapping[str, DecisionMarketCap]

    @property
    def observed_at_dt(self) -> datetime:
        return _parse_aware_datetime(self.observed_at, "observed_at")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "source_id": self.source_id,
            "observed_at": self.observed_at,
            "market_cap_as_of": self.market_cap_as_of,
            "receipt_id": self.receipt_id,
            "content_hash": self.content_hash,
            "items": {
                iid: {
                    "market_cap_cents": item.market_cap_cents,
                    "market_cap_as_of": item.market_cap_as_of,
                }
                for iid, item in sorted(self.items.items())
            },
        }


def _parse_aware_datetime(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise DecisionMarketCapError(f"{field} 不是有效 ISO 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DecisionMarketCapError(f"{field} 必须带时区")
    return parsed


def _parse_date(value: object, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise DecisionMarketCapError(f"{field} 不是有效 ISO 日期") from exc


def normalize_instrument_id(value: object) -> str:
    """把 sidecar/供应商清单中的证券代码归一为内部 ``SH.600519`` 形态。"""

    text = str(value or "").strip().upper().replace("-", ".")
    if "." in text:
        market, code = text.split(".", 1)
        if market in {"SH", "SSE", "SZ", "SZSE"} and code.isdigit():
            prefix = "SH" if market in {"SH", "SSE"} else "SZ"
            if _is_supported_a_share_code(prefix, code):
                return prefix + "." + code
            raise DecisionMarketCapError(f"不支持的沪深证券代码：{value!r}")
    if text.isdigit() and len(text) == 6:
        if text.startswith(("60", "68")):
            return "SH." + text
        if text.startswith(("00", "30")):
            return "SZ." + text
    raise DecisionMarketCapError(f"无法归一证券代码：{value!r}")


def _is_supported_a_share_code(exchange: str, code: str) -> bool:
    """当前快照范围只包含沪深主板/创业板/科创板。

    东财清单的 ``f13`` 对北交所也可能返回深市标识，不能仅凭市场字段把
    4/8 开头代码伪装成 ``SZ``。后续若产品纳入北交所，应单独增加板块和
    交易规则，而不是放宽这里的前缀判断。
    """

    return ((exchange == "SH" and code.startswith(("60", "68"))) or
            (exchange == "SZ" and code.startswith(("00", "30"))))


def instrument_id_from_eastmoney(row: Mapping[str, Any]) -> str:
    """把东财清单的 ``f12``/``f13`` 转为产品内部证券 ID。"""

    raw_code = row.get("f12")
    text = str(raw_code or "").strip().upper()
    if "." in text:
        return normalize_instrument_id(text)
    market = str(row.get("f13") or "").strip().upper()
    if market in {"1", "SH", "SSE"}:
        prefix = "SH"
    elif market in {"0", "SZ", "SZSE"}:
        prefix = "SZ"
    else:
        # 对没有 f13 的离线回放仍允许按六位代码推断，但不猜任意前缀。
        return normalize_instrument_id(raw_code)
    if not text.isdigit() or len(text) != 6:
        raise DecisionMarketCapError(f"东财 f12 不是六位证券代码：{raw_code!r}")
    if not _is_supported_a_share_code(prefix, text):
        raise DecisionMarketCapError(f"东财 f12 不在当前沪深股票范围：{raw_code!r}")
    return f"{prefix}.{text}"


def market_cap_cents_from_yuan(value: object) -> int:
    """把东财 ``f20`` 元值转成产品统一的分，并拒绝非有限/非正值。"""

    if value is None or (isinstance(value, str) and not value.strip()):
        raise DecisionMarketCapError("f20 总市值为空")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DecisionMarketCapError(f"f20 总市值不可解析：{value!r}") from exc
    if not amount.is_finite() or amount <= 0:
        raise DecisionMarketCapError(f"f20 总市值必须为正有限数：{value!r}")
    cents = (amount * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    try:
        result = int(cents)
    except (OverflowError, ValueError) as exc:
        raise DecisionMarketCapError(f"f20 总市值超出范围：{value!r}") from exc
    if result <= 0:
        raise DecisionMarketCapError(f"f20 总市值换算后无效：{value!r}")
    return result


def market_cap_cents_from_eastmoney_rows(
        rows: list[Mapping[str, Any]]) -> dict[str, int]:
    """与 sidecar 生成使用同一套证券和金额归一规则。"""

    items: dict[str, int] = {}
    for row in rows:
        try:
            items[instrument_id_from_eastmoney(row)] = market_cap_cents_from_yuan(
                row.get("f20"))
        except DecisionMarketCapError:
            continue
    return items


def market_cap_cents_from_values(values_yuan: Mapping[str, object]) -> dict[str, int]:
    items: dict[str, int] = {}
    for raw_iid, raw_value in values_yuan.items():
        try:
            items[normalize_instrument_id(raw_iid)] = market_cap_cents_from_yuan(raw_value)
        except DecisionMarketCapError:
            continue
    return items


def _validate_observation_time(observed_at: datetime, as_of: date) -> None:
    local = observed_at.astimezone(_MARKET_TZ)
    if local.date() != as_of:
        raise DecisionMarketCapError(
            f"observed_at 必须发生在 market_cap_as_of 当日，实际为 {local.date()}"
        )
    if local.time() < MARKET_CLOSE:
        raise DecisionMarketCapError("总市值必须在行情日收盘后采集（不早于 15:00 Asia/Shanghai）")


def sidecar_from_eastmoney_rows(
    rows: list[Mapping[str, Any]], *, market_cap_as_of: str | date,
    observed_at: datetime, receipt_id: str, content_hash: str,
    source_id: str = DEFAULT_SOURCE_ID,
) -> DecisionMarketCapSidecar:
    """将一次已归档的东财清单响应转为可审计 sidecar。"""

    as_of = (market_cap_as_of if isinstance(market_cap_as_of, date)
             else _parse_date(market_cap_as_of, "market_cap_as_of"))
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise DecisionMarketCapError("observed_at 必须带时区")
    _validate_observation_time(observed_at, as_of)
    if not str(receipt_id or "").strip():
        raise DecisionMarketCapError("缺少 receipt_id")
    if not str(content_hash or "").strip():
        raise DecisionMarketCapError("缺少 content_hash")
    if not str(source_id or "").strip():
        raise DecisionMarketCapError("缺少 source_id")

    items: dict[str, DecisionMarketCap] = {}
    for iid, cents in market_cap_cents_from_eastmoney_rows(rows).items():
        items[iid] = DecisionMarketCap(
            instrument_id=iid, market_cap_cents=cents,
            market_cap_as_of=as_of.isoformat(),
        )
    return DecisionMarketCapSidecar(
        source_id=str(source_id), observed_at=observed_at.isoformat(),
        market_cap_as_of=as_of.isoformat(), receipt_id=str(receipt_id),
        content_hash=str(content_hash), items=items,
    )


def sidecar_from_market_cap_values(
    values_yuan: Mapping[str, object], *, market_cap_as_of: str | date,
    observed_at: datetime, receipt_id: str, content_hash: str,
    source_id: str,
) -> DecisionMarketCapSidecar:
    """从已归一证券 ID -> 总市值（元）构造可审计 sidecar。"""

    as_of = (market_cap_as_of if isinstance(market_cap_as_of, date)
             else _parse_date(market_cap_as_of, "market_cap_as_of"))
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise DecisionMarketCapError("observed_at 必须带时区")
    _validate_observation_time(observed_at, as_of)
    if source_id not in SUPPORTED_SOURCE_IDS:
        raise DecisionMarketCapError(f"不支持的决策日总市值来源：{source_id!r}")
    if not str(receipt_id or "").strip() or not str(content_hash or "").strip():
        raise DecisionMarketCapError("总市值 sidecar 缺少收据或内容哈希")

    items: dict[str, DecisionMarketCap] = {}
    for iid, cents in market_cap_cents_from_values(values_yuan).items():
        items[iid] = DecisionMarketCap(
            instrument_id=iid,
            market_cap_cents=cents,
            market_cap_as_of=as_of.isoformat(),
        )
    return DecisionMarketCapSidecar(
        source_id=source_id,
        observed_at=observed_at.isoformat(),
        market_cap_as_of=as_of.isoformat(),
        receipt_id=str(receipt_id),
        content_hash=str(content_hash),
        items=items,
    )


def _verified_receipt_bytes(con: sqlite3.Connection, archive_root: Path, *,
                            receipt_id: str, content_hash: str,
                            source_id: str, as_of: date,
                            observed_at: datetime) -> bytes:
    receipt = con.execute(
        "SELECT source_id, outcome, content_hash, responded_at FROM fetch_receipt "
        "WHERE receipt_id=?", (receipt_id,),
    ).fetchone()
    if (receipt is None or receipt["source_id"] != source_id
            or receipt["outcome"] != "OK"
            or receipt["content_hash"] != content_hash):
        raise DecisionMarketCapError(f"总市值收据不匹配：{receipt_id}")
    responded = _parse_aware_datetime(receipt["responded_at"], "收据 responded_at")
    _validate_observation_time(responded, as_of)
    if responded > observed_at:
        raise DecisionMarketCapError("总市值 sidecar 观测时间早于归档收据")
    artifact = con.execute(
        "SELECT stored_path FROM raw_artifact WHERE content_hash=?",
        (content_hash,),
    ).fetchone()
    if artifact is None:
        raise DecisionMarketCapError(f"总市值原始归档缺失：{content_hash}")
    root = archive_root.resolve()
    path = (root / artifact["stored_path"]).resolve()
    if root not in path.parents:
        raise DecisionMarketCapError("总市值原始归档路径越界")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise DecisionMarketCapError(f"无法读取总市值原始归档：{exc}") from exc
    if "sha256:" + hashlib.sha256(payload).hexdigest() != content_hash:
        raise DecisionMarketCapError(f"总市值原始归档哈希不匹配：{receipt_id}")
    return payload


def verify_sidecar_archive(sidecar: DecisionMarketCapSidecar,
                           archive_root: str | Path) -> None:
    """核对 sidecar 数值摘要、批次清单、原始响应及其归档收据。"""

    root = Path(archive_root)
    db_path = root / "meta.sqlite"
    if not db_path.is_file():
        raise DecisionMarketCapError(f"总市值归档数据库缺失：{db_path}")
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        try:
            as_of = date.fromisoformat(sidecar.market_cap_as_of)
            observed_at = sidecar.observed_at_dt
            payload = _verified_receipt_bytes(
                con, root, receipt_id=sidecar.receipt_id,
                content_hash=sidecar.content_hash, source_id=sidecar.source_id,
                as_of=as_of, observed_at=observed_at)
            manifest = json.loads(payload)
            if not isinstance(manifest, dict):
                raise DecisionMarketCapError("总市值归档清单格式错误")
            expected_schema = (
                "aquant.tencent_market_cap_batches.v1"
                if sidecar.source_id == "tencent-qt" else
                "aquant.eastmoney_market_cap_pages.v1"
            )
            if (manifest.get("schema_version") != expected_schema
                    or manifest.get("market_day") != sidecar.market_cap_as_of):
                raise DecisionMarketCapError("总市值归档清单来源或日期不匹配")
            actual_items_hash = market_cap_items_hash({
                iid: item.market_cap_cents for iid, item in sidecar.items.items()
            })
            if manifest.get("items_hash") != actual_items_hash:
                raise DecisionMarketCapError("总市值 sidecar 数值与归档清单不匹配")
            children = manifest.get(
                "batches" if sidecar.source_id == "tencent-qt" else "pages")
            if not isinstance(children, list) or not children:
                raise DecisionMarketCapError("总市值归档清单缺少原始批次")
            for child in children:
                if not isinstance(child, dict):
                    raise DecisionMarketCapError("总市值原始批次格式错误")
                _verified_receipt_bytes(
                    con, root,
                    receipt_id=str(child.get("receipt_id") or ""),
                    content_hash=str(child.get("content_hash") or ""),
                    source_id=sidecar.source_id,
                    as_of=as_of, observed_at=observed_at,
                )
        finally:
            con.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        if isinstance(exc, DecisionMarketCapError):
            raise
        raise DecisionMarketCapError(f"无法校验总市值归档：{exc}") from exc


def load_sidecar(
    path: str | Path, *, expected_as_of: str | date,
    archive_root: str | Path | None = None,
) -> DecisionMarketCapSidecar:
    """加载并校验 sidecar；路径存在但不合格时拒绝发布，避免静默用旧值。"""

    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DecisionMarketCapError(f"无法读取决策日总市值 sidecar：{source}: {exc}") from exc
    if not isinstance(raw, dict):
        raise DecisionMarketCapError("决策日总市值 sidecar 必须是 JSON object")
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise DecisionMarketCapError("决策日总市值 sidecar schema_version 不匹配")

    expected = (expected_as_of if isinstance(expected_as_of, date)
                else _parse_date(expected_as_of, "expected_as_of"))
    as_of = _parse_date(raw.get("market_cap_as_of"), "market_cap_as_of")
    if as_of != expected:
        raise DecisionMarketCapError(
            f"sidecar 的 market_cap_as_of={as_of} 与最近行情日={expected} 不一致"
        )
    observed = _parse_aware_datetime(raw.get("observed_at"), "observed_at")
    _validate_observation_time(observed, as_of)
    source_id = str(raw.get("source_id") or "").strip()
    receipt_id = str(raw.get("receipt_id") or "").strip()
    content_hash = str(raw.get("content_hash") or "").strip()
    if not source_id or not receipt_id or not content_hash:
        raise DecisionMarketCapError("sidecar 必须记录 source_id、receipt_id、content_hash")
    if source_id not in SUPPORTED_SOURCE_IDS:
        raise DecisionMarketCapError(
            f"不支持的决策日总市值来源：{source_id!r}")

    raw_items = raw.get("items")
    if not isinstance(raw_items, dict):
        raise DecisionMarketCapError("sidecar.items 必须是 object")
    items: dict[str, DecisionMarketCap] = {}
    for raw_iid, raw_item in raw_items.items():
        iid = normalize_instrument_id(raw_iid)
        if not isinstance(raw_item, dict):
            raise DecisionMarketCapError(f"sidecar.items[{iid}] 必须是 object")
        item_as_of = _parse_date(raw_item.get("market_cap_as_of", as_of.isoformat()),
                                 f"sidecar.items[{iid}].market_cap_as_of")
        if item_as_of != as_of:
            raise DecisionMarketCapError(f"sidecar.items[{iid}] 日期与 sidecar 不一致")
        cents_raw = raw_item.get("market_cap_cents")
        # 复用统一的有限数/正数检查，但不要把 cents 当成元再次乘 100。
        try:
            cents_decimal = Decimal(str(cents_raw))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise DecisionMarketCapError(f"sidecar.items[{iid}] 总市值不可解析") from exc
        if not cents_decimal.is_finite() or cents_decimal <= 0 or cents_decimal != cents_decimal.to_integral_value():
            raise DecisionMarketCapError(f"sidecar.items[{iid}] 总市值分必须为正整数")
        items[iid] = DecisionMarketCap(
            instrument_id=iid, market_cap_cents=int(cents_decimal),
            market_cap_as_of=as_of.isoformat(),
        )
    sidecar = DecisionMarketCapSidecar(
        source_id=source_id, observed_at=observed.isoformat(),
        market_cap_as_of=as_of.isoformat(), receipt_id=receipt_id,
        content_hash=content_hash, items=items,
    )
    if archive_root is not None:
        verify_sidecar_archive(sidecar, archive_root)
    return sidecar


def inject_sidecar(
    instruments: list[dict[str, Any]], sidecar: DecisionMarketCapSidecar | None,
) -> tuple[int, int]:
    """把已校验值写入快照证券文档，返回 ``(命中, 缺失)``。"""

    if sidecar is None:
        return 0, len(instruments)
    matched = 0
    for instrument in instruments:
        iid = str(instrument.get("instrument_id") or "").upper()
        item = sidecar.items.get(iid)
        if item is None:
            continue
        instrument.update({
            "market_cap_cents": item.market_cap_cents,
            "market_cap_as_of": item.market_cap_as_of,
            "market_cap_source_id": sidecar.source_id,
            "market_cap_observed_at": sidecar.observed_at,
            "market_cap_receipt_id": sidecar.receipt_id,
            "market_cap_content_hash": sidecar.content_hash,
        })
        matched += 1
    return matched, len(instruments) - matched


__all__ = [
    "DEFAULT_FILENAME",
    "DEFAULT_SOURCE_ID",
    "DecisionMarketCap",
    "DecisionMarketCapError",
    "DecisionMarketCapSidecar",
    "SCHEMA_VERSION",
    "inject_sidecar",
    "instrument_id_from_eastmoney",
    "load_sidecar",
    "market_cap_cents_from_yuan",
    "market_cap_cents_from_eastmoney_rows",
    "market_cap_cents_from_values",
    "market_cap_items_hash",
    "normalize_instrument_id",
    "sidecar_from_eastmoney_rows",
    "sidecar_from_market_cap_values",
    "verify_sidecar_archive",
]
