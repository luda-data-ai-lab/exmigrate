"""Target construction and migration orchestration shared by CLI and web."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal

import pandas as pd
import sqlalchemy as sa

from exmigrate.adapters import PostgresAdapter, SQLiteAdapter
from exmigrate.adapters.postgres import sqlalchemy_url
from exmigrate.contracts.adapter import (
    Adapter,
    Issue,
    IssueSeverity,
    MigrationReport,
    TableData,
)
from exmigrate.contracts.ir import SchemaIR
from exmigrate.contracts.translation import Translation
from exmigrate.translate import order_columns, view_statements

TargetName = Literal["sqlite", "postgres"]
TARGETS: tuple[TargetName, ...] = ("sqlite", "postgres")
SCRIPT_NAME = "recompute.py"


def build_adapter(target: str, config: Mapping[str, object], artifacts_dir: Path) -> Adapter:
    """Instantiate the adapter for ``target`` from a user-supplied config mapping."""
    if target == "sqlite":
        return SQLiteAdapter(artifacts_dir / "migration.db")
    if target == "postgres":
        mode = str(config.get("mode") or "live")
        if mode == "dump":
            return PostgresAdapter(mode="dump", dump_path=artifacts_dir / "migration.sql")
        dsn = str(config.get("dsn") or os.environ.get("PG_DSN_DEFAULT") or "")
        if not dsn:
            raise ValueError("postgres live mode needs a DSN (config.dsn or PG_DSN_DEFAULT)")
        return PostgresAdapter(dsn=dsn, mode="live")
    raise ValueError(f"unknown target '{target}' (expected one of {', '.join(TARGETS)})")


def run_migration(
    ir: SchemaIR,
    data: TableData,
    targets: list[str],
    configs: Mapping[str, Mapping[str, object]],
    artifacts_dir: Path,
    translation: Translation | None = None,
) -> list[MigrationReport]:
    """Run every requested target and collect its report.

    Columns with ``include=False`` (typically formula-derived ones the user
    wants recomputed downstream) are removed from both IR and data first. When
    a ``translation`` is given, its recompute views are created in the target
    (or appended to the dump) and the SQL/pandas scripts are written as artifacts.
    """
    excluded = ir.excluded_columns()
    ir, data = apply_exclusions(ir, data)
    reports: list[MigrationReport] = []
    for target in targets:
        config = configs.get(target, {})
        adapter = build_adapter(target, config, artifacts_dir)
        report = adapter.migrate(ir, data)
        report.issues.extend(_exclusion_issues(excluded))
        if translation is not None and translation.columns:
            _apply_translation(target, config, artifacts_dir, translation, report)
        reports.append(report)
    return reports


def _apply_translation(
    target: str,
    config: Mapping[str, object],
    artifacts_dir: Path,
    translation: Translation,
    report: MigrationReport,
) -> None:
    dialect = as_target(target)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    sql_path = artifacts_dir / f"recompute_{dialect}.sql"
    sql_path.write_text(translation.views[dialect], encoding="utf-8")
    script_path = artifacts_dir / SCRIPT_NAME
    script_path.write_text(translation.script, encoding="utf-8")
    report.artifacts.extend([str(sql_path), str(script_path)])
    if any(not t.ok for t in report.tables):
        report.issues.append(
            Issue(
                severity=IssueSeverity.WARNING,
                code="views_skipped",
                message="recompute views not created because a table failed to load",
            )
        )
        return
    statements = view_statements(order_columns(translation.columns), dialect)
    if dialect == "sqlite":
        _run_views(statements, report, lambda: sqlite3.connect(artifacts_dir / "migration.db"))
    elif str(config.get("mode") or "live") == "dump":
        with (artifacts_dir / "migration.sql").open("a", encoding="utf-8") as fh:
            fh.write("\n" + translation.views[dialect])
    else:
        dsn = str(config.get("dsn") or os.environ.get("PG_DSN_DEFAULT") or "")
        engine = sa.create_engine(sqlalchemy_url(dsn))
        try:
            _run_views(statements, report, lambda: engine.connect())
        finally:
            engine.dispose()


def _run_views(
    statements: list[str],
    report: MigrationReport,
    connect: Callable[[], sqlite3.Connection | sa.Connection],
) -> None:
    conn = connect()
    try:
        for statement in statements:
            try:
                if isinstance(conn, sqlite3.Connection):
                    conn.execute(statement)
                    conn.commit()
                else:
                    with conn.begin():
                        conn.execute(sa.text(statement))
            except Exception as exc:  # noqa: BLE001 - reported per view
                report.issues.append(
                    Issue(
                        severity=IssueSeverity.WARNING,
                        code="view_not_created",
                        message=f"{statement.splitlines()[0]}: {exc}",
                    )
                )
    finally:
        conn.close()


def _exclusion_issues(excluded: list[tuple[str, str]]) -> list[Issue]:
    return [
        Issue(
            severity=IssueSeverity.INFO,
            code="column_excluded",
            message=f"{table}.{column} was not migrated (excluded in review)",
            table=table,
            column=column,
        )
        for table, column in excluded
    ]


def apply_exclusions(ir: SchemaIR, data: TableData) -> tuple[SchemaIR, TableData]:
    """Strip excluded columns from ``ir`` and the matching frames in ``data``."""
    excluded = ir.excluded_columns()
    if not excluded:
        return ir, data
    trimmed: dict[str, pd.DataFrame] = dict(data)
    for table, column in excluded:
        frame = trimmed.get(table)
        if frame is not None and column in frame.columns:
            trimmed[table] = frame.drop(columns=[column])
    return ir.for_migration(), trimmed


def as_target(value: str) -> TargetName:
    """Validate a user-supplied target name."""
    for target in TARGETS:
        if value == target:
            return target
    raise ValueError(f"unknown target '{value}'")
