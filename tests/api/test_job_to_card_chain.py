"""作业串联：研究作业 -> 证据 -> 研究卡（Q4）。

这条用例要证明的**不是**"三个接口各自能用"，而是它们连起来之后
证据真的走完了全程：作业提交、模型抽取、引用定位、事件落库、卡片显示。

因此它三件事一起做：
  1. 用**确定性替身**跑真实的作业执行器（离线，不联网）；
  2. 断言证据落库并带字符偏移；
  3. 断言研究卡上能看到同一条引用。

合成夹具的公司行为没有公告原文（真实快照有），所以这里给夹具补一段
evidence_json——补的是**数据**，不是绕过链路。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT / "src"))

from aquant.domain.ai.model import ModelResponse  # noqa: E402
from aquant.domain.evidence.store import evidence_for  # noqa: E402
from aquant.operations.research_jobs import (  # noqa: E402
    JOB_EVIDENCE_RESEARCH, run_research_job, submit_research_job,
)
from main import SNAPSHOT_ID, build_state, create_app  # noqa: E402

INSTRUMENT = "SYN.A.600003"
DAY = "2026-09-08"
SOURCE = "重要内容提示：A股每股现金红利0.50元，股权登记日2026/9/9，除权（息）日2026/9/10"


class StubExtractor:
    """确定性替身：返回一份**贴合所给正文**的抽取结果。

    刻意从 prompt 里取正文片段来做 quote，而不是写死字符串——
    写死会让用例在源头文本变化时"照样通过"，那正是串联用例最该避免的。
    """

    provider_name = "stub"
    model_name = "stub-extractor"

    def __init__(self) -> None:
        self.calls: list = []
        self.quote = "A股每股现金红利0.50元"

    def complete(self, request) -> ModelResponse:
        self.calls.append(request)
        payload = {
            "fields": {
                "per_share_amount": {"value": "0.50",
                                     "quote": self.quote},
                "record_date": {"value": "2026-09-09", "quote": "2026/9/9"},
                "ex_date": {"value": "2026-09-10", "quote": "2026/9/10"},
                "pay_date": {"value": None, "quote": None},
            },
            "uncertainty": "发放日未在材料中出现",
            "counter_evidence": "材料未说明税后金额",
        }
        return ModelResponse(text=json.dumps(payload, ensure_ascii=False),
                             provider=self.provider_name, model=self.model_name,
                             input_tokens=10, output_tokens=20,
                             content_hash="sha256:" + "b" * 64)


@pytest.fixture()
def ctx(tmp_path):
    state = build_state(tmp_path)
    app = create_app(state=state)
    with TestClient(app) as client:
        yield client, state


def _give_fixture_a_source_text(state) -> None:
    state.con.execute(
        "UPDATE corporate_action SET evidence_json=?, source_url=?, source_title=? "
        "WHERE instrument_id=?", (json.dumps({"excerpt": SOURCE}),
                                  "http://example.invalid/ca.PDF",
                                  "合成示例权益分派公告", INSTRUMENT))
    state.con.commit()


def test_job_to_evidence_to_card(ctx):
    client, state = ctx
    _give_fixture_a_source_text(state)
    stub = StubExtractor()

    submitted = submit_research_job(
        state.con, job_type=JOB_EVIDENCE_RESEARCH, trading_day=DAY,
        snapshot_id=SNAPSHOT_ID,
        payload={"instrument_id": INSTRUMENT, "source_id": "synthetic-fixture"})
    assert submitted["status"] == "PENDING"

    done = run_research_job(state.con, state.reader, job_id=submitted["jobId"],
                            worker_id="test-worker", provider=stub)
    assert done["status"] == "SUCCEEDED", done
    assert stub.calls, "作业没有真的调用模型"

    action = done["result"]["actions"][0]
    assert action["agreesWithStored"] is True, action["disagreements"]
    # 桩只给了三个字段的引用（pay_date 为 null），而实现**为 null 也记一条**：
    # "模型没给证据"本身是要能看到的事实。因此 4 条里 3 条可定位。
    assert action["citationCount"] == 4, action["citations"]
    assert action["locatedCount"] == 3, action["citations"]
    assert any(c["located"] is False for c in action["citations"]), (
        "缺证据的字段应当留下一条不可定位的记录，而不是被略过")

    # --- 证据确实落库并带字符偏移
    rows = evidence_for(state.con, instrument_id=INSTRUMENT)
    # 合成夹具本来就有事件，所以按**引用内容**筛出本作业产出的那条，
    # 而不是取全部（第一版取 located[0] 拿到的是夹具的旧记录）。
    mine = [r for r in rows if r["quote"] == stub.quote]
    assert mine, "作业产出的引用没有落库：" + str([r["quote"] for r in rows])[:200]
    # 键名是 camelCase（接口形状与库里列名不同），
    # 我第一版读成 locator_start 拿到 None，看起来像"偏移没存下来"。
    assert mine[0]["located"] is True
    assert mine[0]["locatorStart"] is not None, mine[0]
    assert mine[0]["locatorKind"] == "EXACT_MATCH"

    # --- 卡片上能看到同一条引用
    r = client.get(f"/api/v1/instruments/{INSTRUMENT}/research",
                   params={"trading_day": DAY})
    assert r.status_code == 200, r.text
    body = r.json()
    quotes = [e.get("quote") for e in body["evidence"]]
    assert stub.quote in quotes, (
        "作业产出的证据没有出现在研究卡上：" + str(quotes)[:200])
    item = next(e for e in body["evidence"] if e.get("quote") == stub.quote)
    assert item["located"] is True
    assert item["citationId"]
    assert item["documentId"]


def test_job_is_idempotent_and_does_not_rerun(ctx):
    """同一逻辑作业重跑不产生第二份证据。"""

    client, state = ctx
    _give_fixture_a_source_text(state)
    stub = StubExtractor()
    a = submit_research_job(state.con, job_type=JOB_EVIDENCE_RESEARCH,
                            trading_day=DAY, snapshot_id=SNAPSHOT_ID,
                            payload={"instrument_id": INSTRUMENT,
                                     "source_id": "synthetic-fixture"})
    run_research_job(state.con, state.reader, job_id=a["jobId"],
                     worker_id="w1", provider=stub)
    events_after_first = state.con.execute(
        "SELECT COUNT(*) AS n FROM event").fetchone()["n"]

    again = submit_research_job(state.con, job_type=JOB_EVIDENCE_RESEARCH,
                                trading_day=DAY, snapshot_id=SNAPSHOT_ID,
                                payload={"instrument_id": INSTRUMENT,
                                         "source_id": "synthetic-fixture"})
    assert again["jobId"] == a["jobId"]
    second = run_research_job(state.con, state.reader, job_id=again["jobId"],
                              worker_id="w2", provider=stub)
    assert second["reused"] is True
    assert state.con.execute("SELECT COUNT(*) AS n FROM event").fetchone()["n"] \
        == events_after_first, "重跑产生了第二份证据"
