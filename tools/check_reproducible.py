"""可复现性门禁：确认「提交里的东西」真的能跑。

为什么需要它
------------
本项目曾连续 8 次提交一个**仓储里没有领域层**的仓库：
`src/aquant/domain/data/` 被 `.gitignore` 的 `data/` 规则吞掉
（该规则匹配任意层级的 data 目录），而本机测试却一直通过，
因为文件就躺在工作树里。

本机通过 ≠ 交付可复现。因此本脚本只回答一个问题：

    把这个提交导出到干净目录，还能不能跑通测试？

它刻意**不使用工作树**，也不依赖任何未跟踪文件。
退出码即结论：0 = 可复现，1 = 不可复现。

用法：
    python tools/check_reproducible.py            # 检查 HEAD
    python tools/check_reproducible.py --fast      # 跳过测试，只做导入与自检
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 必须在导出中存在的关键路径。这些是产品的实际实现，不是文档。
REQUIRED_PATHS = [
    "src/aquant/__init__.py",
    "src/aquant/domain/data/pit.py",
    "src/aquant/domain/data/snapshot.py",
    "src/aquant/domain/data/reader.py",
    "src/aquant/domain/data/ingest.py",
    "src/aquant/domain/data/db.py",
    "src/aquant/domain/evidence/citations.py",
    "src/aquant/domain/simulation/simulator.py",
    "src/aquant/domain/simulation/fees.py",
    "src/aquant/domain/simulation/corporate_actions.py",
    "src/aquant/domain/portfolio/construction.py",
    "src/aquant/domain/portfolio/plan.py",
    "src/aquant/domain/strategy/s1.py",
    "src/aquant/application/workspace_view.py",
    "src/aquant/operations/jobs.py",
    "src/aquant/adapters/providers/fetch_guard.py",
    "src/aquant/adapters/providers/cninfo.py",
    "contracts/common.schema.json",
    "schema/001_metadata.sql",
    "configs/research.example.yaml",
    "examples/snap-syn-001.yaml",
    "tests/validate_spec.py",
    "tests/golden/test_s01_s10_simulator.py",
    "tests/pit/test_d01_d08_pit_golden.py",
]

#: 这些模块必须能被干净导出中的解释器导入。导入失败即不可复现。
REQUIRED_IMPORTS = [
    "aquant.domain.data.pit",
    "aquant.domain.data.snapshot",
    "aquant.domain.data.reader",
    "aquant.domain.portfolio.plan",
    "aquant.domain.strategy.s1",
    "aquant.operations.jobs",
]


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """文本模式，解码失败不致命——子进程可能输出非 UTF-8。"""

    kw.setdefault("errors", "replace")
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def export_head(dest: Path) -> None:
    """git archive HEAD -> tar -> dest。只包含已提交内容。

    刻意用二进制模式取 stdout：tar 是二进制，走文本模式会被解码破坏。
    """

    proc = subprocess.run(["git", "archive", "HEAD"], cwd=ROOT, capture_output=True)
    if proc.returncode != 0:
        raise SystemExit(
            "git archive failed: " + proc.stderr.decode("utf-8", errors="replace").strip()
        )
    tar_path = dest.parent / (dest.name + ".tar")
    tar_path.write_bytes(proc.stdout)
    with tarfile.open(tar_path) as tf:
        tf.extractall(dest)
    tar_path.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true",
                    help="跳过 pytest，只做路径与导入检查")
    ap.add_argument("--python", default=sys.executable)
    args = ap.parse_args()

    failures: list[str] = []
    checks = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal checks
        checks += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    head = run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT).stdout.strip()
    print(f"可复现性门禁 · HEAD={head}")
    print(f"仓库: {ROOT}")

    tmp = Path(tempfile.mkdtemp(prefix="aquant-repro-"))
    try:
        print("\n[1] 导出 HEAD 到干净目录")
        export_head(tmp)
        check("git archive HEAD 成功", True)

        print("\n[2] 关键实现路径存在（这一条正是当年漏掉的）")
        for rel in REQUIRED_PATHS:
            check(rel, (tmp / rel).exists())

        print("\n[3] 关键模块可导入（只用 PYTHONPATH=src）")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(tmp / "src")
        env.pop("AQUANT_TRUSTED_PROXY_NETWORKS", None)
        for mod in REQUIRED_IMPORTS:
            proc = run([args.python, "-c", f"import {mod}"], cwd=tmp, env=env)
            check(f"import {mod}", proc.returncode == 0,
                  proc.stderr.strip().splitlines()[-1] if proc.returncode else "")

        print("\n[4] 资料包自检")
        proc = run([args.python, str(tmp / "tests" / "validate_spec.py")], cwd=tmp, env=env)
        check("validate_spec.py 通过", proc.returncode == 0,
              proc.stdout.strip().splitlines()[-1] if proc.stdout else "")

        if not args.fast:
            print("\n[5] 全量测试（干净导出，PYTHONPATH 仅 src）")
            proc = run([args.python, "-m", "pytest", "tests", "-q", "--tb=line"],
                       cwd=tmp, env=env)
            tail = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
            check("pytest 通过", proc.returncode == 0, tail[-1] if tail else "")
            if proc.returncode != 0:
                print(proc.stdout[-2000:])

        print("\n[6] 归档字节是否还在（离线，不联网）")
        # 大体积原始字节不进版本库，所以"归档成功"这句话最容易悄悄失真：
        # 摘要还在、文件没了，读起来一切正常。这里把它变成会失败的检查。
        # 退出码 2 表示"还没有归档"，首次运行前的正常状态，不算失败。
        proc = run([args.python, str(tmp / "tools" / "verify_archive.py")], cwd=tmp)
        if proc.returncode == 2:
            # 措辞要准确：导出目录里没有归档字节是**设计使然**，
            # 不代表本机没跑过抓取。写成"尚未跑过 T5"会误导读者。
            check("归档复核（导出目录无字节，跳过）", True,
                  "归档原始字节不随版本库分发；本机复核请直接跑 tools/verify_archive.py")
        else:
            tail = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
            check("归档复核通过", proc.returncode == 0,
                  tail[0] if tail else "verify_archive 无输出")

        print("\n[7] 工作树是否干净（有未提交变更则导出不等于你正在测的代码）")
        dirty = run(["git", "status", "--porcelain"], cwd=ROOT).stdout.strip()
        check("工作树干净", not dirty,
              (dirty.splitlines()[0] + f" …（共 {len(dirty.splitlines())} 项）") if dirty else "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 62)
    if failures:
        print(f"不可复现：{len(failures)}/{checks} 项未通过")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"OK：{checks}/{checks} 项通过，HEAD 可复现")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
