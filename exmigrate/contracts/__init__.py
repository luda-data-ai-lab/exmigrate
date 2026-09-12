"""Contracts shared by analyzer, adapters and the web layer.

Everything downstream of the analyzer derives from the Schema IR defined here.
Adapters and the UI never read Excel directly.
"""

from exmigrate.contracts.adapter import (
    Adapter,
    Issue,
    IssueSeverity,
    MigrationPlan,
    MigrationReport,
    PlannedTable,
    TableReport,
)
from exmigrate.contracts.ir import ColumnIR, ColumnType, ForeignKey, SchemaIR, TableIR

__all__ = [
    "Adapter",
    "ColumnIR",
    "ColumnType",
    "ForeignKey",
    "Issue",
    "IssueSeverity",
    "MigrationPlan",
    "MigrationReport",
    "PlannedTable",
    "SchemaIR",
    "TableIR",
    "TableReport",
]
