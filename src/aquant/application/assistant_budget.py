"""Explicit price configuration and atomic assistant spend reservations.

No default prices or caps are supplied here. A live call is allowed only when
the server provides a complete, model-matched price schedule and daily cap.
The cap is installation-wide for the Asia/Shanghai calendar day; owner fields
remain on each row for audit attribution. Amounts are budget consumption upper
bounds at the registered upper-bound unit prices, not provider invoice charges.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from aquant.domain.data.db import write_tx

MICRO_CNY_PER_CNY = 1_000_000
PRICE_BASIS = "REGISTERED_PRICE_UPPER_BOUND"
INPUT_FRAMING_TOKEN_ALLOWANCE = 256
MAX_JSON_INT = (1 << 53) - 1
SHANGHAI = ZoneInfo("Asia/Shanghai")

_CONFIG_KEYS = {
    "model": "AQUANT_ASSISTANT_PRICE_MODEL",
    "version": "AQUANT_ASSISTANT_PRICE_VERSION",
    "source": "AQUANT_ASSISTANT_PRICE_SOURCE",
    "effective_from": "AQUANT_ASSISTANT_PRICE_EFFECTIVE_FROM",
    "input_rate": "AQUANT_ASSISTANT_INPUT_PRICE_UPPER_BOUND_MICRO_CNY_PER_MILLION_TOKENS",
    "output_rate": "AQUANT_ASSISTANT_OUTPUT_PRICE_UPPER_BOUND_MICRO_CNY_PER_MILLION_TOKENS",
    "daily_cap": "AQUANT_ASSISTANT_DAILY_CAP_MICRO_CNY",
}
_CONFIG_LABELS = {
    "model": "所选模型的计价标识",
    "version": "有效价格版本",
    "source": "官方价格来源",
    "effective_from": "价格生效日期",
    "input_rate": "输入单价上界",
    "output_rate": "输出单价上界",
    "daily_cap": "每日预算上限",
}


@dataclass(frozen=True, slots=True)
class AssistantBudgetConfig:
    model: str
    price_version: str
    price_source: str
    effective_from: date
    input_rate: int
    output_rate: int
    daily_cap: int

    def public_dict(self) -> dict:
        return {
            "configured": True,
            "model": self.model,
            "priceVersion": self.price_version,
            "priceSource": self.price_source,
            "priceEffectiveFrom": self.effective_from.isoformat(),
            "inputPriceUpperBoundMicroCnyPerMillionTokens": self.input_rate,
            "outputPriceUpperBoundMicroCnyPerMillionTokens": self.output_rate,
            "dailyCapMicroCny": self.daily_cap,
            "currency": "CNY",
            "costBasis": PRICE_BASIS,
            "accountingNote": "按登记的价格上界单价估算预算占用，不代表供应商账单实扣金额",
        }


@dataclass(frozen=True, slots=True)
class Reservation:
    reservation_id: str
    budget_day: str
    input_tokens: int
    output_tokens: int
    reserved_micro_cny: int
    daily_cap_micro_cny: int
    config: AssistantBudgetConfig

    def public_dict(self) -> dict:
        return {
            "status": "RESERVED",
            "reservedUsageCostUpperBoundMicroCny": self.reserved_micro_cny,
            "dailyCapMicroCny": self.daily_cap_micro_cny,
            "priceVersion": self.config.price_version,
            "costBasis": PRICE_BASIS,
        }


class BudgetUnavailable(RuntimeError):
    def __init__(self, code: str, message: str, *, used_upper_bound: int | None, cap: int,
                 unaccounted_legacy_calls: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.used_upper_bound_micro_cny = used_upper_bound
        self.cap_micro_cny = cap
        self.unaccounted_legacy_calls = unaccounted_legacy_calls


class BudgetExceeded(BudgetUnavailable):
    def __init__(self, *, used_upper_bound: int, cap: int,
                 requested_upper_bound: int) -> None:
        super().__init__(
            "ASSISTANT_DAILY_BUDGET_EXCEEDED",
            "本次按登记价格上界计算的预算占用会超过服务端每日上限",
            used_upper_bound=used_upper_bound,
            cap=cap,
        )
        self.requested_upper_bound_micro_cny = requested_upper_bound


def load_budget_config(model_name: str) -> tuple[AssistantBudgetConfig | None, list[dict]]:
    """Parse only explicit server configuration; never consult sample defaults."""

    raw = {name: os.environ.get(key, "").strip()
           for name, key in _CONFIG_KEYS.items()}
    blockers: list[dict] = []
    for name, value in raw.items():
        if not value:
            blockers.append({
                "code": "ASSISTANT_BUDGET_CONFIG_MISSING",
                "message": f"尚未配置{_CONFIG_LABELS[name]}",
                "repairAction": "由服务端管理员按所选模型配置经核实的有效价格与每日上限",
            })
    if blockers:
        return None, blockers

    if raw["model"] != model_name:
        blockers.append({
            "code": "ASSISTANT_PRICE_MODEL_MISMATCH",
            "message": "费用表对应的模型与当前模型不一致",
            "repairAction": "使助手价格模型与服务端所选模型完全一致",
        })
    if len(raw["version"]) > 100 or len(raw["source"]) > 512:
        blockers.append({
            "code": "ASSISTANT_PRICE_REFERENCE_INVALID",
            "message": "模型价格版本或来源长度不合法",
            "repairAction": "配置简短版本标识与官方 HTTPS 价格来源",
        })
    try:
        parsed_source = urlsplit(raw["source"])
        source_valid = (
            parsed_source.scheme == "https" and bool(parsed_source.hostname)
            and parsed_source.port in (None, 443)
            and not parsed_source.username and not parsed_source.password
            and not parsed_source.query and not parsed_source.fragment
            and (parsed_source.hostname == "deepseek.com"
                 or parsed_source.hostname.endswith(".deepseek.com")))
    except ValueError:
        source_valid = False
    if not source_valid:
        blockers.append({
            "code": "ASSISTANT_PRICE_SOURCE_INVALID",
            "message": "助手价格来源必须是 DeepSeek 官方 HTTPS 页面",
            "repairAction": "填写所选模型当前官方价格页面的 HTTPS 地址",
        })

    try:
        effective_from = date.fromisoformat(raw["effective_from"])
        if effective_from.isoformat() != raw["effective_from"]:
            raise ValueError("not canonical ISO date")
        if effective_from > _budget_day():
            raise ValueError("future effective date")
    except ValueError:
        effective_from = date.min
        blockers.append({
            "code": "ASSISTANT_PRICE_EFFECTIVE_DATE_INVALID",
            "message": "助手价格生效日期必须是已生效的 YYYY-MM-DD 日期",
            "repairAction": "填写官方价格已生效日期后重启 API",
        })

    parsed_ints: dict[str, int] = {}
    for name in ("input_rate", "output_rate", "daily_cap"):
        try:
            value = int(raw[name], 10)
            if str(value) != raw[name] or value <= 0 or value > MAX_JSON_INT:
                raise ValueError("out of range")
            parsed_ints[name] = value
        except ValueError:
            blockers.append({
                "code": "ASSISTANT_BUDGET_VALUE_INVALID",
                "message": f"{_CONFIG_LABELS[name]}必须是有效的正数",
                "repairAction": "按服务端约定配置正整数，不留空、不用小数或示例值",
            })

    if blockers:
        return None, blockers
    return AssistantBudgetConfig(
        model=raw["model"],
        price_version=raw["version"],
        price_source=raw["source"],
        effective_from=effective_from,
        input_rate=parsed_ints["input_rate"],
        output_rate=parsed_ints["output_rate"],
        daily_cap=parsed_ints["daily_cap"],
    ), []


def _ceil_price(tokens: int, rate: int) -> int:
    return (tokens * rate + MICRO_CNY_PER_CNY - 1) // MICRO_CNY_PER_CNY


def estimate_input_tokens(instructions: str, context: str) -> int:
    """Conservative input token bound plus fixed provider chat framing."""

    return (len(instructions.encode("utf-8"))
            + len(context.encode("utf-8"))
            + INPUT_FRAMING_TOKEN_ALLOWANCE)


def estimate_reservation(config: AssistantBudgetConfig, *, input_tokens: int,
                         output_tokens: int) -> int:
    if input_tokens <= 0 or output_tokens <= 0:
        raise ValueError("token reservation bounds must be positive")
    amount = (_ceil_price(input_tokens, config.input_rate)
              + _ceil_price(output_tokens, config.output_rate))
    if amount <= 0 or amount > MAX_JSON_INT:
        raise ValueError("reservation amount is outside SQLite integer range")
    return amount


def _budget_day(now: datetime | None = None) -> date:
    current = now or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    return current.astimezone(SHANGHAI).date()


def _utc_bounds(day: date) -> tuple[str, str]:
    start = datetime.combine(day, time.min, tzinfo=SHANGHAI).astimezone(timezone.utc)
    end = datetime.combine(date.fromordinal(day.toordinal() + 1), time.min,
                           tzinfo=SHANGHAI).astimezone(timezone.utc)
    return start.isoformat(), end.isoformat()


def daily_budget_status(con: sqlite3.Connection, config: AssistantBudgetConfig,
                        *, now: datetime | None = None) -> dict:
    """Read upper-bound budget usage; unknown-basis legacy calls block new use."""

    day = _budget_day(now)
    rows = con.execute(
        "SELECT status,reserved_micro_cny,settled_micro_cny "
        "FROM assistant_budget_reservation WHERE budget_day=?",
        (day.isoformat(),),
    ).fetchall()
    used = 0
    for row in rows:
        used += (int(row["settled_micro_cny"] or 0)
                 if row["status"] == "SETTLED"
                 else int(row["reserved_micro_cny"]))

    start, end = _utc_bounds(day)
    legacy_rows = con.execute(
        "SELECT mc.model_call_id FROM model_call mc "
        "WHERE mc.provider='deepseek' AND mc.called_at>=? AND mc.called_at<? "
        "AND mc.outcome IN ('OK','ERROR','TIMEOUT') "
        "AND NOT EXISTS (SELECT 1 FROM assistant_budget_reservation r "
        "WHERE r.model_call_id=mc.model_call_id)",
        (start, end),
    ).fetchall()
    # A legacy cost_micro_cny value has no registered price-basis marker. It
    # may be an invoice value below the required upper bound, so do not use it
    # to admit another provider call.
    unknown = len(legacy_rows)
    complete_usage = None if unknown else used
    remaining = None if unknown else max(0, config.daily_cap - used)
    return {
        "status": "UNKNOWN_USAGE" if unknown else "AVAILABLE",
        "budgetDay": day.isoformat(),
        "usageCostUpperBoundMicroCny": complete_usage,
        "remainingBudgetMicroCny": remaining,
        "dailyCapMicroCny": config.daily_cap,
        "unaccountedLegacyCalls": unknown,
        "currency": "CNY",
        "costBasis": PRICE_BASIS,
    }


def reserve_budget(con: sqlite3.Connection, *, config: AssistantBudgetConfig,
                   provider: str, subject_id: str | None, input_tokens: int,
                   output_tokens: int, now: datetime | None = None) -> Reservation:
    """Atomically check the global cap and reserve before any provider I/O."""

    day = _budget_day(now)
    amount = estimate_reservation(config, input_tokens=input_tokens,
                                  output_tokens=output_tokens)
    reservation_id = "abr-" + hashlib.sha256(
        (f"{day.isoformat()}|{provider}|{config.model}|{subject_id or 'system'}|"
         f"{datetime.now(timezone.utc).isoformat()}|{os.urandom(16).hex()}").encode()
    ).hexdigest()[:24]
    created_at = datetime.now(timezone.utc).isoformat()
    with write_tx(con):
        status = daily_budget_status(con, config, now=now)
        if status["status"] == "UNKNOWN_USAGE":
            raise BudgetUnavailable(
                "ASSISTANT_BUDGET_USAGE_UNKNOWN",
                "本日已有未按登记价格上界核算的 DeepSeek 调用记录，无法安全核算可用预算",
                used_upper_bound=None, cap=config.daily_cap,
                unaccounted_legacy_calls=status["unaccountedLegacyCalls"])
        if status["usageCostUpperBoundMicroCny"] + amount > config.daily_cap:
            raise BudgetExceeded(
                used_upper_bound=status["usageCostUpperBoundMicroCny"],
                cap=config.daily_cap, requested_upper_bound=amount)
        con.execute(
            "INSERT INTO assistant_budget_reservation "
            "(reservation_id,budget_day,subject_id,provider,model,price_version,"
            "price_source,price_effective_from,input_price_micro_cny_per_million_tokens,"
            "output_price_micro_cny_per_million_tokens,daily_cap_micro_cny,"
            "input_tokens_reserved,output_tokens_reserved,reserved_micro_cny,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (reservation_id, day.isoformat(), str(subject_id or "system").strip() or "system",
             provider, config.model, config.price_version, config.price_source,
             config.effective_from.isoformat(), config.input_rate, config.output_rate,
             config.daily_cap, input_tokens, output_tokens, amount, "RESERVED", created_at),
        )
    return Reservation(reservation_id, day.isoformat(), input_tokens, output_tokens,
                       amount, config.daily_cap, config)


def usage_cost_upper_bound(config: AssistantBudgetConfig, *, input_tokens: int,
                           output_tokens: int) -> int:
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("provider usage cannot be negative")
    amount = (_ceil_price(input_tokens, config.input_rate)
              + _ceil_price(output_tokens, config.output_rate))
    if amount > MAX_JSON_INT:
        raise ValueError("usage-based cost upper bound is outside SQLite integer range")
    return amount


def complete_reservation_in_tx(con: sqlite3.Connection, *, reservation: Reservation,
                               model_call_id: str, status: str,
                               settled_micro_cny: int | None,
                               closed_at: str) -> None:
    """Link one archived call and close its budget upper-bound reservation."""

    if status not in {"SETTLED", "HELD"}:
        raise ValueError("reservation can only be settled or held")
    if (status == "SETTLED") != (settled_micro_cny is not None):
        raise ValueError("settled amount does not match reservation status")
    cursor = con.execute(
        "UPDATE assistant_budget_reservation SET status=?,settled_micro_cny=?,"
        "model_call_id=?,closed_at=? WHERE reservation_id=? AND status='RESERVED'",
        (status, settled_micro_cny, model_call_id, closed_at,
         reservation.reservation_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("assistant budget reservation was already closed or missing")
