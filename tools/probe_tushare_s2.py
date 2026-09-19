"""受控探测 Tushare Pro 对 S2 财务字段的真实可用性。

这个脚本是能力探针，不是生产采集器。它只在命令行显式提供
``--token-file`` 后发起网络请求；被导入、被测试或未调用 ``main`` 时不会
联网。请求固定发送到 ``https://api.tushare.pro``，令牌只存在于进程内的
请求体，报告只保存去敏后的响应代码、字段名、行数和拒绝原因。

探针只报告被抽样的证券/报告期，不能把字段存在或小样本通过解释为全市场
覆盖、权限已购买或 PIT 已验收。三张表必须来自同一 Tushare 来源、同一
报告版本，随后交给现有适配器做严格规范化。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.providers.tushare import (  # noqa: E402
    BALANCE_API,
    CASHFLOW_API,
    INCOME_API,
    SOURCE_ID,
    StatementBatch,
    TushareAdapterError,
    TushareValidationError,
    normalize_financial_batches,
)


API_URL = "https://api.tushare.pro"
API_HOST = "api.tushare.pro"

# These are intentionally small.  The defaults cover several industries and
# several recent report periods without turning a capability check into a
# full-market download.
DEFAULT_SAMPLES = ("600519.SH", "601398.SH", "000333.SZ")
DEFAULT_PERIODS = ("20251231", "20260331", "20260630")
DEFAULT_MAX_REQUESTS = 27
DEFAULT_TIMEOUT_SECONDS = 20.0

# Independent non-zero exits make permission failure distinguishable from a
# malformed token or a transport failure in scheduled jobs.
EXIT_OK = 0
EXIT_USAGE_OR_TOKEN = 10
EXIT_CREDENTIALS_INVALID = 11
EXIT_PERMISSION_DENIED = 12
EXIT_PROBE_ERROR = 13

TOKEN_SEGMENT_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])")

_FIELDS = {
    INCOME_API: (
        "ts_code,end_date,ann_date,f_ann_date,report_type,comp_type,update_flag,"
        "n_income_attr_p,n_income,revenue,total_revenue"
    ),
    BALANCE_API: (
        "ts_code,end_date,ann_date,f_ann_date,report_type,comp_type,update_flag,"
        "total_hldr_eqy_exc_min_int"
    ),
    CASHFLOW_API: (
        "ts_code,end_date,ann_date,f_ann_date,report_type,comp_type,update_flag,"
        "n_cashflow_act"
    ),
}

_REQUIRED_FIELDS = {
    INCOME_API: frozenset({
        "ts_code", "end_date", "ann_date", "f_ann_date", "report_type",
        "comp_type", "update_flag", "n_income_attr_p", "n_income",
    }),
    BALANCE_API: frozenset({
        "ts_code", "end_date", "ann_date", "f_ann_date", "report_type",
        "comp_type", "update_flag", "total_hldr_eqy_exc_min_int",
    }),
    CASHFLOW_API: frozenset({
        "ts_code", "end_date", "ann_date", "f_ann_date", "report_type",
        "comp_type", "update_flag", "n_cashflow_act",
    }),
}


class ProbeError(RuntimeError):
    """探针输入、响应或传输异常。"""


class TokenFileError(ProbeError):
    """Token 文件不可读或没有可识别的 token-like 片段。"""


@dataclass(frozen=True, slots=True)
class ApiObservation:
    api_name: str
    sample: str
    period: str
    code: int | None
    message: str | None
    fields: tuple[str, ...]
    rows: tuple[Mapping[str, Any], ...]
    request_ok: bool
    error: str | None = None

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def permission_denied(self) -> bool:
        return self.code == 40203

    @property
    def credentials_invalid(self) -> bool:
        return self.code == 40101

    def as_dict(self, *, token: str | None = None) -> dict[str, Any]:
        return {
            "api_name": self.api_name,
            "sample": self.sample,
            "period": self.period,
            "code": self.code,
            "message": _redact(self.message, token),
            "fields": list(self.fields),
            "row_count": self.row_count,
            "request_ok": self.request_ok,
            "error": _redact(self.error, token),
        }


Transport = Callable[[Mapping[str, Any], float], Any]


def extract_token(text: str) -> str:
    """从带标签文本中提取唯一或最长 token-like 段。

    不记录候选内容。最长规则允许处理 ``TUSHARE_TOKEN=...``、说明文字和
    换行混在一起的文件；调用方仍需通过真实接口验证凭证，不把此函数结果
    视为授权证明。
    """

    candidates = TOKEN_SEGMENT_RE.findall(text)
    if not candidates:
        raise TokenFileError("token file contains no token-like segment")
    return max(candidates, key=len)


def read_token_file(path: str | Path) -> str:
    """读取并提取令牌；异常不包含文件路径或文件内容。"""

    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise TokenFileError("cannot read token file") from exc
    return extract_token(text)


def _redact(value: Any, token: str | None) -> Any:
    if value is None or token is None:
        return value
    if isinstance(value, str):
        return value.replace(token, "[REDACTED]")
    return value


def _response_records(data: Any) -> tuple[tuple[str, ...], tuple[Mapping[str, Any], ...]]:
    """解析 Tushare JSON data={fields: [...], items: [[...]]}。"""

    if not isinstance(data, Mapping):
        return (), ()
    raw_fields = data.get("fields") or []
    fields = tuple(str(field) for field in raw_fields)
    raw_items = data.get("items") or []
    rows: list[Mapping[str, Any]] = []
    for item in raw_items:
        if isinstance(item, Mapping):
            rows.append(dict(item))
            continue
        if not isinstance(item, (list, tuple)):
            raise ProbeError("Tushare response item is not a row")
        if len(item) != len(fields):
            raise ProbeError("Tushare response row length does not match fields")
        rows.append({field: value for field, value in zip(fields, item)})
    return fields, tuple(rows)


def parse_response(payload: bytes | str | Mapping[str, Any]) -> tuple[int | None, str | None, tuple[str, ...], tuple[Mapping[str, Any], ...]]:
    """解析响应但不保留原始 JSON，避免报告意外携带敏感内容。"""

    if isinstance(payload, Mapping):
        body = payload
    else:
        try:
            body = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise ProbeError("Tushare response is not valid JSON") from exc
    if not isinstance(body, Mapping):
        raise ProbeError("Tushare response is not an object")
    raw_code = body.get("code")
    try:
        code = int(raw_code) if raw_code is not None else None
    except (TypeError, ValueError) as exc:
        raise ProbeError("Tushare response code is not numeric") from exc
    message = body.get("msg")
    message_text = str(message) if message not in (None, "") else None
    fields, rows = _response_records(body.get("data"))
    return code, message_text, fields, rows


def https_transport(payload: Mapping[str, Any], timeout: float) -> bytes:
    """只向固定 HTTPS API 发 POST；不允许用户通过参数改目标主机。"""

    parts = urlsplit(API_URL)
    if parts.scheme != "https" or parts.hostname != API_HOST or parts.port not in (None, 443):
        raise ProbeError("refusing non-approved Tushare endpoint")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        API_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "aquant-s2-probe/1"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL is fixed above
        return response.read()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TushareS2Probe:
    """通过显式注入传输执行受控探针。

    未注入传输时保持离线；命令行入口显式传入固定的 HTTPS transport。
    """

    def __init__(self, token: str, *, transport: Transport | None = None,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS,
                 clock: Callable[[], datetime] | None = None) -> None:
        if not token:
            raise TokenFileError("token is empty")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._token = token
        self._transport = transport or _offline_transport
        self._timeout = timeout
        self._clock = clock or _now

    def query(self, api_name: str, sample: str, period: str) -> ApiObservation:
        if api_name not in _FIELDS:
            raise ValueError(f"unsupported API: {api_name}")
        params = {"ts_code": sample, "period": period}
        payload = {
            "api_name": api_name,
            "token": self._token,
            "params": params,
            "fields": _FIELDS[api_name],
        }
        try:
            parsed = self._transport(payload, self._timeout)
            code, message, fields, rows = parse_response(parsed)
            return ApiObservation(
                api_name=api_name, sample=sample, period=period,
                code=code, message=message, fields=fields, rows=rows,
                request_ok=code == 0,
            )
        except Exception as exc:
            # Do not interpolate exception text into the report: a provider
            # client could echo request data or a credential in its message.
            return ApiObservation(
                api_name=api_name, sample=sample, period=period,
                code=None, message=None, fields=(), rows=(), request_ok=False,
                error=type(exc).__name__,
            )

    def run(self, *, samples: Sequence[str] = DEFAULT_SAMPLES,
            periods: Sequence[str] = DEFAULT_PERIODS,
            max_requests: int = DEFAULT_MAX_REQUESTS) -> dict[str, Any]:
        if not samples or not periods:
            raise ValueError("samples and periods must not be empty")
        if max_requests <= 0:
            raise ValueError("max_requests must be positive")

        started_at = self._clock()
        observations: list[ApiObservation] = []
        by_key: dict[tuple[str, str, str], ApiObservation] = {}
        permission_by_api: dict[str, str] = {}
        credential_invalid = False
        permission_denied = False
        stop_reason: str | None = None

        # Probe each API until its entitlement is known.  Once an API returns
        # 40203, more calls to it add no evidence and are skipped.  A 40101 is
        # a credential-wide failure, so the whole probe stops immediately.
        for api_name in (INCOME_API, BALANCE_API, CASHFLOW_API):
            api_denied = False
            for sample in samples:
                for period in periods:
                    if len(observations) >= max_requests:
                        stop_reason = "max_requests_reached"
                        break
                    if api_denied:
                        break
                    obs = self.query(api_name, sample, period)
                    observations.append(obs)
                    by_key[(api_name, sample, period)] = obs
                    if obs.credentials_invalid:
                        credential_invalid = True
                        stop_reason = "credentials_invalid"
                        break
                    if obs.permission_denied:
                        permission_denied = True
                        api_denied = True
                        permission_by_api[api_name] = "denied"
                    elif obs.request_ok:
                        permission_by_api[api_name] = "granted"
                    elif api_name not in permission_by_api:
                        permission_by_api[api_name] = "unknown"
                if stop_reason or api_denied:
                    break
            if stop_reason:
                break

        bundle_checks: list[dict[str, Any]] = []
        normalized_count = 0
        rejection_counts: Counter[str] = Counter()
        pairs = [(sample, period) for sample in samples for period in periods]
        for sample, period in pairs:
            rows = [by_key.get((api, sample, period)) for api in
                    (INCOME_API, BALANCE_API, CASHFLOW_API)]
            if any(obs is None for obs in rows):
                bundle_checks.append({
                    "sample": sample, "period": period,
                    "status": "not_probed",
                    "reason": "not_all_interfaces_observed",
                })
                continue
            assert all(obs is not None for obs in rows)
            if any(not obs.request_ok for obs in rows):
                bundle_checks.append({
                    "sample": sample, "period": period,
                    "status": "rejected",
                    "reason": "api_request_not_successful",
                })
                rejection_counts["api_request_not_successful"] += 1
                continue
            if any(not obs.rows for obs in rows):
                bundle_checks.append({
                    "sample": sample, "period": period,
                    "status": "rejected",
                    "reason": "empty_statement_response",
                })
                rejection_counts["empty_statement_response"] += 1
                continue
            try:
                normalized = normalize_financial_batches(
                    income=StatementBatch(
                        INCOME_API, SOURCE_ID, rows[0].rows,
                        retrieved_at=self._clock()),
                    balancesheet=StatementBatch(
                        BALANCE_API, SOURCE_ID, rows[1].rows,
                        retrieved_at=self._clock()),
                    cashflow=StatementBatch(
                        CASHFLOW_API, SOURCE_ID, rows[2].rows,
                        retrieved_at=self._clock()),
                )
                normalized_count += len(normalized)
                bundle_checks.append({
                    "sample": sample, "period": period,
                    "status": "normalized", "normalized_rows": len(normalized),
                })
            except TushareAdapterError as exc:
                reason = type(exc).__name__ + ": " + str(exc)
                rejection_counts[reason] += 1
                bundle_checks.append({
                    "sample": sample, "period": period,
                    "status": "rejected", "reason": reason,
                })

        api_summary: dict[str, Any] = {}
        for api_name in (INCOME_API, BALANCE_API, CASHFLOW_API):
            api_obs = [obs for obs in observations if obs.api_name == api_name]
            field_counts: Counter[str] = Counter()
            rows_total = 0
            for obs in api_obs:
                field_counts.update(obs.fields)
                rows_total += obs.row_count
            observed_fields = set(field_counts)
            required = _REQUIRED_FIELDS[api_name]
            missing_required = set(required - observed_fields)
            if api_name == INCOME_API and not ({"revenue", "total_revenue"} & observed_fields):
                missing_required.add("revenue|total_revenue")
            api_summary[api_name] = {
                "permission": permission_by_api.get(api_name, "unknown"),
                "requests": len(api_obs),
                "successful_requests": sum(obs.request_ok for obs in api_obs),
                "row_count": rows_total,
                "observed_fields": sorted(observed_fields),
                "field_observation_counts": dict(sorted(field_counts.items())),
                "required_fields": sorted(_REQUIRED_FIELDS[api_name]),
                "missing_required_fields": sorted(missing_required),
                "sample_coverage_only": True,
            }

        statuses = [permission_by_api.get(api, "unknown")
                    for api in (INCOME_API, BALANCE_API, CASHFLOW_API)]
        if all(status == "granted" for status in statuses):
            permission = "granted"
        elif all(status == "denied" for status in statuses):
            permission = "denied"
        elif any(status in {"granted", "denied"} for status in statuses):
            permission = "partial"
        else:
            permission = "unknown"

        return {
            "probe": {
                "source_id": SOURCE_ID,
                "endpoint": API_URL,
                "started_at": started_at.isoformat(),
                "finished_at": self._clock().isoformat(),
                "samples": list(samples),
                "periods": list(periods),
                "max_requests": max_requests,
                "request_count": len(observations),
                "stop_reason": stop_reason,
                "credentials_valid": (False if credential_invalid else
                                       True if observations and any(
                                           obs.code is not None and not obs.credentials_invalid
                                           for obs in observations) else None),
                "permission": permission,
                "permission_denied": permission_denied,
                "coverage_scope": "sampled_only",
            },
            "apis": api_summary,
            "observations": [obs.as_dict(token=self._token) for obs in observations],
            "normalization": {
                "bundle_pairs_checked": len(bundle_checks),
                "normalized_rows": normalized_count,
                "rejected_pairs": sum(
                    check["status"] == "rejected" for check in bundle_checks),
                "rejection_reasons": dict(sorted(rejection_counts.items())),
                "checks": bundle_checks,
            },
        }


def _offline_transport(payload: Mapping[str, Any], timeout: float) -> Any:
    """Default transport is inert; network must be enabled explicitly by CLI."""

    raise ProbeError("network transport is disabled; inject transport explicitly")


def _parse_csv(value: str) -> tuple[str, ...]:
    values = tuple(part.strip() for part in value.split(",") if part.strip())
    if not values:
        raise argparse.ArgumentTypeError("value must contain at least one item")
    return values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", required=True, type=Path,
                        help="包含 Tushare token 或带标签 token 的本地文件")
    parser.add_argument("--output", type=Path,
                        default=Path("deploy/agentctl-q0/tushare-s2-probe.json"),
                        help="去敏 JSON 报告路径")
    parser.add_argument("--samples", type=_parse_csv,
                        default=DEFAULT_SAMPLES,
                        help="逗号分隔的 Tushare 证券代码")
    parser.add_argument("--periods", type=_parse_csv,
                        default=DEFAULT_PERIODS,
                        help="逗号分隔的报告期 YYYYMMDD")
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    return parser


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    report: dict[str, Any] = {
        "probe": {
            "source_id": SOURCE_ID,
            "endpoint": API_URL,
            "token_present": False,
        },
        "error": None,
    }
    token: str | None = None
    try:
        token = read_token_file(args.token_file)
        report["probe"]["token_present"] = True
        probe = TushareS2Probe(token, transport=https_transport, timeout=args.timeout)
        measured = probe.run(samples=args.samples, periods=args.periods,
                             max_requests=args.max_requests)
        # Keep the token-free input metadata from this outer report, then merge
        # only parsed/derived data from the probe.
        report = measured
        report["probe"]["token_present"] = True
        permission = report["probe"].get("permission")
        if report["probe"].get("credentials_valid") is False:
            exit_code = EXIT_CREDENTIALS_INVALID
        elif (permission == "denied"
              or report["probe"].get("permission_denied")):
            exit_code = EXIT_PERMISSION_DENIED
        elif not report["observations"]:
            exit_code = EXIT_PROBE_ERROR
        elif all(not observation["request_ok"]
                 for observation in report["observations"]):
            exit_code = EXIT_PROBE_ERROR
        elif permission != "granted" or report["normalization"]["normalized_rows"] == 0:
            exit_code = EXIT_PROBE_ERROR
        else:
            exit_code = EXIT_OK
    except TokenFileError as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        exit_code = EXIT_USAGE_OR_TOKEN
    except (ProbeError, OSError, ValueError) as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        exit_code = EXIT_PROBE_ERROR
    try:
        _write_report(args.output, report)
    except OSError as exc:
        # There is no useful report path to return if writing itself failed.
        print(f"无法写入探针报告：{args.output} ({type(exc).__name__})", file=sys.stderr)
        return EXIT_PROBE_ERROR
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
