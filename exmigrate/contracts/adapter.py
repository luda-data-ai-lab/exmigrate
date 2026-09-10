"""Target adapter protocol and its result models."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Protocol, runtime_checkable

import pandas as pd
from pydantic import BaseModel, Field

from exmigrate.contracts.ir import SchemaIR

TableData = Mapping[str, pd.DataFrame]
"""Sheet data keyed by IR table name, columns already renamed to IR column names."""


class IssueSeverity(str, Enum):
    """How serious an issue is."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Issue(BaseModel):
    """A problem or note raised by the analyzer or an adapter."""

    severity: IssueSeverity
    code: str
    message: str
    table: str | None = None
    column: str | None = None


class PlannedTable(BaseModel):
    """What the adapter intends to create for one table."""

    name: str
    ddl: str
    row_count: int
    load_strategy: str


class MigrationPlan(BaseModel):
    """Credential-free description of what ``migrate`` will do."""

    target: str
    tables: list[PlannedTable] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)


class TableReport(BaseModel):
    """Outcome for a single table."""

    name: str
    rows_loaded: int
    ok: bool = True
    error: str | None = None


class MigrationReport(BaseModel):
    """Outcome of a whole migration."""

    target: str
    tables: list[TableReport] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when every table loaded successfully."""
        return all(t.ok for t in self.tables)


@runtime_checkable
class Adapter(Protocol):
    """Common interface for every migration target."""

    name: str

    def validate(self, ir: SchemaIR) -> list[Issue]:
        """Check the IR against target constraints without side effects."""
        ...

    def plan(self, ir: SchemaIR) -> MigrationPlan:
        """Describe the DDL and load strategy without touching the target."""
        ...

    def migrate(self, ir: SchemaIR, data: TableData) -> MigrationReport:
        """Create tables and load data."""
        ...
