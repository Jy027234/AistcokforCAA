"""Bounded native-text proof for 000422 parent equity at 2026-06-30.

This case profile proves a current canonical value and its native opening-to-close
bridge.  It deliberately does not decide archive receipt admission, index closure,
or historical point-in-time availability.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import math
import re
from typing import Any, Callable, Mapping

import pdfplumber


class YihuaCanonicalProofError(ValueError):
    """Raised when the exact source package or native table layout does not match."""


_ISSUER = "湖北宜化化工股份有限公司"
_TARGET_INSTRUMENT = "SZ.000422"
_TARGET_PERIOD_END = "2026-06-30"
_EXPECTED_SOURCE_IDS = frozenset(
    {
        "1225468007",  # 2026 H1 current report
        "1225183616",  # 2025 annual opening anchor
        "1222991641",  # FY2023 auditor companion
        "1222991666",  # FY2023 board notice
        "1225183650",  # FY2024 auditor companion, opaque native body
        "1225183652",  # FY2024 board notice
    }
)

_SOURCE_MANIFEST: dict[str, dict[str, Any]] = {
    "1225468007": {
        "title": "2026年半年度报告",
        "sha256": "6963a7b5ca322c8fe53d59eafe383bb882689ad9e2a9b78aa669373f58de4e12",
        "byte_count": 1339790,
        "page_count": 179,
        "title_pages": (9,),
        "native_text_used": True,
    },
    "1225183616": {
        "title": "2025年年度报告",
        "sha256": "13067dd7ca378659a5d01a6600c911dda9335afc970ac10f554dfd79a945987f",
        "byte_count": 1932240,
        "page_count": 260,
        "title_pages": (9,),
        "native_text_used": True,
    },
    "1222991641": {
        "title": "湖北宜化化工股份有限公司2024年度追溯调整前期财务报表数据的专项说明",
        "sha256": "9378ef0cd160da7ead1881ca1e4db5e50b00d5c45db6c0f773eab2a12406743a",
        "byte_count": 2423620,
        "page_count": 13,
        "title_pages": (4,),
        "native_text_used": True,
    },
    "1222991666": {
        "title": "湖北宜化化工股份有限公司董事会关于2024年度追溯调整前期财务报表数据的说明",
        "sha256": "18a7701e0d52d83f6c5d86eb4dab7c55c4be3af2283c30bf8a3ae1c9f135c207",
        "byte_count": 456512,
        "page_count": 6,
        "title_pages": (1,),
        "native_text_used": True,
    },
    "1225183650": {
        "title": "关于湖北宜化化工股份有限公司同一控制下企业合并追溯调整前期财务报表数据的专项说明",
        "sha256": "f46e817c54a818eae59a42579b4a0f5d8bef85707473d441c9a339e735168cb0",
        "byte_count": 2960440,
        "page_count": 16,
        "title_pages": (),
        "native_text_used": False,
    },
    "1225183652": {
        "title": "湖北宜化化工股份有限公司关于同一控制下企业合并追溯调整前期财务报表数据的公告",
        "sha256": "b090c18e1fae1a1335140676251959070a262d880d18cd10e1d15b639163fac1",
        "byte_count": 269236,
        "page_count": 9,
        "title_pages": (1,),
        "native_text_used": True,
    },
}

_MONEY_RE = re.compile(r"(?<![\w])(?:-?\d{1,3}(?:,\d{3})+|-?\d+)\.\d{2}(?!\w)")
_CHECKED = "√✓✔☑☒"
_UNCHECKED = "□☐"


def _fail(message: str) -> None:
    raise YihuaCanonicalProofError(message)


def _norm(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _display(value: Any) -> str:
    return str(value or "").strip()


def _bbox_tuple(bbox: Any) -> tuple[float, float, float, float] | None:
    if bbox is None or len(bbox) != 4:
        return None
    return tuple(round(float(part), 3) for part in bbox)  # type: ignore[return-value]


def _bbox_dict(bbox: Any) -> dict[str, float] | None:
    parts = _bbox_tuple(bbox)
    if parts is None:
        return None
    return {"x0": parts[0], "top": parts[1], "x1": parts[2], "bottom": parts[3]}


def _money(raw: Any, *, label: str) -> tuple[str, Decimal]:
    compact = re.sub(r"[\s,]", "", str(raw or ""))
    if not compact or not re.fullmatch(r"-?\d+(?:\.\d{2})", compact):
        _fail(f"{label}: expected one native two-decimal amount, got {raw!r}")
    try:
        value = Decimal(compact)
    except InvalidOperation as exc:
        raise YihuaCanonicalProofError(f"{label}: invalid decimal {raw!r}") from exc
    return compact, value


def _page_text(page: Any, *, label: str) -> str:
    text = page.extract_text(x_tolerance=2, y_tolerance=3) or ""
    if not text.strip():
        _fail(f"{label}: bounded page has no native text")
    return text


def _page_lines(page: Any) -> list[dict[str, Any]]:
    return page.extract_text_lines(layout=False, strip=True)


def _quote_with_bbox(line: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "text": str(line.get("text", "")),
        "bbox_pdf_points": _bbox_dict(
            (line.get("x0"), line.get("top"), line.get("x1"), line.get("bottom"))
        ),
    }


def _find_line(page: Any, predicate: Callable[[str], bool], *, label: str) -> dict[str, Any]:
    matches = [line for line in _page_lines(page) if predicate(_norm(line.get("text", "")))]
    if len(matches) != 1:
        _fail(f"{label}: expected exactly one native text line, found {len(matches)}")
    return matches[0]


def _line_window(page: Any, predicate: Callable[[str], bool], *, label: str, before: int = 0, after: int = 0) -> list[dict[str, Any]]:
    lines = _page_lines(page)
    matches = [index for index, line in enumerate(lines) if predicate(_norm(line.get("text", "")))]
    if len(matches) != 1:
        _fail(f"{label}: expected exactly one native text anchor, found {len(matches)}")
    index = matches[0]
    start = max(0, index - before)
    end = min(len(lines), index + after + 1)
    return [_quote_with_bbox(line) for line in lines[start:end]]


def _source_cell(
    *,
    announcement_id: str,
    page_number: int,
    table_index: int,
    table: Any,
    rows: list[list[Any]],
    row_index: int,
    column_index: int,
    cell_key: str,
    row_label: str,
    column_role: str,
    header_quote: str,
) -> dict[str, Any]:
    if row_index >= len(rows) or column_index >= len(rows[row_index]):
        _fail(f"{cell_key}: table row/column is absent")
    try:
        bbox = table.rows[row_index].cells[column_index]
    except (AttributeError, IndexError) as exc:
        raise YihuaCanonicalProofError(f"{cell_key}: table cell geometry is unavailable") from exc
    if bbox is None:
        _fail(f"{cell_key}: source table cell has no bounded native geometry")
    return {
        "cell_key": cell_key,
        "announcement_id": announcement_id,
        "physical_page": page_number,
        "table_index": table_index,
        "row_index": row_index,
        "column_index": column_index,
        "row_label": row_label,
        "column_role": column_role,
        "header_quote": header_quote,
        "native_cell_text": _display(rows[row_index][column_index]),
        "bbox_pdf_points": _bbox_dict(bbox),
    }


def _find_table_row(
    page: Any,
    *,
    label: str,
    matches: Callable[[str], bool] | None = None,
    case: str,
) -> tuple[int, Any, list[list[Any]], int]:
    matcher = matches or (lambda actual: actual == _norm(label))
    hits: list[tuple[int, Any, list[list[Any]], int]] = []
    for table_index, table in enumerate(page.find_tables()):
        rows = table.extract()
        for row_index, row in enumerate(rows):
            row_label = _norm(row[0] if row else "")
            if matcher(row_label):
                hits.append((table_index, table, rows, row_index))
    if len(hits) != 1:
        _fail(f"{case}: expected one table row for {label!r}, found {len(hits)}")
    return hits[0]


def _row_by_exact_label(page: Any, label: str, *, case: str) -> tuple[int, Any, list[list[Any]], int]:
    return _find_table_row(page, label=label, case=case)


def _assert_table_header(rows: list[list[Any]], row_index: int, expected: Mapping[int, str], *, case: str) -> None:
    if row_index >= len(rows):
        _fail(f"{case}: expected table header row {row_index}")
    row = rows[row_index]
    for column_index, expected_value in expected.items():
        if column_index >= len(row) or _norm(row[column_index]) != _norm(expected_value):
            _fail(f"{case}: column {column_index} header did not match {expected_value!r}")


def _amount_cell(
    *,
    announcement_id: str,
    page_number: int,
    table_index: int,
    table: Any,
    rows: list[list[Any]],
    row_index: int,
    column_index: int,
    cell_key: str,
    row_label: str,
    column_role: str,
    header_quote: str,
) -> tuple[dict[str, Any], Decimal]:
    cell = _source_cell(
        announcement_id=announcement_id,
        page_number=page_number,
        table_index=table_index,
        table=table,
        rows=rows,
        row_index=row_index,
        column_index=column_index,
        cell_key=cell_key,
        row_label=row_label,
        column_role=column_role,
        header_quote=header_quote,
    )
    raw, value = _money(cell["native_cell_text"], label=cell_key)
    cell["value_yuan"] = raw
    return cell, value


def _blank_cell(
    *,
    announcement_id: str,
    page_number: int,
    table_index: int,
    table: Any,
    rows: list[list[Any]],
    row_index: int,
    column_index: int,
    cell_key: str,
    row_label: str,
    column_role: str,
    header_quote: str,
) -> dict[str, Any]:
    cell = _source_cell(
        announcement_id=announcement_id,
        page_number=page_number,
        table_index=table_index,
        table=table,
        rows=rows,
        row_index=row_index,
        column_index=column_index,
        cell_key=cell_key,
        row_label=row_label,
        column_role=column_role,
        header_quote=header_quote,
    )
    if cell["native_cell_text"]:
        _fail(f"{cell_key}: source printed a value where the candidate expects a literal blank")
    cell.update({"literal_status": "LITERAL_BLANK", "value_yuan": None})
    return cell


def _amount_words(line: Mapping[str, Any], *, label: str) -> list[str]:
    values = _MONEY_RE.findall(str(line.get("text", "")))
    if len(values) != 3:
        _fail(f"{label}: expected three native before/impact/after values, found {values!r}")
    return values


def _word_bbox(page: Any, value: str, *, y_min: float, y_max: float, label: str) -> dict[str, float]:
    hits = []
    for word in page.extract_words(x_tolerance=1, y_tolerance=2, keep_blank_chars=False):
        if _norm(word.get("text", "")) == _norm(value) and y_min <= float(word["top"]) <= y_max:
            hits.append(word)
    if len(hits) != 1:
        _fail(f"{label}: expected one native numeric word {value!r}, found {len(hits)}")
    word = hits[0]
    return _bbox_dict((word["x0"], word["top"], word["x1"], word["bottom"])) or {}


def _line_bbox_union(lines: list[Mapping[str, Any]]) -> dict[str, float] | None:
    boxes = [
        (line.get("x0"), line.get("top"), line.get("x1"), line.get("bottom"))
        for line in lines
        if line.get("x0") is not None
    ]
    if not boxes:
        return None
    return _bbox_dict(
        (
            min(float(box[0]) for box in boxes),
            min(float(box[1]) for box in boxes),
            max(float(box[2]) for box in boxes),
            max(float(box[3]) for box in boxes),
        )
    )


def _native_amount_row(
    page: Any,
    *,
    label: str,
    label_parts: tuple[str, ...],
    case: str,
    header_quote: str,
    row_period: str,
) -> dict[str, Any]:
    lines = _page_lines(page)
    label_indexes = [
        index
        for index, line in enumerate(lines)
        if all(part in _norm(line.get("text", "")) for part in label_parts)
    ]
    if len(label_indexes) == 1:
        label_index = label_indexes[0]
        label_lines = [lines[label_index]]
        amount_line = lines[label_index]
        values = _MONEY_RE.findall(str(amount_line.get("text", "")))
        if len(values) != 3:
            following = [
                line
                for line in lines[label_index + 1 : min(len(lines), label_index + 4)]
                if len(_MONEY_RE.findall(str(line.get("text", "")))) == 3
            ]
            if len(following) != 1:
                _fail(f"{case}: cannot uniquely align the three table values with the row label")
            amount_line = following[0]
            values = _MONEY_RE.findall(str(amount_line.get("text", "")))
            trailing = [
                line
                for line in lines[label_index + 1 : min(len(lines), label_index + 5)]
                if _norm(line.get("text", "")) == "合计"
            ]
            label_lines.extend([amount_line, *trailing[:1]])
    else:
        _fail(f"{case}: expected one native parent-equity label, found {len(label_indexes)}")

    if len(values) != 3:
        _fail(f"{case}: expected three source values, found {values!r}")
    cleaned: list[str] = []
    decimals: list[Decimal] = []
    for index, raw in enumerate(values):
        amount, decimal_value = _money(raw, label=f"{case}:value-{index}")
        cleaned.append(amount)
        decimals.append(decimal_value)

    row_box = _line_bbox_union(label_lines)
    if row_box is None:
        _fail(f"{case}: row quote has no native coordinates")
    value_cells: dict[str, Any] = {}
    roles = ("before", "same_control_effect", "after")
    for index, role in enumerate(roles):
        value_cells[role] = {
            "cell_key": f"{case}:p{row_period}:parent_equity:{role}",
            "physical_page": None,
            "row_label": label,
            "column_role": role,
            "native_cell_text": cleaned[index],
            "value_yuan": cleaned[index],
            "coordinate_kind": "native_numeric_word_bbox_with_aligned_table_header",
            "value_bbox_pdf_points": _word_bbox(
                page,
                values[index],
                y_min=float(amount_line["top"]) - 0.5,
                y_max=float(amount_line["bottom"]) + 0.5,
                label=f"{case}:value-{index}",
            ),
            "row_label_bbox_pdf_points": _line_bbox_union([lines[label_index]]),
            "row_bbox_pdf_points": row_box,
            "period_header_quote": header_quote,
        }
    return {
        "row_label": label,
        "row_quote": str(amount_line.get("text", "")) if label_index == lines.index(amount_line) else " ".join(
            str(line.get("text", "")) for line in label_lines
        ),
        "row_bbox_pdf_points": row_box,
        "period": row_period,
        "period_header_quote": header_quote,
        "native_values": value_cells,
        "_decimals": decimals,
    }


def _verify_identity(source_pdfs: Mapping[str, bytes]) -> tuple[dict[str, Any], dict[str, Any]]:
    if set(source_pdfs) != _EXPECTED_SOURCE_IDS:
        missing = sorted(_EXPECTED_SOURCE_IDS - set(source_pdfs))
        unexpected = sorted(set(source_pdfs) - _EXPECTED_SOURCE_IDS)
        _fail(f"source id closure mismatch; missing={missing}, unexpected={unexpected}")

    identity: dict[str, Any] = {}
    readers: dict[str, Any] = {}
    for announcement_id in sorted(_EXPECTED_SOURCE_IDS):
        raw = source_pdfs[announcement_id]
        if not isinstance(raw, bytes) or not raw.startswith(b"%PDF-"):
            _fail(f"{announcement_id}: source is not raw PDF bytes")
        expected = _SOURCE_MANIFEST[announcement_id]
        actual_hash = sha256(raw).hexdigest()
        if actual_hash != expected["sha256"]:
            _fail(f"{announcement_id}: SHA-256 does not match the receipt-bound source identity")
        if len(raw) != expected["byte_count"]:
            _fail(f"{announcement_id}: byte count does not match the receipt-bound source identity")

        try:
            pdf = pdfplumber.open(BytesIO(raw))
        except Exception as exc:  # fail closed with the source id in the message
            raise YihuaCanonicalProofError(f"{announcement_id}: cannot open source PDF") from exc
        if len(pdf.pages) != expected["page_count"]:
            pdf.close()
            _fail(f"{announcement_id}: physical page count differs from the pinned document identity")
        readers[announcement_id] = pdf

        title_quote = None
        if expected["native_text_used"]:
            for page_number in expected["title_pages"]:
                text = _page_text(pdf.pages[page_number - 1], label=f"{announcement_id}:title")
                compact = _norm(text)
                if _norm(_ISSUER) not in compact or _norm(expected["title"]) not in compact:
                    _fail(f"{announcement_id}: issuer/title identity quote did not match native text")
                title_quote = {
                    "physical_page": page_number,
                    "issuer": _ISSUER,
                    "title": expected["title"],
                    "quote": text,
                }
                break
        identity[announcement_id] = {
            "expected_title": expected["title"],
            "native_title_verified": expected["native_text_used"],
            "identity_basis": (
                "native issuer/title plus exact PDF SHA-256 and byte count"
                if expected["native_text_used"]
                else "exact source ID, PDF SHA-256, and byte count; native body is opaque and unused"
            ),
            "sha256": actual_hash,
            "byte_count": len(raw),
            "physical_page_count": len(pdf.pages),
            "native_title_quote": title_quote,
        }
    return identity, readers


def _assert_checked_box(text: str, *, affirmative: bool, case: str) -> str:
    compact = _norm(text)
    if affirmative:
        expected = f"[{re.escape(_CHECKED)}]是[{re.escape(_UNCHECKED)}]否"
    else:
        expected = f"[{re.escape(_UNCHECKED)}]是[{re.escape(_CHECKED)}]否"
    if not re.search(expected, compact):
        _fail(f"{case}: native checkbox does not show the expected {'yes' if affirmative else 'no'}")
    return compact


def _locate_period_columns(
    rows: list[list[Any]],
    *,
    period_header: str,
    comparison_header: str | None = None,
    adjustment_row_index: int | None = None,
    adjustment_columns: Mapping[str, str] | None = None,
    case: str,
) -> dict[str, int]:
    header_row_index = None
    for row_index, row in enumerate(rows):
        compact = {_norm(value): index for index, value in enumerate(row) if value}
        if _norm(period_header) in compact:
            header_row_index = row_index
            break
    if header_row_index is None:
        _fail(f"{case}: period header {period_header!r} is missing")
    header = rows[header_row_index]
    columns: dict[str, int] = {period_header: next(i for i, value in enumerate(header) if _norm(value) == _norm(period_header))}
    if comparison_header is not None:
        if _norm(comparison_header) not in {_norm(v) for v in header if v}:
            _fail(f"{case}: comparison period header {comparison_header!r} is missing")
        columns[comparison_header] = next(i for i, value in enumerate(header) if _norm(value) == _norm(comparison_header))
    if adjustment_row_index is not None:
        if adjustment_columns is None or adjustment_row_index >= len(rows):
            _fail(f"{case}: adjustment header structure is unavailable")
        active_period: str | None = None
        period_by_column: dict[int, str] = {}
        for index, value in enumerate(rows[header_row_index]):
            compact = _norm(value)
            if re.fullmatch(r"\d{4}年末", compact):
                active_period = compact
            elif "比上年末增减" in compact:
                active_period = None
            if active_period:
                period_by_column[index] = active_period
        roles_row = rows[adjustment_row_index]
        for requested_key, period_role in adjustment_columns.items():
            matching = [
                index
                for index, role in enumerate(roles_row)
                if _norm(role) == _norm(period_role)
                and period_by_column.get(index) == _norm(requested_key.split("|")[0])
            ]
            if len(matching) != 1:
                _fail(f"{case}: could not locate unique {requested_key!r} adjustment column")
            columns[requested_key] = matching[0]
    return columns


def _table_quote(rows: list[list[Any]], row_index: int) -> str:
    return " | ".join(_display(value) for value in rows[row_index])


def _statement_headers(table: Any, rows: list[list[Any]], *, case: str) -> dict[str, Any]:
    _assert_table_header(
        rows,
        0,
        {1: "2026 年半年度"},
        case=f"{case}: period header",
    )
    _assert_table_header(
        rows,
        1,
        {1: "归属于母公司所有者权益", 14: "少数股东权益", 15: "所有者权益合计"},
        case=f"{case}: owner-group headers",
    )
    _assert_table_header(rows, 2, {13: "小计"}, case=f"{case}: parent subtotal header")
    group = _bbox_tuple(table.rows[1].cells[1])
    parent_subtotal = _bbox_tuple(table.rows[2].cells[13])
    minority = _bbox_tuple(table.rows[1].cells[14])
    consolidated_total = _bbox_tuple(table.rows[1].cells[15])
    if not all((group, parent_subtotal, minority, consolidated_total)):
        _fail(f"{case}: owner-group header cell geometry is incomplete")
    if not (
        math.isclose(group[2], parent_subtotal[2], abs_tol=0.02)
        and math.isclose(group[2], minority[0], abs_tol=0.02)
        and math.isclose(minority[2], consolidated_total[0], abs_tol=0.02)
    ):
        _fail(f"{case}: parent subtotal/minority/total columns do not match the native group geometry")
    return {
        "period_header": _source_cell(
            announcement_id="1225468007",
            page_number=65,
            table_index=0,
            table=table,
            rows=rows,
            row_index=0,
            column_index=1,
            cell_key="1225468007:p65:equity_changes:period_header",
            row_label="2026年半年度",
            column_role="statement_period",
            header_quote=_display(rows[0][1]),
        ),
        "parent_owner_group": {
            "header_quote": _display(rows[1][1]),
            "bbox_pdf_points": _bbox_dict(group),
        },
        "parent_owner_subtotal": {
            "header_quote": _display(rows[2][13]),
            "column_index": 13,
            "bbox_pdf_points": _bbox_dict(parent_subtotal),
        },
        "minority_equity": {
            "header_quote": _display(rows[1][14]),
            "column_index": 14,
            "bbox_pdf_points": _bbox_dict(minority),
        },
        "consolidated_equity_total": {
            "header_quote": _display(rows[1][15]),
            "column_index": 15,
            "bbox_pdf_points": _bbox_dict(consolidated_total),
        },
    }


def _get_table(page: Any, *, predicate: Callable[[list[list[Any]]], bool], case: str) -> tuple[int, Any, list[list[Any]]]:
    matches = []
    for table_index, table in enumerate(page.find_tables()):
        rows = table.extract()
        if predicate(rows):
            matches.append((table_index, table, rows))
    if len(matches) != 1:
        _fail(f"{case}: expected exactly one native table, found {len(matches)}")
    return matches[0]


def _current_balance_sheet(readers: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    pdf = readers["1225468007"]
    scope_page = pdf.pages[52]
    scope_text = _page_text(scope_page, label="1225468007:p53 balance-sheet scope")
    if "合并资产负债表" not in _norm(scope_text) or "单位：元" not in _norm(scope_text):
        _fail("1225468007:p53: consolidated balance-sheet title/unit scope is missing")

    page_number = 55
    page = pdf.pages[page_number - 1]
    text = _page_text(page, label="1225468007:p55 balance sheet")
    table_index, table, rows, row_index = _row_by_exact_label(
        page, "归属于母公司所有者权益合计", case="1225468007:p55 parent-equity row"
    )
    _assert_table_header(rows, 0, {0: "项目", 1: "期末余额", 2: "期初余额"}, case="1225468007:p55 balance-sheet columns")
    if not any(_norm(row[0]) == _norm("少数股东权益") for row in rows[row_index + 1 : row_index + 2]):
        _fail("1225468007:p55 parent-equity row is not followed by the distinct minority-equity row")
    if not any(_norm(row[0]) == _norm("所有者权益合计") for row in rows[row_index + 2 : row_index + 3]):
        _fail("1225468007:p55 minority-equity row is not followed by consolidated equity total")

    parent_current, parent_current_decimal = _amount_cell(
        announcement_id="1225468007",
        page_number=page_number,
        table_index=table_index,
        table=table,
        rows=rows,
        row_index=row_index,
        column_index=1,
        cell_key="1225468007:p55:consolidated_balance_sheet:parent_equity:期末余额",
        row_label="归属于母公司所有者权益合计",
        column_role="CURRENT_ENDING_BALANCE",
        header_quote="期末余额",
    )
    parent_opening, parent_opening_decimal = _amount_cell(
        announcement_id="1225468007",
        page_number=page_number,
        table_index=table_index,
        table=table,
        rows=rows,
        row_index=row_index,
        column_index=2,
        cell_key="1225468007:p55:consolidated_balance_sheet:parent_equity:期初余额",
        row_label="归属于母公司所有者权益合计",
        column_role="OPENING_BALANCE",
        header_quote="期初余额",
    )
    minority, minority_decimal = _amount_cell(
        announcement_id="1225468007",
        page_number=page_number,
        table_index=table_index,
        table=table,
        rows=rows,
        row_index=row_index + 1,
        column_index=1,
        cell_key="1225468007:p55:consolidated_balance_sheet:minority_equity:期末余额",
        row_label="少数股东权益",
        column_role="MINORITY_EQUITY_ENDING_BALANCE",
        header_quote="期末余额",
    )
    consolidated_total, consolidated_total_decimal = _amount_cell(
        announcement_id="1225468007",
        page_number=page_number,
        table_index=table_index,
        table=table,
        rows=rows,
        row_index=row_index + 2,
        column_index=1,
        cell_key="1225468007:p55:consolidated_balance_sheet:equity_total:期末余额",
        row_label="所有者权益合计",
        column_role="CONSOLIDATED_EQUITY_TOTAL_ENDING_BALANCE",
        header_quote="期末余额",
    )
    return (
        {
            "statement": "consolidated balance sheet",
            "scope_quote": {
                "text": next(line["text"] for line in _page_lines(scope_page) if "合并资产负债表" in line["text"]),
                "unit": "单位：元",
                "physical_page": 53,
            },
            "physical_page": page_number,
            "period_end": _TARGET_PERIOD_END,
            "column_header": {"current": "期末余额", "opening": "期初余额"},
            "native_unit": "元",
            "parent_current_cell": parent_current,
            "parent_opening_cell": parent_opening,
            "minority_cell": minority,
            "consolidated_total_cell": consolidated_total,
            "native_row_quote": _table_quote(rows, row_index),
            "page_native_text_contains_row": _norm(text).find(_norm("归属于母公司所有者权益合计")) >= 0,
        },
        {
            "parent_current": parent_current_decimal,
            "parent_opening": parent_opening_decimal,
            "minority": minority_decimal,
            "consolidated_total": consolidated_total_decimal,
        },
    )


def _current_summary_and_restatement(readers: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Decimal]]:
    pdf = readers["1225468007"]
    page_number = 9
    page = pdf.pages[page_number - 1]
    text = _page_text(page, label="1225468007:p9 summary")
    if _norm("公司是否需追溯调整或重述以前年度会计数据") not in _norm(text):
        _fail("1225468007:p9: prior-year-restatement question is missing")
    compact = _assert_checked_box(text, affirmative=False, case="1225468007:p9 prior-year-restatement answer")
    if "2026年半年度报告" not in _norm(text) or _ISSUER not in text:
        _fail("1225468007:p9: issuer/report identity quote is missing")

    table_index, table, rows = _get_table(
        page,
        predicate=lambda candidate_rows: any(
            _norm(row[0] if row else "") == _norm("归属于上市公司股东的净资产（元）")
            for row in candidate_rows
        )
        and any(
            _norm(value) == _norm("本报告期末") for row in candidate_rows for value in row if value
        )
        and any(_norm(value) == _norm("上年度末") for row in candidate_rows for value in row if value),
        case="1225468007:p9 period mapping table",
    )
    header_index = next(
        index
        for index, row in enumerate(rows)
        if _norm("本报告期末") in {_norm(value) for value in row if value}
        and _norm("上年度末") in {_norm(value) for value in row if value}
    )
    row_index = next(index for index, row in enumerate(rows) if _norm(row[0] if row else "") == _norm("归属于上市公司股东的净资产（元）"))
    header = rows[header_index]
    current_column = next(index for index, value in enumerate(header) if _norm(value) == _norm("本报告期末"))
    opening_column = next(index for index, value in enumerate(header) if _norm(value) == _norm("上年度末"))
    current_cell, current_decimal = _amount_cell(
        announcement_id="1225468007",
        page_number=page_number,
        table_index=table_index,
        table=table,
        rows=rows,
        row_index=row_index,
        column_index=current_column,
        cell_key="1225468007:p9:management_summary:parent_net_assets:本报告期末",
        row_label="归属于上市公司股东的净资产（元）",
        column_role="CURRENT_PERIOD_END",
        header_quote="本报告期末",
    )
    opening_cell, opening_decimal = _amount_cell(
        announcement_id="1225468007",
        page_number=page_number,
        table_index=table_index,
        table=table,
        rows=rows,
        row_index=row_index,
        column_index=opening_column,
        cell_key="1225468007:p9:management_summary:parent_net_assets:上年度末",
        row_label="归属于上市公司股东的净资产（元）",
        column_role="PRIOR_YEAR_END",
        header_quote="上年度末",
    )
    question_line = _find_line(
        page,
        lambda value: _norm("公司是否需追溯调整或重述以前年度会计数据") in value,
        label="1225468007:p9 prior-year-restatement answer line",
    )
    return (
        {
            "physical_page": page_number,
            "restatement_answer": "NO",
            "restatement_answer_quote": _quote_with_bbox(question_line),
            "restatement_checkbox_text": compact,
            "report_period_header": _source_cell(
                announcement_id="1225468007",
                page_number=page_number,
                table_index=table_index,
                table=table,
                rows=rows,
                row_index=header_index,
                column_index=current_column,
                cell_key="1225468007:p9:summary_header:本报告期末",
                row_label="本报告期末",
                column_role="reporting_period_end",
                header_quote=_display(header[current_column]),
            ),
            "prior_year_period_header": _source_cell(
                announcement_id="1225468007",
                page_number=page_number,
                table_index=table_index,
                table=table,
                rows=rows,
                row_index=header_index,
                column_index=opening_column,
                cell_key="1225468007:p9:summary_header:上年度末",
                row_label="上年度末",
                column_role="prior_year_end",
                header_quote=_display(header[opening_column]),
            ),
            "current_summary_cell": current_cell,
            "prior_year_summary_cell": opening_cell,
            "native_row_quote": _table_quote(rows, row_index),
        },
        {"current": current_decimal, "opening": opening_decimal},
    )


def _equity_movement(readers: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Decimal]]:
    pdf = readers["1225468007"]
    page_number = 65
    page = pdf.pages[page_number - 1]
    text = _page_text(page, label="1225468007:p65 equity changes")
    compact = _norm(text)
    for required in ("合并所有者权益变动表", "本期金额", "单位：元", "2026年半年度"):
        if _norm(required) not in compact:
            _fail(f"1225468007:p65: missing statement scope/header {required!r}")
    table_index, table, rows = _get_table(
        page,
        predicate=lambda candidate_rows: len(candidate_rows) > 10
        and len(candidate_rows[0]) >= 16
        and _norm(candidate_rows[0][1]) == _norm("2026年半年度")
        and _norm(candidate_rows[1][1]) == _norm("归属于母公司所有者权益"),
        case="1225468007:p65 parent-owner table",
    )
    headers = _statement_headers(table, rows, case="1225468007:p65 equity-change table")
    index, _, opening_rows, opening_row_index = _row_by_exact_label(page, "一、上年期末余额", case="1225468007:p65 prior-year ending row")
    if index != table_index:
        _fail("1225468007:p65: opening row resolved to a different table")
    _, _, _, adjustment_policy_index = _row_by_exact_label(page, "加：会计政策变更", case="1225468007:p65 policy adjustment row")
    _, _, _, adjustment_error_index = _row_by_exact_label(page, "前期差错更正", case="1225468007:p65 error correction row")
    _, _, _, adjustment_other_index = _row_by_exact_label(page, "其他", case="1225468007:p65 other adjustment row")
    _, _, _, opening_current_index = _row_by_exact_label(page, "二、本年期初余额", case="1225468007:p65 current-year opening row")
    _, _, _, movement_index = _find_table_row(
        page,
        label="三、本期增减变动金额",
        matches=lambda value: value.startswith(_norm("三、本期增减变动金额")),
        case="1225468007:p65 period-movement row",
    )
    for row_index in (adjustment_policy_index, adjustment_error_index, adjustment_other_index, opening_current_index, movement_index):
        if opening_rows[row_index][13:16] is None:
            _fail("1225468007:p65: parent/minority/total columns are incomplete")

    opening, opening_decimal = _amount_cell(
        announcement_id="1225468007",
        page_number=page_number,
        table_index=table_index,
        table=table,
        rows=opening_rows,
        row_index=opening_row_index,
        column_index=13,
        cell_key="1225468007:p65:equity_changes:parent小计:一、上年期末余额",
        row_label="一、上年期末余额",
        column_role="PARENT_OWNER_SUBTOTAL_OPENING",
        header_quote="归属于母公司所有者权益 > 小计",
    )
    current_year_opening, current_year_opening_decimal = _amount_cell(
        announcement_id="1225468007",
        page_number=page_number,
        table_index=table_index,
        table=table,
        rows=opening_rows,
        row_index=opening_current_index,
        column_index=13,
        cell_key="1225468007:p65:equity_changes:parent小计:二、本年期初余额",
        row_label="二、本年期初余额",
        column_role="PARENT_OWNER_SUBTOTAL_CURRENT_YEAR_OPENING",
        header_quote="归属于母公司所有者权益 > 小计",
    )
    policy_blank = _blank_cell(
        announcement_id="1225468007",
        page_number=page_number,
        table_index=table_index,
        table=table,
        rows=opening_rows,
        row_index=adjustment_policy_index,
        column_index=13,
        cell_key="1225468007:p65:equity_changes:parent小计:加会计政策变更",
        row_label="加：会计政策变更",
        column_role="PARENT_OWNER_SUBTOTAL_OPENING_ADJUSTMENT",
        header_quote="归属于母公司所有者权益 > 小计",
    )
    error_blank = _blank_cell(
        announcement_id="1225468007",
        page_number=page_number,
        table_index=table_index,
        table=table,
        rows=opening_rows,
        row_index=adjustment_error_index,
        column_index=13,
        cell_key="1225468007:p65:equity_changes:parent小计:前期差错更正",
        row_label="前期差错更正",
        column_role="PARENT_OWNER_SUBTOTAL_OPENING_ADJUSTMENT",
        header_quote="归属于母公司所有者权益 > 小计",
    )
    other_blank = _blank_cell(
        announcement_id="1225468007",
        page_number=page_number,
        table_index=table_index,
        table=table,
        rows=opening_rows,
        row_index=adjustment_other_index,
        column_index=13,
        cell_key="1225468007:p65:equity_changes:parent小计:其他",
        row_label="其他",
        column_role="PARENT_OWNER_SUBTOTAL_OPENING_ADJUSTMENT",
        header_quote="归属于母公司所有者权益 > 小计",
    )
    movement, movement_decimal = _amount_cell(
        announcement_id="1225468007",
        page_number=page_number,
        table_index=table_index,
        table=table,
        rows=opening_rows,
        row_index=movement_index,
        column_index=13,
        cell_key="1225468007:p65:equity_changes:parent小计:三、本期增减变动金额",
        row_label="三、本期增减变动金额",
        column_role="PARENT_OWNER_SUBTOTAL_PERIOD_MOVEMENT",
        header_quote="归属于母公司所有者权益 > 小计",
    )
    p67 = pdf.pages[66]
    _page_text(p67, label="1225468007:p67 equity ending")
    end_table_index, end_table, end_rows, ending_index = _row_by_exact_label(
        p67, "四、本期期末余额", case="1225468007:p67 period ending row"
    )
    if len(end_rows[ending_index]) < 16 or end_table_index != 0:
        _fail("1225468007:p67: ending row does not preserve the p65 statement column layout")
    ending, ending_decimal = _amount_cell(
        announcement_id="1225468007",
        page_number=67,
        table_index=end_table_index,
        table=end_table,
        rows=end_rows,
        row_index=ending_index,
        column_index=13,
        cell_key="1225468007:p67:equity_changes:parent小计:四、本期期末余额",
        row_label="四、本期期末余额",
        column_role="PARENT_OWNER_SUBTOTAL_CURRENT_PERIOD_ENDING",
        header_quote="继承 p65 的归属于母公司所有者权益 > 小计列；紧邻少数股东权益、所有者权益合计列",
    )
    ending_minority, ending_minority_decimal = _amount_cell(
        announcement_id="1225468007",
        page_number=67,
        table_index=end_table_index,
        table=end_table,
        rows=end_rows,
        row_index=ending_index,
        column_index=14,
        cell_key="1225468007:p67:equity_changes:minority股东权益:四、本期期末余额",
        row_label="四、本期期末余额",
        column_role="MINORITY_EQUITY_CURRENT_PERIOD_ENDING",
        header_quote="p65 header: 少数股东权益",
    )
    ending_total, ending_total_decimal = _amount_cell(
        announcement_id="1225468007",
        page_number=67,
        table_index=end_table_index,
        table=end_table,
        rows=end_rows,
        row_index=ending_index,
        column_index=15,
        cell_key="1225468007:p67:equity_changes:equity_total:四、本期期末余额",
        row_label="四、本期期末余额",
        column_role="CONSOLIDATED_EQUITY_TOTAL_CURRENT_PERIOD_ENDING",
        header_quote="p65 header: 所有者权益合计",
    )
    if opening_decimal != current_year_opening_decimal:
        _fail("1225468007:p65: prior-year end and current-year opening parent subtotals differ")
    if opening_decimal + movement_decimal != ending_decimal:
        _fail("1225468007:p65/p67: native parent-equity opening + movement does not equal ending")
    return (
        {
            "statement": "consolidated statement of changes in equity",
            "native_unit": "元",
            "physical_pages": [65, 67],
            "scope_quote": {
                "physical_page": 65,
                "quote": "7、合并所有者权益变动表；本期金额；单位：元；2026年半年度",
            },
            "column_identity": headers,
            "opening_parent_subtotal": opening,
            "opening_current_year_parent_subtotal": current_year_opening,
            "opening_adjustment_rows": [policy_blank, error_blank, other_blank],
            "parent_period_movement": movement,
            "ending_parent_subtotal": ending,
            "ending_minority_equity": ending_minority,
            "ending_consolidated_equity_total": ending_total,
            "native_opening_quote": _table_quote(opening_rows, opening_row_index),
            "native_movement_quote": _table_quote(opening_rows, movement_index),
            "native_ending_quote": _table_quote(end_rows, ending_index),
            "exact_decimal_bridge": {
                "opening_yuan": str(opening_decimal),
                "parent_equity_movement_yuan": str(movement_decimal),
                "ending_yuan": str(ending_decimal),
                "equality": True,
            },
            "blank_policy": "PRESERVE_LITERAL_BLANK; no zero coercion",
        },
        {
            "opening": opening_decimal,
            "current_year_opening": current_year_opening_decimal,
            "movement": movement_decimal,
            "ending": ending_decimal,
            "ending_minority": ending_minority_decimal,
            "ending_total": ending_total_decimal,
        },
    )


def _annual_opening_and_successive_comparators(readers: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Decimal]]:
    pdf = readers["1225183616"]
    page8 = pdf.pages[7]
    page8_text = _page_text(page8, label="1225183616:p8 restatement basis")
    compact8 = _assert_checked_box(page8_text, affirmative=True, case="1225183616:p8 annual restatement answer")
    if "同一控制下企业合并" not in _norm(page8_text):
        _fail("1225183616:p8: same-control restatement reason is missing")
    page_number = 9
    page = pdf.pages[page_number - 1]
    _page_text(page, label="1225183616:p9 annual comparative table")
    table_index, table, rows = _get_table(
        page,
        predicate=lambda candidate_rows: any(
            _norm(row[0] if row else "") == _norm("归属于上市公司股东的净资产（元）") for row in candidate_rows
        ),
        case="1225183616:p9 parent-net-assets comparison table",
    )
    row_index = next(
        index
        for index, row in enumerate(rows)
        if _norm(row[0] if row else "") == _norm("归属于上市公司股东的净资产（元）")
    )
    header_index = next(
        index
        for index, row in enumerate(rows)
        if _norm("2025年末") in {_norm(value) for value in row if value}
        and _norm("2024年末") in {_norm(value) for value in row if value}
        and _norm("2023年末") in {_norm(value) for value in row if value}
    )
    role_index = header_index + 1
    if role_index >= len(rows):
        _fail("1225183616:p9: adjusted-before/after role row is missing")
    columns = _locate_period_columns(
        rows,
        period_header="2025年末",
        adjustment_row_index=role_index,
        adjustment_columns={
            "2024年末|adjusted_before": "调整前",
            "2024年末|adjusted_after": "调整后",
            "2023年末|adjusted_before": "调整前",
            "2023年末|latest_adjusted_comparator": "调整后",
        },
        case="1225183616:p9 annual comparison header map",
    )
    annual_values: dict[str, Decimal] = {}
    cell_specs = [
        ("2025-12-31", "2025年末", columns["2025年末"], "current_year_end"),
        ("2024-12-31-adjusted-before", "2024年末调整前", columns["2024年末|adjusted_before"], "adjusted_before"),
        ("2024-12-31-adjusted-after", "2024年末调整后", columns["2024年末|adjusted_after"], "adjusted_after"),
        ("2023-12-31-adjusted-before", "2023年末调整前", columns["2023年末|adjusted_before"], "adjusted_before"),
        ("2023-12-31-latest-adjusted-comparator", "2023年末调整后", columns["2023年末|latest_adjusted_comparator"], "latest_adjusted_comparator"),
    ]
    annual_cells: dict[str, Any] = {}
    for stable_key, header_quote, column_index, role in cell_specs:
        cell, decimal_value = _amount_cell(
            announcement_id="1225183616",
            page_number=page_number,
            table_index=table_index,
            table=table,
            rows=rows,
            row_index=row_index,
            column_index=column_index,
            cell_key=f"1225183616:p9:parent_equity:{stable_key}",
            row_label="归属于上市公司股东的净资产（元）",
            column_role=role,
            header_quote=header_quote,
        )
        annual_values[stable_key] = decimal_value
        annual_cells[stable_key] = cell
    restatement_line = _find_line(
        page8,
        lambda value: _norm("公司是否需追溯调整或重述以前年度会计数据") in value,
        label="1225183616:p8 annual restatement answer line",
    )
    return (
        {
            "annual_opening_anchor": annual_cells["2025-12-31"],
            "restatement_answer": "YES_SAME_CONTROL_MERGER",
            "restatement_answer_quote": _quote_with_bbox(restatement_line),
            "restatement_checkbox_text": compact8,
            "same_control_reason_quote": "同一控制下企业合并",
            "comparison_cells": {
                "2024-12-31": {
                    "adjusted_before": annual_cells["2024-12-31-adjusted-before"],
                    "adjusted_after": annual_cells["2024-12-31-adjusted-after"],
                },
                "2023-12-31": {
                    "adjusted_before": annual_cells["2023-12-31-adjusted-before"],
                    "latest_adjusted_comparator": annual_cells["2023-12-31-latest-adjusted-comparator"],
                    "state_note": "Distinct successive comparative states; do not collapse or infer a delta between them.",
                },
            },
            "native_row_quote": _table_quote(rows, row_index),
            "period_header_quote": "2025年末；2024年末调整前/调整后；2023年末调整前/调整后",
        },
        annual_values,
    )


def _cause_2023(readers: Mapping[str, Any]) -> dict[str, Any]:
    board = readers["1222991666"]
    board_p1 = board.pages[0]
    board_text = _page_text(board_p1, label="1222991666:p1 cause description")
    compact = _norm(board_text)
    for required in (_ISSUER, "新疆驰源", "同一控制下企业合并", "2024年5月24日"):
        if _norm(required) not in compact:
            _fail(f"1222991666:p1: same-control cause quote lacks {required!r}")
    period_line = _find_line(
        board_p1,
        lambda value: "2023年12月31日" in value and "合并资产负债表项目" in value,
        label="1222991666:p1 FY2023 balance-sheet scope",
    )
    board_values = _native_amount_row(
        board.pages[2],
        label="归属于母公司所有者权益合计",
        label_parts=("归属于母公司所有者权益合计",),
        case="1222991666:p3 FY2023 board table",
        header_quote="项目 / 重塑前 / 同控合并影响 / 重塑后；单位：元",
        row_period="2023-12-31",
    )
    board_values["physical_page"] = 3
    auditor = readers["1222991641"]
    auditor_p4 = auditor.pages[3]
    auditor_text = _page_text(auditor_p4, label="1222991641:p4 cause description")
    for required in (_ISSUER, "新疆驰源", "同一控制下企业合并"):
        if _norm(required) not in _norm(auditor_text):
            _fail(f"1222991641:p4: auditor companion cause quote lacks {required!r}")
    auditor_scope_line = _find_line(
        auditor.pages[4],
        lambda value: "2023年12月31日" in value and "合并资产负债表" in value,
        label="1222991641:p5 FY2023 balance-sheet scope",
    )
    auditor_values = _native_amount_row(
        auditor.pages[5],
        label="归属于母公司所有者权益合计",
        label_parts=("归属于母公司所有者权益",),
        case="1222991641:p6 FY2023 auditor table",
        header_quote="项目 / 重塑前 / 同控合并影响 / 重塑后；单位：元",
        row_period="2023-12-31",
    )
    auditor_values["physical_page"] = 6
    board_decimals = board_values.pop("_decimals")
    auditor_decimals = auditor_values.pop("_decimals")
    if board_decimals != auditor_decimals:
        _fail("FY2023 board and auditor parent-equity rows disagree")
    return {
        "cause_key": "xinjiang_chiyuan_fy2023",
        "event": "新疆驰源环保科技有限公司 same-control acquisition",
        "source_ids": ["1222991641", "1222991666"],
        "period_scope": "2023-12-31 / FY2023",
        "native_unit": "元",
        "board_notice": {
            "announcement_id": "1222991666",
            "cause_quote": _line_window(
                board_p1,
                lambda value: "新疆驰源为控股股东控制的全资子公司" in value,
                label="1222991666:p1 same-control paragraph",
                after=2,
            ),
            "period_scope_quote": _quote_with_bbox(period_line),
            "parent_equity_row": board_values,
        },
        "auditor_companion": {
            "announcement_id": "1222991641",
            "cause_quote": _line_window(
                auditor_p4,
                lambda value: "新疆驰源为控股股东控制的全资子公司" in value,
                label="1222991641:p4 same-control paragraph",
                after=2,
            ),
            "period_scope_quote": _quote_with_bbox(auditor_scope_line),
            "parent_equity_row": auditor_values,
            "native_body_used": True,
        },
        "independent_native_rows_match": True,
        "annual_chain_reference": {
            "announcement_id": "1225183616",
            "page": 9,
            "role": "FY2023 adjusted-before comparator matches this direct notice after-state; later comparator remains separate",
        },
    }


def _cause_2024(readers: Mapping[str, Any], annual: Mapping[str, Any], annual_values: Mapping[str, Decimal]) -> dict[str, Any]:
    board = readers["1225183652"]
    page1 = board.pages[0]
    text1 = _page_text(page1, label="1225183652:p1 cause description")
    compact1 = _norm(text1)
    for required in ("证券代码：000422", "证券简称：湖北宜化", "公告编号：2026-042", "宜昌新发产业投资有限公司", "同一控制下企业合并"):
        if _norm(required) not in compact1:
            _fail(f"1225183652:p1: issuer/cause identity quote lacks {required!r}")
    quote = _line_window(
        page1,
        lambda value: "2024年11月24日" in value and "2025年5月14日" in value,
        label="1225183652:p1 acquisition transaction quote",
        after=5,
    )
    page_number = 4
    page4 = board.pages[page_number - 1]
    page4_text = _page_text(page4, label="1225183652:p4 FY2024 parent-equity adjustment")
    unit_page_number = next(
        (
            number
            for number in (2, 3, 4)
            if "单位：人民币元" in _norm(_page_text(board.pages[number - 1], label=f"1225183652:p{number} unit scope"))
        ),
        None,
    )
    if unit_page_number is None:
        _fail("1225183652:p4: native adjustment table unit is missing")
    unit_page = board.pages[unit_page_number - 1]
    unit_line = _find_line(unit_page, lambda value: "单位：人民币元" in value, label=f"1225183652:p{unit_page_number} unit line")
    period_header = "2024年12月31日余额（追溯调整前）"
    after_header = "2024年12月31日余额（追溯调整后）"
    table_index, table, rows = _get_table(
        page4,
        predicate=lambda candidate_rows: any(
            len(row) >= 2 and _norm(row[0]) == _norm(period_header) and _norm(row[1]) == _norm(after_header)
            for row in candidate_rows
        ),
        case="1225183652:p4 FY2024 restatement table",
    )
    label_first = _find_line(page4, lambda value: value == _norm("归属于母公司所有者"), label="1225183652:p4 split row-label first line")
    label_second = _find_line(page4, lambda value: value == _norm("权益合计"), label="1225183652:p4 split row-label second line")
    label_union = _line_bbox_union([label_first, label_second])
    if label_union is None:
        _fail("1225183652:p4: parent-equity row label has no native geometry")
    row_candidates = []
    for row_index, row in enumerate(rows):
        if len(row) < 2 or row_index >= len(table.rows):
            continue
        candidate_bbox = _bbox_tuple(table.rows[row_index].cells[0])
        if candidate_bbox is None:
            continue
        aligned_to_parent_label = (
            candidate_bbox[1] <= label_union["top"] + 0.02
            and candidate_bbox[3] >= label_union["bottom"] - 0.02
        )
        if aligned_to_parent_label:
            _money(row[0], label="1225183652:p4 candidate adjusted-before cell")
            _money(row[1], label="1225183652:p4 candidate adjusted-after cell")
            row_candidates.append((row_index, row))
    if len(row_candidates) != 1:
        _fail(f"1225183652:p4: expected one two-period parent-equity value row, found {len(row_candidates)}")
    row_index, row = row_candidates[0]
    row_bbox = _bbox_tuple(table.rows[row_index].cells[0])
    if row_bbox is None or not (row_bbox[1] <= label_union["bottom"] and row_bbox[3] >= label_union["top"]):
        _fail("1225183652:p4: parent-equity label does not align to the before/after amount row")
    before_cell, before_decimal = _amount_cell(
        announcement_id="1225183652",
        page_number=page_number,
        table_index=table_index,
        table=table,
        rows=rows,
        row_index=row_index,
        column_index=0,
        cell_key="1225183652:p4:restatement_table:parent_equity:2024-12-31:adjusted_before",
        row_label="归属于母公司所有者权益合计",
        column_role="ADJUSTED_BEFORE",
        header_quote=period_header,
    )
    after_cell, after_decimal = _amount_cell(
        announcement_id="1225183652",
        page_number=page_number,
        table_index=table_index,
        table=table,
        rows=rows,
        row_index=row_index,
        column_index=1,
        cell_key="1225183652:p4:restatement_table:parent_equity:2024-12-31:adjusted_after",
        row_label="归属于母公司所有者权益合计",
        column_role="ADJUSTED_AFTER",
        header_quote=after_header,
    )
    if before_decimal != annual_values["2024-12-31-adjusted-before"] or after_decimal != annual_values["2024-12-31-adjusted-after"]:
        _fail("FY2024 board parent-equity before/after cells do not match the annual adjusted comparatives")

    annual_pdf = readers["1225183616"]
    note_page = annual_pdf.pages[219]
    note_text = _page_text(note_page, label="1225183616:p220 same-control acquisition note")
    note_compact = _norm(note_text)
    required_note = "根据上述议案决议，公司与控股股东湖北宜化集团有限责任公司签订了股权转让协议"
    restatement_note = "根据同一控制下企业合并的相关原则，对期初数进行追溯调整"
    if _norm(required_note) not in note_compact or _norm(restatement_note) not in note_compact:
        _fail("1225183616:p220: native narrative does not bind the merger to retrospective opening adjustment")
    note_quote = _find_line(
        note_page,
        lambda value: "同一控制下企业合并的相关原则" in value and "期初数进行追溯调整" in value,
        label="1225183616:p220 retrospective-opening quote",
    )
    table_index, table, rows = _get_table(
        note_page,
        predicate=lambda candidate_rows: any(
            len(row) >= 9
            and _norm(row[0]) == _norm("被合并方名称")
            and _norm(row[1]) == _norm("企业合并中取得的权益比例")
            for row in candidate_rows
        )
        and any(any(_norm(value).startswith(_norm("宜昌新发产业投资有限公司")) for value in row if value) for row in candidate_rows),
        case="1225183616:p220 same-control acquisition table",
    )
    merger_row_index = next(
        index
        for index, row in enumerate(rows)
        if row and _norm(row[0]).startswith(_norm("宜昌新发产业投资有限公司"))
    )
    merger_row = rows[merger_row_index]
    if _norm(merger_row[1]) != _norm("100.00%") or "并非暂时性" not in _norm(merger_row[2]) or "2025年05月30日" not in _norm(merger_row[3]):
        _fail("1225183616:p220: merger table does not show 100% ownership, non-transitory common control, and date")
    table_cells = {
        key: _source_cell(
            announcement_id="1225183616",
            page_number=220,
            table_index=table_index,
            table=table,
            rows=rows,
            row_index=merger_row_index,
            column_index=column_index,
            cell_key=f"1225183616:p220:merger_note:{key}",
            row_label="宜昌新发产业投资有限公司",
            column_role=key,
            header_quote=_display(rows[0][column_index]),
        )
        for key, column_index in (
            ("acquired_entity", 0),
            ("ownership_percentage", 1),
            ("common_control_basis", 2),
            ("merger_date", 3),
        )
    }
    opaque_cpa = {
        "announcement_id": "1225183650",
        "native_body_used": False,
        "identity_role": "exact source identity only; native text is opaque and contributes no scope or amount",
    }
    return {
        "cause_key": "yichang_xinfa_fy2024",
        "event": "宜昌新发产业投资有限公司 same-control acquisition",
        "source_ids": ["1225183650", "1225183652"],
        "period_scope": "2024-12-31 / FY2024 comparative balance-sheet scope",
        "native_unit": "人民币元",
        "unit_quote": {"physical_page": unit_page_number, **_quote_with_bbox(unit_line)},
        "board_notice": {
            "announcement_id": "1225183652",
            "cause_quote": quote,
            "parent_equity_row": {
                "physical_page": page_number,
                "row_label_quote": [_quote_with_bbox(label_first), _quote_with_bbox(label_second)],
                "row_bbox_pdf_points": _bbox_dict(row_bbox),
                "period_before_header": period_header,
                "period_after_header": after_header,
                "adjusted_before_cell": before_cell,
                "adjusted_after_cell": after_cell,
            },
        },
        "auditor_companion": opaque_cpa,
        "annual_note": {
            "announcement_id": "1225183616",
            "physical_page": 220,
            "retrospective_opening_quote": _quote_with_bbox(note_quote),
            "merger_cells": table_cells,
            "common_control_scope": "同受湖北宜化集团有限责任公司控制且该控制并非暂时性的",
            "merger_date": "2025年05月30日",
        },
        "annual_comparative_crosscheck": {
            "fy24_adjusted_before_yuan": str(annual_values["2024-12-31-adjusted-before"]),
            "fy24_adjusted_after_yuan": str(annual_values["2024-12-31-adjusted-after"]),
            "matches_direct_board_notice": True,
        },
    }


def extract_canonical_current_evidence(
    source_pdfs: Mapping[str, bytes],
    *,
    instrument_id: str,
    target_period_end: str,
) -> dict[str, Any]:
    """Extract a bounded 000422 H126 native canonical-current proof.

    ``source_pdfs`` must map exactly the six announcement IDs documented in
    ``_EXPECTED_SOURCE_IDS`` to original PDF bytes.  Receipt, index-closure,
    archival-availability and formal-admission checks remain the caller's job.
    """

    if instrument_id != _TARGET_INSTRUMENT:
        _fail(f"unsupported instrument_id {instrument_id!r}")
    if target_period_end != _TARGET_PERIOD_END:
        _fail(f"unsupported target_period_end {target_period_end!r}")
    if not isinstance(source_pdfs, Mapping):
        _fail("source_pdfs must be a mapping from announcement ID to original bytes")
    identity, readers = _verify_identity(source_pdfs)
    try:
        balance_sheet, balance_values = _current_balance_sheet(readers)
        summary, summary_values = _current_summary_and_restatement(readers)
        movement, movement_values = _equity_movement(readers)
        annual, annual_values = _annual_opening_and_successive_comparators(readers)
        cause_2023 = _cause_2023(readers)
        cause_2024 = _cause_2024(readers, annual, annual_values)

        if balance_values["parent_current"] != summary_values["current"]:
            _fail("H126 p9 summary current value does not equal p55 balance-sheet parent-equity cell")
        if balance_values["parent_opening"] != summary_values["opening"]:
            _fail("H126 p9 prior-year summary value does not equal p55 balance-sheet opening cell")
        if balance_values["parent_current"] != movement_values["ending"]:
            _fail("H126 p55 balance-sheet parent-equity current value does not equal p67 ending subtotal")
        if annual_values["2025-12-31"] != movement_values["opening"]:
            _fail("FY25 annual current parent-equity does not equal the H126 opening subtotal")
        if annual_values["2023-12-31-adjusted-before"] != Decimal(cause_2023["board_notice"]["parent_equity_row"]["native_values"]["after"]["value_yuan"]):
            _fail("FY23 direct notice after-state does not match the distinct FY25 annual adjusted-before comparator")
    finally:
        for pdf in readers.values():
            pdf.close()

    return {
        "schema": "aquant.yihua.h126-canonical-current-evidence.v1",
        "candidate_disposition": "CANONICAL_CURRENT_AFTER_REVISION",
        "formal_admission": "NOT_ADMITTED",
        "pit_eligible": False,
        "target": {
            "instrument_id": _TARGET_INSTRUMENT,
            "security_code": "000422",
            "issuer": _ISSUER,
            "metric": "parent_equity",
            "period_end": _TARGET_PERIOD_END,
            "statement": "consolidated balance sheet",
            "row": "归属于母公司所有者权益合计",
            "column_role": "CURRENT",
            "native_unit": "元",
            "unit": "CNY",
        },
        "values": {
            "parent_equity": {
                "value_yuan": str(balance_values["parent_current"]),
                "unit": "CNY",
                "period_end": _TARGET_PERIOD_END,
                "role": "CURRENT",
                "source_cell_key": balance_sheet["parent_current_cell"]["cell_key"],
                "source_announcement_id": "1225468007",
                "physical_page": 55,
            }
        },
        "source_identity": identity,
        "current_native_proof": {
            "p9_period_and_restatement": summary,
            "p55_balance_sheet": balance_sheet,
            "p65_p67_equity_movement": movement,
            "annual_opening_anchor": annual["annual_opening_anchor"],
            "annual_restatement_and_comparators": annual,
        },
        "cause_document_ids": ["1222991641", "1222991666", "1225183650", "1225183652"],
        "all_cause_evidence": [cause_2023, cause_2024],
        "source_value_relationships": {
            "h126_p9_current_equals_p55_parent_current": True,
            "h126_p9_prior_year_end_equals_p55_parent_opening": True,
            "h126_opening_plus_parent_movement_equals_ending": True,
            "fy25_annual_current_equals_h126_opening": True,
            "fy23_board_after_equals_fy25_annual_adjusted_before": True,
            "fy23_annual_adjusted_before_and_latest_adjusted_comparator_preserved_separately": True,
            "fy24_board_before_after_match_fy25_annual_comparatives": True,
        },
        "classification_semantics": {
            "classification": "CANONICAL_CURRENT_AFTER_REVISION",
            "reason": "Exact H126 current parent-equity cell is supported by the report's native opening-to-ending parent subtotal bridge after the documented historical revision chain.",
            "not_asserted": [
                "NO_EFFECT for any historical same-control acquisition",
                "an exact before/after delta for the 2026-06-30 current cell",
                "numeric zero for literal-blank H126 opening adjustment cells",
                "a delta between the FY23 annual adjusted-before and latest adjusted comparator states",
                "formal admission or historical point-in-time availability",
            ],
        },
    }


__all__ = ["YihuaCanonicalProofError", "extract_canonical_current_evidence"]
