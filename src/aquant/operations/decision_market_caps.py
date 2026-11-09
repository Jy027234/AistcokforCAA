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
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from aquant.artifact_paths import resolve_archive_member

SCHEMA_VERSION = "aquant.decision_market_caps.v1"
REPLAY_SCHEMA_VERSION = "aquant.decision_market_caps.replay.v1"
REPLAY_MANIFEST_SCHEMA_VERSION = "aquant.decision_market_caps.replay_manifest.v1"
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
    schema_version: str = SCHEMA_VERSION
    derived_at: str | None = None

    @property
    def observed_at_dt(self) -> datetime:
        return _parse_aware_datetime(self.observed_at, "observed_at")

    @property
    def derived_at_dt(self) -> datetime:
        """Replay availability time; v1 keeps its original single clock."""

        if self.derived_at is None:
            return self.observed_at_dt
        return _parse_aware_datetime(self.derived_at, "derived_at")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
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
            **({"derived_at": self.derived_at} if self.derived_at is not None else {}),
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
        if market in {"SH", "SSE", "SZ", "SZSE"} and re.fullmatch(r"[0-9]{6}", code):
            prefix = "SH" if market in {"SH", "SSE"} else "SZ"
            if _is_supported_a_share_code(prefix, code):
                return prefix + "." + code
            raise DecisionMarketCapError(f"不支持的沪深证券代码：{value!r}")
    if re.fullmatch(r"[0-9]{6}", text):
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


def sidecar_from_replay_manifest(
    manifest: Mapping[str, Any], *, receipt_id: str, content_hash: str,
) -> DecisionMarketCapSidecar:
    """Create the explicitly versioned sidecar for an archived quote replay.

    The returned object is only a serialization of the manifest claims. It is
    not accepted for production use until ``load_sidecar`` verifies its SQL
    receipt, parent receipts and original quote bytes against ``archive_root``.
    """

    if manifest.get("schema_version") != REPLAY_MANIFEST_SCHEMA_VERSION:
        raise DecisionMarketCapError("历史重放派生清单 schema_version 不匹配")
    if manifest.get("source_id") != "tencent-qt":
        raise DecisionMarketCapError("历史报价重放只支持 tencent-qt")
    as_of = _parse_date(manifest.get("market_day"), "market_day")
    observed = _parse_aware_datetime(manifest.get("source_observed_at"),
                                     "source_observed_at")
    derived = _parse_aware_datetime(manifest.get("derived_at"), "derived_at")
    _validate_observation_time(observed, as_of)
    if derived < observed:
        raise DecisionMarketCapError("派生清单生成早于原始报价观测")
    if not str(receipt_id or "").strip() or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(content_hash or "")):
        raise DecisionMarketCapError("历史重放 sidecar 缺少有效派生收据或哈希")
    raw_items = manifest.get("items")
    if not isinstance(raw_items, Mapping) or not raw_items:
        raise DecisionMarketCapError("历史重放派生清单没有数值")
    items: dict[str, DecisionMarketCap] = {}
    for raw_iid, raw_item in raw_items.items():
        iid = normalize_instrument_id(raw_iid)
        if iid in items or not isinstance(raw_item, Mapping):
            raise DecisionMarketCapError("历史重放派生清单证券重复或字段格式错误")
        cents = raw_item.get("market_cap_cents")
        try:
            cents_decimal = Decimal(str(cents))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise DecisionMarketCapError(f"历史重放 {iid} 总市值不可解析") from exc
        if (not cents_decimal.is_finite() or cents_decimal <= 0
                or cents_decimal != cents_decimal.to_integral_value()):
            raise DecisionMarketCapError(f"历史重放 {iid} 总市值分必须为正整数")
        items[iid] = DecisionMarketCap(
            instrument_id=iid,
            market_cap_cents=int(cents_decimal),
            market_cap_as_of=as_of.isoformat(),
        )
    return DecisionMarketCapSidecar(
        source_id="tencent-qt", observed_at=observed.isoformat(),
        market_cap_as_of=as_of.isoformat(), receipt_id=str(receipt_id),
        content_hash=str(content_hash), items=items,
        schema_version=REPLAY_SCHEMA_VERSION, derived_at=derived.isoformat(),
    )


