"""Schema IR models.

The IR carries its full field shape (``pk``, ``fk``, ``derived``) from Phase 1.
Later phases populate these fields; they never restructure them.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ColumnType(str, Enum):
    """Logical column types inferred by the analyzer."""

    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    TEXT = "text"


class ForeignKey(BaseModel):
    """Reference from a column to another table's column."""

    table: str
    column: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class ColumnIR(BaseModel):
    """A single column of a table."""

    name: str
    source_name: str
    type: ColumnType
    nullable: bool = True
    pk: bool = False
    fk: ForeignKey | None = None
    derived: bool = False
    null_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    max_length: int | None = None


class TableIR(BaseModel):
    """A table derived from one worksheet."""

    name: str
    source_file: str
    source_sheet: str
    row_count: int = 0
    header_row: int = 1
    columns: list[ColumnIR] = Field(default_factory=list)

    def column(self, name: str) -> ColumnIR:
        """Return the column called ``name`` or raise ``KeyError``."""
        for col in self.columns:
            if col.name == name:
                return col
        raise KeyError(name)


class SchemaIR(BaseModel):
    """Root of the Schema IR."""

    version: int = 1
    tables: list[TableIR] = Field(default_factory=list)

    def table(self, name: str) -> TableIR:
        """Return the table called ``name`` or raise ``KeyError``."""
        for tbl in self.tables:
            if tbl.name == name:
                return tbl
        raise KeyError(name)
