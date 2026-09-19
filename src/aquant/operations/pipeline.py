"""每日流水线的护栏：并发锁与运行日志（§14.2）。

为什么这两件事单独成模块
------------------------
流水线本身只是按顺序调几个已有脚本，没什么逻辑。真正会出事的是：

  * **两次运行重叠**。定时任务偶尔会重叠（上一次跑得慢、或者被手工触发），
    而两次运行会同时写同一份采集缓存与同一个快照目录——
    上一次我在补前收时就因为残留进程互相覆盖缓存，
    排查花了很久，而症状只是"进度不动"。
  * **失败无人知**。定时任务最常见的失败模式不是崩溃，而是**静默地
    什么也没做**（例如市场休市、数据源没更新）。没有留痕就没法回答
    "昨天到底跑了没有、结果是什么"。

因此锁用"独占创建文件"（跨进程有效，且进程崩溃后文件留着——
这本身是线索），日志用 JSONL 追加（每次运行一行，可直接 grep）。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


class PipelineBusy(RuntimeError):
    """已有一次运行在进行中。**不等待**——等下去只会让两次运行都变慢。"""

    def __init__(self, lock_path: Path, holder: str) -> None:
        super().__init__(f"流水线正在运行中（锁文件 {lock_path}，持有者 {holder}）")
        self.lock_path = lock_path
        self.holder = holder


class PipelineLock:
    """跨进程互斥。用 O_EXCL 创建锁文件，内容记录持有者与时间。

    刻意**不**做超时自动夺取：无法区分"上一次还在跑"与"上一次崩了"，
    自动夺取会在长任务上制造两次并发写。崩了留下的锁由人清掉——
    而清除动作本身留下了"发生过崩溃"的记录。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._held = False

    def __enter__(self) -> "PipelineLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        holder = f"pid={os.getpid()} at={datetime.now(timezone.utc).isoformat()}"
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            existing = ""
            try:
                existing = self.path.read_text(encoding="utf-8")
            except OSError:
                pass
            raise PipelineBusy(self.path, existing.strip() or "未知") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(holder)
        self._held = True
        return self

    def __exit__(self, *exc: object) -> None:
        if self._held:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self._held = False


@dataclass(slots=True)
class RunRecord:
    """一次运行的留痕。字段刻意少而实：能回答"跑了没有、成没成、为什么"。"""

    trading_day: str
    snapshot_id: str
    outcome: str                      # PUBLISHED / SKIPPED / FAILED
    steps: list[dict] = field(default_factory=list)
    reason: str | None = None
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    #: 采集缓存里**实际**的最后一个交易日（不是"今天"）。
    #:
    #: 记它是为了回答一个运维上很容易被忽略的问题：快照落后了没有。
    #: 光看"上次跑成功是什么时候"不够——天天跑成功但数据源没更新，
    #: 快照照样停在几天前，而界面上一切正常。
    data_last_day: str | None = None
    #: 谁发起的一次运行：manual（命令行/界面按钮）或 scheduler（到点）。
    #: 两者都合法，但**排查时的下一步不一样**：前者要问"为什么点了"，
    #: 后者要问"为什么到点没跑/跑了没成"。
    source: str = "manual"

    def as_dict(self) -> dict:
        return asdict(self)


class RunLog:
    """JSONL 运行日志。每次运行追加一行。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, record: RunRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.as_dict(), ensure_ascii=False) + "\n")

    def entries(self, limit: int = 20) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines[-limit:] if line.strip()]

    def last_successful_day(self) -> str | None:
        """最近一次成功发布的交易日。用来判断"今天是不是已经跑过了"。"""

        for entry in reversed(self.entries(limit=200)):
            if entry.get("outcome") == "PUBLISHED":
                return entry.get("trading_day")
        return None
