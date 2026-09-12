"""Shared SQLAlchemy helpers for SQL targets."""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd
import sqlalchemy as sa
from sqlalchemy.engine import Dialect
from sqlalchemy.schema import AddConstraint, CreateTable

from exmigrate.contracts.adapter import Issue, IssueSeverity
from exmigrate.contracts.ir import ColumnType, SchemaIR, TableIR

_SA_TYPES: dict[ColumnType, type[sa.types.TypeEngine[Any]]] = {
    ColumnType.INTEGER: sa.BigInteger,
    ColumnType.FLOAT: sa.Float,
    ColumnType.BOOLEAN: sa.Boolean,
    ColumnType.DATE: sa.Date,
    ColumnType.DATETIME: sa.DateTime,
    ColumnType.TEXT: sa.Text,
}


@dataclass(frozen=True)
class FKEdge:
    """A foreign key that will become a real constraint."""

    table: str
    column: str
    ref_table: str
    ref_column: str

    @property
    def constraint_name(self) -> str:
        """Deterministic constraint identifier."""
        return f"fk_{self.table}_{self.column}"[:63]


@dataclass
class SchemaPlan:
    """Resolved FK edges, load order and the edges that must be added after loading."""

    edges: list[FKEdge]
    order: list[str]
    deferred: list[FKEdge]
    issues: list[Issue]

    def inline_edges(self) -> list[FKEdge]:
        """Edges emitted inside ``CREATE TABLE``."""
        deferred = set(self.deferred)
        return [e for e in self.edges if e not in deferred]


def plan_schema(ir: SchemaIR) -> SchemaPlan:
    """Validate FKs, order tables by dependency and pick edges to defer.

    Invalid FKs (unknown target, non-PK target, incompatible type) are dropped
    with a warning. Self-references and edges inside a dependency cycle are
    deferred: created after data load (drop → load → re-add).
    """
    edges, issues = resolve_foreign_keys(ir)
    deferred = [e for e in edges if e.table == e.ref_table]
    order, cyclic = load_order(ir, [e for e in edges if e.table != e.ref_table])
    for edge in cyclic:
        issues.append(
            Issue(
                severity=IssueSeverity.WARNING,
                code="fk_cycle",
                message=(
                    f"'{edge.table}.{edge.column}' → '{edge.ref_table}' is part of a cycle; "
                    "constraint is added after loading"
                ),
                table=edge.table,
                column=edge.column,
            )
        )
    deferred.extend(cyclic)
    return SchemaPlan(edges=edges, order=order, deferred=deferred, issues=issues)


def resolve_foreign_keys(ir: SchemaIR) -> tuple[list[FKEdge], list[Issue]]:
    """Turn ``ColumnIR.fk`` into edges, skipping ones no SQL target can enforce."""
    tables = {t.name: t for t in ir.tables}
    edges: list[FKEdge] = []
    issues: list[Issue] = []
    for table in ir.tables:
        for col in table.columns:
            fk = col.fk
            if fk is None:
                continue
            target = tables.get(fk.table)
            ref_col = (
                next((c for c in target.columns if c.name == fk.column), None) if target else None
            )
            reason: str | None = None
            if target is None or ref_col is None:
                reason = f"target '{fk.table}.{fk.column}' does not exist"
            elif not ref_col.pk:
                reason = f"target '{fk.table}.{fk.column}' is not a primary key"
            elif ref_col.type != col.type:
                reason = f"type {col.type.value} does not match target {ref_col.type.value}"
            if reason is not None:
                issues.append(
                    Issue(
                        severity=IssueSeverity.WARNING,
                        code="fk_skipped",
                        message=f"'{table.name}.{col.name}': foreign key skipped; {reason}",
                        table=table.name,
                        column=col.name,
                    )
                )
                continue
            edges.append(FKEdge(table.name, col.name, fk.table, fk.column))
    return edges, issues


def load_order(ir: SchemaIR, edges: Sequence[FKEdge]) -> tuple[list[str], list[FKEdge]]:
    """Topologically sort tables (parents first); return the edges broken to resolve cycles.

    Cycles are broken at the edge leaving the latest table in IR order, so
    earlier sheets keep their inline constraints.
    """
    names = [t.name for t in ir.tables]
    position = {n: i for i, n in enumerate(names)}
    remaining = list(edges)
    broken: list[FKEdge] = []
    while True:
        order = _kahn(names, remaining)
        if len(order) == len(names):
            return order, broken
        stuck = {n for n in names if n not in order}
        victim = max(
            (e for e in remaining if e.table in stuck and e.ref_table in stuck),
            key=lambda e: position[e.table],
        )
        remaining.remove(victim)
        broken.append(victim)


def _kahn(names: Sequence[str], edges: Sequence[FKEdge]) -> list[str]:
    indeg = {n: 0 for n in names}
    children: dict[str, list[str]] = {n: [] for n in names}
    for e in edges:
        indeg[e.table] += 1
        children[e.ref_table].append(e.table)
    ready = [n for n in names if indeg[n] == 0]
    order: list[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for child in children[node]:
            indeg[child] -= 1
            if indeg[child] == 0:
                ready.append(child)
    return order


def build_metadata(ir: SchemaIR, edges: Sequence[FKEdge] = ()) -> sa.MetaData:
    """Translate the IR into SQLAlchemy ``Table`` objects with the given inline FKs."""
    metadata = sa.MetaData()
    by_table: dict[str, list[FKEdge]] = {}
    for edge in edges:
        by_table.setdefault(edge.table, []).append(edge)
    for table in ir.tables:
        build_table(table, metadata, by_table.get(table.name, ()))
    return metadata


def build_table(table: TableIR, metadata: sa.MetaData, edges: Sequence[FKEdge] = ()) -> sa.Table:
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
    constraints = [fk_constraint(edge) for edge in edges]
    return sa.Table(table.name, metadata, *columns, *constraints)


def fk_constraint(edge: FKEdge) -> sa.ForeignKeyConstraint:
    """SQLAlchemy constraint for ``edge``."""
    return sa.ForeignKeyConstraint(
        [edge.column], [f"{edge.ref_table}.{edge.ref_column}"], name=edge.constraint_name
    )


def ddl_for(table: sa.Table, dialect: Dialect) -> str:
    """Render ``CREATE TABLE`` for ``table`` in ``dialect``."""
    return str(CreateTable(table).compile(dialect=dialect)).strip()


def add_fk_ddl(metadata: sa.MetaData, edge: FKEdge, dialect: Dialect) -> str:
    """Render ``ALTER TABLE … ADD CONSTRAINT`` for a deferred edge."""
    constraint = fk_constraint(edge)
    metadata.tables[edge.table].append_constraint(constraint)
    return str(AddConstraint(constraint).compile(dialect=dialect)).strip()


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
