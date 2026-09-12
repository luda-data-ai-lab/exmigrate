"""Second read pass (``data_only=False``): formula strings, same-file scope only.

Recognised formula families: plain references (``=A2``, ``=Sheet2!B3``),
arithmetic over them, and ``VLOOKUP`` / ``XLOOKUP`` whose table array lives in
another sheet of the same workbook. Anything else is treated as an opaque
formula: the column still counts as derived, but yields no key evidence.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils.cell import column_index_from_string

DERIVED_MIN_RATIO = 0.5

_CELL_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d+)$")
_RANGE_RE = re.compile(r"^\$?([A-Za-z]{1,3})(?:\$?\d+)?(?::\$?([A-Za-z]{1,3})(?:\$?\d+)?)?$")
_FUNC_RE = re.compile(r"(VLOOKUP|XLOOKUP)\s*\(", re.IGNORECASE)


@dataclass(frozen=True)
class SheetRef:
    """A (sheet, zero-based column) location inside the workbook."""

    sheet: str
    column: int


@dataclass(frozen=True)
class LookupEdge:
    """``lookup_column`` (in ``sheet``) is looked up against ``target``."""

    sheet: str
    lookup_column: int
    target: SheetRef
    function: str


@dataclass
class SheetFormulas:
    """Formula facts for one worksheet, keyed by zero-based column."""

    title: str
    formula_counts: dict[int, int] = field(default_factory=dict)
    samples: dict[int, str] = field(default_factory=dict)
    lookups: list[LookupEdge] = field(default_factory=list)
    references: dict[int, set[SheetRef]] = field(default_factory=dict)

    def derived_columns(self, row_count: int) -> set[int]:
        """Columns where at least ``DERIVED_MIN_RATIO`` of rows hold a formula."""
        if row_count <= 0:
            return set()
        return {
            col
            for col, count in self.formula_counts.items()
            if count / row_count >= DERIVED_MIN_RATIO
        }


def scan_formulas(path: str | Path, header_rows: dict[str, int]) -> dict[str, SheetFormulas]:
    """Collect formula facts per sheet.

    ``header_rows`` maps sheet title → 1-based header row; rows at or above the
    header are ignored. Sheets missing from the mapping are skipped.
    """
    wb = load_workbook(path, data_only=False, read_only=True)
    out: dict[str, SheetFormulas] = {}
    try:
        for ws in wb.worksheets:
            header = header_rows.get(ws.title)
            if header is None:
                continue
            facts = SheetFormulas(title=ws.title)
            for row in ws.iter_rows(min_row=header + 1, values_only=True):
                for col, value in enumerate(row):
                    if not isinstance(value, str) or not value.startswith("="):
                        continue
                    facts.formula_counts[col] = facts.formula_counts.get(col, 0) + 1
                    if col not in facts.samples:
                        facts.samples[col] = value
                        _extract(value, ws.title, col, facts)
            out[ws.title] = facts
    finally:
        wb.close()
    return out


def _extract(formula: str, sheet: str, col: int, facts: SheetFormulas) -> None:
    for match in _FUNC_RE.finditer(formula):
        args = _split_args(formula, match.end())
        edge = _lookup_edge(match.group(1).upper(), args, sheet)
        if edge is not None and edge not in facts.lookups:
            facts.lookups.append(edge)
    refs = {r for r in _sheet_refs(formula, sheet) if r.sheet != sheet}
    if refs:
        facts.references.setdefault(col, set()).update(refs)


def _lookup_edge(func: str, args: Sequence[str], sheet: str) -> LookupEdge | None:
    if len(args) < 2:
        return None
    lookup = _cell_column(args[0], sheet)
    if lookup is None or lookup.sheet != sheet:
        return None
    array_arg = args[1]
    target = _range_first_column(array_arg, sheet)
    if target is None or target.sheet == sheet:
        return None
    return LookupEdge(sheet=sheet, lookup_column=lookup.column, target=target, function=func)


def _split_args(formula: str, start: int) -> list[str]:
    """Split the argument list of a call whose ``(`` ends at ``start``."""
    args: list[str] = []
    depth = 0
    in_str = False
    buf: list[str] = []
    for ch in formula[start:]:
        if in_str:
            buf.append(ch)
            if ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            if depth == 0:
                args.append("".join(buf).strip())
                return args
            depth -= 1
            buf.append(ch)
        elif ch in ",;" and depth == 0:
            args.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    args.append("".join(buf).strip())
    return args


def _split_sheet(ref: str, default_sheet: str) -> tuple[str, str]:
    if "!" not in ref:
        return default_sheet, ref
    sheet, _, rest = ref.rpartition("!")
    sheet = sheet.strip()
    if sheet.startswith("'") and sheet.endswith("'"):
        sheet = sheet[1:-1].replace("''", "'")
    return sheet, rest.strip()


def _cell_column(ref: str, default_sheet: str) -> SheetRef | None:
    sheet, rest = _split_sheet(ref.strip(), default_sheet)
    m = _CELL_RE.match(rest)
    if not m:
        return None
    return SheetRef(sheet=sheet, column=column_index_from_string(m.group(1).upper()) - 1)


def _range_first_column(ref: str, default_sheet: str) -> SheetRef | None:
    sheet, rest = _split_sheet(ref.strip(), default_sheet)
    m = _RANGE_RE.match(rest)
    if not m:
        return None
    return SheetRef(sheet=sheet, column=column_index_from_string(m.group(1).upper()) - 1)


_REF_TOKEN_RE = re.compile(
    r"(?:'(?:[^']|'')+'|[A-Za-z0-9_.]+)!\$?[A-Za-z]{1,3}\$?\d*(?::\$?[A-Za-z]{1,3}\$?\d*)?"
)


def _sheet_refs(formula: str, default_sheet: str) -> set[SheetRef]:
    refs: set[SheetRef] = set()
    for token in _REF_TOKEN_RE.findall(formula):
        target = _range_first_column(token, default_sheet)
        if target is not None:
            refs.add(target)
    return refs