def _validate_sidecar_cutoff(sidecar: DecisionMarketCapSidecar,
                             cutoff: datetime | None) -> None:
    if cutoff is None:
        return
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise DecisionMarketCapError("PIT cutoff 必须带时区")
    if sidecar.observed_at_dt > cutoff:
        raise DecisionMarketCapError("总市值原始观测晚于决策cutoff")
    if sidecar.derived_at_dt > cutoff:
        raise DecisionMarketCapError("总市值派生清单可用时间晚于决策cutoff")


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
    try:
        path = resolve_archive_member(root, artifact["stored_path"])
    except ValueError as exc:
        raise DecisionMarketCapError("总市值原始归档路径越界") from exc
    if root not in path.parents:
        raise DecisionMarketCapError("总市值原始归档路径越界")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise DecisionMarketCapError(f"无法读取总市值原始归档：{exc}") from exc
    if "sha256:" + hashlib.sha256(payload).hexdigest() != content_hash:
        raise DecisionMarketCapError(f"总市值原始归档哈希不匹配：{receipt_id}")
    return payload


def market_cap_pool_ids_hash(instrument_ids: list[str] | tuple[str, ...]) -> str:
    """Hash a canonical, sorted pool identifier list for replay manifests."""

    normalized = [normalize_instrument_id(iid) for iid in instrument_ids]
    if len(normalized) != len(set(normalized)):
        raise DecisionMarketCapError("重放证券池包含重复证券")
    body = json.dumps(sorted(normalized), ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _tencent_replay_requested_symbols(url: str) -> list[str]:
    parsed = urlsplit(str(url))
    if parsed.scheme != "https" or parsed.netloc != "qt.gtimg.cn":
        raise DecisionMarketCapError("原始报价 URL 必须是 https://qt.gtimg.cn")
    query_text = parsed.query or parsed.path.lstrip("/")
    query_symbols = parse_qs(query_text).get("q")
    if not isinstance(query_symbols, list) or len(query_symbols) != 1:
        raise DecisionMarketCapError("原始报价 URL 缺少唯一 q 参数")
    symbols = query_symbols[0].split(",")
    if (not symbols or any(not re.fullmatch(r"(?:sh|sz|bj)[0-9]{6}", item)
                            for item in symbols)
            or len(symbols) != len(set(symbols))):
        raise DecisionMarketCapError("原始报价 URL q 参数包含无效或重复证券符号")
    return symbols


def _verified_archived_payload(con: sqlite3.Connection, archive_root: Path, *,
                               receipt_id: str, content_hash: str,
                               source_id: str) -> tuple[sqlite3.Row, bytes]:
    """Read an archive object after checking its SQL receipt and stored bytes.

    Timestamp policy is intentionally left to the caller: ordinary v1 receipts
    still go through ``_verified_receipt_bytes`` and its original same-day rule;
    replay receipts use separate source and derived clocks.
    """

    receipt = con.execute(
        "SELECT * FROM fetch_receipt WHERE receipt_id=?", (receipt_id,),
    ).fetchone()
    if (receipt is None or receipt["source_id"] != source_id
            or receipt["outcome"] != "OK"
            or receipt["content_hash"] != content_hash):
        raise DecisionMarketCapError(f"总市值收据不匹配：{receipt_id}")
    if not str(content_hash).startswith("sha256:"):
        raise DecisionMarketCapError(f"总市值收据哈希格式错误：{receipt_id}")
    artifact = con.execute(
        "SELECT stored_path FROM raw_artifact WHERE content_hash=?",
        (content_hash,),
    ).fetchone()
    if artifact is None:
        raise DecisionMarketCapError(f"总市值原始归档缺失：{content_hash}")
    root = archive_root.resolve()
    try:
        path = resolve_archive_member(root, artifact["stored_path"])
    except ValueError as exc:
        raise DecisionMarketCapError("总市值原始归档路径越界") from exc
    if root not in path.parents:
        raise DecisionMarketCapError("总市值原始归档路径越界")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise DecisionMarketCapError(f"无法读取总市值原始归档：{exc}") from exc
    if "sha256:" + hashlib.sha256(payload).hexdigest() != content_hash:
        raise DecisionMarketCapError(f"总市值原始归档哈希不匹配：{receipt_id}")
    if receipt["byte_size"] is not None and int(receipt["byte_size"]) != len(payload):
        raise DecisionMarketCapError(f"总市值原始归档长度不匹配：{receipt_id}")
    return receipt, payload


def _parse_tencent_replay_batch(payload: bytes, *, market_day: date,
                                expected_symbols: set[str],
                                receipt_id: str) -> dict[str, tuple[int, str]]:
    """Reparse selected Tencent values and quote times from original bytes."""

    parsed: dict[str, tuple[int, str]] = {}
    pattern = re.compile(r'v_((?:sh|sz)[0-9]{6})="([^"]*)";')
    for match in pattern.finditer(payload.decode("gbk", "replace")):
        symbol, body = match.groups()
        if symbol not in expected_symbols:
            continue
        if symbol in parsed:
            raise DecisionMarketCapError(f"腾讯原始响应证券重复：{symbol} ({receipt_id})")
        fields = body.split("~")
        if len(fields) <= 45 or fields[2].strip() != symbol[2:]:
            raise DecisionMarketCapError(f"腾讯原始响应证券字段/代码不匹配：{symbol} ({receipt_id})")
        if not re.fullmatch(r"[0-9]{14}", fields[30].strip()):
            raise DecisionMarketCapError(f"腾讯原始响应缺少有效行情时刻：{symbol} ({receipt_id})")
        try:
            quote_at = datetime.strptime(fields[30].strip(), "%Y%m%d%H%M%S")
            cents = market_cap_cents_from_yuan(
                Decimal(fields[45].strip()) * Decimal("100000000"))
        except (ValueError, InvalidOperation, DecisionMarketCapError) as exc:
            raise DecisionMarketCapError(f"腾讯原始响应总市值无效：{symbol} ({receipt_id})") from exc
        if quote_at.date() != market_day or quote_at.time() < MARKET_CLOSE:
            raise DecisionMarketCapError(f"腾讯原始响应行情日期/时刻无效：{symbol} ({receipt_id})")
        parsed[symbol] = (cents, quote_at.replace(tzinfo=_MARKET_TZ).isoformat())
    missing = expected_symbols - parsed.keys()
    if missing:
        raise DecisionMarketCapError(
            f"腾讯原始响应缺少证券：{', '.join(sorted(missing))} ({receipt_id})")
    return parsed


def _verify_replay_manifest(con: sqlite3.Connection, archive_root: Path, *,
                            receipt_id: str, content_hash: str,
                            source_id: str, expected_as_of: date,
                            expected_observed_at: datetime | None = None,
                            expected_derived_at: datetime | None = None,
                            cutoff: datetime | None = None) -> tuple[dict[str, Any], dict[str, int], datetime, datetime]:
    """Verify a historical quote replay from the manifest receipt to raw bytes."""

    if source_id != "tencent-qt":
        raise DecisionMarketCapError("历史报价重放只支持 tencent-qt")
    derived_receipt, payload = _verified_archived_payload(
        con, archive_root, receipt_id=receipt_id,
        content_hash=content_hash, source_id=source_id)
    if int(derived_receipt["http_status"] or 0) != 200:
        raise DecisionMarketCapError("重放派生清单收据不是 HTTP 200")
    derived_at = _parse_aware_datetime(derived_receipt["responded_at"], "派生清单收据 responded_at")
    derived_first_seen = _parse_aware_datetime(
        derived_receipt["first_seen_at"], "派生清单收据 first_seen_at")
    if derived_first_seen != derived_at:
        raise DecisionMarketCapError(
            "派生清单 SQL first_seen_at 与 responded_at 不一致")
    requested_at = _parse_aware_datetime(derived_receipt["requested_at"], "派生清单收据 requested_at")
    if requested_at > derived_at:
        raise DecisionMarketCapError("重放派生清单收据请求时间晚于响应时间")
    if expected_derived_at is not None and derived_at != expected_derived_at:
        raise DecisionMarketCapError("sidecar derived_at 与派生清单 SQL 收据不一致")
    try:
        manifest = json.loads(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise DecisionMarketCapError("历史重放派生清单不是有效 JSON") from exc
    if not isinstance(manifest, dict):
        raise DecisionMarketCapError("历史重放派生清单必须是 JSON object")
    if (manifest.get("schema_version") != REPLAY_MANIFEST_SCHEMA_VERSION
            or manifest.get("source_id") != source_id
            or manifest.get("market_day") != expected_as_of.isoformat()):
        raise DecisionMarketCapError("历史重放清单来源、schema 或日期不匹配")
    manifest_derived = _parse_aware_datetime(manifest.get("derived_at"), "清单 derived_at")
    if manifest_derived != derived_at:
        raise DecisionMarketCapError("清单 derived_at 与真实派生 SQL 收据不一致")
    source_observed = _parse_aware_datetime(
        manifest.get("source_observed_at"), "清单 source_observed_at")
    _validate_observation_time(source_observed, expected_as_of)
    if source_observed > derived_at:
        raise DecisionMarketCapError("派生清单生成早于最后一笔原始报价")
    if expected_observed_at is not None and source_observed != expected_observed_at:
        raise DecisionMarketCapError("sidecar observed_at 与原始报价收据不一致")
    if cutoff is not None:
        if source_observed > cutoff:
            raise DecisionMarketCapError("历史报价原始观测晚于决策cutoff")
        if derived_at > cutoff:
            raise DecisionMarketCapError("历史报价派生清单可用时间晚于决策cutoff")

    pool = manifest.get("pool")
    if not isinstance(pool, dict) or not isinstance(pool.get("instrument_ids"), list):
        raise DecisionMarketCapError("历史重放清单缺少完整证券池")
    pool_ids: list[str] = []
    for raw_iid in pool["instrument_ids"]:
        pool_ids.append(normalize_instrument_id(raw_iid))
    if not pool_ids or len(pool_ids) != len(set(pool_ids)):
        raise DecisionMarketCapError("历史重放证券池为空或包含重复证券")
    if pool_ids != sorted(pool_ids):
        raise DecisionMarketCapError("历史重放证券池必须按证券 ID 规范排序")
    if pool.get("instrument_ids_hash") != market_cap_pool_ids_hash(pool_ids):
        raise DecisionMarketCapError("历史重放证券池 ID 哈希不匹配")
    if (not isinstance(pool.get("content_hash"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", pool["content_hash"])):
        raise DecisionMarketCapError("历史重放证券池缺少原始文件哈希")
    if (int(pool.get("requested_count", -1)) != len(pool_ids)
            or int(pool.get("accepted_count", -1)) != len(pool_ids)
            or int(manifest.get("requested_count", -1)) != len(pool_ids)
            or int(manifest.get("accepted_count", -1)) != len(pool_ids)):
        raise DecisionMarketCapError("历史重放证券池覆盖数不完整")
    pool_set = set(pool_ids)

    source_snapshot = manifest.get("source_quote_snapshot")
    if not isinstance(source_snapshot, dict):
        raise DecisionMarketCapError("历史重放清单缺少原始报价快照摘要")
    if (source_snapshot.get("market_date") != expected_as_of.isoformat()
            or source_snapshot.get("quote_time_kind") != "after_close_research_snapshot"):
        raise DecisionMarketCapError("原始收盘报价日期或类型不匹配")
    for key in ("quotes_sha256", "coverage_sha256", "stage_manifest_sha256"):
        if not isinstance(source_snapshot.get(key), str) or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", source_snapshot[key]):
            raise DecisionMarketCapError(f"原始报价快照缺少有效 {key}")
    for key in ("quotes_bytes", "coverage_bytes", "stage_manifest_bytes", "row_count"):
        if isinstance(source_snapshot.get(key), bool) or int(source_snapshot.get(key, 0)) <= 0:
            raise DecisionMarketCapError(f"原始报价快照缺少有效 {key}")

    batches = manifest.get("batches")
    if not isinstance(batches, list) or not batches:
        raise DecisionMarketCapError("历史重放清单缺少原始报价批次")
    if int(source_snapshot.get("batch_count", -1)) != len(batches):
        raise DecisionMarketCapError("原始报价批次数与快照摘要不一致")
    all_seen_ids: set[str] = set()
    response_times: list[datetime] = []
    replayed_items: dict[str, dict[str, Any]] = {}
    seen_receipts: set[str] = set()
    for batch in batches:
        if not isinstance(batch, dict):
            raise DecisionMarketCapError("历史重放原始批次格式错误")
        batch_receipt_id = str(batch.get("receipt_id") or "")
        batch_hash = str(batch.get("content_hash") or "")
        if not batch_receipt_id or batch_receipt_id in seen_receipts:
            raise DecisionMarketCapError("历史重放原始批次收据为空或重复")
        seen_receipts.add(batch_receipt_id)
        requested_symbols = batch.get("requested_symbols")
        accepted_ids_raw = batch.get("accepted_ids")
        if not isinstance(requested_symbols, list) or not isinstance(accepted_ids_raw, list):
            raise DecisionMarketCapError("历史重放原始批次缺少请求符号或接纳证券")
        requested_set: set[str] = set()
        for symbol in requested_symbols:
            if not isinstance(symbol, str) or not re.fullmatch(r"(?:sh|sz|bj)[0-9]{6}", symbol):
                raise DecisionMarketCapError("历史重放批次包含无效腾讯证券符号")
            if symbol in requested_set:
                raise DecisionMarketCapError("历史重放批次请求符号重复")
            requested_set.add(symbol)
        accepted_ids = [normalize_instrument_id(iid) for iid in accepted_ids_raw]
        if len(accepted_ids) != len(set(accepted_ids)):
            raise DecisionMarketCapError("历史重放批次接纳证券重复")
        requested_pool = {
            iid for iid in pool_set
            if ("sh" if iid.startswith("SH.") else "sz") + iid[3:] in requested_set
        }
        if set(accepted_ids) != requested_pool:
            raise DecisionMarketCapError("历史重放批次声明的证券覆盖与请求池不一致")
        if all_seen_ids & requested_pool:
            raise DecisionMarketCapError("历史重放证券被多个原始批次重复覆盖")

        raw_receipt, raw_payload = _verified_archived_payload(
            con, archive_root, receipt_id=batch_receipt_id,
            content_hash=batch_hash, source_id=source_id)
        if int(raw_receipt["http_status"] or 0) != 200:
            raise DecisionMarketCapError(f"原始报价收据不是 HTTP 200：{batch_receipt_id}")
        actual_symbols = _tencent_replay_requested_symbols(raw_receipt["url"])
        if actual_symbols != requested_symbols:
            raise DecisionMarketCapError(
                f"原始报价批次请求符号与真实 SQL 收据 URL 不一致：{batch_receipt_id}")
        for manifest_field, receipt_field in (("url", "url"), ("requested_at", "requested_at"),
                                               ("responded_at", "responded_at"),
                                               ("byte_size", "byte_size")):
            if manifest_field == "byte_size":
                if int(batch.get(manifest_field, -1)) != int(raw_receipt[receipt_field]):
                    raise DecisionMarketCapError(f"原始报价收据长度不匹配：{batch_receipt_id}")
            elif manifest_field in {"requested_at", "responded_at"}:
                listed = _parse_aware_datetime(batch.get(manifest_field),
                                               f"批次 {manifest_field}")
                actual = _parse_aware_datetime(raw_receipt[receipt_field],
                                               f"原始收据 {receipt_field}")
                if listed != actual:
                    raise DecisionMarketCapError(f"原始报价收据时间不匹配：{batch_receipt_id}")
            elif batch.get(manifest_field) != raw_receipt[receipt_field]:
                raise DecisionMarketCapError(f"原始报价收据 URL 不匹配：{batch_receipt_id}")
        raw_requested = _parse_aware_datetime(raw_receipt["requested_at"], "原始收据 requested_at")
        raw_responded = _parse_aware_datetime(raw_receipt["responded_at"], "原始收据 responded_at")
        raw_first_seen = _parse_aware_datetime(raw_receipt["first_seen_at"],
                                               "原始收据 first_seen_at")
        if raw_requested > raw_responded:
            raise DecisionMarketCapError(f"原始报价收据请求时间晚于响应时间：{batch_receipt_id}")
        if raw_first_seen != raw_responded:
            raise DecisionMarketCapError(
                f"原始报价 SQL first_seen_at 与 responded_at 不一致：{batch_receipt_id}")
        _validate_observation_time(raw_responded, expected_as_of)
        if raw_responded > source_observed or raw_responded > derived_at:
            raise DecisionMarketCapError(f"原始报价晚于重放观测/派生时钟：{batch_receipt_id}")
        response_times.append(raw_responded)
        symbol_results = _parse_tencent_replay_batch(
            raw_payload, market_day=expected_as_of,
            expected_symbols={("sh" if iid.startswith("SH.") else "sz") + iid[3:]
                              for iid in requested_pool},
            receipt_id=batch_receipt_id)
        for iid in sorted(requested_pool):
            symbol = ("sh" if iid.startswith("SH.") else "sz") + iid[3:]
            cents, quote_time = symbol_results[symbol]
            replayed_items[iid] = {
                "market_cap_cents": cents,
                "quote_time": quote_time,
                "receipt_id": batch_receipt_id,
                "content_hash": batch_hash,
            }
        all_seen_ids.update(requested_pool)
    if all_seen_ids != pool_set:
        raise DecisionMarketCapError("历史重放批次未完整覆盖证券池")
    if not response_times or max(response_times) != source_observed:
        raise DecisionMarketCapError("source_observed_at 不是原始报价收据的最晚观测时间")
    manifest_items = manifest.get("items")
    if not isinstance(manifest_items, dict) or set(manifest_items) != pool_set:
        raise DecisionMarketCapError("历史重放数值与完整证券池不一致")
    values: dict[str, int] = {}
    for iid in sorted(pool_set):
        saved = manifest_items.get(iid)
        if not isinstance(saved, dict) or saved != replayed_items[iid]:
            raise DecisionMarketCapError(f"{iid} 重放值/行情时刻/原始收据与原文不匹配")
        values[iid] = int(replayed_items[iid]["market_cap_cents"])
    actual_hash = market_cap_items_hash(values)
    if manifest.get("items_hash") != actual_hash:
        raise DecisionMarketCapError("历史重放清单数值哈希与原始报价不匹配")
    return manifest, values, source_observed, derived_at


def verify_sidecar_archive(sidecar: DecisionMarketCapSidecar,
                           archive_root: str | Path, *,
                           cutoff: datetime | None = None) -> None:
    """核对 sidecar 数值摘要、批次清单、原始响应及其归档收据。"""

    root = Path(archive_root)
    if not sidecar.items:
        raise DecisionMarketCapError("总市值sidecar没有可验证证券")
    db_path = root / "meta.sqlite"
    if not db_path.is_file():
        raise DecisionMarketCapError(f"总市值归档数据库缺失：{db_path}")
    if sidecar.schema_version == REPLAY_SCHEMA_VERSION:
        _validate_sidecar_cutoff(sidecar, cutoff)
        try:
            with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as con:
                con.row_factory = sqlite3.Row
                manifest, values, _observed, _derived = _verify_replay_manifest(
                    con, root, receipt_id=sidecar.receipt_id,
                    content_hash=sidecar.content_hash, source_id=sidecar.source_id,
                    expected_as_of=date.fromisoformat(sidecar.market_cap_as_of),
                    expected_observed_at=sidecar.observed_at_dt,
                    expected_derived_at=sidecar.derived_at_dt,
                    cutoff=cutoff,
                )
        except (sqlite3.Error, OSError, ValueError) as exc:
            if isinstance(exc, DecisionMarketCapError):
                raise
            raise DecisionMarketCapError(f"无法校验历史重放总市值归档：{exc}") from exc
        sidecar_values = {iid: item.market_cap_cents for iid, item in sidecar.items.items()}
        if sidecar_values != values:
            raise DecisionMarketCapError("历史重放 sidecar 数值与原始报价重算结果不匹配")
        if manifest.get("items_hash") != market_cap_items_hash(sidecar_values):
            raise DecisionMarketCapError("历史重放 sidecar 数值摘要不匹配")
        return
    if sidecar.schema_version != SCHEMA_VERSION:
        raise DecisionMarketCapError("决策日总市值 sidecar schema_version 不匹配")
    _validate_sidecar_cutoff(sidecar, cutoff)
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
    verify_frozen_market_caps_archive([
        {"instrument_id": iid, "market_cap_cents": item.market_cap_cents,
         "market_cap_as_of": item.market_cap_as_of,
         "market_cap_source_id": sidecar.source_id,
         "market_cap_receipt_id": sidecar.receipt_id,
         "market_cap_content_hash": sidecar.content_hash,
         "market_cap_observed_at": sidecar.observed_at}
        for iid, item in sidecar.items.items()], archive_root=root,
        expected_as_of=sidecar.market_cap_as_of,
        cutoff=cutoff or sidecar.observed_at_dt)


def verify_frozen_market_caps_archive(instruments: list[dict], *, archive_root: str | Path,
                                      expected_as_of: str, cutoff: datetime,
                                      expected_instrument_ids: list[str] | tuple[str, ...] | None = None) -> None:
    """Verify frozen cap rows against their original batch receipts and values.

    The frozen pool can be a subset of the archived collection. Verification
    therefore replays original bytes instead of comparing a subset items hash
    against the whole collection's hash. This function performs no fetches.
    """
    root = Path(archive_root)
    if not (root / "meta.sqlite").is_file():
        raise DecisionMarketCapError("总市值归档数据库缺失")
    grouped: dict[tuple, list[dict]] = {}
    for row in instruments:
        if row.get("market_cap_cents") is None:
            continue
        key = tuple(row.get("market_cap_" + name) for name in
                    ("source_id", "receipt_id", "content_hash", "observed_at"))
        grouped.setdefault(key, []).append(row)
    as_of = date.fromisoformat(expected_as_of)
    expected_ids = ({normalize_instrument_id(iid) for iid in expected_instrument_ids}
                    if expected_instrument_ids is not None else None)
    with sqlite3.connect((root / "meta.sqlite").resolve().as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        for (source, receipt_id, digest, stamp), rows in grouped.items():
            if source not in SUPPORTED_SOURCE_IDS:
                raise DecisionMarketCapError("冻结总市值缺少受支持的真实来源")
            observed = _parse_aware_datetime(stamp, "market_cap_observed_at")
            _validate_observation_time(observed, as_of)
            if observed > cutoff:
                raise DecisionMarketCapError("冻结总市值观测晚于决策cutoff")
            receipt_row, candidate_payload = _verified_archived_payload(
                con, root, receipt_id=receipt_id, content_hash=digest,
                source_id=source)
            try:
                candidate_manifest = json.loads(candidate_payload)
            except (UnicodeDecodeError, ValueError) as exc:
                raise DecisionMarketCapError("冻结总市值归档清单格式错误") from exc
            if (isinstance(candidate_manifest, dict)
                    and candidate_manifest.get("schema_version") == REPLAY_MANIFEST_SCHEMA_VERSION):
                manifest, values, source_observed, derived = _verify_replay_manifest(
                    con, root, receipt_id=receipt_id, content_hash=digest,
                    source_id=source, expected_as_of=as_of,
                    expected_observed_at=observed, cutoff=cutoff)
                if source_observed != observed:
                    raise DecisionMarketCapError("冻结历史重放原始观测时间不匹配")
                if expected_ids is not None:
                    pool_ids = {normalize_instrument_id(iid)
                                for iid in manifest["pool"]["instrument_ids"]}
                    if pool_ids != expected_ids:
                        raise DecisionMarketCapError(
                            "冻结历史重放证券池与snapshot完整证券池不一致")
                for row in rows:
                    iid = normalize_instrument_id(row["instrument_id"])
                    if (row.get("market_cap_as_of") != expected_as_of
                            or values.get(iid) != row["market_cap_cents"]):
                        raise DecisionMarketCapError(
                            f"{iid} 冻结历史总市值与原始响应不匹配")
                    if row.get("market_cap_derived_at") is not None:
                        row_derived = _parse_aware_datetime(
                            row["market_cap_derived_at"], "market_cap_derived_at")
                        if row_derived != derived:
                            raise DecisionMarketCapError(
                                f"{iid} 冻结派生时钟与真实清单收据不匹配")
                continue
            manifest = json.loads(_verified_receipt_bytes(con, root, receipt_id=receipt_id,
                content_hash=digest, source_id=source, as_of=as_of, observed_at=observed))
            schema = ("aquant.tencent_market_cap_batches.v1" if source == "tencent-qt"
                      else "aquant.eastmoney_market_cap_pages.v1")
            if manifest.get("schema_version") != schema or manifest.get("market_day") != expected_as_of:
                raise DecisionMarketCapError("冻结总市值归档清单来源或日期不匹配")
            children = manifest.get("batches" if source == "tencent-qt" else "pages")
            if not isinstance(children, list) or not children:
                raise DecisionMarketCapError("冻结总市值归档缺少原始批次")
            values: dict[str, int] = {}
            for child in children:
                payload = _verified_receipt_bytes(con, root, receipt_id=child["receipt_id"],
                    content_hash=child["content_hash"], source_id=source, as_of=as_of,
                    observed_at=observed)
                if source == "tencent-qt":
                    parsed = {}
                    for match in re.finditer(r'v_((?:sh|sz)[0-9]{6})="([^"]*)";',
                                             payload.decode("gbk", "replace")):
                        symbol, body = match.groups()
                        fields = body.split("~")
                        if len(fields) <= 45 or fields[2].strip() != symbol[2:]:
                            continue
                        try:
                            quote_at = datetime.strptime(fields[30], "%Y%m%d%H%M%S")
                            cents = market_cap_cents_from_yuan(Decimal(fields[45]) * Decimal("100000000"))
                        except (ValueError, InvalidOperation):
                            continue
                        iid = normalize_instrument_id(symbol[:2] + "." + symbol[2:])
                        if quote_at.date() == as_of and quote_at.time() >= MARKET_CLOSE:
                            if "accepted_ids" not in child or iid in child["accepted_ids"]:
                                if iid in parsed:
                                    raise DecisionMarketCapError("冻结总市值原始响应证券重复")
                                parsed[iid] = cents
                else:
                    raw_rows = (json.loads(payload).get("data") or {}).get("diff") or []
                    if isinstance(raw_rows, dict):
                        raw_rows = list(raw_rows.values())
                    parsed = market_cap_cents_from_eastmoney_rows(raw_rows)
                if values.keys() & parsed.keys():
                    raise DecisionMarketCapError("冻结总市值原始批次证券重复")
                values.update(parsed)
            if manifest.get("items_hash") != market_cap_items_hash(values):
                raise DecisionMarketCapError("冻结总市值归档清单与原始数值不匹配")
            for row in rows:
                iid = normalize_instrument_id(row["instrument_id"])
                if row.get("market_cap_as_of") != expected_as_of or values.get(iid) != row["market_cap_cents"]:
                    raise DecisionMarketCapError(f"{iid} 冻结总市值与原始响应不匹配")


def load_sidecar(
    path: str | Path, *, expected_as_of: str | date,
    archive_root: str | Path | None = None,
    cutoff: datetime | None = None,
    expected_instrument_ids: list[str] | tuple[str, ...] | None = None,
) -> DecisionMarketCapSidecar:
    """加载并校验 sidecar；路径存在但不合格时拒绝发布，避免静默用旧值。"""

    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DecisionMarketCapError(f"无法读取决策日总市值 sidecar：{source}: {exc}") from exc
    if not isinstance(raw, dict):
        raise DecisionMarketCapError("决策日总市值 sidecar 必须是 JSON object")
    schema_version = raw.get("schema_version")
    if schema_version not in {SCHEMA_VERSION, REPLAY_SCHEMA_VERSION}:
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
    derived_at: datetime | None = None
    if schema_version == REPLAY_SCHEMA_VERSION:
        derived_at = _parse_aware_datetime(raw.get("derived_at"), "derived_at")
        if derived_at < observed:
            raise DecisionMarketCapError("派生清单生成早于原始报价观测")
    source_id = str(raw.get("source_id") or "").strip()
    receipt_id = str(raw.get("receipt_id") or "").strip()
    content_hash = str(raw.get("content_hash") or "").strip()
    if not source_id or not receipt_id or not content_hash:
        raise DecisionMarketCapError("sidecar 必须记录 source_id、receipt_id、content_hash")
    if source_id not in SUPPORTED_SOURCE_IDS:
        raise DecisionMarketCapError(
            f"不支持的决策日总市值来源：{source_id!r}")
    if schema_version == REPLAY_SCHEMA_VERSION and source_id != "tencent-qt":
        raise DecisionMarketCapError("历史报价重放只支持 tencent-qt")

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
        content_hash=content_hash, items=items, schema_version=schema_version,
        derived_at=derived_at.isoformat() if derived_at is not None else None,
    )
    if schema_version == REPLAY_SCHEMA_VERSION and expected_instrument_ids is not None:
        expected_ids = {normalize_instrument_id(iid) for iid in expected_instrument_ids}
        if set(sidecar.items) != expected_ids:
            raise DecisionMarketCapError("总市值 sidecar 证券集合与冻结证券池不一致")
    if schema_version == REPLAY_SCHEMA_VERSION and archive_root is None:
        raise DecisionMarketCapError("历史重放 sidecar 必须绑定归档收据并重验原始报价")
    if archive_root is not None:
        verify_sidecar_archive(sidecar, archive_root, cutoff=cutoff)
    else:
        _validate_sidecar_cutoff(sidecar, cutoff)
    return sidecar


def inject_sidecar(
    instruments: list[dict[str, Any]], sidecar: DecisionMarketCapSidecar | None,
) -> tuple[int, int]:
    """把已校验值写入快照证券文档，返回 ``(命中, 缺失)``。"""

    if sidecar is None:
        return 0, len(instruments)
    if sidecar.schema_version == REPLAY_SCHEMA_VERSION:
        target_ids = [normalize_instrument_id(row.get("instrument_id"))
                      for row in instruments]
        if len(target_ids) != len(set(target_ids)) or set(target_ids) != set(sidecar.items):
            raise DecisionMarketCapError("历史重放总市值证券集合与snapshot证券池不一致")
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
        if sidecar.schema_version == REPLAY_SCHEMA_VERSION:
            instrument["market_cap_derived_at"] = sidecar.derived_at
        matched += 1
    return matched, len(instruments) - matched


__all__ = [
    "DEFAULT_FILENAME",
    "DEFAULT_SOURCE_ID",
    "DecisionMarketCap",
    "DecisionMarketCapError",
    "DecisionMarketCapSidecar",
    "REPLAY_MANIFEST_SCHEMA_VERSION",
    "REPLAY_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "inject_sidecar",
    "instrument_id_from_eastmoney",
    "load_sidecar",
    "market_cap_cents_from_yuan",
    "market_cap_cents_from_eastmoney_rows",
    "market_cap_cents_from_values",
    "market_cap_items_hash",
    "market_cap_pool_ids_hash",
    "normalize_instrument_id",
    "sidecar_from_eastmoney_rows",
    "sidecar_from_market_cap_values",
    "sidecar_from_replay_manifest",
    "verify_sidecar_archive",
    "verify_frozen_market_caps_archive",
]
