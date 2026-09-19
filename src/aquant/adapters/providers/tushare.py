"""窄化的 Tushare Pro 财报适配层。

这个模块只负责两件事：把 Tushare Pro 的三张表变成一个可校验的、带
PIT/修订元数据的记录，以及把「字段存在」和「真实可用」明确分开。

默认实例是离线的：不会读取 Token、创建 Tushare 客户端或发起网络请求。
调用方必须显式注入一个已经授权的客户端，或注入测试/生产传输函数。这样
能力探针可以在没有凭证的开发环境中稳定运行，也不会把凭证带进异常或结果。

S2 的 F07--F10 要求同一报告期、同一公告版本、同一合并口径的利润表、
资产负债表和现金流量表。适配器因此拒绝缺列、缺公告日、非合并报表、
跨来源或跨修订拼接；它不会用另一来源的同名字段悄悄补齐。

金额字段保留为 ``Decimal``，并在结果中明确标注 ``amount_unit='yuan'``。
这里不把 Tushare 的字段直接伪装成已通过全市场覆盖/PIT 验收的领域因子；
覆盖率、权限、历史修订可见性仍需由上层能力探针测量。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Mapping, Protocol


SOURCE_ID = "tushare-pro"
AMOUNT_UNIT = "yuan"

# Tushare report_type=1 is the normal consolidated statement. 4 is the
# adjusted consolidated statement and 5 retains the pre-adjustment value.
# Keeping both sides is necessary to audit a revision chain; parent-only and
# single-quarter report types are not safe inputs for the S2 bundle.
CONSOLIDATED_REPORT_TYPES = frozenset({"1", "4", "5"})
# 7 is diversified financials in the provider contract.  The adapter keeps
# it; the factor layer must still apply the separate financial-industry
# template required by the product specification.
COMPANY_TYPES = frozenset({"1", "2", "3", "4", "7"})

INCOME_API = "income"
BALANCE_API = "balancesheet"
CASHFLOW_API = "cashflow"

_METADATA_FIELDS = (
    "ts_code",
    "end_date",
    "ann_date",
    "f_ann_date",
    "report_type",
    "comp_type",
    "update_flag",
)


class TushareAdapterError(RuntimeError):
    """适配器拒绝了输入或无法完成一次显式请求。"""


class TushareOfflineError(TushareAdapterError):
    """未注入客户端/传输；默认离线模式不会发起网络请求。"""


class TushareRequestError(TushareAdapterError):
    """已经显式注入传输，但请求失败。"""


class TushareValidationError(TushareAdapterError, ValueError):
    """源记录无法证明满足 S2 口径。"""


class QueryClient(Protocol):
    """Tushare Pro ``pro_api`` 客户端的最小可注入接口。"""

    def query(self, api_name: str, **params: Any) -> Any:
        ...


Transport = Callable[[str, Mapping[str, Any]], Any]


@dataclass(frozen=True, slots=True)
class StatementBatch:
    """一次单一来源查询的原始批次。

    ``rows`` 保留原始字段，``request`` 不应包含 Token；适配器本身从不
    读取或生成 Token。``retrieved_at`` 是抓取时间，不会被当作公告时间。
    """

    api_name: str
    source_id: str
    rows: tuple[Mapping[str, Any], ...]
    retrieved_at: datetime | None = None
    request: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.api_name:
            raise TushareValidationError("statement batch api_name is required")
        if not self.source_id:
            raise TushareValidationError("statement batch source_id is required")
        if self.retrieved_at is not None and self.retrieved_at.tzinfo is None:
            raise TushareValidationError("retrieved_at must be timezone-aware")
        if any(not isinstance(row, Mapping) for row in self.rows):
            raise TushareValidationError("statement rows must be mappings")


@dataclass(frozen=True, slots=True)
class RevisionMetadata:
    """报告版本与合并口径；这些字段必须在三张表中完全一致。"""

    ann_date: date
    f_ann_date: date | None
    report_type: str
    comp_type: str
    update_flag: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ann_date": self.ann_date.isoformat(),
            "f_ann_date": (self.f_ann_date.isoformat()
                            if self.f_ann_date is not None else None),
            "report_type": self.report_type,
            "comp_type": self.comp_type,
            "update_flag": self.update_flag,
        }


@dataclass(frozen=True, slots=True)
class NormalizedFinancialStatement:
    """可供后续 PIT/因子层消费的一条同源财报记录。

    数值仍是源单位的 Decimal（当前为元），避免在适配层猜测或丢失精度。
    ``raw`` 留下三张表的原始行，便于审计字段映射；结果不包含客户端或
    请求凭证。
    """

    ts_code: str
    end_date: date
    ann_date: date
    f_ann_date: date | None
    report_type: str
    comp_type: str
    update_flag: str
    n_income_attr_p: Decimal
    n_income: Decimal
    revenue: Decimal
    total_hldr_eqy_exc_min_int: Decimal
    n_cashflow_act: Decimal
    source_id: str
    source_apis: tuple[str, str, str]
    retrieved_at: datetime | None
    amount_unit: str = AMOUNT_UNIT
    raw: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    @property
    def revision(self) -> RevisionMetadata:
        return RevisionMetadata(
            ann_date=self.ann_date,
            f_ann_date=self.f_ann_date,
            report_type=self.report_type,
            comp_type=self.comp_type,
            update_flag=self.update_flag,
        )

    def as_dict(self) -> dict[str, Any]:
        """序列化为无客户端、无凭证的可审计字典。"""

        return {
            "ts_code": self.ts_code,
            "end_date": self.end_date.isoformat(),
            "ann_date": self.ann_date.isoformat(),
            "f_ann_date": (self.f_ann_date.isoformat()
                            if self.f_ann_date is not None else None),
            "report_type": self.report_type,
            "comp_type": self.comp_type,
            "update_flag": self.update_flag,
            "n_income_attr_p": str(self.n_income_attr_p),
            "n_income": str(self.n_income),
            "revenue": str(self.revenue),
            "total_hldr_eqy_exc_min_int": str(self.total_hldr_eqy_exc_min_int),
            "n_cashflow_act": str(self.n_cashflow_act),
            "source": {
                "source_id": self.source_id,
                "apis": list(self.source_apis),
                "retrieved_at": (self.retrieved_at.isoformat()
                                  if self.retrieved_at is not None else None),
                "amount_unit": self.amount_unit,
            },
            "revision": self.revision.as_dict(),
            "raw": {name: dict(row) for name, row in self.raw.items()},
        }


def _parse_date(value: Any, *, field_name: str, required: bool) -> date | None:
    if value in (None, ""):
        if required:
            raise TushareValidationError(f"missing {field_name}")
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        if len(text) == 8 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d").date()
        return date.fromisoformat(text[:10])
    except (TypeError, ValueError) as exc:
        raise TushareValidationError(f"invalid {field_name}") from exc


def _text(value: Any, *, field_name: str) -> str:
    if value in (None, ""):
        raise TushareValidationError(f"missing {field_name}")
    text = str(value).strip()
    if not text:
        raise TushareValidationError(f"missing {field_name}")
    return text


def _code(value: Any, *, field_name: str) -> str:
    text = _text(value, field_name=field_name)
    # Tushare codes are strings such as 600519.SH; do not silently coerce a
    # row with a different code into the requested instrument.
    return text


def _decimal(value: Any, *, field_name: str) -> Decimal:
    if value in (None, ""):
        raise TushareValidationError(f"missing {field_name}")
    try:
        out = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise TushareValidationError(f"invalid numeric field {field_name}") from exc
    if not out.is_finite():
        raise TushareValidationError(f"non-finite numeric field {field_name}")
    return out


def _report_type(value: Any) -> str:
    text = _text(value, field_name="report_type")
    if text not in CONSOLIDATED_REPORT_TYPES:
        raise TushareValidationError(
            f"unsupported report_type {text!r}; expected consolidated report")
    return text


def _comp_type(value: Any) -> str:
    text = _text(value, field_name="comp_type")
    if text not in COMPANY_TYPES:
        raise TushareValidationError(f"unsupported comp_type {text!r}")
    return text


def _records(value: Any) -> tuple[Mapping[str, Any], ...]:
    """Convert the common Pro/DataFrame/list response shapes without pandas."""

    if value is None:
        return ()
    if isinstance(value, Mapping):
        for key in ("data", "items", "rows"):
            nested = value.get(key)
            if isinstance(nested, (list, tuple)):
                return tuple(_records(nested))
        return (value,)
    if hasattr(value, "to_dict"):
        try:
            converted = value.to_dict(orient="records")
        except TypeError:
            converted = value.to_dict()
        return _records(converted)
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        rows = tuple(value)
        if any(not isinstance(row, Mapping) for row in rows):
            raise TushareRequestError("statement response contains non-mapping row")
        return rows
    raise TushareRequestError("unsupported statement response shape")


def _batch(api_name: str, value: Any, *, request: Mapping[str, Any],
           retrieved_at: datetime) -> StatementBatch:
    return StatementBatch(
        api_name=api_name,
        source_id=SOURCE_ID,
        rows=_records(value),
        retrieved_at=retrieved_at,
        request=dict(request),
    )


def _key(row: Mapping[str, Any]) -> tuple[str, str, str, str, str, str, str]:
    """Raw identity key; validation happens in ``_normalize_row``."""

    return tuple(str(row.get(field, "")) for field in _METADATA_FIELDS)  # type: ignore[return-value]


def _normalize_row(row: Mapping[str, Any], *, expected_api: str) -> dict[str, Any]:
    """Validate and normalize one row while retaining source values."""

    missing = [field for field in _METADATA_FIELDS if field not in row]
    if missing:
        raise TushareValidationError(
            f"{expected_api} row missing fields: {', '.join(missing)}")

    ts_code = _code(row.get("ts_code"), field_name="ts_code")
    end_date = _parse_date(row.get("end_date"), field_name="end_date", required=True)
    ann_date = _parse_date(row.get("ann_date"), field_name="ann_date", required=True)
    f_ann_date = _parse_date(row.get("f_ann_date"), field_name="f_ann_date", required=False)
    report_type = _report_type(row.get("report_type"))
    comp_type = _comp_type(row.get("comp_type"))
    update_flag = _text(row.get("update_flag"), field_name="update_flag")

    # Expose all common normalized fields.  Table-specific fields are checked
    # by normalize_financial_batches below, where the source table is known.
    return {
        "ts_code": ts_code,
        "end_date": end_date,
        "ann_date": ann_date,
        "f_ann_date": f_ann_date,
        "report_type": report_type,
        "comp_type": comp_type,
        "update_flag": update_flag,
        "raw": dict(row),
        "identity": (ts_code, end_date.isoformat(),
                     ann_date.isoformat(),
                     f_ann_date.isoformat() if f_ann_date else "",
                     report_type, comp_type, update_flag),
    }


def _unique_rows(batch: StatementBatch) -> dict[tuple[str, str, str, str, str, str, str], Mapping[str, Any]]:
    if not batch.rows:
        raise TushareValidationError(f"{batch.api_name} returned no statement rows")
    out: dict[tuple[str, str, str, str, str, str, str], Mapping[str, Any]] = {}
    for row in batch.rows:
        normalized = _normalize_row(row, expected_api=batch.api_name)
        identity = normalized["identity"]
        if identity in out:
            raise TushareValidationError(
                f"{batch.api_name} contains duplicate revision key {identity}")
        out[identity] = row
    return out


def normalize_financial_batches(*, income: StatementBatch,
                                balancesheet: StatementBatch,
                                cashflow: StatementBatch) -> tuple[NormalizedFinancialStatement, ...]:
    """把三张同源表按完整修订键合并；任何不一致都显式拒绝。

    这里的合并只允许同一个 ``source_id``。它不是跨源 fallback；如果
    需要比较另一源，调用方应另建一批记录并单独标识实验。
    """

    batches = (income, balancesheet, cashflow)
    expected = (INCOME_API, BALANCE_API, CASHFLOW_API)
    for batch, api_name in zip(batches, expected):
        if batch.api_name != api_name:
            raise TushareValidationError(
                f"expected {api_name} batch, got {batch.api_name}")
    source_ids = {batch.source_id for batch in batches}
    if source_ids != {SOURCE_ID}:
        raise TushareValidationError(
            f"cross-source statement stitching is forbidden: {sorted(source_ids)}")

    keyed = tuple(_unique_rows(batch) for batch in batches)
    identities = [set(rows) for rows in keyed]
    if not (identities[0] == identities[1] == identities[2]):
        raise TushareValidationError(
            "income, balancesheet and cashflow revisions/periods do not match")

    out: list[NormalizedFinancialStatement] = []
    for identity in sorted(identities[0]):
        inc = _normalize_row(keyed[0][identity], expected_api=INCOME_API)
        bal = _normalize_row(keyed[1][identity], expected_api=BALANCE_API)
        cfl = _normalize_row(keyed[2][identity], expected_api=CASHFLOW_API)

        # F09 的规范字段是「营业收入」(revenue)。营业总收入
        # (total_revenue) 是不同会计项目，在金融企业尤其可能不同，不能把
        # 两者不相等误判成供应商错误。仅当 revenue 缺失时才显式降级到
        # total_revenue，并由上层能力报告记录该口径。
        revenue_name = ("revenue"
                        if keyed[0][identity].get("revenue") not in (None, "")
                        else "total_revenue")
        revenue_raw = keyed[0][identity].get(revenue_name)
        if revenue_raw in (None, ""):
            raise TushareValidationError(
                "income row missing revenue/total_revenue")
        revenue_value = _decimal(revenue_raw, field_name=revenue_name)

        values = {
            "n_income_attr_p": _decimal(
                keyed[0][identity].get("n_income_attr_p"),
                field_name="n_income_attr_p"),
            "n_income": _decimal(
                keyed[0][identity].get("n_income"), field_name="n_income"),
            "revenue": revenue_value,
            "total_hldr_eqy_exc_min_int": _decimal(
                keyed[1][identity].get("total_hldr_eqy_exc_min_int"),
                field_name="total_hldr_eqy_exc_min_int"),
            "n_cashflow_act": _decimal(
                keyed[2][identity].get("n_cashflow_act"),
                field_name="n_cashflow_act"),
        }

        metadata = (inc, bal, cfl)
        meta_fields = ("ts_code", "end_date", "ann_date", "f_ann_date",
                       "report_type", "comp_type", "update_flag")
        for field_name in meta_fields:
            if any(row[field_name] != inc[field_name] for row in metadata):
                raise TushareValidationError(
                    f"statement metadata mismatch for {field_name}")

        retrieved = {batch.retrieved_at for batch in batches}
        retrieved_at = (next(iter(retrieved)) if len(retrieved) == 1
                        else max((x for x in retrieved if x is not None), default=None))
        out.append(NormalizedFinancialStatement(
            ts_code=inc["ts_code"],
            end_date=inc["end_date"],
            ann_date=inc["ann_date"],
            f_ann_date=inc["f_ann_date"],
            report_type=inc["report_type"],
            comp_type=inc["comp_type"],
            update_flag=inc["update_flag"],
            **values,
            source_id=SOURCE_ID,
            source_apis=(income.api_name, balancesheet.api_name, cashflow.api_name),
            retrieved_at=retrieved_at,
            raw={
                INCOME_API: dict(keyed[0][identity]),
                BALANCE_API: dict(keyed[1][identity]),
                CASHFLOW_API: dict(keyed[2][identity]),
            },
        ))
    return tuple(out)


class TushareProFinancials:
    """显式注入的 Tushare Pro 财报读取器。

    ``client`` 与 ``transport`` 二选一。两者都不提供时保持离线并在读取
    时抛出 ``TushareOfflineError``。构造函数不导入 tushare，也不读取环境
    变量，因此单元测试和 CLI 的默认启动都不会意外联网。
    """

    source_id = SOURCE_ID

    def __init__(self, *, client: QueryClient | None = None,
                 transport: Transport | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        if client is not None and transport is not None:
            raise ValueError("inject either client or transport, not both")
        if client is not None and not callable(getattr(client, "query", None)):
            raise TypeError("client must provide query(api_name, **params)")
        self._client = client
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _query(self, api_name: str, params: Mapping[str, Any]) -> StatementBatch:
        if self._client is None and self._transport is None:
            raise TushareOfflineError(
                "Tushare Pro is offline by default; inject client or transport")
        request = dict(params)
        try:
            if self._client is not None:
                result = self._client.query(api_name, **request)
            else:
                assert self._transport is not None
                result = self._transport(api_name, request)
        except Exception as exc:
            # Avoid echoing provider exceptions: some client versions include
            # connection details or credentials in their error text.
            raise TushareRequestError(f"{api_name} request failed") from exc
        return _batch(api_name, result, request=request, retrieved_at=self._clock())

    def fetch_bundle(self, ts_code: str, *, end_date: str | None = None,
                     period: str | None = None,
                     limit: int | None = None) -> tuple[NormalizedFinancialStatement, ...]:
        """请求三张表并规范化；任一表失败即失败，不做跨源 fallback。"""

        ts_code = _code(ts_code, field_name="ts_code")
        if limit is not None and (isinstance(limit, bool) or limit <= 0):
            raise ValueError("limit must be a positive integer")
        params: dict[str, Any] = {"ts_code": ts_code}
        if end_date is not None:
            params["end_date"] = end_date
        if period is not None:
            params["period"] = period
        if limit is not None:
            params["limit"] = limit
        income = self._query(INCOME_API, params)
        balancesheet = self._query(BALANCE_API, params)
        cashflow = self._query(CASHFLOW_API, params)
        return normalize_financial_batches(
            income=income, balancesheet=balancesheet, cashflow=cashflow)
