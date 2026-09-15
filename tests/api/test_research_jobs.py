"""研究作业接口（主文档 §8.4、§14.2）。

§8.4 的幂等键 = 作业类型 + 交易日 + 配置版本 + 输入快照，由数据库唯一约束保证。
这条用例要证明的是它**真的在阻止重复计算**，而不是只是插了一行：

  1. 重复提交同一逻辑作业返回同一条 job_id（不新建）；
  2. 跑完一条作业会产出研究运行，且因子值真的落了库；
  3. 重复提交后再跑，**不产生第二份研究运行**；
  4. 未知作业类型被拒，而不是静默当成别的作业。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT / "src"))

from main import SNAPSHOT_ID, build_state, create_app  # noqa: E402

TRADING_DAY = "2026-09-08"
USER = {"X-Aquant-Subject": "user:alice"}


@pytest.fixture()
def ctx(tmp_path):
    state = build_state(tmp_path)
    app = create_app(state=state)
    with TestClient(app) as client:
        yield client, state.con


def submit(client, **over):
    body = {"job_type": "FACTOR_COMPUTE", "trading_day": TRADING_DAY,
            "snapshot_id": SNAPSHOT_ID, "payload": {"limit": 2}}
    body.update(over)
    return client.post("/api/v1/research/jobs", json=body, headers=USER)


def test_submit_creates_a_pending_job(ctx):
    client, con = ctx
    r = submit(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    assert body["status"] == "PENDING", "提交只入队，不执行"

    row = con.execute("SELECT * FROM job WHERE job_id=?", (body["jobId"],)).fetchone()
    assert row is not None
    assert row["status"] == "PENDING"
    assert row["attempt_count"] == 0
    assert row["input_snapshot_id"] == SNAPSHOT_ID


def test_resubmitting_the_same_job_returns_the_same_one(ctx):
    """§8.4：同一逻辑作业重复提交不新建。"""

    client, con = ctx
    first = submit(client).json()
    second = submit(client).json()
    assert first["jobId"] == second["jobId"]
    assert second["created"] is False

    n = con.execute("SELECT COUNT(*) AS n FROM job").fetchone()["n"]
    assert n == 1, f"重复提交产生了 {n} 条作业"


def test_a_different_snapshot_is_a_different_job(ctx):
    client, _con = ctx
    a = submit(client).json()
    b = submit(client, snapshot_id="snap-other").json()
    assert a["jobId"] != b["jobId"], "输入快照不同就不是同一条作业"


def test_unknown_job_type_is_rejected(ctx):
    client, con = ctx
    r = submit(client, job_type="SOMETHING_ELSE")
    assert r.status_code == 422, r.text
    assert con.execute("SELECT COUNT(*) AS n FROM job").fetchone()["n"] == 0


def test_running_a_job_produces_a_research_run_with_factor_values(ctx):
    client, con = ctx
    job = submit(client).json()

    r = client.post(f"/api/v1/research/jobs/{job['jobId']}/run", headers=USER)
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "SUCCEEDED", done

    run_id = done["result"]["researchRunId"]
    assert run_id, done
    stored = con.execute("SELECT status,snapshot_id FROM research_run WHERE "
                         "research_run_id=?", (run_id,)).fetchone()
    assert stored is not None, "作业成功却没有留下研究运行"
    assert stored["snapshot_id"] == SNAPSHOT_ID

    n = con.execute("SELECT COUNT(*) AS n FROM feature_value WHERE research_run_id=?",
                    (run_id,)).fetchone()["n"]
    assert n > 0, "研究运行里没有任何因子值"

    row = con.execute("SELECT status,result_json FROM job WHERE job_id=?",
                      (job["jobId"],)).fetchone()
    assert row["status"] == "SUCCEEDED"
    assert row["result_json"], "作业结果必须落库，否则事后无法回答它做了什么"


def test_rerunning_a_finished_job_reuses_the_result(ctx):
    """幂等的关键一步：已成功的作业不再重算，不产生第二份研究运行。"""

    client, con = ctx
    job = submit(client).json()
    first = client.post(f"/api/v1/research/jobs/{job['jobId']}/run",
                        headers=USER).json()
    runs_after_first = con.execute(
        "SELECT COUNT(*) AS n FROM research_run").fetchone()["n"]

    # 再提交一次（同为幂等命中），再跑一次
    again = submit(client).json()
    assert again["jobId"] == job["jobId"]
    second = client.post(f"/api/v1/research/jobs/{again['jobId']}/run",
                         headers=USER).json()

    assert second["reused"] is True, second
    assert second["result"]["researchRunId"] == first["result"]["researchRunId"]
    runs_after_second = con.execute(
        "SELECT COUNT(*) AS n FROM research_run").fetchone()["n"]
    assert runs_after_second == runs_after_first, (
        f"重跑产生了新的研究运行：{runs_after_first} -> {runs_after_second}")


def test_running_an_unknown_job_is_404(ctx):
    client, _con = ctx
    r = client.post("/api/v1/research/jobs/job_does_not_exist/run", headers=USER)
    assert r.status_code == 404


def test_failed_job_records_an_error_code(ctx):
    """作业失败必须留下错误码与修复建议，不能停在 RUNNING。"""

    client, con = ctx
    job = submit(client, snapshot_id="snap-does-not-exist").json()
    r = client.post(f"/api/v1/research/jobs/{job['jobId']}/run", headers=USER)
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "FAILED", done

    row = con.execute("SELECT status,error_code,error_detail,lease_owner FROM job "
                      "WHERE job_id=?", (job["jobId"],)).fetchone()
    assert row["status"] == "FAILED"
    assert row["error_code"], "失败作业没有错误码"
    assert row["lease_owner"] is None, "失败的作业不得继续占着租约"
