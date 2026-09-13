"""Formula → SQL / pandas translation results (Phase 4).

Every formula-derived column gets one :class:`TranslatedColumn`. ``status``
is ``ok`` when the whole formula was translated, ``partial`` when some
sub-expression fell back to ``NULL`` (a ``TODO`` note says which) and
``unsupported`` when nothing could be translated.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Dialect = Literal["sqlite", "postgres"]
DIALECTS: tuple[Dialect, ...] = ("sqlite", "postgres")
TranslationStatus = Literal["ok", "partial", "unsupported"]


class TranslatedColumn(BaseModel):
    """SQL and pandas equivalents of one derived column's formula."""

    table: str
    column: str
    include: bool = True
    formula: str
    status: TranslationStatus
    sql: dict[str, str] = Field(default_factory=dict)
    pandas: str = ""
    notes: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


class Translation(BaseModel):
    """All translated columns plus the generated artefacts."""

    version: int = 1
    columns: list[TranslatedColumn] = Field(default_factory=list)
    views: dict[str, str] = Field(default_factory=dict)
    script: str = ""

    def counts(self) -> dict[str, int]:
        """Number of columns per status."""
        out = {"ok": 0, "partial": 0, "unsupported": 0}
        for col in self.columns:
            out[col.status] += 1
        return out
