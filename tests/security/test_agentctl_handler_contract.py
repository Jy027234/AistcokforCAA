"""能力 handler 的装载契约（Q1）。

为什么这条用例存在
------------------
handler 从「读模块内写死的夹具」改成「读注入的读取器」时，我曾经把
签名写成 `reader` 为**必填关键字参数**。测试全绿——因为我所有测试都
显式传了 reader。

而 agentctl 装载 handler 用的是：

    inspect.signature(function).bind({})    # 一个位置参数

于是真实接入侧会看到「能力注册成功、handler 可装载」，直到第一次
真实调用才 TypeError。这正是本项目反复出现的那类缺陷：
**验的是装得上，走的却是另一条路。**

所以这里不复制业务逻辑，而是把**基座真实使用的装载方式**搬到测试里：
先按 agentctl 的 sys.path 约定 import handler，再按它的规则校验签名。
基座一旦换装载方式，这条用例才会跟着变，而不是悄悄失去意义。
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

ROOT = Path(__file__).resolve().parents[2]
CAPABILITIES = ROOT / "capabilities"
sys.path.insert(0, str(ROOT / "src"))

#: 基座通过 manifest 的 `handler:` 字符串 import 的模块名。
#: 它必须与 capabilities/agentctl.capabilities.yaml 里写的一致，
#: 否则我们测的是另一个模块。
HANDLER_MODULE = "aquant_lab_agentctl_handlers"
HANDLER_FUNCTION = "research_card_read"
RUNTIME_MODULE = "aquant.adapters.agentctl.runtime"


@contextmanager
def _prepend_sys_path(path: Path) -> Iterator[None]:
    """照抄 agentctl `_prepend_sys_path` 的语义（插入并在退出时移除）。"""

    text = str(path)
    inserted = text not in sys.path
    if inserted:
        sys.path.insert(0, text)
    try:
        yield
    finally:
        if inserted and text in sys.path:
            sys.path.remove(text)


def _load_like_agentctl():
    """按基座的方式装载 handler，并施加基座的签名校验。

    这是 assert 的对象：能 import + 签名能 `bind({})`。
    """

    with _prepend_sys_path(CAPABILITIES):
        module = importlib.import_module(HANDLER_MODULE)
    function = getattr(module, HANDLER_FUNCTION, None)
    assert callable(function), f"{HANDLER_MODULE}:{HANDLER_FUNCTION} 不可调用"
    inspect.signature(function).bind({})
    return module, function


class _RecordingReader:
    """记录被调用的读取器替身。只实现 handler 用到的两个方法。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def snapshot(self, snapshot_id: str):
        self.calls.append(("snapshot", snapshot_id))
        return {"snapshot_id": snapshot_id, "as_of_time": "2026-09-11T20:30:00+08:00",
                "data_mode": "SYNTHETIC", "watermark": "X", "quality": {}}

    def card(self, snapshot_id: str, instrument_id: str):
        self.calls.append(("card", snapshot_id, instrument_id))
        return {"instrument_id": instrument_id, "exchange": "SSE", "board": "MAIN",
                "display_name": "记录用替身", "industry_code": None, "industry_name": None,
                "classification_version": None, "status": "LISTED", "listed_on": None,
                "factors": [], "factor_note": None}


def _fake_runtime(reader) -> types.ModuleType:
    """替掉运行期模块，使默认工厂返回给定读取器（不碰真实数据目录）。"""

    module = types.ModuleType(RUNTIME_MODULE)
    module.card_reader = lambda: reader          # type: ignore[attr-defined]
    return module


@contextmanager
def _patched_runtime(reader) -> Iterator[None]:
    saved = sys.modules.get(RUNTIME_MODULE)
    sys.modules[RUNTIME_MODULE] = _fake_runtime(reader)
    try:
        yield
    finally:
        if saved is None:
            sys.modules.pop(RUNTIME_MODULE, None)
        else:
            sys.modules[RUNTIME_MODULE] = saved


# ------------------------------------------------------- 装载契约
def test_handler_loads_and_binds_an_empty_mapping():
    """基座的装载方式：一个位置参数，且不得有必填关键字参数。"""

    _load_like_agentctl()


def test_manifest_points_at_the_module_we_test():
    """manifest 里的 handler 字符串必须指向被测模块。

    否则上述校验可以全绿，而基座装载的是另一个（可能不存在的）模块。
    """

    text = (CAPABILITIES / "agentctl.capabilities.yaml").read_text(encoding="utf-8")
    assert f"handler: {HANDLER_MODULE}:{HANDLER_FUNCTION}" in text, text


def test_module_name_is_importable_from_the_manifest_directory():
    """manifest 同目录必须真的能 import 到该模块名。"""

    with _prepend_sys_path(CAPABILITIES):
        spec = importlib.util.find_spec(HANDLER_MODULE)
    assert spec is not None, "manifest 同目录下找不到 handler 模块"
    assert spec.origin and Path(spec.origin).parent == CAPABILITIES, spec.origin


