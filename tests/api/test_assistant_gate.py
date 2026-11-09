"""Card-bound assistant egress, citation verification and fail-closed tests."""

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import sys
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT / "src"))

from aquant.application.assistant import AssistantError, Material, ask_assistant  # noqa: E402
from aquant.application import assistant as assistant_module  # noqa: E402
from aquant.application import assistant_materials as assistant_materials_module  # noqa: E402
from aquant.application.assistant_budget import daily_budget_status, load_budget_config  # noqa: E402
from aquant.domain.ai.model import ModelResponse, ModelUnavailable  # noqa: E402
from aquant.domain.ai.archive import ArchivedModelProvider  # noqa: E402
from aquant.domain.evidence.store import record_evidence  # noqa: E402
from aquant.domain.data.db import connect  # noqa: E402
from aquant.domain.data.reader import SnapshotRef  # noqa: E402
from main import build_state, create_app  # noqa: E402
import main as api_main  # noqa: E402

USER = {"X-Aquant-Subject": "user:alice"}
INSTRUMENT = "SYN.A.600519"
DAY = "2026-09-08"
QUOTE = "A股每股现金红利0.50元"
UNRELATED_TEXT = "除权（息）日为2026/9/10"


class StubProvider:
    """Explicit non-network provider for test-only assistant calls."""

    provider_name = "stub"
    model_name = "stub-model"
    test_only = True

    def __init__(self) -> None:
        self.calls: list = []

    def complete(self, request) -> ModelResponse:
        self.calls.append(request)
        citation_ids = re.findall(r"\[citation_id: ([^\]\n]+)\]", request.context)
        return ModelResponse(
            text=json.dumps({"answer": "依据所选摘录，材料记录每股现金红利为 0.50 元。",
                             "citationIds": citation_ids[:1]}, ensure_ascii=False),
            provider=self.provider_name, model=self.model_name,
            input_tokens=123, output_tokens=45,
        )


class InvalidCitationProvider(StubProvider):
    def complete(self, request) -> ModelResponse:
        self.calls.append(request)
        return ModelResponse(
            text='{"answer":"未经核实的回答","citationIds":["invented-citation"]}',
            provider=self.provider_name, model=self.model_name,
            input_tokens=1, output_tokens=1,
        )


class FailingProvider(StubProvider):
    def complete(self, request):
        from aquant.domain.ai.model import ModelUnavailable
        self.calls.append(request)
        raise ModelUnavailable("上游 503", repair_action="稍后重试")


class NeverCallProvider:
    """Looks like the configured live provider but must be stopped pre-call."""

    provider_name = "deepseek"
    model_name = "unconfigured-test-model"
    base_url = "https://api.deepseek.com"

    def __init__(self) -> None:
        self.calls: list = []

    def complete(self, request):
        self.calls.append(request)
        raise AssertionError("live provider must never be called in these tests")


class BudgetPathFakeProvider:
    """Offline fake that deliberately exercises the configured live budget path."""

    provider_name = "deepseek"
    model_name = "deepseek-budget-test-model"
    base_url = "https://api.deepseek.com"

    def __init__(self, *, usage=(11, 5), failure=None,
                 before_complete=None) -> None:
        self.calls: list = []
        self.usage = usage
        self.failure = failure
        self.before_complete = before_complete

    def complete(self, request) -> ModelResponse:
        self.calls.append(request)
        if self.before_complete is not None:
            self.before_complete()
        if self.failure is not None:
            raise self.failure
        input_tokens, output_tokens = self.usage
        return ModelResponse(
            text="受控测试回答", provider=self.provider_name, model=self.model_name,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )


