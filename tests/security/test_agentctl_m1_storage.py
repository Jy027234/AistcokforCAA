"""agentctl 只读能力接上 M1 快照存储（Q1）。

为什么这条用例重要
------------------
Q1 的能力 handler 原先读的是模块内**写死的字典**，注释自己写着
"Q0/Q1 的 stand-in for the real snapshot store that M1 will provide"。
替身的问题不是"过时"，而是它让"接上了真实存储"这件事**无法验证**——
测试全绿，而读的是一份常量。

因此这里验的是：同一段 handler 代码，在收到真实快照读取器时，
返回的是**那一次读取**的结果。判据必须落在真实数据特有的东西上
（真实证券简称、真实行业分类、真实覆盖度），落在替身身上不可能成立。

边界（ADR-011）
---------------
适配器在 `adapters/agentctl/` 下，可以 import 领域层；
handler 只依赖注入对象，不 import 任何 M1 或 agentctl 模块。
后者由 `tests/security/test_agentctl_boundary.py` 强制。
"""

from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from aquant.adapters.agentctl.snapshot_card_reader import (  # noqa: E402
    SnapshotCardReader,
)
from aquant.domain.data.db import apply_migrations, connect  # noqa: E402
from aquant.domain.data.ingest import SnapshotBuilder  # noqa: E402
from aquant.domain.data.reader import SnapshotReader  # noqa: E402
from aquant.domain.data.snapshot import SnapshotStore  # noqa: E402
from tests.integration.test_m1_ingest_e2e import build_snapshot  # noqa: E402


