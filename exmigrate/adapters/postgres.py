"""PostgreSQL target: live DSN mode or ``.sql`` dump mode."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Literal

import pandas as pd
import psycopg
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import base as pg_base
from sqlalchemy.engine import Dialect

from exmigrate.adapters.sql_common import (
    add_fk_ddl,
    build_metadata,
    chunked,
    ddl_for,
    iter_rows,
    plan_schema,
)
from exmigrate.contracts.adapter import (
    Issue,
    IssueSeverity,
    MigrationPlan,
    MigrationReport,
    PlannedTable,
    TableData,
    TableReport,
)
from exmigrate.contracts.ir import SchemaIR, TableIR

MAX_IDENTIFIER_LENGTH = 63
COPY_THRESHOLD = 10_000
BATCH_SIZE = 1000
RESERVED_WORDS = frozenset(pg_base.RESERVED_WORDS)


class PostgresAdapter:
    """Migrate the IR into PostgreSQL, either live or as a SQL dump."""

    name = "postgres"

    def __init__(
        self,
        dsn: str | None = None,
        *,
        mode: Literal["live", "dump"] = "live",
        dump_path: str | Path | None = None,
    ) -> None:
        if mode == "live" and not dsn:
            raise ValueError("live mode requires a DSN")
        if mode == "dump" and dump_path is None:
            raise ValueError("dump mode requires dump_path")
        self.dsn = dsn
        self.mode = mode
        self.dump_path = Path(dump_path) if dump_path is not None else None

    def validate(self, ir: SchemaIR) -> list[Issue]:
        """Flag identifiers longer than 63 chars, reserved words and unenforceable FKs."""
        issues: list[Issue] = list(plan_schema(ir).issues)
        for table in ir.tables:
            issues.extend(self._check_identifier(table.name, table.name, None))
            for col in table.columns:
                issues.extend(self._check_identifier(col.name, table.name, col.name))
        return issues

    @staticmethod
    def _check_identifier(ident: str, table: str, column: str | None) -> list[Issue]:
        issues: list[Issue] = []
        what = f"column '{table}.{column}'" if column else f"table '{table}'"
        if len(ident.encode("utf-8")) > MAX_IDENTIFIER_LENGTH:
            issues.append(
                Issue(
                    severity=IssueSeverity.ERROR,
                    code="identifier_too_long",
                    message=f"{what}: identifier exceeds {MAX_IDENTIFIER_LENGTH} bytes",
                    table=table,
                    column=column,
                )
            )
        if ident.lower() in RESERVED_WORDS:
            issues.append(
                Issue(
                    severity=IssueSeverity.WARNING,
                    code="reserved_word",
                    message=f"{what}: '{ident}' is a reserved word and will be quoted",
                    table=table,
                    column=column,
                )
            )
        return issues

    def plan(self, ir: SchemaIR) -> MigrationPlan:
        """Render DDL in dependency order and choose COPY vs. batched INSERT per table."""
        schema = plan_schema(ir)
        metadata = build_metadata(ir, schema.inline_edges())
        dialect = _pg_dialect()
        tables = [
            PlannedTable(
                name=name,
                ddl=ddl_for(metadata.tables[name], dialect),
                row_count=ir.table(name).row_count,
                load_strategy=_strategy(ir.table(name)),
            )
            for name in schema.order
        ]
        for edge in schema.deferred:
            tables.append(
                PlannedTable(
                    name=f"{edge.table} (constraint)",
                    ddl=add_fk_ddl(metadata, edge, dialect),
                    row_count=0,
                    load_strategy="ALTER TABLE after load",
                )
            )
        return MigrationPlan(target=self.name, tables=tables, issues=self.validate(ir))

    def migrate(self, ir: SchemaIR, data: TableData) -> MigrationReport:
        """Dispatch to live or dump mode."""
        issues = self.validate(ir)
        if any(i.severity is IssueSeverity.ERROR for i in issues):
            return MigrationReport(target=self.name, issues=issues)
        if self.mode == "dump":
            return self._migrate_dump(ir, data, issues)
        return self._migrate_live(ir, data, issues)

    def _migrate_live(self, ir: SchemaIR, data: TableData, issues: list[Issue]) -> MigrationReport:
        assert self.dsn is not None
        engine = sa.create_engine(sqlalchemy_url(self.dsn))
        schema = plan_schema(ir)
        metadata = build_metadata(ir, schema.inline_edges())
        dialect = _pg_dialect()
        reports: list[TableReport] = []
        try:
            with engine.begin() as conn:
                for name in reversed(schema.order):
                    conn.execute(sa.text(f"DROP TABLE IF EXISTS {_quote(name)} CASCADE"))
                metadata.create_all(conn)
            for name in schema.order:
                table = ir.table(name)
                frame = data.get(table.name)
                if frame is None or not len(frame):
                    reports.append(TableReport(name=table.name, rows_loaded=0))
                    continue
                try:
                    if len(frame) > COPY_THRESHOLD:
                        loaded = self._copy_table(table, frame)
                    else:
                        loaded = 0
                        with engine.begin() as conn:
                            sa_table = metadata.tables[table.name]
                            for batch in chunked(iter_rows(table, frame), BATCH_SIZE):
                                conn.execute(sa.insert(sa_table), batch)
                                loaded += len(batch)
                    reports.append(TableReport(name=table.name, rows_loaded=loaded))
                except Exception as exc:  # noqa: BLE001 - reported per table
                    reports.append(
                        TableReport(name=table.name, rows_loaded=0, ok=False, error=str(exc))
                    )
            for edge in schema.deferred:
                statement = add_fk_ddl(metadata, edge, dialect)
                try:
                    with engine.begin() as conn:
                        conn.execute(sa.text(statement))
                except Exception as exc:  # noqa: BLE001 - reported per constraint
                    issues.append(
                        Issue(
                            severity=IssueSeverity.WARNING,
                            code="fk_not_created",
                            message=f"'{edge.table}.{edge.column}': {exc}",
                            table=edge.table,
                            column=edge.column,
                        )
                    )
        finally:
            engine.dispose()
        reports.sort(key=lambda r: [t.name for t in ir.tables].index(r.name))
        return MigrationReport(target=self.name, tables=reports, issues=issues)

    def _copy_table(self, table: TableIR, frame: pd.DataFrame) -> int:
        assert self.dsn is not None
        cols = ", ".join(_quote(c.name) for c in table.columns)
        sql = f"COPY {_quote(table.name)} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '')"
        loaded = 0
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur, cur.copy(sql) as copy:
            for batch in chunked(iter_rows(table, frame), BATCH_SIZE):
                buf = io.StringIO()
                writer = csv.writer(buf)
                for row in batch:
                    writer.writerow(["" if v is None else _csv_value(v) for v in row.values()])
                copy.write(buf.getvalue())
                loaded += len(batch)
        return loaded

    def _migrate_dump(self, ir: SchemaIR, data: TableData, issues: list[Issue]) -> MigrationReport:
        assert self.dump_path is not None
        self.dump_path.parent.mkdir(parents=True, exist_ok=True)
        schema = plan_schema(ir)
        metadata = build_metadata(ir, schema.inline_edges())
        dialect = _pg_dialect()
        reports: list[TableReport] = []
        with self.dump_path.open("w", encoding="utf-8") as fh:
            fh.write("BEGIN;\n")
            for name in reversed(schema.order):
                fh.write(f"DROP TABLE IF EXISTS {_quote(name)} CASCADE;\n")
            for name in schema.order:
                fh.write(ddl_for(metadata.tables[name], dialect) + ";\n")
            for name in schema.order:
                table = ir.table(name)
                frame = data.get(table.name)
                loaded = 0
                if frame is not None and len(frame):
                    cols = ", ".join(_quote(c.name) for c in table.columns)
                    for batch in chunked(iter_rows(table, frame), BATCH_SIZE):
                        values = ",\n".join(
                            "(" + ", ".join(_sql_literal(v) for v in row.values()) + ")"
                            for row in batch
                        )
                        fh.write(f"INSERT INTO {_quote(table.name)} ({cols}) VALUES\n{values};\n")
                        loaded += len(batch)
                reports.append(TableReport(name=table.name, rows_loaded=loaded))
            for edge in schema.deferred:
                fh.write(add_fk_ddl(metadata, edge, dialect) + ";\n")
            fh.write("COMMIT;\n")
        reports.sort(key=lambda r: [t.name for t in ir.tables].index(r.name))
        return MigrationReport(
            target=self.name,
            tables=reports,
            artifacts=[str(self.dump_path)],
            issues=issues,
        )


def _strategy(table: TableIR) -> str:
    if table.row_count > COPY_THRESHOLD:
        return "COPY FROM STDIN (csv)"
    return f"INSERT (batch {BATCH_SIZE})"


def sqlalchemy_url(dsn: str) -> str:
    if dsn.startswith("postgresql+"):
        return dsn
    if dsn.startswith("postgres://"):
        dsn = "postgresql://" + dsn[len("postgres://") :]
    return dsn.replace("postgresql://", "postgresql+psycopg://", 1)


def _quote(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _csv_value(value: object) -> str:
    if isinstance(value, bool):
        return "t" if value else "f"
    return str(value)


def _sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _pg_dialect() -> Dialect:
    return sa.create_mock_engine("postgresql+psycopg://", lambda *a, **k: None).dialect
