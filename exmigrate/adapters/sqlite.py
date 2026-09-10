"""SQLite target: writes a downloadable ``.db`` artifact."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

from exmigrate.adapters.sql_common import build_metadata, chunked, ddl_for, iter_rows
from exmigrate.contracts.adapter import (
    Issue,
    IssueSeverity,
    MigrationPlan,
    MigrationReport,
    PlannedTable,
    TableData,
    TableReport,
)
from exmigrate.contracts.ir import SchemaIR

BATCH_SIZE = 1000


class SQLiteAdapter:
    """Migrate the IR into a SQLite database file."""

    name = "sqlite"

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def validate(self, ir: SchemaIR) -> list[Issue]:
        """SQLite is permissive; only duplicate table names are fatal."""
        issues: list[Issue] = []
        seen: set[str] = set()
        for table in ir.tables:
            if table.name.lower() in seen:
                issues.append(
                    Issue(
                        severity=IssueSeverity.ERROR,
                        code="duplicate_table",
                        message=f"table name '{table.name}' is not unique",
                        table=table.name,
                    )
                )
            seen.add(table.name.lower())
            if not table.columns:
                issues.append(
                    Issue(
                        severity=IssueSeverity.ERROR,
                        code="no_columns",
                        message=f"table '{table.name}' has no columns",
                        table=table.name,
                    )
                )
        return issues

    def plan(self, ir: SchemaIR) -> MigrationPlan:
        """Render DDL for every table."""
        metadata = build_metadata(ir)
        dialect = sqlite.dialect()
        tables = [
            PlannedTable(
                name=t.name,
                ddl=ddl_for(metadata.tables[t.name], dialect),
                row_count=t.row_count,
                load_strategy=f"executemany (batch {BATCH_SIZE})",
            )
            for t in ir.tables
        ]
        return MigrationPlan(target=self.name, tables=tables, issues=self.validate(ir))

    def migrate(self, ir: SchemaIR, data: TableData) -> MigrationReport:
        """Create the ``.db`` file and load every table in its own transaction."""
        issues = self.validate(ir)
        if any(i.severity is IssueSeverity.ERROR for i in issues):
            return MigrationReport(target=self.name, issues=issues)

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.db_path.exists():
            self.db_path.unlink()
        engine = sa.create_engine(f"sqlite:///{self.db_path}")
        metadata = build_metadata(ir)
        reports: list[TableReport] = []
        try:
            with engine.begin() as conn:
                metadata.create_all(conn)
            for table in ir.tables:
                sa_table = metadata.tables[table.name]
                frame = data.get(table.name)
                loaded = 0
                try:
                    with engine.begin() as conn:
                        if frame is not None and len(frame):
                            for batch in chunked(iter_rows(table, frame), BATCH_SIZE):
                                conn.execute(sa.insert(sa_table), batch)
                                loaded += len(batch)
                    reports.append(TableReport(name=table.name, rows_loaded=loaded))
                except Exception as exc:  # noqa: BLE001 - reported per table
                    reports.append(
                        TableReport(name=table.name, rows_loaded=0, ok=False, error=str(exc))
                    )
        finally:
            engine.dispose()
        return MigrationReport(
            target=self.name,
            tables=reports,
            artifacts=[str(self.db_path)],
            issues=issues,
        )
