"""离线复核前向归档：把"归档成功"从记录变成可检验的断言。

背景
----
`deploy/agentctl-q0/forward-archive/t5-cninfo-run.json` 是 T5 运行留下的
证据摘要。它自己声称了每份公告的 `document_content_hash` 与
`document_bytes`，但**一份自称已归档的清单并不能证明字节真的还在**。
大体积原始字节不进版本库（见 .gitignore 的说明），因此本地归档库是
唯一的字节来源——更需要一个能独立跑通的复核入口。

本脚本不联网、不依赖 pytest、不读环境变量（除了可选的 --root）。

它对每条记录做四项检查：
  1. 内容寻址：文件必须位于 raw/<hash 前两位>/<hash 其余部分>
  2. 体积一致：磁盘字节数 == 记录里的 document_bytes
  3. 摘要一致：重新计算 sha256 == 记录里的 document_content_hash
  4. 头部一致：PDF 记录必须以 %PDF- 开头

用法
----
    python tools/verify_archive.py
    python tools/verify_archive.py --root deploy/agentctl-q0/forward-archive

退出码：0 = 全部通过；1 = 存在不一致（并逐条打印差异）；2 = 找不到摘要。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE = ROOT / "deploy" / "agentctl-q0" / "forward-archive"
SUMMARY_NAME = "t5-cninfo-run.json"
CAS_DIRNAME = "raw"


def _cas_path(root: Path, digest: str) -> Path:
    """内容寻址路径。

    必须与 ForwardArchive.store_bytes 的布局逐字一致：
        rel = f"{hex[:2]}/{hex}"
    即**一级目录取哈希前两位，文件名是完整 64 位十六进制**（不是余下部分）。
    这条曾经写错成 raw/ab/<剩余>，于是复核器找不到任何文件、
    把一次完全正常的归档报成"字节不一致"——复核器的路径算术本身
    也需要被证据检验。
    """

    bare = digest.removeprefix("sha256:")
    return root / CAS_DIRNAME / bare[:2] / bare


def verify(root: Path) -> tuple[list[dict], list[dict]]:
    """返回 (checks, failures)。checks 是全部检查项，failures 是未通过项。"""

    summary_path = root / SUMMARY_NAME
    if not summary_path.exists():
        raise FileNotFoundError(summary_path)

    doc = json.loads(summary_path.read_text(encoding="utf-8"))
    records = doc.get("records") or []

    checks: list[dict] = []
    failures: list[dict] = []

    def check(kind: str, ok: bool, detail: str, **extra) -> None:
        item = {"check": kind, "ok": bool(ok), "detail": detail, **extra}
        checks.append(item)
        if not ok:
            failures.append(item)

    check("manifest_non_empty", bool(records),
          f"摘要中的记录条数 = {len(records)}")

    for rec in records:
        ann = str(rec.get("announcement_id"))
        digest = rec.get("document_content_hash")
        declared_bytes = rec.get("document_bytes")

        if not rec.get("document_archived"):
            check("archived_flag", False, f"{ann}: 摘要自称未归档", announcement_id=ann)
            continue
        if not digest:
            check("hash_present", False, f"{ann}: 摘要未记录 content_hash", announcement_id=ann)
            continue

        path = _cas_path(root, digest)
        check("cas_layout", path.exists(),
              f"{ann}: {path.relative_to(root).as_posix()}", announcement_id=ann)
        if not path.exists():
            continue

        payload = path.read_bytes()
        check("byte_size", declared_bytes == len(payload),
              f"{ann}: 摘要 {declared_bytes} / 磁盘 {len(payload)}", announcement_id=ann)

        actual = "sha256:" + hashlib.sha256(payload).hexdigest()
        check("content_hash", actual == digest,
              f"{ann}: {actual[:23]}...", announcement_id=ann)

        if str(rec.get("document_url", "")).lower().endswith(".pdf"):
            check("pdf_magic", payload[:5] == b"%PDF-",
                  f"{ann}: 头部 {payload[:5]!r}", announcement_id=ann)

    return checks, failures


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="离线复核前向归档的字节一致性")
    ap.add_argument("--root", type=Path, default=DEFAULT_ARCHIVE,
                    help=f"归档根目录（默认 {DEFAULT_ARCHIVE}）")
    args = ap.parse_args(argv)

    try:
        checks, failures = verify(args.root)
    except FileNotFoundError as exc:
        print(f"[skip] 找不到归档摘要：{exc}")
        print("       这是首次运行前的正常状态；跑过一次 T5 之后再复核。")
        return 2

    passed = len(checks) - len(failures)
    print(f"归档根目录：{args.root}")
    print(f"检查项 {passed}/{len(checks)} 通过")

    for item in failures:
        print(f"  ✗ {item['check']}: {item['detail']}")

    if failures:
        print("\n结论：归档字节与摘要不一致——不得引用为该次运行的证据。")
        return 1

    print("结论：摘要中的每一份归档字节都在磁盘上，且体积与摘要一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
