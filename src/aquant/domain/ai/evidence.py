"""证据研究作业：让模型独立读公告，并与解析结果交叉核对（§5.3、§9）。

为什么值得做"第二次独立抽取"
----------------------------
公司行为已经由确定性解析器抽好了。再让模型读一遍的价值**不是**
"再确认一次"，而是：

  1. 解析器是规则驱动的：它按表头和列位置取日期。公告换一种排版，
     它会抽错，而且抽错时同样"成功"。
  2. 模型的读法是语义的。两条**独立路径**给出同一个值，才是
     "这个值来自公告"的证据；两者不一致本身就是发现。

因此产出里一定有 agreesWithStored。它不决定以谁为准——
不一致时两边证据都留档，由人来看。

引用必须可定位（§15.3）
-----------------------
每条 quote 都会拿去在来源正文里做**精确**匹配。匹配不上时照常留档
并标 located=0：那是"模型引用了一句原文里没有的话"，
本身就是需要被看到的事实。静默丢弃它才是最糟的处理。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, timezone

from aquant.domain.ai.model import ModelUnavailable
from aquant.domain.data.reader import SnapshotReader
from aquant.domain.evidence.store import Citation, locate, record_evidence
from aquant.operations.jobs import Job

#: 抽取提示词。三个要点：
#:   * 只输出 JSON，便于机器核对；
#:   * quote 必须**逐字复制**原文片段——可定位的前提；
#:   * 抽不到就写 null，**不要猜**：猜出来的一致性毫无意义。
EXTRACTION_INSTRUCTIONS = (
    "你是 A 股公告字段抽取器。只依据我给你的公告正文抽取字段。"
    "quote 必须是正文里**原样存在**的一段文字，逐字复制，不得改写、"
    "不得补全、不得跨过省略号拼接。抽取不到的字段写 null，不要猜测。"
    "只输出一个 JSON 对象，不要输出解释文字或代码块标记。"
    "JSON 结构："
    '{"fields":{"per_share_amount":{"value":"字符串或null","quote":"字符串或null"},'
    '"record_date":{"value":"YYYY-MM-DD或null","quote":"..."},'
    '"ex_date":{"value":"YYYY-MM-DD或null","quote":"..."},'
    '"pay_date":{"value":"YYYY-MM-DD或null","quote":"..."}},'
    '"uncertainty":"字符串","counter_evidence":"字符串"}'
)

FIELD_NAMES = ("per_share_amount", "record_date", "ex_date", "pay_date")

_DATE_FIELD = {"record_date": "record_date", "ex_date": "ex_date", "pay_date": "pay_date"}


def research_evidence(con: sqlite3.Connection, reader: SnapshotReader,
                      job: Job, *, provider: object | None = None) -> dict:
    """执行一次证据研究作业。

    provider 可由调用方注入：生产是 DeepSeekProvider，
    验收用确定性替身，因此"作业 -> 证据 -> 卡片"这条链路离线可测。
    **只在没注入时才去构造生产实现**——反过来（先构造再覆盖）
    会让离线路径悄悄尝试联网。
    """

    from aquant.application.assistant import Material, ask_assistant
    from aquant.operations.research_jobs import ResearchJobError

    snapshot_id = job.input_snapshot_id
    payload = json.loads(job.payload_json) if job.payload_json else {}
    instrument_id = payload.get("instrument_id")
    if not snapshot_id or not instrument_id:
        raise ResearchJobError(
            "DATA_NOT_READY",
            "evidence job needs input snapshot and payload.instrument_id",
            "submit with payload {instrument_id: ..., action_id: ...}")

    ref = reader.ref(snapshot_id)
    actions = _corporate_actions(con, instrument_id, payload.get("action_id"))
    if not actions:
        raise ResearchJobError(
            "DATA_NOT_READY", f"no corporate action found for {instrument_id}",
            "run the universe snapshot first; check corporate_actions")

    if provider is None:
        from aquant.adapters.models.deepseek import DeepSeekProvider

        provider = DeepSeekProvider()
    # 来源由调用方给出，**不硬编码**。原先写死 "cninfo"，
    # 于是跑合成快照（只登记了 synthetic-fixture）时被外键拦下。
    # 来源是数据属性，不是代码属性：写死它意味着每加一个来源都要改代码。
    source_id = payload.get("source_id")
    if not source_id:
        raise ResearchJobError(
            "DATA_NOT_READY",
            "evidence job needs payload.source_id",
            "submit with payload {instrument_id:..., source_id: 'cninfo'}；"
            "来源必须是快照 source_registry 里已登记的那个")
    results: list[dict] = []
    for action in actions:
        source_text = _source_text(action)
        if not source_text:
            continue
        out = ask_assistant(
            con, provider,
            materials=[Material(
                source_id=source_id,
                text="公告标题：" + (action.get("source_title") or "")
                     + "\n公告正文摘录：" + source_text,
                contains_personal_data=False)],
            purpose=f"独立抽取公司行为字段 {action['action_id']}",
            data_mode=ref.data_mode, job_id=job.job_id,
            instructions=EXTRACTION_INSTRUCTIONS,
            # 8192 而不是 2048：本次实测 deepseek-flash 会把 2048 个 token
            # 全部用在思考上，正文为空——JSON 被 token 上限截没了。
            # 而"正文为空"在解析层表现为"模型没给引用"，
            # 于是一次截断看起来像模型不听话。
            max_output_tokens=8192)

        parsed = _parse_output(out["text"])
        if not parsed.get("fields"):
            # 解析不出 JSON 时**必须显式失败**。
            #
            # 第一版静默降级：所有字段变成"未抽取"、引用全空，
            # 于是作业"成功"、交叉核对"无不一致"，而实际上什么都没抽到。
            # 那是这个项目里最危险的一种失败——看起来完全正常。
            # 真实原因当时是 token 上限截断（finish_reason=length）。
            raise ResearchJobError(
                "DATA_NOT_READY",
                f"模型没有返回可解析的抽取结果（action={action['action_id']}，"
                f"输出 {out.get('outputTokens')} tokens）："
                + out["text"][:200],
                "检查模型输出是否被 token 上限截断；必要时提高 max_output_tokens，"
                "或换用非思考型模型")
        comparison = _compare(parsed, action, source_text)
        bundle = record_evidence(
            con, instrument_id=instrument_id, source_id=source_id,
            source_url=action.get("source_url"),
            source_title=action.get("source_title") or "",
            source_text=source_text,
            available_at=_available_at(action, ref.as_of_time),
            fact_summary=_summary(action, comparison),
            verification_status=("VERIFIED" if comparison["agrees"]
                                 else "UNVERIFIED"),
            model_version=out["model"], prompt_version="evidence-extract-v1",
            raw_output_hash="sha256:" + hashlib.sha256(
                out["text"].encode("utf-8")).hexdigest(),
            extra={"announced_on": action.get("announced_on"),
                   "structured": comparison["structured"],
                   "citations": comparison["citations"]},
            research_run_id=job.job_id)
        results.append({
            "actionId": action["action_id"], "eventId": bundle.event_id,
            "documentId": bundle.document_id, "modelCallId": out["modelCallId"],
            "agreesWithStored": comparison["agrees"],
            "disagreements": comparison["disagreements"],
            "citations": bundle.citations,
            "locatedCount": sum(1 for c in bundle.citations if c["located"]),
            "citationCount": len(bundle.citations),
            "uncertainty": parsed.get("uncertainty"),
            "counterEvidence": parsed.get("counter_evidence"),
        })

    return {
        "instrumentId": instrument_id, "snapshotId": snapshot_id,
        "actions": results,
        "note": ("每条字段由模型独立抽取并与解析结果逐一比对；"
                 "引用已在来源正文中做精确字符定位，不可定位的同样留档"
                 "（located=0），不静默丢弃。"),
    }


def _corporate_actions(con: sqlite3.Connection, instrument_id: str,
                       action_id: str | None) -> list[dict]:
    """从**快照入库结果**读公司行为。

    比对的两边必须来自不同路径：这里是解析器写进快照的结果，
    另一边是模型读同一份公告原文的结果。
    """

    sql = ("SELECT action_id,instrument_id,action_type,announced_on,record_date,"
           "ex_date,pay_date,cash_per_share_micros,evidence_json,source_url,"
           "source_title FROM corporate_action WHERE instrument_id=?")
    args: list[object] = [instrument_id]
    if action_id:
        sql += " AND action_id=?"
        args.append(action_id)
    return [dict(r) for r in con.execute(sql, args)]


def _source_text(action: dict) -> str:
    """公告原文摘录。解析时看到的就是这些片段，比对必须用同一份文本——
    拿一份"更完整"的正文会让引用定位结果不可比。"""

    raw = action.get("evidence_json")
    if not raw:
        return ""
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return str(raw)
    if isinstance(doc, dict):
        return " ".join(str(v) for v in doc.values() if v)
    return " ".join(str(v) for v in doc) if isinstance(doc, list) else str(doc)


def _available_at(action: dict, fallback: datetime) -> datetime:
    """证据的可得时点。

    announced_on 只有**日期精度**，取当日 15:00 保守处理：
    取开盘会假设"开盘前就看到了"，而我们没有分钟级证据支持这个假设。
    """

    raw = action.get("announced_on")
    if not raw:
        return fallback
    day = date.fromisoformat(str(raw))
    return datetime(day.year, day.month, day.day, 15, 0, tzinfo=timezone.utc)


def _parse_output(text: str) -> dict:
    """从模型输出里取出 JSON。

    模型偶尔会包一层代码块或加一句前言。这里做**有限**清洗：
    只剥离代码块标记并截取第一个花括号到最后一个花括号，
    不做"尽力猜测"式的修复——猜出来的字段没法用于核对。
    """

    # 反引号用 \x60 写，避免在源码里嵌三个反引号。
    # 顺序很重要：先去开头的围栏，**再**去掉语言标记（去掉后可能又有空白），
    # 最后去结尾围栏。第一版先去语言标记，于是 "\n{...}" 这种
    # 前面带回车的输出没被剥掉，JSON 解析直接失败——而失败被
    # 静默降级成"模型没给引用"，看起来像模型不听话。
    fence = "\x60" * 3
    cleaned = text.strip()
    if cleaned.startswith(fence):
        cleaned = cleaned[len(fence):].lstrip()
        cleaned = re.sub(r"^[a-zA-Z]+", "", cleaned).lstrip()
    if cleaned.endswith(fence):
        cleaned = cleaned[:-len(fence)].rstrip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        doc = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _normalise_date(value: object) -> str | None:
    """把模型给的日期规范成 ISO。认不出就返回 None——不猜。"""

    if not isinstance(value, str):
        return None
    text = value.strip().replace("/", "-").replace(".", "-")
    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", text)
    if not match:
        return None
    year, month, day = (int(g) for g in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _stored_date(value: object) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        return None


def _compare(parsed: dict, action: dict, source_text: str) -> dict:
    """把模型抽到的字段与解析结果逐一比对，并把引用做定位。

    金额用**精确十进制**比较，不做浮点容差：
    28.02423 与 28.0242 是两个不同的每股分红，差 0.00003 元/股
    在一万手上就是几百元——"差不多就算一致"会把这类差异抹掉。
    """

    from decimal import Decimal, InvalidOperation

    fields = (parsed or {}).get("fields") or {}
    structured: list[dict] = []
    citations: list[Citation] = []
    disagreements: list[dict] = []

    # --- 金额
    stored_micros = action.get("cash_per_share_micros")
    amount = fields.get("per_share_amount") or {}
    raw_amount = amount.get("value")
    model_micros: int | None = None
    if isinstance(raw_amount, str) and raw_amount.strip():
        try:
            model_micros = int((Decimal(raw_amount.strip()) * 1_000_000)
                               .to_integral_value())
        except (InvalidOperation, ValueError):
            model_micros = None
    if model_micros is not None and stored_micros is not None:
        if model_micros != int(stored_micros):
            disagreements.append({
                "field": "per_share_amount", "stored": int(stored_micros),
                "model": model_micros, "unit": "micros"})
    structured.append({"name": "per_share_amount_micros",
                       "value_text": (str(model_micros) if model_micros is not None
                                      else "null"),
                       "unit": "MICROS", "raw_value": raw_amount,
                       "raw_unit": "TEXT"})
    _cite(amount.get("quote"), source_text, citations)

    # --- 三个日期
    for field in ("record_date", "ex_date", "pay_date"):
        hold = fields.get(field) or {}
        model_day = _normalise_date(hold.get("value"))
        stored_day = _stored_date(action.get(field))
        if model_day is not None and stored_day is not None and model_day != stored_day:
            disagreements.append({"field": field, "stored": stored_day,
                                  "model": model_day, "unit": "DATE"})
        structured.append({"name": field, "value_text": model_day or "null",
                           "unit": "DATE", "raw_value": hold.get("value"),
                           "raw_unit": "TEXT"})
        _cite(hold.get("quote"), source_text, citations)

    return {
        "agrees": not disagreements and bool(structured),
        "disagreements": disagreements,
        "structured": structured,
        "citations": citations,
    }


def _cite(quote: object, source_text: str, out: list[Citation]) -> None:
    """记录一条引用。**空引用也记**——"模型没给证据"本身要能看到。"""

    if not isinstance(quote, str) or not quote.strip():
        out.append(Citation(quote="", located=False))
        return
    out.append(locate(source_text, quote))


def _summary(action: dict, comparison: dict) -> str:
    kind = action.get("action_type") or "CORPORATE_ACTION"
    instrument = action.get("instrument_id")
    if comparison["agrees"]:
        return (f"{instrument} {kind}：模型独立抽取与解析结果一致"
                f"（{action.get('record_date')} / {action.get('ex_date')}）")
    detail = "；".join(
        f"{d['field']}: 解析={d['stored']} 模型={d['model']}"
        for d in comparison["disagreements"]) or "模型未抽出可比对字段"
    return f"{instrument} {kind}：模型抽取与解析结果**不一致**——{detail}"
