"""Shared SQLAlchemy helpers for SQL targets."""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Iterator
from typing import Any

import pandas as pd
import sqlalchemy as sa
from sqlalchemy.engine import Dialect
from sqlalchemy.schema import CreateTable

from exmigrate.contracts.ir import ColumnType, SchemaIR, TableIR

_SA_TYPES: dict[ColumnType, type[sa.types.TypeEngine[Any]]] = {
    ColumnType.INTEGER: sa.BigInteger,
    ColumnType.FLOAT: sa.Float,
    ColumnType.BOOLEAN: sa.Boolean,
    ColumnType.DATE: sa.Date,
    ColumnType.DATETIME: sa.DateTime,
    ColumnType.TEXT: sa.Text,
}


def build_metadata(ir: SchemaIR) -> sa.MetaData:
    """Translate the IR into SQLAlchemy ``Table`` objects (values only, no FKs)."""
    metadata = sa.MetaData()
    for table in ir.tables:
        build_table(table, metadata)
    return metadata


def build_table(table: TableIR, metadata: sa.MetaData) -> sa.Table:
    """Create the SQLAlchemy ``Table`` for one IR table."""
    columns = [
        sa.Column(
            col.name,
            _SA_TYPES[col.type](),
            primary_key=col.pk,
            nullable=col.nullable and not col.pk,
        )
        for col in table.columns
    ]
    return sa.Table(table.name, metadata, *columns)


def ddl_for(table: sa.Table, dialect: Dialect) -> str:
    """Render ``CREATE TABLE`` for ``table`` in ``dialect``."""
    return str(CreateTable(table).compile(dialect=dialect)).strip()


def coerce_value(value: object, col_type: ColumnType) -> object:
    """Convert a cell value into something the DB driver accepts for ``col_type``."""
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if col_type is ColumnType.INTEGER:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int | float):
            return int(value)
        return int(float(str(value)))
    if col_type is ColumnType.FLOAT:
        if isinstance(value, int | float):
            return float(value)
        return float(str(value))
    if col_type is ColumnType.BOOLEAN:
        if isinstance(value, bool):
            return value
        if isinstance(value, int | float):
            return bool(value)
        return str(value).strip().lower() in {"true", "yes", "y", "t", "1"}
    if col_type is ColumnType.DATE:
        if isinstance(value, pd.Timestamp):
            return value.date()
        if isinstance(value, dt.datetime):
            return value.date()
        if isinstance(value, dt.date):
            return value
        return dt.date.fromisoformat(str(value)[:10])
    if col_type is ColumnType.DATETIME:
        if isinstance(value, pd.Timestamp):
            return value.to_pydatetime()
        if isinstance(value, dt.datetime):
            return value
        if isinstance(value, dt.date):
            return dt.datetime(value.year, value.month, value.day)
        return dt.datetime.fromisoformat(str(value))
    return str(value)


def iter_rows(table: TableIR, frame: pd.DataFrame) -> Iterator[dict[str, object]]:
    """Yield coerced row dicts for ``frame`` in IR column order."""
    types = [(col.name, col.type) for col in table.columns]
    for raw in frame.itertuples(index=False, name=None):
        yield {name: coerce_value(val, typ) for (name, typ), val in zip(types, raw, strict=True)}


def chunked(rows: Iterable[dict[str, object]], size: int) -> Iterator[list[dict[str, object]]]:
    """Batch ``rows`` into lists of at most ``size``."""
    batch: list[dict[str, object]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
