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
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from aquant.domain.ai.egress import EgressDenied, EgressItem, assert_egress_allowed, build_model_context
from aquant.domain.ai.model import ModelRequest, ModelUnavailable, TextModelProvider
from aquant.domain.ai.archive import (
    _archive_output_in_tx,
    input_hash as model_input_hash,
    load_archive,
    output_hash as model_output_hash,
)
from aquant.domain.data.db import write_tx
from aquant.application.assistant_budget import (
    BudgetExceeded,
    BudgetUnavailable,
    MAX_JSON_INT,
    PRICE_BASIS,
    Reservation,
    complete_reservation_in_tx,
    estimate_input_tokens,
    load_budget_config,
    reserve_budget,
    usage_cost_upper_bound,
)

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
                 *, blockers: list[dict] | None = None,
                 budget: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.repair_action = repair_action
        self.blockers = blockers or []
        self.budget = budget


@dataclass(frozen=True, slots=True)
class Material:
    """一份准备交给模型的材料。**必须**声明来源，否则闸门无法判定。"""

    source_id: str
    text: str
    contains_personal_data: bool | None = False


_PRIVATE_QUESTION_TERMS = (
    "账户", "持仓", "我的仓位", "成交明细", "交易记录", "资金账号",
    "银行卡", "身份证", "手机号", "电话号码", "个人信息", "个人资料",
)


