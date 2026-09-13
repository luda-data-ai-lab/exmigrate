"""Per-column formula inventory produced by the analyzer's formula pass."""

from __future__ import annotations

from pydantic import BaseModel, Field


class FormulaColumn(BaseModel):
    """Formula facts for one worksheet column."""

    file: str
    sheet: str
    table: str
    column: str
    column_index: int | None = None
    formula_cells: int
    row_count: int
    derived: bool
    functions: dict[str, int] = Field(default_factory=dict)
    references: list[str] = Field(default_factory=list)
    sample: str


class FormulaInventory(BaseModel):
    """All formula columns found across the analyzed workbooks."""

    columns: list[FormulaColumn] = Field(default_factory=list)
    books: dict[str, list[str]] = Field(default_factory=dict)
    """Per workbook, the external link targets in ``[1]``, ``[2]``… order."""

    def by_file(self) -> dict[str, list[FormulaColumn]]:
        """Group columns by source workbook, preserving order."""
        out: dict[str, list[FormulaColumn]] = {}
        for col in self.columns:
            out.setdefault(col.file, []).append(col)
        return out

    def function_totals(self, file: str | None = None) -> dict[str, int]:
        """Function name → call count, optionally restricted to one workbook."""
        totals: dict[str, int] = {}
        for col in self.columns:
            if file is not None and col.file != file:
                continue
            for name, count in col.functions.items():
                totals[name] = totals.get(name, 0) + count
        return dict(sorted(totals.items(), key=lambda kv: (-kv[1], kv[0])))
