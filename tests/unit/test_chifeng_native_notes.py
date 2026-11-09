from __future__ import annotations

import os
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from aquant.adapters.providers.chifeng_native_notes import (
    ChifengNativeNoteError,
    extract_native_note_facts,
    is_supported_pdf,
    verify_native_note_evidence,
)
from aquant.adapters.providers.s2_machine_verification import MachineVerificationError


PDF_PATH = os.environ.get("CHIFENG_NATIVE_PDF", "/source/chifeng.pdf")
REQUIRED = (
    "revenue",
    "net_profit_attributable",
    "net_profit_consolidated",
    "operating_cashflow",
)
REQUEST = {
    "instrument_id": "600988",
    "company": "赤峰吉隆黄金矿业股份有限公司",
    "period_end": date(2025, 12, 31),
    "announcement_id": "1225021883",
    "document_url": "https://static.cninfo.com.cn/finalpage/2026-03-21/1225021883.PDF",
}


@pytest.fixture(scope="module")
def pdf_bytes() -> bytes:
    path = Path(PDF_PATH)
    if not path.is_file():
        pytest.skip("set CHIFENG_NATIVE_PDF to the registered CNINFO PDF for source-bound tests")
    return path.read_bytes()


@pytest.fixture(scope="module")
def candidate(pdf_bytes):
    return extract_native_note_facts(
        pdf_bytes, **REQUEST, required_fields=REQUIRED,
    )


def test_registered_original_yields_four_current_yuan_note_candidates(pdf_bytes, candidate) -> None:
    assert is_supported_pdf(pdf_bytes)
    assert candidate.version_label == "native_consolidated_notes_v1"
    assert candidate.instrument_id == "600988"
    assert candidate.period_end == date(2025, 12, 31)
    assert candidate.pit_eligible is False
    assert candidate.pdf_sha256.startswith("sha256:d53382f3")
    assert [item.field for item in candidate.candidates] == list(REQUIRED)
    assert {item.amount_unit for item in candidate.candidates} == {"元"}
    assert {item.currency for item in candidate.candidates} == {"CNY"}
    by_field = candidate.by_field
    assert str(by_field["revenue"].value_yuan) == "12638506229.12"
    assert str(by_field["net_profit_attributable"].value_yuan) == "3082367791.68"
    assert str(by_field["net_profit_consolidated"].value_yuan) == "3447387967.50"
    assert str(by_field["operating_cashflow"].value_yuan) == "5555589287.50"
    assert by_field["revenue"].source_current_cell_index == 2
    assert by_field["revenue"].source_prior_cell_index == 7
    assert all(by_field[field].source_current_cell_index == 1
               and by_field[field].source_prior_cell_index == 2
               for field in REQUIRED[1:])


def test_verifier_replays_native_identity_geometry_and_independent_arithmetic(pdf_bytes, candidate) -> None:
    result = verify_native_note_evidence(
        pdf_bytes, candidate=candidate, expected_fields=REQUIRED,
    )
    assert result.verified
    assert all(item.verified for item in result.fields)
    assert all(item.satisfied_paths == ("native_full_report_note_crosscheck",)
               for item in result.fields)
    proof = result.sections[0]
    assert proof["proof_schema"] == "cninfo-native-full-report-note-crosscheck-v1"
    assert proof["identity"]["legacy_cover_check"].startswith("not_claimed")
    assert proof["identity"]["page9_a_share_table_row"]["row_bbox"] is not None
    assert proof["identity"]["page9_h_share_table_row"]["raw_cells"][2] == "06693"
    assert proof["basis"]["main_statement_scan"].startswith("not_scanned")
    for field in REQUIRED:
        primary = proof["field_evidence"][field]["primary_current_fact"]
        current_cell = primary["row"]["cells"][primary["current_cell_index"]]
        assert primary["page"] in (165, 166, 178)
        assert current_cell["bbox"] is not None
        assert current_cell["native_glyphs"]
        assert proof["field_evidence"][field]["independent_native_arithmetic"]


def test_fixed_field_subset_is_supported(pdf_bytes) -> None:
    candidate = extract_native_note_facts(
        pdf_bytes, **REQUEST, required_fields=("revenue",),
    )
    assert tuple(candidate.by_field) == ("revenue",)


@pytest.mark.parametrize(
    ("key", "bad_value"),
    [
        ("instrument_id", "06693"),
        ("company", "赤峰黄金"),
        ("period_end", date(2024, 12, 31)),
        ("announcement_id", "1225021884"),
        ("document_url", "https://static.cninfo.com.cn/other.PDF"),
    ],
)
def test_request_identity_mismatches_fail_closed(pdf_bytes, key, bad_value) -> None:
    request = {**REQUEST, key: bad_value}
    with pytest.raises(ChifengNativeNoteError):
        extract_native_note_facts(
            pdf_bytes, **request, required_fields=("revenue",),
        )


def test_unregistered_or_modified_pdf_fails_closed(pdf_bytes) -> None:
    modified = pdf_bytes + b" "
    assert not is_supported_pdf(modified)
    with pytest.raises(ChifengNativeNoteError, match="SHA-256"):
        extract_native_note_facts(
            modified, **REQUEST, required_fields=("revenue",),
        )


def test_invalid_and_duplicate_field_cohorts_fail_closed(pdf_bytes) -> None:
    for fields in ((), ("parent_equity",), ("revenue", "revenue")):
        with pytest.raises(ChifengNativeNoteError):
            extract_native_note_facts(pdf_bytes, **REQUEST, required_fields=fields)


def test_candidate_source_evidence_mutation_is_rejected(pdf_bytes, candidate) -> None:
    revenue = candidate.by_field["revenue"]
    mutated_item = replace(revenue, source_current_cell_index=7)
    mutated = replace(
        candidate,
        candidates=tuple(mutated_item if item.field == "revenue" else item
                         for item in candidate.candidates),
    )
    with pytest.raises(MachineVerificationError, match="source-row evidence differ"):
        verify_native_note_evidence(pdf_bytes, candidate=mutated, expected_fields=REQUIRED)


def test_verifier_rejects_wrong_expected_field_set(pdf_bytes, candidate) -> None:
    with pytest.raises(MachineVerificationError, match="candidate fields"):
        verify_native_note_evidence(
            pdf_bytes, candidate=candidate, expected_fields=("revenue",),
        )
