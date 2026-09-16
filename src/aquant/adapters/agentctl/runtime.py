"""agentctl 能力在产品进程内的运行期接线。

为什么需要这个模块
------------------
Q1 的能力 handler 原先把依赖藏在模块顶部的写死夹具里，因此它「自己就能跑」。
换成注入式读取器之后，注入点必须存在于**运行期**——否则：

  * 集成冒烟（agentctl integration smoke）只验到「能 import、
    签名能 bind 一个 invocation」，随后真实调用必然失败；
  * 线上则表现为能力「已注册但一调就报错」。

两者属于同一类缺陷：**验的是装得上，用的却是另一条路**。
因此这里提供一个显式的默认工厂，并让 handler 在未被注入时走它——
被注入的读取器永远优先。

边界（ADR-011）
---------------
本模块在 adapters 下，允许 import 领域层；方向不会反过来。
能力 handler 不 import 本模块，它在**调用时**按需解析，
因此 handler 单独打包给别人用时也不会因为缺依赖而 import 失败。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from aquant.domain.data.db import connect
from aquant.domain.data.reader import SnapshotReader
from aquant.domain.data.snapshot import SnapshotStore

#: 资料包内自带的真实快照位置。作为最后兜底——
#: 它存在就意味着「这台机器上确实有一份真数据可读」。
_BUNDLED_DATA_DIRS = ("deploy/universe-snapshot", "deploy/real-snapshot")


def repository_root() -> Path:
    """runtime.py 位于 src/aquant/adapters/agentctl/ 下，回退四级得到仓库根。

    不依赖调用方的 cwd：能力可能由别的进程、别的工作目录拉起。
    """

    return Path(__file__).resolve().parents[4]


def data_dir_from_env() -> Path | None:
    """AQUANT_DATA_DIR 指向的数据目录。未设置返回 None。

    这里**只读**：不像 apps/api 那样处理 AQUANT_RESET_DATA——
    一个只读能力没有资格删除数据目录。
    """

    raw = os.environ.get("AQUANT_DATA_DIR")
    return Path(raw).expanduser() if raw else None


def _candidate_data_dirs() -> list[Path]:
    """按优先级给出候选数据目录。**只挑真的存在的**。"""

    out: list[Path] = []
    env = data_dir_from_env()
    if env is not None:
        out.append(env)
    root = repository_root()
    for rel in _BUNDLED_DATA_DIRS:
        out.append(root / rel)
    return out


def _ensure_src_on_path() -> None:
    """让同进程中别的包也能 import 到 aquant。

    典型场景：agentctl 的集成冒烟按 manifest 的 handler: 字符串 import
    本产品的 capabilities 模块，而那个进程的 sys.path 里可能只有仓库根，
    没有 src。这时 handler 内部 import aquant 会失败——
    现象是「能力装好了但一调就报 ModuleNotFoundError」，很难查。

    只加真实存在的目录，幂等。
    """

    src = repository_root() / "src"
    if src.is_dir():
        entry = str(src)
        if entry not in sys.path:
            sys.path.insert(0, entry)


#: 显式注入的读取器（由 apps/api 在启动时登记）。
#: 它优先于任何按环境推断的结果——「被明确告知的」胜过「猜出来的」。
_INJECTED: Any | None = None


def set_card_reader(reader: Any) -> None:
    """登记进程级读取器。传 None 表示撤销。"""

    global _INJECTED
    _INJECTED = reader


def injected_card_reader() -> Any | None:
    return _INJECTED


def card_reader() -> Any | None:
    """默认读取器。**读不到就返回 None**，由 handler 转成结构化错误。

    三种来源，按优先级：
      1. 显式注入的读取器；
      2. AQUANT_DATA_DIR 下有 meta.sqlite 的数据目录；
      3. 资料包内自带的真实快照目录。

    连接以**只读方式**打开：这是只读能力，
    让 SQLite 在文件层替我们兜住「不小心写了库」，
    比靠代码约定可靠。
    """

    if _INJECTED is not None:
        return _INJECTED
    for data_dir in _candidate_data_dirs():
        if not (data_dir / "meta.sqlite").is_file():
            continue
        _ensure_src_on_path()
        # 延迟 import：避免本模块在 import 阶段就依赖 capabilities 包，
        # 也避免没数据时白付一次 import 代价。
        from aquant.adapters.agentctl.snapshot_card_reader import (  # noqa: PLC0415
            SnapshotCardReader,
        )

        con = connect(data_dir / "meta.sqlite", read_only=True)
        return SnapshotCardReader(con, SnapshotReader(SnapshotStore(con, data_dir / "api")))
    return None