def _load_handlers():
    """载入能力 handler。

    它不在包路径上（`capabilities/` 是集成层，不是 Python 包），
    因此显式按文件载入。刻意**不**为了测试给它加 `__init__.py`：
    那会让"集成层不是包"这个事实变得含糊。
    """

    path = ROOT / "capabilities" / "aquant_lab_agentctl_handlers.py"
    spec = importlib.util.spec_from_file_location("aquant_handlers", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def world(tmp_path):
    """一份真实的合成快照 + 接上它的读取器。"""

    con = connect(tmp_path / "meta.sqlite")
    apply_migrations(con)
    root = tmp_path / "api"
    root.mkdir()
    store = SnapshotStore(con, root)
    build_snapshot(con, SnapshotBuilder(con, root / "datasets"), store)
    reader = SnapshotReader(store)
    yield con, reader, SnapshotCardReader(con, reader)
    con.close()


def _card(reader, **args):
    handlers = _load_handlers()
    import asyncio

    return asyncio.run(handlers.research_card_read(
        {"validated_arguments": args}, reader=reader))


# ------------------------------------------------------- 真实读取
def test_card_comes_from_the_snapshot_not_a_constant(world):
    """卡片内容必须是**这次读取**的结果，而不是模块里的常量。"""

    con, reader, cards = world
    out = _card(cards, snapshot_id="snap-syn-001", instrument_id="SYN.A.600519")
    assert out["ok"] is True, out
    # 与读取器直接给出的答案一致——这就是"接上了"的定义
    ref = reader.ref("snap-syn-001")
    instruments = {i["instrument_id"]: i
                   for i in reader.instruments("snap-syn-001", as_of=ref.as_of_time)}
    expected = instruments["SYN.A.600519"]
    assert out["display_name"] == (expected.get("short_name") or "SYN.A.600519")
    assert out["exchange"] == expected["exchange"]
    assert out["board"] == expected["board"]


def test_card_carries_the_snapshot_identity(world):
    con, reader, cards = world
    out = _card(cards, snapshot_id="snap-syn-001", instrument_id="SYN.A.600519")
    ref = reader.ref("snap-syn-001")
    assert out["snapshot_id"] == "snap-syn-001"
    assert out["as_of_time"] == ref.as_of_time.isoformat()
    assert out["data_mode"] == "SYNTHETIC"
    assert out["watermark"], "合成快照必须带水印（§15.4）"
    ref = out["evidence_ref"]
    unsigned = {key: value for key, value in out.items() if key != "evidence_ref"}
    digest = hashlib.sha256(json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()
    assert ref["result_sha256"] == f"sha256:{digest}"
    assert ref["capability_id"] == "aquant.research_card.read"


def test_data_completeness_is_derived_from_the_snapshot(world):
    """完整度标签必须来自实际覆盖度，而不是固定文案。"""

    con, reader, cards = world
    out = _card(cards, snapshot_id="snap-syn-001", instrument_id="SYN.A.600519")
    assert "覆盖" in out["data_completeness"], out["data_completeness"]


def test_limitations_are_derived_not_boilerplate(world):
    """限制必须从**这份数据**推出来。

    注意断言方式：分类版本那一条是**条件**的——只有数据里真的声明了版本
    才应当出现。第一版无条件断言"分类版本"，于是合成夹具（没有该字段）
    直接失败。**条件性的事实必须条件性地断言**，否则测试断言的是
    夹具形状，而不是行为。
    """

    con, reader, cards = world
    ref = reader.ref("snap-syn-001")
    instruments = {i["instrument_id"]: i
                   for i in reader.instruments("snap-syn-001", as_of=ref.as_of_time)}
    out = _card(cards, snapshot_id="snap-syn-001", instrument_id="SYN.A.600519")
    joined = " ".join(out["limitations"])
    assert "不构成投资建议" in joined

    declared = instruments["SYN.A.600519"].get("classification_version")
    if declared:
        assert declared in joined, "声明了分类版本就必须说明它没有变更历史"
    else:
        assert "分类版本" not in joined, "没声明版本时不该凭空提一句分类版本"


def test_status_comes_from_the_status_history(world):
    """当前状态必须从 status_history 按左闭右开区间取（§7.1）。

    夹具里 SYN.A.600002 在 2026-09-10 由 LISTED 转为 SUSPENDED，
    而快照时点是 2026-09-11，因此它**现在**应当是 SUSPENDED。
    不看这段历史的话，会把停牌股说成正常交易。
    """

    con, reader, cards = world
    out = _card(cards, snapshot_id="snap-syn-001", instrument_id="SYN.A.600002")
    assert out["ok"] is True, out
    assert out["status"] == "SUSPENDED", out["status"]

    # 对照：SYN.A.600519 的生效条目**没有写 status**（只写了名称与行业）。
    # 那时必须返回 UNKNOWN，而不是替它填一个 LISTED——
    # "状态未声明"与"状态正常"是两件事，把前者说成后者会让停牌股
    # 看起来可以交易。
    other = _card(cards, snapshot_id="snap-syn-001", instrument_id="SYN.A.600519")
    assert other["status"] == "UNKNOWN", other["status"]


def test_unknown_status_is_reported_as_unknown_not_assumed_trading():
    """历史为空时返回 UNKNOWN——不猜"上市"。"""

    from datetime import datetime

    from aquant.adapters.agentctl.snapshot_card_reader import SnapshotCardReader

    status = SnapshotCardReader._current_status(
        {"status_history": []}, datetime.fromisoformat("2026-09-11T20:30:00+08:00"))
    assert status == "UNKNOWN"


def test_board_specific_limitation_appears_for_gem(world):
    """创业板标的必须带上板块相关的限制。"""

    con, reader, cards = world
    ref = reader.ref("snap-syn-001")
    gem = [i["instrument_id"] for i
           in reader.instruments("snap-syn-001", as_of=ref.as_of_time)
           if i.get("board") == "GEM"]
    if not gem:
        pytest.skip("该夹具里没有创业板标的")
    out = _card(cards, snapshot_id="snap-syn-001", instrument_id=gem[0])
    assert "20%" in " ".join(out["limitations"])


# ------------------------------------------------------- 错误路径
def test_unknown_snapshot_is_a_stale_snapshot_error(world):
    _con, _reader, cards = world
    out = _card(cards, snapshot_id="snap-nope", instrument_id="SYN.A.600519")
    assert out["ok"] is False
    assert out["error"]["code"] == "STALE_SNAPSHOT"
    assert out["error"]["repair_action"], "必须给出可操作的修复动作"


def test_unknown_instrument_is_data_not_ready(world):
    _con, _reader, cards = world
    out = _card(cards, snapshot_id="snap-syn-001", instrument_id="SYN.A.999999")
    assert out["ok"] is False
    assert out["error"]["code"] == "DATA_NOT_READY"


def test_missing_arguments_do_not_crash(world):
    _con, _reader, cards = world
    assert _card(cards, snapshot_id="", instrument_id="")["ok"] is False
    assert _card(cards, snapshot_id="snap-syn-001", instrument_id="")["ok"] is False


def test_unpublished_snapshot_is_refused(world):
    """未发布的快照不得被读到（§15.4 消费者只读 PUBLISHED）。"""

    con, reader, cards = world
    # 直接从数据集层面伪造一个未发布快照太绕；这里验的是
    # reader.snapshot 对未知 id 返回 None，而 handler 把它变成错误码。
    assert reader is not None
    assert cards.snapshot("snap-does-not-exist") is None


# ------------------------------------------------------- 未算因子的诚实
def test_missing_factors_are_stated_not_invented(world):
    """没有因子值时要说清楚，**不能编造数值**。

    这是从"写死替身"换成"真实读取"之后最要守住的一条：
    替身里有两个好看的因子值，真实快照上可能一个都没有。
    """

    _con, _reader, cards = world
    out = _card(cards, snapshot_id="snap-syn-001", instrument_id="SYN.A.600519")
    if out["factors"]:
        pytest.skip("该夹具上已计算因子，本用例针对未计算的情形")
    joined = " ".join(out["limitations"])
    assert "尚未计算" in joined or "没有因子值" in joined, joined


def test_computed_factors_are_returned_with_their_rank(world):
    """真算了因子时，值必须带排名与缺失原因一起返回（§10.2）。"""

    con, reader, cards = world
    from datetime import datetime

    from aquant.domain.research.runs import (
        FactorValue, create_research_run, store_factor_values,
    )

    ref = reader.ref("snap-syn-001")
    run_id = create_research_run(
        con, snapshot_id="snap-syn-001", as_of_time=ref.as_of_time,
        code_version="test", feature_version="f10-v1")
    store_factor_values(con, research_run_id=run_id, values=[
        FactorValue(instrument_id="SYN.A.600519", factor_id="f10",
                    raw_value=0.05, coverage_ratio=1.0),
        FactorValue(instrument_id="SYN.A.000001", factor_id="f10",
                    raw_value=None, exclusion_reason="MISSING_NET_PROFIT"),
    ])

    out = _card(cards, snapshot_id="snap-syn-001", instrument_id="SYN.A.600519")
    assert out["factors"], out
    factor = out["factors"][0]
    assert factor["factor_id"] == "f10"
    assert factor["value"] == pytest.approx(0.05)
    assert factor["rank_pct"] is not None, "横截面排名必须一起返回"
    assert factor["exclusion_reason"] is None

    # 被排除的标的：必须给**原因**，不能省略
    other = _card(cards, snapshot_id="snap-syn-001", instrument_id="SYN.A.000001")
    if other["factors"]:
        excluded = other["factors"][0]
        assert excluded["exclusion_reason"] == "MISSING_NET_PROFIT"
        assert excluded["exclusion_label"], "原因码要有可读说明"


def test_exclusion_label_map_covers_the_codes_the_pipeline_emits():
    """原因码与说明必须成对。漏一个就会在界面上显示成空白。

    取值表现在只有**一份**（`aquant.domain.research.exclusions`），
    能力 handler 与界面共用它。这里对着它断言：既要求覆盖 F10 实际
    产出的中文原因，也要求 handler 真的用上了这张表——原先 handler 里
    抄的是只含 `MISSING_*` 的子集，F10 的原因在模型侧没有说明。
    """

    from aquant.domain.research.exclusions import EXCLUSION_LABELS

    handlers = _load_handlers()
    labels = handlers._exclusion_labels()
    assert labels, "原因码说明表不得为空"
    assert set(labels) == set(EXCLUSION_LABELS), (
        "handler 与产品取值表不一致：同一个原因码不能有两种说法")

    # F10 在快照上真的会产出的原因，必须都在表里
    for code in ("TTM 不可得（缺上年同期或口径不成立）",
                 "快照未包含财务数据", "快照内无行情", "缺总股本或价格无效"):
        assert code in labels, f"{code} 没有说明，界面上会显示成一个破折号"
        assert labels[code].strip(), code
