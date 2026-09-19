"""状态接口不得有副作用（一次真实事故的回归用例）。

事故
----
/api/v1/status 返回 500。原因不是数据问题：

    daily_run_log_path() → _data_dir_from_env() → shutil.rmtree(data_dir)

_data_dir_from_env() **带副作用**——AQUANT_RESET_DATA=1 时它会清空数据目录。
把它放进请求路径的后果是**每个 /status 请求都在删自己的数据目录**，
而那时进程还开着 SQLite 连接（Windows 上表现为 PermissionError）。

这类缺陷的形状值得记住：一个看起来**纯读取**的函数（"取一下日志路径"）
在内部调用了带副作用的初始化逻辑。名字骗了人。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api"))
sys.path.insert(0, str(ROOT / "src"))

USER = {"X-Aquant-Subject": "user:demo"}


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """固定数据目录 + 与验收脚本相同的环境变量。

    刻意**不设** AQUANT_RESET_DATA=1。两个理由：
      * 那是"启动时清空"的开关，副作用只应在启动时发生，
        而测试想验的正是**请求期间**不再有副作用；
      * reset 会在 build_state 里 rmtree，而 pytest 的临时目录可能还留着
        上一个测试未关闭的 SQLite 连接（Windows 上直接 PermissionError），
        测试会因为无关的原因红——第一版就是这样。
    启动时的重置能力另有一条用例单独保证。
    """

    data = tmp_path / "data"
    monkeypatch.setenv("AQUANT_DATA_DIR", str(data))
    monkeypatch.delenv("AQUANT_RESET_DATA", raising=False)
    monkeypatch.delenv("AQUANT_SNAPSHOT_ID", raising=False)

    from main import build_state, create_app

    state = build_state()
    with TestClient(create_app(state=state)) as client:
        yield client, state, data


def test_status_does_not_recreate_or_delete_the_data_dir(isolated):
    """连续请求 /status，数据目录与账本文件必须原封不动。"""

    client, _state, data = isolated
    db = data / "meta.sqlite"
    assert db.exists(), "建账后应当有 meta.sqlite"
    before = db.stat().st_mtime_ns

    for i in range(3):
        r = client.get("/api/v1/status", headers=USER)
        assert r.status_code == 200, f"第 {i + 1} 次请求失败：{r.text[:200]}"

    assert data.exists(), "数据目录在请求期间被删了"
    assert db.exists(), "账本文件在请求期间被删了"
    assert db.stat().st_mtime_ns == before, "账本文件在请求期间被改写了"


def test_status_reports_freshness_without_crashing(isolated):
    """新鲜度是附加信息：读不出留痕也不该让状态接口不可用。"""

    client, _state, _data = isolated
    body = client.get("/api/v1/status", headers=USER).json()
    assert "freshness" in body, "状态接口没有返回新鲜度字段"
    fresh = body["freshness"]
    assert "stale" in fresh and "detail" in fresh


def test_data_dir_is_resolved_once_at_startup(isolated):
    """AppState 必须持有启动时解析好的数据目录。

    请求路径要用的路径不能再解析一次——那次解析带副作用。
    """

    _client, state, data = isolated
    assert state.data_dir is not None, "AppState 没有记录数据目录"
    assert Path(state.data_dir) == data


def test_run_log_path_does_not_re_resolve_the_data_dir(isolated):
    """取日志路径必须是**纯函数**：不得触发目录清理。"""

    from main import daily_run_log_path

    _client, state, data = isolated
    before = sorted(p.name for p in data.iterdir())
    for _ in range(3):
        daily_run_log_path(state.data_dir)
    after = sorted(p.name for p in data.iterdir())
    assert before == after, "取日志路径改动了数据目录"
    assert (data / "meta.sqlite").exists()


def test_status_follows_current_snapshot_pointer_without_restart(isolated):
    """发布新物理快照后，下一次请求应读取新指针而无需重启 API。"""

    client, state, data = isolated
    from aquant.domain.data.ingest import SnapshotBuilder
    from aquant.operations.snapshot_lifecycle import write_current_pointer
    from test_m1_ingest_e2e import build_snapshot

    next_snapshot = "snap-syn-002"
    previous_snapshot = state.snapshot_id
    builder = SnapshotBuilder(state.con, state.root / "datasets")
    build_snapshot(state.con, builder, state.store, snapshot_id=next_snapshot)
    write_current_pointer(data, next_snapshot)

    # 指针变化前内存状态保持旧值；只读请求会完整切换。费率未配置不应
    # 阻断状态/研究读取，真实快照的模拟入口会单独拒绝占位费率。
    assert state.snapshot_id == previous_snapshot

    response = client.get("/api/v1/status", headers=USER)
    assert response.status_code == 200, response.text
    assert response.json()["snapshotId"] == next_snapshot
    assert state.snapshot_id == next_snapshot


def test_reset_still_works_at_startup(tmp_path, monkeypatch):
    """收紧副作用不等于取消防御：启动时的重置必须仍然生效。

    否则这个修复会把"显式清空数据"这个有意能力一起弄坏。
    """

    data = tmp_path / "reset"
    data.mkdir()
    (data / "stale.txt").write_text("上一个部署留下的东西", encoding="utf-8")
    monkeypatch.setenv("AQUANT_DATA_DIR", str(data))
    monkeypatch.setenv("AQUANT_RESET_DATA", "1")
    monkeypatch.delenv("AQUANT_SNAPSHOT_ID", raising=False)

    from main import build_state

    state = build_state()
    try:
        assert not (data / "stale.txt").exists(), "启动时的重置失效了"
        assert (data / "meta.sqlite").exists(), "重置后应当重新建账"
    finally:
        state.con.close()
