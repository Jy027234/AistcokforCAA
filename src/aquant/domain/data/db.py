"""SQLite 访问层。

主文档 §14.2：SQLite WAL、单机本地盘、短写事务；
**不在写事务内等待网络或模型调用**（因此本模块不提供长事务上下文管理器）。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_DIR = Path(__file__).resolve().parents[3].parent / "schema"


def schema_files() -> list[Path]:
    return sorted(SCHEMA_DIR.glob("*.sql"))


def connect(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """打开连接。WAL 模式，外键开启。"""

    p = Path(path)
    if read_only:
        uri = f"file:{p.as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, isolation_level=None)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(p, isolation_level=None)
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.row_factory = sqlite3.Row
    return con


def apply_migrations(con: sqlite3.Connection) -> list[str]:
    """按序执行 schema/*.sql。全部使用 IF NOT EXISTS，可安全重入。"""

    applied: list[str] = []
    for f in schema_files():
        con.executescript(f.read_text(encoding="utf-8"))
        applied.append(f.name)
    return applied


@contextmanager
def write_tx(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """短写事务。

    主文档 §14.2：写事务必须短，且**不得在其中等待网络或模型调用**。
    调用方有责任保证事务体内没有远程 I/O。
    """

    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def fk_violations(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """外键完整性检查，供对账与自检使用。"""

    return list(con.execute("PRAGMA foreign_key_check"))
