"""Workbook reading and Schema IR construction.

Pass 1 (``data_only=True``) yields values, types and tables; pass 2
(``data_only=False``) marks formula-derived columns and collects same-file
lookup evidence, which feeds PK/FK inference.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from exmigrate.analyzer.formulas import SheetFormulas, scan_formulas
from exmigrate.analyzer.keys import (
    LookupEvidence,
    infer_foreign_keys,
    infer_primary_keys,
    table_by_sheet,
)
from exmigrate.analyzer.naming import dedupe, to_identifier
from exmigrate.analyzer.types import infer_column
from exmigrate.contracts.adapter import Issue, IssueSeverity
from exmigrate.contracts.ir import ColumnIR, SchemaIR, TableIR

HEADER_MIN_STRING_RATIO = 0.8
HEADER_SCAN_ROWS = 50


@dataclass
class AnalysisResult:
    """Schema IR plus the raw sheet data it was derived from."""

    ir: SchemaIR
    frames: list[pd.DataFrame] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    def table_data(self) -> dict[str, pd.DataFrame]:
        """Bind frames to the (possibly edited) IR for adapters."""
        return bind_data(self.ir, self.frames)


def analyze(paths: Sequence[str | Path]) -> tuple[SchemaIR, list[Issue]]:
    """Analyze workbooks and return the Schema IR and any issues found."""
    result = analyze_with_data(paths)
    return result.ir, result.issues


def analyze_with_data(paths: Sequence[str | Path], *, infer_keys: bool = True) -> AnalysisResult:
    """Analyze workbooks and additionally return the sheet data per table.

    With ``infer_keys`` the formula pass runs and PK/FK candidates are filled in.
    """
    tables: list[TableIR] = []
    frames: list[pd.DataFrame] = []
    issues: list[Issue] = []
    raw_names: list[str] = []
    pending: list[tuple[TableIR, pd.DataFrame]] = []
    workbooks: list[Path] = []

    for path in paths:
        path = Path(path)
        if path.suffix.lower() not in {".xlsx", ".xlsm"}:
            issues.append(
                Issue(
                    severity=IssueSeverity.ERROR,
                    code="unsupported_file",
                    message=f"{path.name}: only .xlsx/.xlsm workbooks are supported",
                )
            )
            continue
        workbooks.append(path)
        wb = load_workbook(path, data_only=True, read_only=True)
        try:
            for ws in wb.worksheets:
                table, frame, sheet_issues = _analyze_sheet(ws, path.name)
                issues.extend(sheet_issues)
                if table is None or frame is None:
                    continue
                raw_names.append(table.name)
                pending.append((table, frame))
        finally:
            wb.close()

    for name, (table, frame) in zip(dedupe(raw_names), pending, strict=True):
        table.name = name
        tables.append(table)
        frames.append(frame)

    ir = SchemaIR(version=1, tables=tables)
    if infer_keys:
        lookups = _apply_formulas(ir, frames, workbooks, issues)
        infer_primary_keys(ir, frames)
        infer_foreign_keys(ir, frames, lookups)
    return AnalysisResult(ir=ir, frames=frames, issues=issues)


def _apply_formulas(
    ir: SchemaIR, frames: Sequence[pd.DataFrame], workbooks: Sequence[Path], issues: list[Issue]
) -> list[LookupEvidence]:
    """Run the formula pass, flag derived columns and translate lookups to IR names."""
    evidence: list[LookupEvidence] = []
    for path in workbooks:
        header_rows = {
            t.source_sheet: t.header_row for t in ir.tables if t.source_file == path.name
        }
        facts = scan_formulas(path, header_rows)
        for sheet, sheet_facts in facts.items():
            table = table_by_sheet(ir, path.name, sheet)
            if table is None:
                continue
            _mark_derived(table, sheet_facts, frames[ir.tables.index(table)], issues)
            evidence.extend(_lookup_evidence(ir, path.name, table, sheet_facts))
    return evidence


def _mark_derived(
    table: TableIR, facts: SheetFormulas, frame: pd.DataFrame, issues: list[Issue]
) -> None:
    for col_idx in facts.derived_columns(table.row_count):
        if col_idx >= len(table.columns):
            continue
        column = table.columns[col_idx]
        column.derived = True
        if len(frame) and column.null_ratio >= 1.0:
            issues.append(
                Issue(
                    severity=IssueSeverity.INFO,
                    code="formula_cache_empty",
                    message=(
                        f"{table.source_file}/{table.source_sheet}: column '{column.name}' "
                        "holds formulas without cached values; open and save the workbook "
                        "in Excel to populate them"
                    ),
                    table=table.name,
                    column=column.name,
                )
            )


def _lookup_evidence(
    ir: SchemaIR, file_name: str, table: TableIR, facts: SheetFormulas
) -> list[LookupEvidence]:
    out: list[LookupEvidence] = []
    for edge in facts.lookups:
        target = table_by_sheet(ir, file_name, edge.target.sheet)
        if target is None:
            continue
        if edge.lookup_column >= len(table.columns) or edge.target.column >= len(target.columns):
            continue
        out.append(
            LookupEvidence(
                table=table.name,
                column=table.columns[edge.lookup_column].name,
                ref_table=target.name,
                ref_column=target.columns[edge.target.column].name,
            )
        )
    return out


def bind_data(ir: SchemaIR, frames: Sequence[pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Pair ``frames`` (one per IR table, in order) with IR names.

    Columns are matched positionally, so renaming tables or columns in the IR
    keeps working; the column count must match the frame width.
    """
    if len(frames) != len(ir.tables):
        raise ValueError(f"expected {len(ir.tables)} frames, got {len(frames)}")
    out: dict[str, pd.DataFrame] = {}
    for table, frame in zip(ir.tables, frames, strict=True):
        if len(table.columns) != len(frame.columns):
            raise ValueError(
                f"table '{table.name}': IR has {len(table.columns)} columns "
                f"but data has {len(frame.columns)}"
            )
        bound = frame.copy()
        bound.columns = [c.name for c in table.columns]
        out[table.name] = bound
    return out


