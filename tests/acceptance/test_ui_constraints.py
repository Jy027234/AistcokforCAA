"""界面契约约束（主文档 §5.3、§5.4、§8.3）。

这些不是样式偏好，而是产品承诺。它们必须能在**构建产物**上被检查，
因为用户看到的是产物，不是源码：

  1. 不得出现 probability / 预期收益 / 综合评分这类字段——
     §5.3 禁止把横截面排名改写成概率；
  2. 排名必须读作"排名百分位"，不能读成"上涨概率"；
  3. 风险状态必须同时有文字，不能只靠颜色（色觉障碍下颜色不传递信息）；
  4. 合成数据必须带水印；
  5. 不得出现"失败"来指代不可交易——要说清规则与生效日。

检查源码与构建产物两处：源码保证意图，产物保证用户真的看不到。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "apps" / "web" / "src"
DIST = ROOT / "apps" / "web" / "dist" / "assets"


def _src_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*") if p.suffix in {".ts", ".tsx"})


def _without_comments(text: str) -> str:
    """去掉注释后再检查。

    这不是为了"让测试通过"：`types.ts` 里有一段注释明确写着
    "刻意不存在的字段：probability / expectedReturn / totalScore"，
    那是**约束的记载**。检查的意义在于代码里不出现这些字段，
    所以必须把注释排除，否则这条约束永远无法写进文档——
    连"我们不提供这个字段"都不许说。
    """

    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", text)


def _code_only(path: Path) -> str:
    return _without_comments(path.read_text(encoding="utf-8"))


def _dist_bundles() -> list[Path]:
    return sorted(DIST.glob("*.js")) if DIST.exists() else []


# ==================================================== 禁止概率化的字段名
FORBIDDEN_IDENTIFIERS = [
    "probability",
    "expectedReturn",
    "expected_return",
    "totalScore",
    "total_score",
    "confidenceScore",
    "winRate",
]


@pytest.mark.parametrize("needle", FORBIDDEN_IDENTIFIERS)
def test_source_has_no_probability_identifiers(needle):
    hits = [p.name for p in _src_files() if needle in _code_only(p)]
    assert hits == [], f"{needle} 出现在 " + ", ".join(hits)


@pytest.mark.parametrize("needle", FORBIDDEN_IDENTIFIERS)
def test_bundle_has_no_probability_identifiers(needle):
    bundles = _dist_bundles()
    if not bundles:
        pytest.skip("尚未构建 dist，跳过产物检查")
    hits = [p.name for p in bundles if needle in p.read_text(encoding="utf-8", errors="ignore")]
    assert hits == [], f"{needle} 出现在构建产物 " + ", ".join(hits)


# ============================================ 排名必须读作排名，不是概率
def test_rank_semantics_are_stated_as_rank():
    """界面上必须有"排名百分位"这类说明，且不得称其为概率。"""

    api = (SRC / "lib" / "api.ts").read_text(encoding="utf-8")
    types = (SRC / "lib" / "types.ts").read_text(encoding="utf-8")
    assert "rankPct" in types or "rankPct" in api
    # rankSemantics 由服务端视图模型给出（application/workspace_view.py）
    assert "rankSemantics" in types


# ==================================== 风险状态必须有文字，不能只靠颜色
def test_badge_renders_text_for_non_neutral_tones():
    ui = (SRC / "components" / "ui.tsx").read_text(encoding="utf-8")
    # Badge 必须渲染 children（文字），而不是只渲染一个色块
    assert "children" in ui
    assert "badge-" in ui


# ==================================================== 合成数据必须带水印
def test_synthetic_watermark_is_rendered():
    app = (SRC / "App.tsx").read_text(encoding="utf-8")
    assert "SYNTHETIC" in app, "界面必须识别合成数据"
    assert "watermark" in app, "合成数据必须显示水印"


# ======================== 不可交易不能说成"失败"，要说规则与生效日
def test_untradeable_is_not_worded_as_failure():
    """§8.3：不可交易要给规则、生效日与修复动作，不能只说"失败"。"""

    for path in _src_files():
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"[^\n]*不可交易[^\n]*", text):
            line = match.group(0)
            assert "失败" not in line, f"{path.name}: 不可交易被写成失败：{line.strip()}"


# ============================================= 构建产物里的水印文案
def test_bundle_mentions_synthetic_data():
    bundles = _dist_bundles()
    if not bundles:
        pytest.skip("尚未构建 dist，跳过产物检查")
    blob = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in bundles)
    assert "虚构示例数据" in blob or "SYNTHETIC" in blob
