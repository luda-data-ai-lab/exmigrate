"""Primary/foreign key inference over the Schema IR and sheet data."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

import pandas as pd

from exmigrate.contracts.ir import ColumnIR, ColumnType, ForeignKey, SchemaIR, TableIR

PK_NAME_RE = re.compile(r"^(id|.+_id|code|no|.+_code|.+_no)$", re.IGNORECASE)
CONTAINMENT_MIN = 0.95
VALUES_ONLY_MIN_COVERAGE = 0.5

CONF_LOOKUP = 0.95
CONF_NAME_AND_VALUES = 0.9
CONF_VALUES_ONLY = 0.7
CONF_LOOKUP_ALL = 0.98
CONF_CROSS_FILE = 0.99

_NUMERIC = {ColumnType.INTEGER, ColumnType.FLOAT}


@dataclass(frozen=True)
class LookupEvidence:
    """A lookup/aggregate formula from ``(table, column)`` into ``(ref_table, ref_column)``.

    ``cross_file`` marks references into another uploaded workbook.
    """

    table: str
    column: str
    ref_table: str
    ref_column: str
    cross_file: bool = False


def infer_primary_keys(ir: SchemaIR, frames: Sequence[pd.DataFrame]) -> None:
    """Mark at most one PK per table: unique + not-null, preferring id-like names."""
    for table, frame in zip(ir.tables, frames, strict=True):
        if any(c.pk for c in table.columns):
            continue
        candidates = [
            i
            for i, col in enumerate(table.columns)
            if len(frame) and _is_unique_not_null(frame.iloc[:, i])
        ]
        if not candidates:
            continue
        named = [i for i in candidates if PK_NAME_RE.match(table.columns[i].name)]
        if named:
            table.columns[named[0]].pk = True
        elif candidates[0] == 0:
            table.columns[0].pk = True


def infer_foreign_keys(
    ir: SchemaIR,
    frames: Sequence[pd.DataFrame],
    lookups: Sequence[LookupEvidence] = (),
) -> None:
    """Attach ``fk`` to columns with name/value/lookup evidence against another table's PK."""
    pk_values: dict[str, tuple[ColumnIR, set[object]]] = {}
    for table, frame in zip(ir.tables, frames, strict=True):
        for i, col in enumerate(table.columns):
            if col.pk:
                pk_values[table.name] = (col, _value_set(frame.iloc[:, i]))
                break

    lookup_index = {(e.table, e.column): e for e in lookups}

    for table, frame in zip(ir.tables, frames, strict=True):
        for i, col in enumerate(table.columns):
            if col.pk or col.fk is not None:
                continue
            best: ForeignKey | None = None
            for ref_table, (ref_col, ref_set) in pk_values.items():
                if ref_table == table.name or not _compatible(col.type, ref_col.type):
                    continue
                evidence = lookup_index.get((table.name, col.name))
                has_lookup = evidence is not None and evidence.ref_table == ref_table
                name_match = _name_matches(col.name, ref_table, ref_col.name)
                values_match = _contained(frame.iloc[:, i], ref_set)
                if values_match and not (has_lookup or name_match):
                    values_match = _coverage(frame.iloc[:, i], ref_set) >= VALUES_ONLY_MIN_COVERAGE
                cross_file = has_lookup and evidence is not None and evidence.cross_file
                confidence = _score(has_lookup, name_match, values_match, cross_file)
                if confidence is None:
                    continue
                if best is None or confidence > best.confidence:
                    best = ForeignKey(table=ref_table, column=ref_col.name, confidence=confidence)
            if best is not None:
                col.fk = best


def _score(
    has_lookup: bool, name_match: bool, values_match: bool, cross_file: bool = False
) -> float | None:
    if cross_file:
        return CONF_CROSS_FILE
    if has_lookup and name_match and values_match:
        return CONF_LOOKUP_ALL
    if has_lookup:
        return CONF_LOOKUP
    if values_match and name_match:
        return CONF_NAME_AND_VALUES
    if values_match:
        return CONF_VALUES_ONLY
    return None


def _name_matches(col_name: str, ref_table: str, ref_col: str) -> bool:
    col = col_name.lower()
    if col == ref_col.lower():
        return True
    stems = {ref_table.lower()}
    stems.update(_singulars(ref_table.lower()))
    return any(col in {f"{s}_id", f"{s}id", f"{s}_code", f"{s}_no", s} for s in stems)


def _singulars(name: str) -> set[str]:
    out = set()
    if name.endswith("ies"):
        out.add(name[:-3] + "y")
    if name.endswith("ses") or name.endswith("xes"):
        out.add(name[:-2])
    if name.endswith("s"):
        out.add(name[:-1])
    return out


def _compatible(a: ColumnType, b: ColumnType) -> bool:
    if a == b:
        return True
    return a in _NUMERIC and b in _NUMERIC


def _clean(series: pd.Series) -> pd.Series:
    non_null = series.dropna()
    return non_null[~non_null.map(lambda v: isinstance(v, str) and not v.strip())]


def _is_unique_not_null(series: pd.Series) -> bool:
    clean = _clean(series)
    if len(clean) != len(series):
        return False
    return bool(clean.map(_norm).is_unique)


def _norm(value: object) -> object:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        return value.strip()
    return value


def _value_set(series: pd.Series) -> set[object]:
    return {_norm(v) for v in _clean(series)}


def _contained(series: pd.Series, ref_set: set[object]) -> bool:
    values = [_norm(v) for v in _clean(series)]
    if not values or not ref_set:
        return False
    hits = sum(1 for v in values if v in ref_set)
    return hits / len(values) >= CONTAINMENT_MIN


def _coverage(series: pd.Series, ref_set: set[object]) -> float:
    """Share of the referenced key values actually used by ``series``."""
    if not ref_set:
        return 0.0
    return len(_value_set(series) & ref_set) / len(ref_set)


def table_by_sheet(ir: SchemaIR, file_name: str, sheet: str) -> TableIR | None:
    """Locate the table produced from ``sheet`` of ``file_name``."""
    for table in ir.tables:
        if table.source_file == file_name and table.source_sheet == sheet:
            return table
    return None
