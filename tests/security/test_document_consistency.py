"""文档一致性：文档里写下的路径必须真的存在。

为什么值得一条用例
------------------
文档债的典型形态不是"写得不对"，而是**指向了不存在的东西**：
提了一个被删掉的文件、引了一个改名后的脚本、说"见 X 节"而 X 节已经重写。
人读的时候会以为是自己找不到；机器读的时候才发现它根本不存在。

这条检查很便宜，而且它随文档量增长而增值。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: 参与路径检查的文档：**描述本仓库的那些**。
#:
#: 刻意加上这个范围限制。上游规格与外部报告里有大量指向**别的仓库**的引用
#: （agentctl 基座、qlib、tradingagents 等），把它们当成本仓库的坏引用
#: 会产生一片假警报，而假警报多了真警报就没人看了。
#: 判断标准很简单：这份文档是不是在讲"本仓库里的东西"。
CHECKED_DOCS = [
    ROOT / "README.md",
    ROOT / "docs" / "spec-revisions-2026-09.md",
    ROOT / "docs" / "implementation-baseline.md",
    ROOT / "docs" / "daily-pipeline.md",
    ROOT / "docs" / "data-rights-register.md",
    ROOT / "docs" / "data-capability-baostock.md",
    ROOT / "docs" / "data-capability-eastmoney-direct.md",
] + sorted((ROOT / "docs" / "adr").glob("*.md"))

DOCS = [p for p in CHECKED_DOCS if p.exists()]

#: 允许指向"尚未创建"的路径——**必须显式列出并写明理由**。
#: 把它做成白名单而不是"存在才查"，是为了让新出现的坏引用一定会被发现：
#: 漏查比误报危险得多。
ALLOWED_MISSING = {
    # 主规格 §0 列出的资料包清单，是**完整的**（含尚未生成的项）
    "CODEX_TASKS.md": "主规格 §0 声明的资料包清单，实施中未生成该文件",
    "AGENTS.md": "同上",
}

#: 反引号里的路径引用。
#:
#: 只认**带文件扩展名**的引用。第一版把纯目录名（如 domain/ai/）也算进来，
#: 结果一片假警报——文档里的 domain/ai/ 是相对 src/aquant/ 的简写，
#: 不是仓库根下的路径。而真正会伤人的坏引用是"指向一个不存在的文件或
#: 脚本"，那一定带扩展名。假警报多了，真警报就没人看了。
PATH_IN_BACKTICKS = re.compile(
    r"`([A-Za-z0-9_./-]+/[A-Za-z0-9_./-]+\.[A-Za-z0-9]{1,6})`")

#: 这些前缀指向**运行时生成物或外部系统**，不要求存在于仓库。
SKIP_PREFIXES = (
    "deploy/",          # 产物目录（gitignored）
    "data/",
    "node_modules/",
    "site-packages/",
    "adapters/",        # 相对 src/aquant 的简写
)


def _candidate_paths(raw: str) -> list[Path]:
    """一个引用可能对应的若干真实位置。

    文档里的路径有几种常见基准：仓库根、`src/aquant/`（领域层内部习惯
    写 `domain/data/reader.py`）、`src/`、`apps/`。都试一遍。
    """

    cleaned = raw.strip().rstrip(".,;：:）)")
    return [
        ROOT / cleaned,
        ROOT / "src" / "aquant" / cleaned,
        ROOT / "src" / cleaned,
        ROOT / "apps" / cleaned,
        ROOT / "apps" / "web" / cleaned,
    ]


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_document_path_references_exist(doc: Path):
    """文档里反引号括起来的路径引用必须能在仓库里找到。"""

    text = doc.read_text(encoding="utf-8")
    missing: list[str] = []
    for raw in sorted(set(PATH_IN_BACKTICKS.findall(text))):
        if raw.startswith(SKIP_PREFIXES):
            continue
        if raw in ALLOWED_MISSING:
            continue
        # 通配与占位（如 tests/{a,b}/、src/*/x.py）不做存在性检查
        if any(ch in raw for ch in "*?{}<>"):
            continue
        if not any(c.exists() for c in _candidate_paths(raw)):
            missing.append(raw)

    assert not missing, (
        f"{doc.relative_to(ROOT)} 引用了不存在的路径：{missing}。"
        "如果它属于尚未创建的文件，请加进本用例的 ALLOWED_MISSING 并写明理由。")


def test_the_documentation_chain_is_wired():
    """四份关键文档必须互相指得到。

    阅读链条断了的表现不是报错，而是**读者不知道还有下一份文档**——
    主规格不知道有修订记录、README 不知道有实施状态。
    """

    def has(path: str, needle: str) -> bool:
        return needle in (ROOT / path).read_text(encoding="utf-8")

    chain = [
        ("README.md", "docs/spec-revisions-2026-09.md", "README 指向主规格修订"),
        ("A-Quant-Lab_开发文档_v0.2.md", "docs/spec-revisions-2026-09.md",
         "主规格指向修订记录"),
        ("A-Quant-Lab_开发文档_v0.2.md", "docs/implementation-baseline.md",
         "主规格指向实施状态"),
        ("docs/implementation-baseline.md", "docs/spec-revisions-2026-09.md",
         "基线指向修订记录"),
        ("README.md", "docs/implementation-baseline.md", "README 指向实施状态"),
        ("README.md", "docs/daily-pipeline.md", "README 指向流水线说明"),
        ("README.md", "docs/data-rights-register.md", "README 指向数据权利表"),
    ]
    broken = [label for path, needle, label in chain if not has(path, needle)]
    assert not broken, "文档阅读链条断了：" + str(broken)


def test_readme_has_no_stale_verification_numbers():
    """README 不写容易漂移的小计数。

    写死的计数会过期，而过期的事实比没有事实更糟——读者会以为它还是准的。
    这里只禁止**具体的"X/Y 通过"数字**，不禁止描述性的说明。
    """

    text = (ROOT / "README.md").read_text(encoding="utf-8")
    stale = re.findall(r"\|\s*\d{2,3}/\d{2,3}\s*\|", text)
    assert not stale, (
        "README 里出现了写死的通过计数 " + str(stale)
        + "；这类数字会随时间失真，请改成描述性说明或指向证据脚本。")
