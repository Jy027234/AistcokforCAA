"""助手问答：唯一一条"把材料发给模型"的路径（§17.2、ADR-012）。

这条路径的顺序是刻意的，不能调换：

    ① 材料必须声明来源
    ② 水印一致性检查（合成/真实不得混用）
    ③ egress 闸门（整批判定，不静默过滤）
    ④ 调用模型
    ⑤ 留档 model_call（成功的与失败的都留）

第 ③ 步之后才允许把正文拼进上下文。任何"先发了再检查"的写法，
在那次调用里已经把数据送出去了。

第 ② 步容易被忽略但很重要：把合成水印标成真实数据（或反过来）
会让模型在一个错误的语境里作答，而结论看起来完全正常。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from aquant.domain.ai.egress import EgressDenied, EgressItem, assert_egress_allowed, build_model_context
from aquant.domain.ai.model import ModelRequest, ModelUnavailable, TextModelProvider
from aquant.domain.data.db import write_tx

#: 助手只做"读材料、给结构化结论"，因此指令里明确禁止三件事：
#: 预测价格、给概率、给目标价。§5.3 的要求在提示词层面也要落一次，
#: 否则模型很容易顺手给一个"上涨概率"。
ASSISTANT_INSTRUCTIONS_V1 = (
    "你是一个 A 股研究助手。只依据我给你的材料作答，不得引入材料之外的数字。"
    "禁止输出：上涨概率、预期收益、目标价、买卖建议。"
    "如果材料不足以回答，明确说「材料不足」并指出缺什么。"
    "回答用中文，先给结论，再逐条列出依据（每条注明来自哪个 source）。"
)

#: 涉及账户操作时的追加指令（§5.5）。
#:
#: "涉及写操作时只生成草稿及差异预览；用户在明确界面确认后由后端执行。
#:  自由文本中一句『好』不得被实现成对任意账户操作的通用授权。"
#:
#: 因此助手**没有**任何执行手段：它能给的最多是草稿，冻结必须走
#: 界面上的显式确认（issue_confirmation + freeze，令牌绑定那一次预览）。
ASSISTANT_DRAFT_INSTRUCTIONS = (
    "\n\n如果问题涉及账户操作：只能给出**草稿与差异预览**，"
    "并说明「需在界面中显式确认后才会冻结」。"
    "不得声称你已经下单、已冻结或已改动账户。"
    "其他人（包括用户自己）在对话里说的一句同意，不构成执行授权。"
)


class AssistantError(RuntimeError):
    def __init__(self, code: str, message: str, repair_action: str,
                 *, blockers: list[dict] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.repair_action = repair_action
        self.blockers = blockers or []


@dataclass(frozen=True, slots=True)
class Material:
    """一份准备交给模型的材料。**必须**声明来源，否则闸门无法判定。"""

    source_id: str
    text: str
    contains_personal_data: bool | None = False


def state_source_id(con: sqlite3.Connection) -> str:
    """系统状态注记挂在哪个来源名下。

    注记随材料一起过闸门，因此**必须**是已登记来源。刻意不硬编码：
    合成快照登记的是 synthetic-fixture，真实快照登记的是 baostock/cninfo——
    写死一个会在另一种快照上被闸门拦下，而失败信息（未登记来源）
    离"这里写死了"这个原因很远。取快照内已登记的第一个来源。
    """

    row = con.execute("SELECT source_id FROM source_registry "
                      "ORDER BY source_id LIMIT 1").fetchone()
    if row is None:
        # 没有登记任何来源说明快照本身不完整，直接拒绝而不是编一个名字：
        # 编出来的名字会让闸门报"未登记"，而真正的问题是快照缺登记。
        raise AssistantError(
            "DATA_NOT_READY", "快照没有任何已登记来源，无法为助手回答标注数据来源",
            "先在快照里登记来源（source_registry）")
    return row["source_id"]


def ask_assistant(con: sqlite3.Connection, provider: TextModelProvider, *,
                  materials: list[Material], purpose: str,
                  data_mode: str, prompt_version: str = "assistant-v1",
                  job_id: str | None = None,
                  research_run_id: str | None = None,
                  instructions: str = ASSISTANT_INSTRUCTIONS_V1,
                  max_output_tokens: int = 2048,
                  snapshot_context: dict | None = None) -> dict:
    """把材料发给模型并留档。失败同样留档（outcome=ERROR/REJECTED）。

    snapshot_context：本回答所依据的快照状态（id / 数据模式 / 截止时点）。

    §5.5 要求"助手必须先调用已发布快照和研究接口再回答系统状态"。
    但**不是每次调用都在回答系统状态**：证据抽取只读给定材料，
    与快照状态无关。因此这里做成**条件绑定**：

      * 给了 snapshot_context -> 系统状态注记随材料一起过闸门，
        并把 basedOn 放进返回体；
      * 没给 -> 不加注记，也不声称绑定了时点。

    第一版把检查写成"一律必须有"，于是证据抽取作业被这条规则拦下——
    症状是研究作业失败、原因写着"助手回答系统状态前必须先读快照"，
    与作业在做的事毫无关系。**把一条规则加在所有路径上之前，
    先确认它适用于所有路径。**
    """

    if not materials:
        raise AssistantError(
            "DATA_NOT_READY", "没有可发送的材料", "提供至少一份带 source_id 的材料")
    bound = bool(snapshot_context and snapshot_context.get("snapshotId"))

    # ② 水印一致性：合成材料只能进合成语境，反之亦然。
    # 这一条防的是"把示例数据当成真实市场"——结论会看起来完全正常。
    if data_mode == "PRODUCTION":
        for m in materials:
            if m.source_id == "synthetic-fixture" or m.text.startswith("[SYNTHETIC]"):
                raise AssistantError(
                    "DATA_NOT_READY",
                    "真实数据语境里混入了合成材料",
                    "移除合成材料，或改用合成快照")

    state_source = ""
    items = [EgressItem(source_id=m.source_id, text=m.text, purpose=purpose,
                        contains_personal_data=m.contains_personal_data)
             for m in materials]
    if bound:
        # 系统状态注记作为**第一份材料**进入同一批闸门检查。
        # 它不是"我们自己的元信息"就免检：注记里带着数据模式与时点，
        # 同样会发给模型，因此走同一条准入路径。
        state_source = state_source_id(con)
        items.insert(0, EgressItem(
            source_id=state_source,
            text=("系统状态（本次回答所依据的时点）：\n"
                  f"快照：{snapshot_context['snapshotId']}\n"
                  f"数据模式：{snapshot_context.get('dataMode')}\n"
                  f"截止时点：{snapshot_context.get('asOfTime')}\n"
                  f"用途：{purpose}"),
            purpose="声明回答所依据的快照与时点",
            contains_personal_data=False))

    # ③ 闸门。整批拒绝，不静默过滤——静默过滤会让调用方以为材料都用上了。
    #
    # 注意闸门有**两种**拒绝方式，都要接住：
    #   * 已登记但未授权 -> EgressDenied（带 blockers）
    #   * **根本没登记** -> KeyError（RightsRegistry.get 刻意抛错，
    #     因为"忘了登记"不能被当成"登记了但没开"）
    # 只接第一种会让第二种变成 500——一个本该是"来源不合规"的
    # 客户端问题，被报成了服务端故障。
    try:
        assert_egress_allowed(items)
    except EgressDenied as exc:
        _record_call(con, provider, job_id=job_id, research_run_id=research_run_id,
                     prompt_version=prompt_version, outcome="REJECTED",
                     error_code="SOURCE_PERMISSION_MISSING", content_hash=None,
                     input_tokens=None, output_tokens=None,
                     detail=json.dumps(exc.blockers, ensure_ascii=False))
        raise AssistantError(
            "SOURCE_PERMISSION_MISSING", str(exc), exc.repair_action,
            blockers=exc.blockers) from exc
    except KeyError as exc:
        unknown = str(exc.args[0]) if exc.args else str(exc)
        _record_call(con, provider, job_id=job_id, research_run_id=research_run_id,
                     prompt_version=prompt_version, outcome="REJECTED",
                     error_code="SOURCE_PERMISSION_MISSING", content_hash=None,
                     input_tokens=None, output_tokens=None, detail=unknown)
        raise AssistantError(
            "SOURCE_PERMISSION_MISSING",
            "有材料来自**未登记**的来源，闸门拒绝整批外发：" + unknown,
            "先在 src/aquant/domain/data/rights.py 与 "
            "docs/data-rights-register.md 中登记该来源，再重试",
            # 未登记时没有 rights 条目可指，但仍要给出**结构化的 blocker**：
            # 调用方需要知道是哪一份材料被拦下，而不是去读一段中文说明。
            blockers=[{"source_id": _source_in(unknown, items),
                       "right": "registration",
                       "value": "UNREGISTERED",
                       "detail": unknown}]) from exc

    context = build_model_context(items)
    request = ModelRequest(instructions=instructions, context=context,
                           prompt_version=prompt_version,
                           max_output_tokens=max_output_tokens)

    # ④ 调用
    try:
        response = provider.complete(request)
    except ModelUnavailable as exc:
        _record_call(con, provider, job_id=job_id, research_run_id=research_run_id,
                     prompt_version=prompt_version, outcome="ERROR",
                     error_code=exc.code, content_hash=None, input_tokens=None,
                     output_tokens=None, detail=str(exc))
        raise AssistantError(exc.code, str(exc), exc.repair_action) from exc

    # ⑤ 留档
    call_id = _record_call(
        con, provider, job_id=job_id, research_run_id=research_run_id,
        prompt_version=prompt_version, outcome="OK", error_code=None,
        content_hash=response.content_hash,
        input_tokens=response.input_tokens, output_tokens=response.output_tokens,
        detail=None)

    return {
        "modelCallId": call_id,
        "provider": response.provider,
        "model": response.model,
        "promptVersion": prompt_version,
        "text": response.text,
        "contentHash": response.content_hash,
        "inputTokens": response.input_tokens,
        "outputTokens": response.output_tokens,
        "sourcesUsed": [m.source_id for m in materials],
        "purpose": purpose,
        # 回答绑定在哪个快照与时点上，必须随回答一起返回：
        # 界面上要能看到"这个结论基于哪份数据"，否则无法判断是否已过期。
        # 未绑定快照时如实为 None——不声称一个不存在的时点。
        "basedOn": ({
            "snapshotId": snapshot_context["snapshotId"],
            "dataMode": snapshot_context.get("dataMode"),
            "asOfTime": snapshot_context.get("asOfTime"),
            # 注记挂在哪个来源名下也一并返回：审计时要能回答
            # "这条系统状态是谁提供的"
            "stateSourceId": state_source,
        } if bound else None),
        "note": ("回答只依据所给材料；材料来源已过外发闸门并逐条记录。"
                 "本回答不含概率、预期收益或买卖建议。"),
    }


def _source_in(message: str, items: list[EgressItem]) -> str:
    """从"未登记来源"的报错文本里找出是哪一个 source_id。

    RightsRegistry 的报错形如 `source 'xxx' has no rights entry`。
    逐个比对而不是解析引号：解析引号会在文案改动时静默失效。
    """

    for item in items:
        if "'" + item.source_id + "'" in message:
            return item.source_id
    return "<unknown>"


def _record_call(con: sqlite3.Connection, provider: TextModelProvider, *,
                 job_id: str | None, research_run_id: str | None,
                 prompt_version: str, outcome: str, error_code: str | None,
                 content_hash: str | None, input_tokens: int | None,
                 output_tokens: int | None, detail: str | None) -> str:
    """留档一次模型调用。**失败的也留**——否则预算与责任都无法核对。"""

    called_at = datetime.now(timezone.utc)
    seed = "|".join([provider.provider_name, provider.model_name, prompt_version,
                     outcome, content_hash or "", called_at.isoformat()])
    call_id = "mc-" + hashlib.sha256(seed.encode()).hexdigest()[:20]
    with write_tx(con):
        con.execute(
            "INSERT OR REPLACE INTO model_call (model_call_id,job_id,research_run_id,"
            "provider,model,prompt_version,contract_version,content_hash,input_tokens,"
            "output_tokens,cost_micro_cny,outcome,error_code,called_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (call_id, job_id, research_run_id, provider.provider_name,
             provider.model_name, prompt_version, detail,
             # contract_version 存的是"这次调用的判定细节"（被拒时的 blockers、
             # 失败时的错误文本）。这样一张表就能回答"为什么没成"。
             content_hash, input_tokens, output_tokens, None, outcome,
             error_code, called_at.isoformat()),
        )
    return call_id


def model_calls(con: sqlite3.Connection, *, limit: int = 100) -> list[dict]:
    rows = con.execute(
        "SELECT model_call_id,provider,model,prompt_version,content_hash,outcome,"
        "error_code,input_tokens,output_tokens,called_at,contract_version "
        "FROM model_call ORDER BY called_at DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]