def _analyze_sheet(
    ws: Worksheet, file_name: str
) -> tuple[TableIR | None, pd.DataFrame | None, list[Issue]]:
    issues: list[Issue] = []
    rows = list(ws.iter_rows(values_only=True))
    header_idx = _find_header_row(rows)
    if header_idx is None:
        issues.append(
            Issue(
                severity=IssueSeverity.WARNING,
                code="no_header",
                message=f"{file_name}/{ws.title}: no header row found; sheet skipped",
            )
        )
        return None, None, issues

    header_raw = rows[header_idx]
    width = len(header_raw)
    source_names = [str(h) if h is not None else "" for h in header_raw]
    body = [tuple(list(r) + [None] * (width - len(r)))[:width] for r in rows[header_idx + 1 :]]
    frame = pd.DataFrame(body, columns=source_names) if body else pd.DataFrame(columns=source_names)
    frame = _drop_empty_rows(frame)

    col_names = dedupe(
        to_identifier(name, fallback=f"col_{i + 1}") for i, name in enumerate(source_names)
    )
    columns: list[ColumnIR] = []
    for i, (source, name) in enumerate(zip(source_names, col_names, strict=True)):
        stats = infer_column(frame.iloc[:, i])
        if not source:
            issues.append(
                Issue(
                    severity=IssueSeverity.WARNING,
                    code="blank_header",
                    message=f"{file_name}/{ws.title}: blank header at column {i + 1}",
                    table=ws.title,
                    column=name,
                )
            )
        columns.append(
            ColumnIR(
                name=name,
                source_name=source,
                type=stats.type,
                nullable=stats.nullable,
                null_ratio=stats.null_ratio,
                max_length=stats.max_length,
            )
        )

    table = TableIR(
        name=to_identifier(ws.title, fallback="sheet"),
        source_file=file_name,
        source_sheet=ws.title,
        row_count=len(frame),
        header_row=header_idx + 1,
        columns=columns,
    )
    return table, frame, issues


def _find_header_row(rows: Sequence[tuple[object, ...]]) -> int | None:
    """Return the index of the first row where >=80% of cells are non-null strings."""
    for idx, row in enumerate(rows[:HEADER_SCAN_ROWS]):
        cells = [c for c in row if c is not None and not (isinstance(c, str) and not c.strip())]
        if not cells:
            continue
        strings = sum(1 for c in cells if isinstance(c, str))
        if strings / len(cells) >= HEADER_MIN_STRING_RATIO and len(cells) >= len(row) * 0.5:
            return idx
    return None


def _drop_empty_rows(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(frame.reset_index(drop=True))
    mask = frame.map(lambda v: v is not None and not (isinstance(v, str) and not v.strip()))
    kept = frame.loc[mask.any(axis=1)]
    return pd.DataFrame(kept.reset_index(drop=True))