def _question_contains_private_data(question: str) -> bool:
    compact = re.sub(r"[\s，。！？：；、,.!?;:/\\_()（）\[\]{}]+", "", question)
    return (
        any(term in compact for term in _PRIVATE_QUESTION_TERMS)
        or re.search(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", question) is not None
        or re.search(r"https?://|www\.", question, flags=re.IGNORECASE) is not None
        or re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", question) is not None
        or re.search(r"(?<!\d)\d{16,19}(?!\d)", question) is not None
        or re.search(r"(?<!\d)\d{17}[\dXx](?!\d)", question) is not None
    )


def assistant_readiness(provider: TextModelProvider, *,
                        data_mode: str | None = None) -> dict:
    """Return safe-to-display provider readiness without reading a secret.

    Live DeepSeek calls stay closed until the model, key-file path and endpoint
    are explicitly configured and an effective price/cap enforcement path
    exists.  File checks use metadata only; this function never reads key bytes.
    Test stubs and archive replays are explicit non-network providers.
    """

    if getattr(provider, "test_only", False):
        return {"enabled": True, "mode": "TEST_STUB", "blockingIssues": []}
    if getattr(provider, "replay_only", False):
        return {"enabled": True, "mode": "ARCHIVE_REPLAY", "blockingIssues": []}

    blockers: list[dict] = []
    if data_mode == "SYNTHETIC":
        blockers.append({
            "code": "SYNTHETIC_DATA_EGRESS_DISABLED",
            "message": "演示数据不能发送给真实模型",
            "repairAction": "切换到符合条件的真实研究卡；演示数据仅用于离线测试",
        })

    if getattr(provider, "provider_name", "") != "deepseek":
        blockers.append({
            "code": "MODEL_PROVIDER_UNCONFIGURED",
            "message": "当前模型提供方没有经过受控配置",
            "repairAction": "配置项目明确批准的模型提供方后再启用助手",
        })
    else:
        configured_model = os.environ.get("AQUANT_DEEPSEEK_MODEL", "").strip()
        if not configured_model:
            blockers.append({
                "code": "MODEL_SELECTION_UNCONFIGURED",
                "message": "尚未显式配置模型",
                "repairAction": "配置服务端模型名称后重启 API",
            })
        elif getattr(provider, "model_name", None) != configured_model:
            blockers.append({
                "code": "MODEL_SELECTION_MISMATCH",
                "message": "运行中的模型与显式配置不一致",
                "repairAction": "统一服务端模型配置并重启 API",
            })

        configured_url = os.environ.get("AQUANT_DEEPSEEK_BASE_URL", "").strip()
        if not configured_url:
            blockers.append({
                "code": "MODEL_ENDPOINT_UNCONFIGURED",
                "message": "尚未显式配置模型服务地址",
                "repairAction": "配置经批准的 HTTPS 模型服务地址后重启 API",
            })
        else:
            parsed_url = urlsplit(configured_url)
            if parsed_url.scheme != "https" or not parsed_url.netloc:
                blockers.append({
                    "code": "MODEL_ENDPOINT_INVALID",
                    "message": "模型服务地址必须是有效的 HTTPS 地址",
                    "repairAction": "配置经批准的 HTTPS 模型服务地址后重启 API",
                })
            elif (parsed_url.hostname != "api.deepseek.com"
                  or parsed_url.port not in (None, 443)
                  or parsed_url.username or parsed_url.password
                  or parsed_url.path not in ("", "/")
                  or parsed_url.query or parsed_url.fragment):
                blockers.append({
                    "code": "MODEL_ENDPOINT_UNAPPROVED",
                    "message": "当前助手只接入项目已有的 DeepSeek 官方 HTTPS 服务路径",
                    "repairAction": "使用已接入的官方服务地址；其他代理尚未纳入助手配置",
                })
            elif getattr(provider, "base_url", None) != configured_url:
                blockers.append({
                    "code": "MODEL_ENDPOINT_MISMATCH",
                    "message": "运行中的模型服务地址与显式配置不一致",
                    "repairAction": "统一服务端模型地址配置并重启 API",
                })

        key_file = os.environ.get("AQUANT_DEEPSEEK_KEY_FILE", "").strip()
        if not key_file:
            blockers.append({
                "code": "MODEL_CREDENTIAL_UNCONFIGURED",
                "message": "尚未配置服务端模型凭据文件",
                "repairAction": "将密钥文件安全挂载到 API，并配置 AQUANT_DEEPSEEK_KEY_FILE",
            })
        else:
            try:
                path = Path(key_file).expanduser()
                key_file_ready = (path.is_file() and os.access(path, os.R_OK)
                                  and path.stat().st_size > 0)
            except OSError:
                key_file_ready = False
            if not key_file_ready:
                blockers.append({
                    "code": "MODEL_CREDENTIAL_UNAVAILABLE",
                    "message": "模型凭据文件不存在、不可读或为空",
                    "repairAction": "检查 API 容器内的只读密钥挂载及文件权限",
                })

        budget_config, budget_blockers = load_budget_config(
            getattr(provider, "model_name", ""))
        blockers.extend(budget_blockers)
        budget = (budget_config.public_dict() if budget_config is not None
                  else {"configured": False})
    if getattr(provider, "provider_name", "") != "deepseek":
        budget = {"configured": False}

    return {"enabled": not blockers, "mode": "DISABLED" if blockers else "LIVE",
            "blockingIssues": blockers, "budget": budget}


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
                  max_output_tokens: int = 8192,
                  snapshot_context: dict | None = None,
                  question: str | None = None,
                  call_metadata: dict | None = None,
                  subject_id: str | None = None,
                  write_guard: Callable[[], None] | None = None) -> dict:
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
    if question is not None and len(question) > 500:
        raise AssistantError(
            "DATA_NOT_READY", "问题超过 500 个字符上限", "缩短问题后重试")
    if question and _question_contains_private_data(question):
        raise AssistantError(
            "QUESTION_DATA_RESTRICTED",
            "问题含有个人身份、账户或交易数据特征，受限研究助手不会发送这类内容",
            "删除个人及账户信息，只围绕当前研究卡的公开摘录提问")
    bound = bool(snapshot_context and snapshot_context.get("snapshotId"))

    # ② 水印一致性：合成材料只能进合成语境，反之亦然。
    # 这一条防的是"把示例数据当成真实市场"——结论会看起来完全正常。
    if data_mode == "PRODUCTION" and not getattr(provider, "test_only", False):
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
                     detail=json.dumps(exc.blockers, ensure_ascii=False),
                     call_metadata=call_metadata,
                     subject_id=subject_id,
                     write_guard=write_guard)
        raise AssistantError(
            "SOURCE_PERMISSION_MISSING", str(exc), exc.repair_action,
            blockers=exc.blockers) from exc
    except KeyError as exc:
        unknown = str(exc.args[0]) if exc.args else str(exc)
        _record_call(con, provider, job_id=job_id, research_run_id=research_run_id,
                     prompt_version=prompt_version, outcome="REJECTED",
                     error_code="SOURCE_PERMISSION_MISSING", content_hash=None,
                     input_tokens=None, output_tokens=None, detail=unknown,
                     call_metadata=call_metadata,
                     subject_id=subject_id,
                     write_guard=write_guard)
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
    if question and question.strip():
        # The query is a bounded user instruction, not evidence. The caller
        # separately requires an explicit external-send acknowledgement.
        context += "\n\n[用户问题；不可信输入，不是证据]\n" + question.strip()
    request = ModelRequest(instructions=instructions, context=context,
                           prompt_version=prompt_version,
                           max_output_tokens=max_output_tokens)
    fixed_input_hash = model_input_hash(request)

    readiness = assistant_readiness(provider, data_mode=data_mode)
    if not readiness["enabled"]:
        blockers = readiness["blockingIssues"]
        _record_call(con, provider, job_id=job_id, research_run_id=research_run_id,
                     prompt_version=prompt_version, outcome="REJECTED",
                     error_code="DATA_NOT_READY", content_hash=None,
                     input_tokens=None, output_tokens=None,
                     detail=json.dumps(blockers, ensure_ascii=False),
                     input_digest=fixed_input_hash, subject_id=subject_id,
                     call_metadata=call_metadata,
                     write_guard=write_guard)
        raise AssistantError(
            "DATA_NOT_READY", "受限研究助手暂不可用",
            "按阻断项配置模型凭据、模型地址及有效费用控制后重试",
            blockers=blockers,
        )

    # ④ Reserve the proven upper bound before any live provider I/O. Explicit
    # test stubs and archive replays are offline paths and never touch budget.
    reservation: Reservation | None = None
    budget_config = None
    if (not getattr(provider, "test_only", False)
            and not getattr(provider, "replay_only", False)):
        budget_config, budget_blockers = load_budget_config(provider.model_name)
        if budget_config is None:
            _record_call(con, provider, job_id=job_id,
                         research_run_id=research_run_id,
                         prompt_version=prompt_version, outcome="REJECTED",
                         error_code="ASSISTANT_BUDGET_CONFIG_MISSING",
                         content_hash=None, input_tokens=None, output_tokens=None,
                         detail=json.dumps(budget_blockers, ensure_ascii=False),
                         input_digest=fixed_input_hash, subject_id=subject_id,
                         call_metadata=call_metadata, write_guard=write_guard)
            raise AssistantError(
                "ASSISTANT_BUDGET_CONFIG_MISSING",
                "受限研究助手暂不可用：模型价格或每日费用上限配置不完整",
                "由服务端管理员配置所选模型的有效价格与每日上限",
                blockers=budget_blockers,
            )
        call_metadata = dict(call_metadata or {})
        call_metadata["costBasis"] = PRICE_BASIS
        try:
            reservation = reserve_budget(
                con, config=budget_config, provider=provider.provider_name,
                subject_id=subject_id,
                input_tokens=estimate_input_tokens(request.instructions, request.context),
                output_tokens=request.max_output_tokens,
            )
        except BudgetUnavailable as exc:
            budget_detail = {
                "status": "UNAVAILABLE",
                "dailyCapMicroCny": exc.cap_micro_cny,
                "costBasis": PRICE_BASIS,
            }
            if exc.used_upper_bound_micro_cny is not None:
                budget_detail["usageCostUpperBoundMicroCny"] = exc.used_upper_bound_micro_cny
            if exc.unaccounted_legacy_calls:
                budget_detail["unaccountedLegacyCalls"] = exc.unaccounted_legacy_calls
            if isinstance(exc, BudgetExceeded):
                budget_detail["requestedUsageCostUpperBoundMicroCny"] = (
                    exc.requested_upper_bound_micro_cny)
            detail = {
                "code": exc.code,
                "message": str(exc),
                "repairAction": "等待当日预算重置，或由管理员核对计价记录与有效上限",
                "budget": budget_detail,
            }
            _record_call(con, provider, job_id=job_id,
                         research_run_id=research_run_id,
                         prompt_version=prompt_version, outcome="REJECTED",
                         error_code=exc.code, content_hash=None,
                         input_tokens=None, output_tokens=None,
                         detail=json.dumps(detail, ensure_ascii=False),
                         input_digest=fixed_input_hash, subject_id=subject_id,
                         call_metadata=call_metadata, write_guard=write_guard)
            raise AssistantError(
                exc.code, str(exc), detail["repairAction"], blockers=[{
                    "code": exc.code, "message": str(exc),
                    "repairAction": detail["repairAction"],
                }], budget=detail["budget"],
            ) from exc
        except ValueError as exc:
            blockers = [{
                "code": "ASSISTANT_BUDGET_BOUND_INVALID",
                "message": "无法安全计算本次助手调用的费用上界",
                "repairAction": "检查服务端模型价格配置及输出长度限制",
            }]
            _record_call(con, provider, job_id=job_id,
                         research_run_id=research_run_id,
                         prompt_version=prompt_version, outcome="REJECTED",
                         error_code="ASSISTANT_BUDGET_BOUND_INVALID",
                         content_hash=None, input_tokens=None, output_tokens=None,
                         detail=json.dumps(blockers, ensure_ascii=False),
                         input_digest=fixed_input_hash, subject_id=subject_id,
                         call_metadata=call_metadata, write_guard=write_guard)
            raise AssistantError(
                "ASSISTANT_BUDGET_BOUND_INVALID", blockers[0]["message"],
                blockers[0]["repairAction"], blockers=blockers,
            ) from exc

    # ⑤ Call exactly once. Once reserved, any ambiguous failure remains held
    # until an operator reconciles provider billing; never release it as zero.
    try:
        response = provider.complete(request)
    except ModelUnavailable as exc:
        budget_info = None
        metadata = dict(call_metadata or {})
        if reservation is not None:
            budget_info = {
                "status": "HELD",
                "reservedUsageCostUpperBoundMicroCny": reservation.reserved_micro_cny,
                "inputTokensReserved": reservation.input_tokens,
                "outputTokensReserved": reservation.output_tokens,
                "usageCostUpperBoundMicroCny": None,
                "priceVersion": reservation.config.price_version,
                "costBasis": PRICE_BASIS,
                "message": "供应商账单金额未知，预算仍按调用前价格上界预留；这不代表零费用",
            }
            metadata["budget"] = budget_info
        _record_call(con, provider, job_id=job_id, research_run_id=research_run_id,
                     prompt_version=prompt_version, outcome="ERROR",
                     error_code=exc.code, content_hash=None, input_tokens=None,
                     output_tokens=None, detail=str(exc),
                     input_digest=fixed_input_hash, subject_id=subject_id,
                     call_metadata=metadata if reservation is not None else call_metadata,
                     reservation=reservation, reservation_status="HELD",
                     write_guard=write_guard)
        message = str(exc)
        if reservation is not None:
            message += "；费用未知，预留额度仍占用，不代表零费用"
        raise AssistantError(exc.code, message, exc.repair_action,
                             budget=budget_info) from exc
    except Exception as exc:
        budget_info = None
        metadata = dict(call_metadata or {})
        if reservation is not None:
            budget_info = {
                "status": "HELD",
                "reservedUsageCostUpperBoundMicroCny": reservation.reserved_micro_cny,
                "inputTokensReserved": reservation.input_tokens,
                "outputTokensReserved": reservation.output_tokens,
                "usageCostUpperBoundMicroCny": None,
                "priceVersion": reservation.config.price_version,
                "costBasis": PRICE_BASIS,
                "message": "供应商账单金额未知，预算仍按调用前价格上界预留；这不代表零费用",
            }
            metadata["budget"] = budget_info
        _record_call(con, provider, job_id=job_id, research_run_id=research_run_id,
                     prompt_version=prompt_version, outcome="ERROR",
                     error_code="MODEL_CALL_FAILED_COST_UNKNOWN",
                     content_hash=None, input_tokens=None, output_tokens=None,
                     detail="model provider failed after reservation; cost unknown",
                     input_digest=fixed_input_hash, subject_id=subject_id,
                     call_metadata=metadata if reservation is not None else call_metadata,
                     reservation=reservation, reservation_status="HELD",
                     write_guard=write_guard)
        if reservation is None:
            raise
        raise AssistantError(
            "MODEL_CALL_FAILED_COST_UNKNOWN",
            "模型请求失败；费用未知，预留额度仍占用，不代表零费用",
            "先核对模型服务账单与助手费用记录后再决定是否重试",
            budget=budget_info,
        ) from exc

    budget_info = None
    usage_cost_bound: int | None = None
    reservation_status: str | None = None
    if reservation is not None:
        metadata = dict(call_metadata or {})
        usage_is_valid = (
            type(response.input_tokens) is int and response.input_tokens > 0
            and response.input_tokens <= MAX_JSON_INT
            and type(response.output_tokens) is int and response.output_tokens >= 0
            and response.output_tokens <= MAX_JSON_INT
        )
        if (response.provider != provider.provider_name
                or response.model != reservation.config.model):
            budget_info = {
                "status": "HELD",
                "reservedUsageCostUpperBoundMicroCny": reservation.reserved_micro_cny,
                "inputTokensReserved": reservation.input_tokens,
                "outputTokensReserved": reservation.output_tokens,
                "usageCostUpperBoundMicroCny": None,
                "priceVersion": reservation.config.price_version,
                "costBasis": PRICE_BASIS,
                "message": "实际返回模型与价格配置不一致，供应商账单金额未知，仍保留预算上界",
            }
            reservation_status = "HELD"
            outcome, error_code = "ERROR", "ASSISTANT_PRICE_MODEL_MISMATCH"
            detail = "provider response did not match configured provider/model price"
        elif not usage_is_valid:
            budget_info = {
                "status": "HELD",
                "reservedUsageCostUpperBoundMicroCny": reservation.reserved_micro_cny,
                "inputTokensReserved": reservation.input_tokens,
                "outputTokensReserved": reservation.output_tokens,
                "usageCostUpperBoundMicroCny": None,
                "priceVersion": reservation.config.price_version,
                "costBasis": PRICE_BASIS,
                "message": "模型未返回可核验 usage，供应商账单金额未知，预算仍保留价格上界；这不代表零费用",
            }
            reservation_status = "HELD"
            outcome, error_code, detail = "OK", None, None
        else:
            try:
                usage_cost_bound = usage_cost_upper_bound(
                    reservation.config, input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens)
            except ValueError:
                budget_info = {
                    "status": "HELD",
                    "reservedUsageCostUpperBoundMicroCny": reservation.reserved_micro_cny,
                    "inputTokensReserved": reservation.input_tokens,
                    "outputTokensReserved": reservation.output_tokens,
                    "usageCostUpperBoundMicroCny": None,
                    "priceVersion": reservation.config.price_version,
                    "costBasis": PRICE_BASIS,
                    "message": "模型 usage 无法按登记价格上界安全计算，预算仍保留预留额度；供应商账单未知",
                }
                reservation_status = "HELD"
                outcome, error_code, detail = "OK", None, None
            else:
                reservation_status = "SETTLED"
                token_bound_exceeded = (
                    response.input_tokens > reservation.input_tokens
                    or response.output_tokens > reservation.output_tokens)
                cost_overrun = usage_cost_bound > reservation.reserved_micro_cny
                budget_info = {
                    "status": "SETTLED",
                    "reservedUsageCostUpperBoundMicroCny": reservation.reserved_micro_cny,
                    "inputTokensReserved": reservation.input_tokens,
                    "outputTokensReserved": reservation.output_tokens,
                    "usageCostUpperBoundMicroCny": usage_cost_bound,
                    "dailyCapMicroCny": reservation.daily_cap_micro_cny,
                    "priceVersion": reservation.config.price_version,
                    "costBasis": PRICE_BASIS,
                    "reservationExceeded": token_bound_exceeded or cost_overrun,
                }
                if cost_overrun:
                    budget_info["message"] = (
                        "按登记价格上界计算的预算占用超过调用前预留，已按该上界计入预算；"
                        "此金额不代表供应商账单实扣金额，每日预算上限可能超出")
                elif token_bound_exceeded:
                    budget_info["message"] = (
                        "模型 usage 超过调用前 token 估算上界，已按登记价格上界计算预算占用；"
                        "此金额不代表供应商账单实扣金额")
                outcome, error_code, detail = "OK", None, None
        metadata["budget"] = budget_info
    else:
        metadata = call_metadata
        outcome, error_code, detail = "OK", None, None

    # ⑥ Archive the output and close its reservation in the same write txn.
    # Provider supplied hashes are useful metadata, but the product must hash
    # the bytes it actually received.  This also makes malformed provider
    # metadata visible instead of making the archive unverifiable.
    actual_output_hash = model_output_hash(response.text)
    call_id = _record_call(
        con, provider, job_id=job_id, research_run_id=research_run_id,
        prompt_version=prompt_version, outcome=outcome, error_code=error_code,
        content_hash=actual_output_hash,
        input_tokens=(response.input_tokens if usage_is_valid
                      else None) if reservation is not None else response.input_tokens,
        output_tokens=(response.output_tokens if usage_is_valid
                       else None) if reservation is not None else response.output_tokens,
        detail=detail, input_digest=fixed_input_hash,
        raw_output=(response.text if outcome == "OK" else None),
        call_metadata=metadata,
        subject_id=subject_id,
        cost_micro_cny=usage_cost_bound,
        reservation=reservation,
        reservation_status=reservation_status,
        reservation_cost=usage_cost_bound,
        write_guard=write_guard)

    if outcome != "OK":
        raise AssistantError(
            error_code or "ASSISTANT_CALL_NOT_USABLE",
            budget_info["message"] if budget_info else "模型回答未能核验",
            "核对实际模型、用量和有效价格后再决定是否重试",
            budget=budget_info)
    run_row = con.execute(
        "SELECT model_run_id,archive_id,artifact_hash FROM model_run WHERE model_call_id=?",
        (call_id,),
    ).fetchone()

    return {
        "modelCallId": call_id,
        "modelRunId": run_row["model_run_id"] if run_row else None,
        "archiveId": run_row["archive_id"] if run_row else None,
        "artifactHash": run_row["artifact_hash"] if run_row else None,
        "provider": response.provider,
        "model": response.model,
        "promptVersion": prompt_version,
        "text": response.text,
        "contentHash": actual_output_hash,
        "inputTokens": response.input_tokens,
        "outputTokens": response.output_tokens,
        "budget": budget_info,
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
                 output_tokens: int | None, detail: str | None,
                 input_digest: str | None = None,
                 raw_output: str | None = None,
                 call_metadata: dict | None = None,
                 subject_id: str | None = None,
                 cost_micro_cny: int | None = None,
                 reservation: Reservation | None = None,
                 reservation_status: str | None = None,
                 reservation_cost: int | None = None,
                 write_guard: Callable[[], None] | None = None) -> str:
    """留档一次模型调用。**失败的也留**——否则预算与责任都无法核对。"""

    called_at = datetime.now(timezone.utc)
    if call_metadata is not None:
        detail = json.dumps({"detail": detail, "audit": call_metadata},
                            ensure_ascii=False, sort_keys=True)
    seed = "|".join([provider.provider_name, provider.model_name, prompt_version,
                     outcome, content_hash or "", called_at.isoformat()])
    call_id = "mc-" + hashlib.sha256(seed.encode()).hexdigest()[:20]
    with write_tx(con):
        if write_guard is not None:
            write_guard()
        archive = None
        owner = str(subject_id or "").strip() or "system"
        if raw_output is not None:
            if input_digest is None:
                raise ValueError("an archived model output requires its input hash")
            source_archive_id = getattr(provider, "archive_id", None)
            if source_archive_id:
                # A replay consumes the immutable source archive.  Do not
                # create a second archive under the synthetic
                # ``archive-replay`` provider name; the independent model_run
                # row below is the replay registration.
                archive = load_archive(
                    con, str(source_archive_id), subject_id=owner
                )
                if archive.input_hash != input_digest or archive.output_text != raw_output:
                    raise ValueError("replayed output does not match its source archive")
            else:
                archive = _archive_output_in_tx(
                    con,
                    input_digest=input_digest,
                    output_text=raw_output,
                    provider=provider.provider_name,
                    model=provider.model_name,
                    prompt_version=prompt_version,
                    source_model_call_id=call_id,
                    subject_id=owner,
                )
        con.execute(
            "INSERT OR REPLACE INTO model_call (model_call_id,job_id,research_run_id,"
            "provider,model,prompt_version,contract_version,content_hash,input_tokens,"
            "output_tokens,cost_micro_cny,outcome,error_code,called_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (call_id, job_id, research_run_id, provider.provider_name,
             provider.model_name, prompt_version, detail,
             # contract_version 存的是"这次调用的判定细节"（被拒时的 blockers、
             # 失败时的错误文本）。这样一张表就能回答"为什么没成"。
             content_hash, input_tokens, output_tokens, cost_micro_cny, outcome,
             error_code, called_at.isoformat()),
        )
        con.execute(
            "INSERT OR REPLACE INTO model_call_owner "
            "(model_call_id,subject_id,bound_at) VALUES (?,?,?)",
            (call_id, owner, called_at.isoformat()),
        )
        if reservation is not None:
            if reservation_status not in {"SETTLED", "HELD"}:
                raise ValueError("a budget reservation needs a final status")
            complete_reservation_in_tx(
                con, reservation=reservation, model_call_id=call_id,
                status=reservation_status,
                settled_micro_cny=reservation_cost,
                closed_at=called_at.isoformat())
        # model_call is the historical call index.  model_run is the explicit
        # run registration used by A12: replaying the same archive still gets
        # a new model_run_id and model_call_id.
        if input_digest is not None:
            run_id = "mr-" + hashlib.sha256(call_id.encode()).hexdigest()[:24]
            con.execute(
                "INSERT OR REPLACE INTO model_run "
                "(model_run_id,model_call_id,research_run_id,archive_id,"
                "subject_id,input_hash,output_hash,artifact_hash,provider,model,"
                "prompt_version,run_kind,registered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    call_id,
                    research_run_id,
                    archive.archive_id if archive is not None else None,
                    owner,
                    input_digest,
                    content_hash,
                    archive.artifact_hash if archive is not None else None,
                    provider.provider_name,
                    provider.model_name,
                    prompt_version,
                    "REPLAY" if hasattr(provider, "archive_id") else "ORIGINAL",
                    called_at.isoformat(),
                ),
            )
    return call_id


def model_calls(
    con: sqlite3.Connection,
    *,
    limit: int = 100,
    subject_id: str | None = None,
) -> list[dict]:
    owner_filter = ""
    params: list[object] = []
    if subject_id is not None:
        owner_filter = "WHERE mco.subject_id=?"
        params.append(str(subject_id).strip() or "system")
    params.append(int(limit))
    rows = con.execute(
        "SELECT model_call.model_call_id,model_call.provider,model_call.model,"
        "model_call.prompt_version,model_call.content_hash,model_call.outcome,"
        "model_call.error_code,model_call.input_tokens,model_call.output_tokens,"
        "model_call.called_at,model_call.contract_version,"
        "mco.subject_id,mr.model_run_id,mr.archive_id,mr.input_hash,"
        "mr.artifact_hash,mr.run_kind "
        "FROM model_call LEFT JOIN model_run mr "
        "ON mr.model_call_id=model_call.model_call_id "
        "LEFT JOIN model_call_owner mco "
        "ON mco.model_call_id=model_call.model_call_id "
        + owner_filter + " ORDER BY model_call.called_at DESC LIMIT ?", params).fetchall()
    return [dict(r) for r in rows]