# ------------------------------------------------------- 调用契约
def test_call_without_reader_uses_the_resolved_default():
    """不传 reader 时**必须真的被调用**，而不是抛 TypeError。

    这是修复前会失败的那一条：必填关键字参数在真实调用点不存在。
    """

    import asyncio

    module, function = _load_like_agentctl()
    reader = _RecordingReader()
    with _patched_runtime(reader):
        out = asyncio.run(function({"validated_arguments": {
            "instrument_id": "SYN.A.600519", "snapshot_id": "snap-syn-001"}}))
    assert out["ok"] is True, out
    assert reader.calls == [("snapshot", "snap-syn-001"),
                            ("card", "snap-syn-001", "SYN.A.600519")], reader.calls
    assert out["display_name"] == "记录用替身"


def test_injected_reader_wins_over_the_default():
    """显式注入必须优先——否则「注入」只是个说法。"""

    import asyncio

    module, function = _load_like_agentctl()
    default, injected = _RecordingReader(), _RecordingReader()
    with _patched_runtime(default):
        out = asyncio.run(function(
            {"validated_arguments": {"instrument_id": "SYN.A.600519",
                                     "snapshot_id": "snap-syn-001"}},
            reader=injected))
    assert out["ok"] is True, out
    assert injected.calls and not default.calls


def test_no_reachable_store_is_a_structured_error_not_a_crash():
    """读不到任何快照存储时：给 §16.4 形状的错误码，不抛栈。

    真实的接入侧可能根本没配数据目录；那时「能力不可用」是诚实答案，
    而 500 让人以为是自己用错了参数。
    """

    import asyncio

    module, function = _load_like_agentctl()
    with _patched_runtime(None):
        out = asyncio.run(function({"validated_arguments": {
            "instrument_id": "SH.600519", "snapshot_id": "snap-universe"}}))
    assert out["ok"] is False
    error = out["error"]
    assert error["code"] == "DATA_NOT_READY"
    assert error["retryable"] is True
    assert "AQUANT_DATA_DIR" in error["repair_action"], error


def test_runtime_resolution_is_delayed_until_the_call():
    """import handler 本身不得依赖 aquant。

    装载与调用是两个时刻：接入侧先装载（可能在一个没有产品依赖的进程里），
    之后才调用。若 import 阶段就要 aquant，装载会直接失败。
    """

    module, _ = _load_like_agentctl()
    # 只按文件载入（模拟一个 sys.path 上没有 src 的进程）
    path = CAPABILITIES / f"{HANDLER_MODULE}.py"
    spec = importlib.util.spec_from_file_location("isolated_handlers", path)
    isolated = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    saved = {k: sys.modules[k] for k in list(sys.modules) if k.startswith("aquant")}
    for key in saved:
        sys.modules.pop(key)
    saved_src = [p for p in sys.path if Path(p).name == "src"]
    for p in saved_src:
        sys.path.remove(p)
    try:
        spec.loader.exec_module(isolated)          # 不抛即通过
    finally:
        sys.modules.update(saved)
        sys.path[:0] = saved_src


# ------------------------------------------------------- 真实数据（可选）
def _bundled_data_dir() -> Path | None:
    for rel in ("deploy/universe-snapshot", "deploy/real-snapshot"):
        if (ROOT / rel / "meta.sqlite").is_file():
            return ROOT / rel
    return None


def test_against_the_real_bundled_snapshot(monkeypatch):
    """有真实快照时，走**默认工厂**读一次真数据。

    前面几条用的是替身读取器（证明接线正确）；这一条证明默认工厂在
    真实数据目录上确实能工作——两者缺一不可。

    快照是构建产物、不进版本库，因此缺失时跳过，
    但跳过不能掩盖失败：没有它时其余用例仍然覆盖装载与调用契约。
    """

    import asyncio

    data_dir = _bundled_data_dir()
    if data_dir is None:
        pytest.skip("本机没有可用的真实快照目录")
    module, function = _load_like_agentctl()

    runtime = importlib.import_module(RUNTIME_MODULE)
    runtime.set_card_reader(None)
    monkeypatch.setenv("AQUANT_DATA_DIR", str(data_dir))
    snapshot_id = "snap-universe" if data_dir.name == "universe-snapshot" else "snap-real-61d"
    instrument_id = "SH.600519"

    out = asyncio.run(function({"validated_arguments": {
        "instrument_id": instrument_id, "snapshot_id": snapshot_id}}))
    assert out["ok"] is True, out
    # 判据落在真实数据特有的东西上：替身不可能给出这些值
    assert out["display_name"] == "贵州茅台", out
    assert out["industry_code"].startswith("C"), out
    assert out["status"] == "LISTED", out
    assert out["data_mode"] == "PRODUCTION", out
    assert out["factors"] == [], "该快照未跑研究作业，不应凭空出现因子值"
    assert any("尚未计算任何因子" in x for x in out["limitations"]), out


def test_default_factory_opens_the_store_read_only(monkeypatch, tmp_path):
    """默认工厂打开的连接必须**拒绝写入**。

    「只读能力」不能只写在 manifest 的 side_effect_class 里：
    让 SQLite 在文件层兜住，比靠代码约定可靠。
    """

    import sqlite3

    data_dir = _bundled_data_dir()
    if data_dir is None:
        pytest.skip("本机没有可用的真实快照目录")
    runtime = importlib.import_module(RUNTIME_MODULE)
    runtime.set_card_reader(None)
    monkeypatch.setenv("AQUANT_DATA_DIR", str(data_dir))
    reader = runtime.card_reader()
    assert reader is not None
    with pytest.raises(sqlite3.OperationalError):
        reader.con.execute("CREATE TABLE should_not_exist (x INTEGER)")
    reader.con.close()

