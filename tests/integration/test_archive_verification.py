"""归档复核器自身的测试。

一个只会说"通过"的复核器比没有复核器更糟：它会把未验证的东西
包装成已验证的结论。所以这里用合成归档逐项制造破坏，要求复核器
**必须**报错。

真实归档目录里的一次运行恰好暴露了一个 bug：复核器的路径算术
与 ForwardArchive 不一致，于是它把一次完全正常的归档整批报成
"字节不一致"。那次的教训写进了 _cas_path 的文档字符串——复核器
的路径规则必须与写入方逐字一致，否则结论毫无意义。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from verify_archive import CAS_DIRNAME, SUMMARY_NAME, verify  # noqa: E402


def _make_archive(tmp_path: Path, *, pdfs: int = 2) -> Path:
    """造一份自洽的合成归档：摘要里的哈希与磁盘字节真的对得上。"""

    root = tmp_path / "forward-archive"
    (root / CAS_DIRNAME).mkdir(parents=True)

    records = []
    for i in range(pdfs):
        payload = b"%PDF-1.7\n" + bytes([65 + i]) * (500 + i * 137)
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        bare = digest.removeprefix("sha256:")
        target = root / CAS_DIRNAME / bare[:2] / bare
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        records.append({
            "announcement_id": f"12254692{80 + i}",
            "instrument_code": "002344",
            "document_url": f"http://static.cninfo.com.cn/finalpage/2026-08-11/12254692{80 + i}.PDF",
            "document_content_hash": digest,
            "document_bytes": len(payload),
            "document_archived": True,
            "document_hash_verified": True,
        })

    (root / SUMMARY_NAME).write_bytes(
        json.dumps({"records": records}, ensure_ascii=False, indent=2).encode("utf-8")
    )
    return root


def _cas_of(root: Path, digest: str) -> Path:
    bare = digest.removeprefix("sha256:")
    return root / CAS_DIRNAME / bare[:2] / bare


def _read_summary(root: Path) -> dict:
    return json.loads((root / SUMMARY_NAME).read_text(encoding="utf-8"))


# ============================================================ 正向：自洽即通过
def test_self_consistent_archive_passes(tmp_path):
    root = _make_archive(tmp_path)
    checks, failures = verify(root)
    assert failures == [], [f["detail"] for f in failures]
    # 每份记录应覆盖 4 项：路径、体积、摘要、PDF 头
    assert len(checks) == 1 + 2 * 4


# ==================================================== 反向：改一个字节就报错
def test_tampered_byte_is_caught(tmp_path):
    root = _make_archive(tmp_path)
    rec = _read_summary(root)["records"][0]
    victim = _cas_of(root, rec["document_content_hash"])
    victim.write_bytes(victim.read_bytes() + b"x")   # 只多一个字节

    _, failures = verify(root)
    kinds = {f["check"] for f in failures}
    assert "byte_size" in kinds, failures
    assert "content_hash" in kinds, failures


# ==================================================== 反向：删掉字节就报错
def test_missing_blob_is_caught(tmp_path):
    root = _make_archive(tmp_path)
    rec = _read_summary(root)["records"][0]
    _cas_of(root, rec["document_content_hash"]).unlink()

    _, failures = verify(root)
    assert any(f["check"] == "cas_layout" for f in failures), failures


# ============================================ 反向：摘要自己撒谎也要报错
def test_manifest_lying_about_size_is_caught(tmp_path):
    """字节没动，只把摘要里的体积改掉——这正是"自称已验证"的形态。"""

    root = _make_archive(tmp_path)
    doc = _read_summary(root)
    doc["records"][1]["document_bytes"] = doc["records"][1]["document_bytes"] + 1
    (root / SUMMARY_NAME).write_bytes(
        json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")
    )

    _, failures = verify(root)
    assert [f["check"] for f in failures] == ["byte_size"], failures


# ======================================== 反向：把 PDF 换成别的东西要报错
def test_non_pdf_masquerading_as_pdf_is_caught(tmp_path):
    root = _make_archive(tmp_path)
    rec = _read_summary(root)["records"][0]
    victim = _cas_of(root, rec["document_content_hash"])
    payload = b"<html>not a pdf</html>"
    victim.write_bytes(payload)
    # 同步更新摘要里的哈希与体积，让"路径/体积/摘要"三项全部自洽，
    # 只剩 PDF 头这一项能揭穿它——模拟"页面被当成 PDF 存下来"。
    doc = _read_summary(root)
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    bare = digest.removeprefix("sha256:")
    new_path = root / CAS_DIRNAME / bare[:2] / bare
    new_path.parent.mkdir(parents=True, exist_ok=True)
    new_path.write_bytes(payload)
    victim.unlink()
    doc["records"][0]["document_content_hash"] = digest
    doc["records"][0]["document_bytes"] = len(payload)
    (root / SUMMARY_NAME).write_bytes(
        json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")
    )

    _, failures = verify(root)
    assert [f["check"] for f in failures] == ["pdf_magic"], failures


# ================================================ 没有摘要时不得假装通过
def test_missing_summary_is_reported(tmp_path):
    with pytest.raises(FileNotFoundError):
        verify(tmp_path / "nowhere")


# ============================== 没有字节时是"跳过"，不是"通过"，也不是失败
def test_missing_blob_tree_exits_as_skip(tmp_path, capsys):
    """干净导出里没有归档字节，这既不是通过也不是失败。

    版本库不携带大体积字节（见 .gitignore 与归档 README），所以
    "导出目录里查不到字节"是预期状态。但也不能报成通过——
    那会把"没查"包装成"查过了没问题"。
    """

    from verify_archive import main

    code = main(["--root", str(tmp_path)])
    assert code == 2, "没有可查字节时必须返回跳过码，而不是 0（通过）或 1（不一致）"
    out = capsys.readouterr().out
    assert "[skip]" in out
    assert "不等于归档通过" in out, "跳过必须说清它不能当作通过"
