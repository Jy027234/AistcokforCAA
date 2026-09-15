"""ADR-011 边界强制：领域层不得依赖 agentctl。

为什么这条必须由测试强制
------------------------
"领域层不依赖 agentctl"是一个**架构约定**。约定会随着一次次"临时 import
一下"而失效，而且失效时不会有任何报错——直到某天 agentctl 的一次版本
前移改变了某个行为，量化计算跟着变，而没人能解释为什么结果不一样。

这类问题必须在引入的那一刻就被拦住，也就是靠测试而不是靠评审。

方向是单向的：
    adapters/agentctl/  ->  领域层      ✅ 允许
    领域层              ->  agentctl    ❌ 禁止
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "aquant"

#: 允许 import agentctl 的目录（适配层）。只此一处。
ALLOWED_PREFIX = SRC / "adapters" / "agentctl"

#: 明确禁止被领域层引入的模块前缀。
FORBIDDEN_MODULES = ("agentctl",)


def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _imported_modules(path: Path) -> set[str]:
    """取出文件里全部 import 的顶层模块名。

    用 AST 而不是正则：正则会把注释与字符串里的 "agentctl" 也算进去，
    那样测试会因为一句文档而误报，进而被"顺手"放宽。
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # 相对 import（level>0）不可能是外部模块
            if node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return found


def _violations() -> list[str]:
    out: list[str] = []
    for path in _python_files(SRC):
        if ALLOWED_PREFIX in path.parents:
            continue
        for module in _imported_modules(path):
            if module in FORBIDDEN_MODULES:
                out.append(f"{path.relative_to(ROOT)} imports {module}")
    return out


def test_domain_layer_does_not_import_agentctl():
    violations = _violations()
    assert violations == [], (
        "领域层引入了 agentctl，违反 ADR-011 的单向依赖：\n  "
        + "\n  ".join(violations)
        + "\n请把该依赖移到 src/aquant/adapters/agentctl/ 下。"
    )


def test_the_boundary_check_actually_reads_files():
    """守卫自身必须非空运行。

    一个"扫了 0 个文件所以通过"的测试，比没有测试更糟：
    它给出一个看起来有效的保证。这条断言防止扫描范围被静默改坏。
    """

    files = _python_files(SRC)
    assert len(files) > 20, f"只扫到 {len(files)} 个文件，扫描范围可能被改坏"
    # 适配层本身必须存在，否则 ALLOWED_PREFIX 就成了空话
    assert ALLOWED_PREFIX.exists(), f"适配层目录不存在：{ALLOWED_PREFIX}"


def test_ast_parser_detects_a_real_import(tmp_path):
    """守卫对真实 import 有效（用合成文件验证，不依赖当前代码恰好干净）。"""

    sample = tmp_path / "sample.py"
    sample.write_text("import agentctl\nfrom agentctl import x\n", encoding="utf-8")
    assert _imported_modules(sample) == {"agentctl"}

    ok = tmp_path / "ok.py"
    ok.write_text(
        "# import agentctl\n"
        'DOC = "agentctl"\n'
        "from . import sibling\n",
        encoding="utf-8",
    )
    assert _imported_modules(ok) == set(), "注释与字符串不得被当成 import"
