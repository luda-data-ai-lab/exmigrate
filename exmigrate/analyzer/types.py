"""Column type inference."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd

from exmigrate.contracts.ir import ColumnType

SAMPLE_ROWS = 10_000
_BOOL_STRINGS = {"true", "false", "yes", "no", "y", "n", "t", "f"}


@dataclass(frozen=True)
class TypeStats:
    """Result of inferring one column."""

    type: ColumnType
    nullable: bool
    null_ratio: float
    max_length: int | None


def infer_column(series: pd.Series) -> TypeStats:
    """Infer the logical type of ``series`` from at most ``SAMPLE_ROWS`` values."""
    total = len(series)
    non_null = series.dropna()
    non_null = non_null[~non_null.map(_is_blank)]
    null_count = total - len(non_null)
    null_ratio = (null_count / total) if total else 0.0
    sample = non_null.head(SAMPLE_ROWS)
    values = list(sample)

    col_type = _classify(values)
    max_length: int | None = None
    if col_type is ColumnType.TEXT:
        max_length = max((len(str(v)) for v in values), default=0)
    return TypeStats(
        type=col_type,
        nullable=null_count > 0,
        null_ratio=null_ratio,
        max_length=max_length,
    )


def _is_blank(value: object) -> bool:
    return isinstance(value, str) and value.strip() == ""


def _classify(values: list[object]) -> ColumnType:
    if not values:
        return ColumnType.TEXT
    if all(isinstance(v, bool) for v in values):
        return ColumnType.BOOLEAN
    if all(_is_datetime(v) for v in values):
        return ColumnType.DATETIME if any(_has_time(v) for v in values) else ColumnType.DATE
    if all(isinstance(v, dt.date) and not isinstance(v, dt.datetime) for v in values):
        return ColumnType.DATE
    if all(_is_number(v) for v in values):
        if all(_is_integral(v) for v in values):
            return ColumnType.INTEGER
        return ColumnType.FLOAT
    if all(isinstance(v, str) and v.strip().lower() in _BOOL_STRINGS for v in values):
        return ColumnType.BOOLEAN
    return ColumnType.TEXT


def _is_datetime(value: object) -> bool:
    return isinstance(value, dt.datetime | pd.Timestamp)


def _has_time(value: object) -> bool:
    if isinstance(value, dt.datetime):
        return value.time() != dt.time(0, 0)
    return False


def _is_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float):
        return not (isinstance(value, float) and value != value)
    return False


def _is_integral(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return value.is_integer()
    return False
