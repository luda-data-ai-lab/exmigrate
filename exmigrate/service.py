"""Target construction and migration orchestration shared by CLI and web."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import pandas as pd

from exmigrate.adapters import PostgresAdapter, SQLiteAdapter
from exmigrate.contracts.adapter import (
    Adapter,
    Issue,
    IssueSeverity,
    MigrationReport,
    TableData,
)
from exmigrate.contracts.ir import SchemaIR

TargetName = Literal["sqlite", "postgres"]
TARGETS: tuple[TargetName, ...] = ("sqlite", "postgres")


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
) -> list[MigrationReport]:
    """Run every requested target and collect its report.

    Columns with ``include=False`` (typically formula-derived ones the user
    wants recomputed downstream) are removed from both IR and data first.
    """
    excluded = ir.excluded_columns()
    ir, data = apply_exclusions(ir, data)
    reports: list[MigrationReport] = []
    for target in targets:
        config = configs.get(target, {})
        adapter = build_adapter(target, config, artifacts_dir)
        report = adapter.migrate(ir, data)
        report.issues.extend(_exclusion_issues(excluded))
        reports.append(report)
    return reports


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
