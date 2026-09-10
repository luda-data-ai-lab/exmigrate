"""Target construction and migration orchestration shared by CLI and web."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from exmigrate.adapters import PostgresAdapter, SQLiteAdapter
from exmigrate.contracts.adapter import Adapter, MigrationReport, TableData
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
    """Run every requested target and collect its report."""
    reports: list[MigrationReport] = []
    for target in targets:
        config = configs.get(target, {})
        adapter = build_adapter(target, config, artifacts_dir)
        reports.append(adapter.migrate(ir, data))
    return reports


def as_target(value: str) -> TargetName:
    """Validate a user-supplied target name."""
    for target in TARGETS:
        if value == target:
            return target
    raise ValueError(f"unknown target '{value}'")