class PausingStubProvider(StubProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def complete(self, request) -> ModelResponse:
        self.calls.append(request)
        self.started.set()
        if not self.release.wait(timeout=15):
            raise RuntimeError("test provider was not released")
        citation_ids = re.findall(r"\[citation_id: ([^\]\n]+)\]", request.context)
        return ModelResponse(
            text=json.dumps({"answer": "等待结束后的合成测试回答。",
                             "citationIds": citation_ids[:1]}, ensure_ascii=False),
            provider=self.provider_name, model=self.model_name,
            input_tokens=123, output_tokens=45,
        )


def configure_budget_path(monkeypatch, tmp_path, *, daily_cap="10000"):
    key_file = tmp_path / "offline-test-provider-marker.txt"
    key_file.write_text("offline test fixture; not a credential", encoding="utf-8")
    values = {
        "AQUANT_DEEPSEEK_MODEL": BudgetPathFakeProvider.model_name,
        "AQUANT_DEEPSEEK_BASE_URL": BudgetPathFakeProvider.base_url,
        "AQUANT_DEEPSEEK_KEY_FILE": str(key_file),
        "AQUANT_ASSISTANT_PRICE_MODEL": BudgetPathFakeProvider.model_name,
        "AQUANT_ASSISTANT_PRICE_VERSION": "unit-test-only",
        "AQUANT_ASSISTANT_PRICE_SOURCE": "https://www.deepseek.com/pricing",
        "AQUANT_ASSISTANT_PRICE_EFFECTIVE_FROM": date.today().isoformat(),
        "AQUANT_ASSISTANT_INPUT_PRICE_UPPER_BOUND_MICRO_CNY_PER_MILLION_TOKENS": "1000000",
        "AQUANT_ASSISTANT_OUTPUT_PRICE_UPPER_BOUND_MICRO_CNY_PER_MILLION_TOKENS": "1000000",
        "AQUANT_ASSISTANT_DAILY_CAP_MICRO_CNY": daily_cap,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


@pytest.fixture()
def ctx(tmp_path):
    state = build_state(tmp_path)
    stub = StubProvider()
    state.model_provider = stub
    app = create_app(state=state)
    with TestClient(app) as client:
        yield client, state.con, stub, state


def card_with_evidence(client, con, *, source_id="synthetic-fixture", quote=QUOTE,
                       unlocatable=False):
    source_text = f"重要内容提示：{QUOTE}；{UNRELATED_TEXT}"
    if quote not in source_text and not unlocatable:
        source_text = quote
    con.execute(
        "INSERT OR IGNORE INTO source_registry "
        "(source_id,display_name,created_at,updated_at) VALUES (?,?,?,?)",
        (source_id, "离线助手测试来源", "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z"))
    bundle = record_evidence(
        con, instrument_id=INSTRUMENT, source_id=source_id,
        source_url="https://example.invalid/notice.pdf", source_title="测试披露摘录",
        source_text=source_text,
        available_at=datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc),
        fact_summary="每股现金红利 0.50 元", verification_status="VERIFIED",
        extra={"announced_on": "2026-09-07", "citations": [quote]},
    )
    response = client.get(
        f"/api/v1/instruments/{INSTRUMENT}/research", params={"trading_day": DAY})
    assert response.status_code == 200, response.text
    card = response.json()
    # The seed also contains an intentionally untrusted prompt-injection
    # sample. Select this test's located citation, never the newest seed row.
    own_ids = {item["citation_id"] for item in bundle.citations}
    card["evidence"].sort(key=lambda item: item["citationId"] not in own_ids)
    return card


def ask_card(client, card, *, citation_ids=None, question_id="EVIDENCE_SUMMARY",
             confirm=True, **extra):
    body = {
        "card_id": card["cardId"],
        "question_id": question_id,
        "citation_ids": ([card["evidence"][0]["citationId"]]
                         if citation_ids is None else citation_ids),
        "confirm_external_send": confirm,
        **extra,
    }
    return client.post("/api/v1/assistant/messages", json=body, headers=USER)


def verified_scan_fixture():
    return json.loads((ROOT / "tests/fixtures/assistant_research_scan_v2.json")
                      .read_text(encoding="utf-8"))


def scan_message_body(packet, *, snapshot_id=None, receipt_id=None):
    event = packet["candidates"][0]["events"][0]
    return {
        "snapshot_id": snapshot_id or packet["snapshotId"],
        "instrument_id": packet["candidates"][0]["instrumentId"],
        "receipt_ids": [receipt_id or event["receiptId"]],
        "question_id": "EVIDENCE_SUMMARY",
        "confirm_external_send": True,
    }


def install_verified_scan_fixture(monkeypatch, packet):
    raw = json.dumps(packet, ensure_ascii=False, sort_keys=True).encode("utf-8")
    monkeypatch.setattr(
        assistant_materials_module, "load_verified_research_scan_packet",
        lambda _path: (packet, "sha256:" + hashlib.sha256(raw).hexdigest()),
    )
    scan_as_of = datetime.fromisoformat(packet["asOfTime"])
    current_ref = SnapshotRef(
        snapshot_id="test-formal-snapshot", as_of_time=scan_as_of + timedelta(days=2),
        published_at=None, data_mode="SYNTHETIC", watermark="test-only",
    )
    monkeypatch.setattr(api_main, "active_snapshot",
                        lambda *_args, **_kwargs: "test-formal-snapshot")
    monkeypatch.setattr(api_main.SnapshotReader, "ref", lambda _self, _sid: current_ref)


def test_selected_current_excerpt_is_sent_and_audited(ctx):
    client, con, stub, _state = ctx
    card = card_with_evidence(client, con)
    response = ask_card(client, card)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["citationIds"] == [card["evidence"][0]["citationId"]]
    assert body["citations"][0]["documentId"] == card["evidence"][0]["documentId"]
    assert body["basedOn"]["snapshotId"] == card["snapshotId"]
    assert body["basedOn"]["asOfTime"] == card["asOfTime"]

    assert len(stub.calls) == 1
    context = stub.calls[0].context
    assert QUOTE in context and "概括所选公开摘录能够确认的内容" in context
    assert UNRELATED_TEXT not in context, "未选中的正文不能进入模型上下文"
    assert "citation_id: " + card["evidence"][0]["citationId"] in context
    for forbidden in ("概率", "预期收益", "目标价", "买卖建议"):
        assert forbidden in stub.calls[0].instructions

    row = con.execute(
        "SELECT * FROM model_call WHERE model_call_id=?", (body["modelCallId"],)
    ).fetchone()
    assert row["outcome"] == "OK"
    assert row["input_tokens"] == 123 and row["output_tokens"] == 45
    audit = json.loads(row["contract_version"])
    assert audit["audit"]["cardId"] == card["cardId"]
    assert audit["audit"]["snapshotId"] == card["snapshotId"]
    assert audit["audit"]["citations"][0]["documentHash"]
    assert audit["audit"]["citations"][0]["quoteHash"].startswith("sha256:")


def test_verified_scan_receipt_is_bound_to_current_research_and_formal_snapshots(
    ctx, monkeypatch,
):
    client, shared_con, stub, _state = ctx
    packet = verified_scan_fixture()
    install_verified_scan_fixture(monkeypatch, packet)

    response = client.post("/api/v1/assistant/messages", json=scan_message_body(packet),
                           headers=USER)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["researchBinding"] == {
        "snapshotId": packet["snapshotId"],
        "asOfTime": packet["asOfTime"],
        "dataAsOf": packet["dataAsOf"],
        "instrumentId": "SH.600415",
    }
    citation = body["citations"][0]
    event = packet["candidates"][0]["events"][0]
    assert citation["receiptId"] == event["receiptId"]
    assert citation["documentHash"] == event["contentHash"]
    assert citation["quoteHash"].startswith("sha256:")
    assert citation["locatorKind"] == "PDF_PAGE_CHARS"
    assert (citation["page"], citation["locatorStart"], citation["locatorEnd"]) == (1, 155, 235)
    assert citation["url"] == event["url"]
    assert len(stub.calls) == 1
    assert event["quote"] in stub.calls[0].context
    assert "账户" not in stub.calls[0].context and "持仓" not in stub.calls[0].context

    audit_row = shared_con.execute(
        "SELECT contract_version FROM model_call WHERE model_call_id=?",
        (body["modelCallId"],),
    ).fetchone()
    audit = json.loads(audit_row["contract_version"])["audit"]
    assert audit["flow"] == "verified-research-scan-assistant-v1"
    assert audit["scanBinding"]["researchSnapshotId"] == packet["snapshotId"]
    assert audit["scanBinding"]["instrumentId"] == "SH.600415"
    assert audit["scanBinding"]["receiptIds"] == [event["receiptId"]]
    assert audit["currentFormalSnapshotId"] == "test-formal-snapshot"
    assert audit["citations"][0]["documentHash"] == event["contentHash"]


def test_verified_scan_old_version_and_missing_receipt_fail_then_recover(ctx, monkeypatch):
    client, _shared_con, stub, _state = ctx
    packet = verified_scan_fixture()
    install_verified_scan_fixture(monkeypatch, packet)

    stale = client.post("/api/v1/assistant/messages",
                        json=scan_message_body(packet, snapshot_id="old-snapshot"),
                        headers=USER)
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "STALE_RESEARCH_CARD"

    legacy = dict(packet, schema="aquant.research-candidates.v1")
    install_verified_scan_fixture(monkeypatch, legacy)
    old_version = client.post("/api/v1/assistant/messages",
                              json=scan_message_body(legacy), headers=USER)
    assert old_version.status_code == 409, old_version.text
    assert old_version.json()["detail"]["code"] == "PIT_UNVERIFIED"

    no_material_packet = json.loads(json.dumps(packet))
    no_material_packet["candidates"][0]["events"] = []
    install_verified_scan_fixture(monkeypatch, no_material_packet)
    no_material = client.post("/api/v1/assistant/messages",
                              json=scan_message_body(packet), headers=USER)
    assert no_material.status_code == 409, no_material.text
    assert no_material.json()["detail"]["code"] == "PIT_UNVERIFIED"
    assert stub.calls == []

    install_verified_scan_fixture(monkeypatch, packet)
    recovered = client.post("/api/v1/assistant/messages",
                            json=scan_message_body(packet), headers=USER)
    assert recovered.status_code == 200, recovered.text
    assert len(stub.calls) == 1


def test_slow_assistant_uses_independent_connection_and_leaves_status_reads_available(
    ctx, monkeypatch,
):
    client, shared_con, _stub, state = ctx
    card = card_with_evidence(client, shared_con)
    provider = PausingStubProvider()
    state.model_provider = provider

    observed_connections: list[sqlite3.Connection] = []
    resolve_original = api_main.resolve_research_card_materials
    ask_original = api_main.ask_assistant
    record_original = assistant_module._record_call

    def track_resolve(con, **kwargs):
        observed_connections.append(con)
        assert con is not shared_con
        return resolve_original(con, **kwargs)

    def track_ask(con, *args, **kwargs):
        observed_connections.append(con)
        assert con is not shared_con
        return ask_original(con, *args, **kwargs)

    def track_record(con, *args, **kwargs):
        observed_connections.append(con)
        assert con is not shared_con
        return record_original(con, *args, **kwargs)

    monkeypatch.setattr(api_main, "resolve_research_card_materials", track_resolve)
    monkeypatch.setattr(api_main, "ask_assistant", track_ask)
    monkeypatch.setattr(assistant_module, "_record_call", track_record)

    with ThreadPoolExecutor(max_workers=3) as pool:
        assistant_future = pool.submit(ask_card, client, card)
        assert provider.started.wait(timeout=5), "assistant provider was not entered"
        status_future = pool.submit(client.get, "/api/v1/status")
        schedule_future = pool.submit(client.get, "/api/v1/schedule")
        try:
            status = status_future.result(timeout=3)
            schedule = schedule_future.result(timeout=3)
        finally:
            provider.release.set()
        assistant = assistant_future.result(timeout=10)

    assert status.status_code == 200, status.text
    assert schedule.status_code == 200, schedule.text
    assert assistant.status_code == 200, assistant.text
    assert len(observed_connections) == 3
    assert all(con is not shared_con for con in observed_connections)
    assert shared_con.execute(
        "SELECT count(*) FROM model_call WHERE outcome='OK'"
    ).fetchone()[0] == 1


def test_readiness_reports_only_blockers_and_live_gate_never_calls_provider(
    tmp_path, monkeypatch,
):
    for name in ("AQUANT_DEEPSEEK_MODEL", "AQUANT_DEEPSEEK_BASE_URL",
                 "AQUANT_DEEPSEEK_KEY_FILE",
                 "AQUANT_ASSISTANT_PRICE_MODEL",
                 "AQUANT_ASSISTANT_PRICE_VERSION",
                 "AQUANT_ASSISTANT_PRICE_SOURCE",
                 "AQUANT_ASSISTANT_PRICE_EFFECTIVE_FROM",
                 "AQUANT_ASSISTANT_INPUT_PRICE_UPPER_BOUND_MICRO_CNY_PER_MILLION_TOKENS",
                 "AQUANT_ASSISTANT_OUTPUT_PRICE_UPPER_BOUND_MICRO_CNY_PER_MILLION_TOKENS",
                 "AQUANT_ASSISTANT_DAILY_CAP_MICRO_CNY"):
        monkeypatch.delenv(name, raising=False)
    state = build_state(tmp_path)
    provider = NeverCallProvider()
    state.model_provider = provider
    app = create_app(state=state)
    with TestClient(app) as client:
        status = client.get("/api/v1/assistant/readiness", headers=USER)
        assert status.status_code == 200, status.text
        body = status.json()
        assert body["enabled"] is False
        codes = {item["code"] for item in body["blockingIssues"]}
        assert {"MODEL_SELECTION_UNCONFIGURED", "MODEL_ENDPOINT_UNCONFIGURED",
                "MODEL_CREDENTIAL_UNCONFIGURED", "ASSISTANT_BUDGET_CONFIG_MISSING",
                "SYNTHETIC_DATA_EGRESS_DISABLED"}.issubset(codes)
        assert "path" not in json.dumps(body).lower()

    with pytest.raises(AssistantError) as raised:
        ask_assistant(
            state.con, provider,
            materials=[Material("cninfo", "公开披露测试摘录", False)],
            purpose="测试真实门禁", data_mode="PRODUCTION", subject_id="user:alice")
    error = raised.value
    assert getattr(error, "code", None) == "DATA_NOT_READY"
    assert provider.calls == []
    row = state.con.execute(
        "SELECT outcome,error_code,contract_version FROM model_call ORDER BY called_at DESC LIMIT 1"
    ).fetchone()
    assert row["outcome"] == "REJECTED" and row["error_code"] == "DATA_NOT_READY"
    assert "ASSISTANT_BUDGET_CONFIG_MISSING" in row["contract_version"]


def test_live_budget_upper_bound_rejects_before_provider_call(tmp_path, monkeypatch):
    configure_budget_path(monkeypatch, tmp_path, daily_cap="10")
    state = build_state(tmp_path)
    provider = BudgetPathFakeProvider()

    with pytest.raises(AssistantError) as raised:
        ask_assistant(
            state.con, provider,
            materials=[Material("cninfo", "公开披露摘录", False)],
            purpose="测试调用前费用上界", data_mode="PRODUCTION",
            subject_id="user:alice")

    assert raised.value.code == "ASSISTANT_DAILY_BUDGET_EXCEEDED"
    assert provider.calls == []
    assert state.con.execute(
        "SELECT count(*) FROM assistant_budget_reservation"
    ).fetchone()[0] == 0
    row = state.con.execute(
        "SELECT outcome,error_code FROM model_call ORDER BY called_at DESC LIMIT 1"
    ).fetchone()
    assert tuple(row) == ("REJECTED", "ASSISTANT_DAILY_BUDGET_EXCEEDED")
    state.con.close()


def test_live_budget_settles_usage_based_price_upper_bound(tmp_path, monkeypatch):
    configure_budget_path(monkeypatch, tmp_path)
    state = build_state(tmp_path)
    def assert_reservation_committed_before_provider():
        with closing(connect(state.data_dir / "meta.sqlite", read_only=True)) as observer:
            row = observer.execute(
                "SELECT status FROM assistant_budget_reservation"
            ).fetchone()
            assert row is not None and row["status"] == "RESERVED"

    provider = BudgetPathFakeProvider(
        usage=(11, 5), before_complete=assert_reservation_committed_before_provider)
    config, blockers = load_budget_config(provider.model_name)
    assert config is not None and blockers == []

    answer = ask_assistant(
        state.con, provider,
        materials=[Material("cninfo", "公开披露摘录", False)],
        purpose="测试用量结算", data_mode="PRODUCTION",
        max_output_tokens=50, subject_id="user:alice")

    call = state.con.execute(
        "SELECT cost_micro_cny,input_tokens,output_tokens,contract_version FROM model_call "
        "WHERE model_call_id=?", (answer["modelCallId"],)
    ).fetchone()
    reservation = state.con.execute(
        "SELECT status,reserved_micro_cny,settled_micro_cny,model_call_id "
        "FROM assistant_budget_reservation"
    ).fetchone()
    assert (call["cost_micro_cny"], call["input_tokens"], call["output_tokens"]) == (16, 11, 5)
    call_audit = json.loads(call["contract_version"])
    assert call_audit["audit"]["costBasis"] == "REGISTERED_PRICE_UPPER_BOUND"
    assert call_audit["audit"]["budget"]["costBasis"] == "REGISTERED_PRICE_UPPER_BOUND"
    assert reservation["status"] == "SETTLED"
    assert reservation["settled_micro_cny"] == 16
    assert reservation["model_call_id"] == answer["modelCallId"]
    assert reservation["reserved_micro_cny"] > reservation["settled_micro_cny"]
    assert answer["budget"]["status"] == "SETTLED"
    assert answer["budget"]["usageCostUpperBoundMicroCny"] == 16
    assert answer["budget"]["costBasis"] == "REGISTERED_PRICE_UPPER_BOUND"
    assert daily_budget_status(state.con, config)["usageCostUpperBoundMicroCny"] == 16

    replay_provider = ArchivedModelProvider(
        state.con, answer["archiveId"], subject_id="user:alice")
    replay = ask_assistant(
        state.con, replay_provider,
        materials=[Material("cninfo", "公开披露摘录", False)],
        purpose="测试用量结算", data_mode="PRODUCTION",
        max_output_tokens=50, subject_id="user:alice")
    assert replay["text"] == answer["text"]
    assert replay["budget"] is None
    assert state.con.execute(
        "SELECT count(*) FROM assistant_budget_reservation"
    ).fetchone()[0] == 1
    replay_call = state.con.execute(
        "SELECT cost_micro_cny FROM model_call WHERE model_call_id=?",
        (replay["modelCallId"],),
    ).fetchone()
    assert replay_call["cost_micro_cny"] is None
    state.con.close()


def test_missing_usage_and_timeout_keep_the_reservation_held(tmp_path, monkeypatch):
    configure_budget_path(monkeypatch, tmp_path)
    state = build_state(tmp_path)
    provider = BudgetPathFakeProvider(usage=(None, None))
    answer = ask_assistant(
        state.con, provider,
        materials=[Material("cninfo", "公开披露摘录", False)],
        purpose="测试未知费用", data_mode="PRODUCTION",
        max_output_tokens=50, subject_id="user:alice")
    assert answer["budget"]["status"] == "HELD"
    assert answer["budget"]["usageCostUpperBoundMicroCny"] is None
    assert "不代表零费用" in answer["budget"]["message"]
    row = state.con.execute(
        "SELECT status,settled_micro_cny,reserved_micro_cny "
        "FROM assistant_budget_reservation WHERE model_call_id=?",
        (answer["modelCallId"],),
    ).fetchone()
    assert row["status"] == "HELD" and row["settled_micro_cny"] is None
    held_amount = row["reserved_micro_cny"]
    config, blockers = load_budget_config(provider.model_name)
    assert config is not None and blockers == []
    assert daily_budget_status(state.con, config)["usageCostUpperBoundMicroCny"] == held_amount

    timeout_provider = BudgetPathFakeProvider(
        failure=ModelUnavailable("simulated timeout"))
    with pytest.raises(AssistantError) as raised:
        ask_assistant(
            state.con, timeout_provider,
            materials=[Material("cninfo", "另一段公开披露摘录", False)],
            purpose="测试超时费用未知", data_mode="PRODUCTION",
            max_output_tokens=50, subject_id="user:alice")
    assert raised.value.budget["status"] == "HELD"
    assert "不代表零费用" in raised.value.message
    assert state.con.execute(
        "SELECT count(*) FROM assistant_budget_reservation WHERE status='HELD'"
    ).fetchone()[0] == 2
    state.con.close()


def test_usage_upper_bound_over_reservation_is_recorded_and_blocks_next_call(
    tmp_path, monkeypatch,
):
    configure_budget_path(monkeypatch, tmp_path, daily_cap="10000")
    state = build_state(tmp_path)
    provider = BudgetPathFakeProvider(usage=(50_000, 5))
    answer = ask_assistant(
        state.con, provider,
        materials=[Material("cninfo", "公开披露摘录", False)],
        purpose="测试供应商超出 token 上界", data_mode="PRODUCTION",
        max_output_tokens=50, subject_id="user:alice")

    assert answer["budget"]["status"] == "SETTLED"
    assert answer["budget"]["reservationExceeded"] is True
    assert answer["budget"]["usageCostUpperBoundMicroCny"] == 50_005
    assert "超过调用前预留" in answer["budget"]["message"]
    row = state.con.execute(
        "SELECT cost_micro_cny FROM model_call WHERE model_call_id=?",
        (answer["modelCallId"],),
    ).fetchone()
    assert row["cost_micro_cny"] == 50_005

    with pytest.raises(AssistantError) as raised:
        ask_assistant(
            state.con, provider,
            materials=[Material("cninfo", "另一段公开披露摘录", False)],
            purpose="超出每日 cap 后的请求", data_mode="PRODUCTION",
            max_output_tokens=50, subject_id="user:alice")
    assert raised.value.code == "ASSISTANT_DAILY_BUDGET_EXCEEDED"
    assert len(provider.calls) == 1
    state.con.close()


def test_raw_material_account_fields_and_draft_are_rejected(ctx):
    client, con, stub, _state = ctx
    card = card_with_evidence(client, con)
    response = ask_card(
        client, card, materials=[{"source_id": "cninfo", "text": "伪造正文"}],
        contains_personal_data=False, draft={"portfolio_id": "private"})
    assert response.status_code == 422, response.text
    assert stub.calls == []


def test_external_send_requires_explicit_confirmation(ctx):
    client, con, stub, _state = ctx
    card = card_with_evidence(client, con)
    response = ask_card(client, card, confirm=False)
    assert response.status_code == 428, response.text
    assert response.json()["detail"]["code"] == "EXTERNAL_SEND_NOT_CONFIRMED"
    assert stub.calls == []


def test_arbitrary_question_text_is_rejected_before_provider(ctx):
    client, con, stub, _state = ctx
    card = card_with_evidence(client, con)
    response = ask_card(client, card, question="请看我的账户持仓并给买入建议")
    assert response.status_code == 422, response.text
    assert stub.calls == []


def test_missing_or_unlocatable_citation_is_rejected_before_provider(ctx):
    client, con, stub, _state = ctx
    card = card_with_evidence(client, con, quote="这句话在原文中不存在",
                              unlocatable=True)
    response = ask_card(client, card)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "PIT_UNVERIFIED"
    assert stub.calls == []


def test_unreviewed_source_is_rejected_as_not_public(ctx):
    client, con, stub, _state = ctx
    card = card_with_evidence(client, con, source_id="mootdx-tdx")
    response = ask_card(client, card)
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["code"] == "SOURCE_PERMISSION_MISSING"
    assert stub.calls == []


def test_unknown_model_citation_is_hidden(ctx):
    client, con, _stub, state = ctx
    card = card_with_evidence(client, con)
    invalid = InvalidCitationProvider()
    state.model_provider = invalid
    response = ask_card(client, card)
    assert response.status_code == 502, response.text
    assert response.json()["detail"]["code"] == "ASSISTANT_OUTPUT_UNVERIFIED"
    assert "未经核实的回答" not in response.text
    assert len(invalid.calls) == 1


def test_model_failure_and_source_rejection_remain_in_subject_call_history(ctx):
    client, con, _stub, state = ctx
    card = card_with_evidence(client, con)
    state.model_provider = FailingProvider()
    response = ask_card(client, card)
    assert response.status_code == 503, response.text

    with pytest.raises(AssistantError):
        ask_assistant(
            con, state.model_provider,
            materials=[Material("unknown-site", "正文", False)],
            purpose="测试未登记来源", data_mode="SYNTHETIC", subject_id="user:alice")
    calls = client.get("/api/v1/assistant/calls", headers=USER)
    assert calls.status_code == 200, calls.text
    outcomes = {item["outcome"] for item in calls.json()["calls"]}
    assert outcomes == {"ERROR", "REJECTED"}, outcomes


def test_empty_or_duplicate_citation_selection_is_rejected(ctx):
    client, con, stub, _state = ctx
    card = card_with_evidence(client, con)
    citation_id = card["evidence"][0]["citationId"]
    empty = ask_card(client, card, citation_ids=[])
    duplicate = ask_card(client, card, citation_ids=[citation_id, citation_id])
    assert empty.status_code == 422
    assert duplicate.status_code == 409
    assert stub.calls == []
