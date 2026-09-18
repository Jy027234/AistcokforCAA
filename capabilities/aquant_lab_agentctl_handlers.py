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
