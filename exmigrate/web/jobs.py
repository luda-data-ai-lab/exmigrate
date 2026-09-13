"""Filesystem-backed job store: IR JSON + parquet cache per job."""

from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import pandas as pd
from pydantic import BaseModel, Field

from exmigrate.contracts.adapter import Issue, MigrationReport
from exmigrate.contracts.formulas import FormulaInventory
from exmigrate.contracts.ir import SchemaIR
from exmigrate.contracts.lineage import LineageIR

JobState = Literal["analyzed", "migrating", "done", "failed"]
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class JobStatus(BaseModel):
    """Persisted job status document."""

    job_id: str
    state: JobState = "analyzed"
    created_at: datetime | None = None
    created_by: str | None = None
    files: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    reports: list[MigrationReport] = Field(default_factory=list)
    error: str | None = None


class JobNotFound(KeyError):
    """Raised when a job id does not exist."""


class JobStore:
    """Persist jobs under ``root/<job_id>/``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self) -> str:
        """Allocate a new job directory and return its id."""
        job_id = uuid.uuid4().hex[:12]
        (self.root / job_id / "uploads").mkdir(parents=True)
        (self.root / job_id / "data").mkdir()
        (self.root / job_id / "artifacts").mkdir()
        return job_id

    def exists(self, job_id: str) -> bool:
        """Whether ``job_id`` has a directory."""
        if not job_id or job_id in {".", ".."} or _SAFE.sub("", job_id) != job_id:
            return False
        path = self.root / job_id
        return path.is_dir() and path.resolve().parent == self.root.resolve()

    def path(self, job_id: str) -> Path:
        """Job directory, raising ``JobNotFound`` if missing."""
        if not self.exists(job_id):
            raise JobNotFound(job_id)
        return self.root / job_id

    def uploads_dir(self, job_id: str) -> Path:
        """Directory holding the uploaded workbooks."""
        return self.path(job_id) / "uploads"

    def artifacts_dir(self, job_id: str) -> Path:
        """Directory holding produced artifacts."""
        return self.path(job_id) / "artifacts"

    def save_ir(self, job_id: str, ir: SchemaIR) -> None:
        """Write the Schema IR as JSON."""
        (self.path(job_id) / "schema.json").write_text(
            ir.model_dump_json(indent=2), encoding="utf-8"
        )

    def load_ir(self, job_id: str) -> SchemaIR:
        """Read the Schema IR."""
        raw = (self.path(job_id) / "schema.json").read_text(encoding="utf-8")
        return SchemaIR.model_validate_json(raw)

    def save_formulas(self, job_id: str, inventory: FormulaInventory) -> None:
        """Write the formula inventory as JSON."""
        (self.path(job_id) / "formulas.json").write_text(
            inventory.model_dump_json(indent=2), encoding="utf-8"
        )

    def load_formulas(self, job_id: str) -> FormulaInventory:
        """Read the formula inventory (empty for jobs analyzed before it existed)."""
        file = self.path(job_id) / "formulas.json"
        if not file.is_file():
            return FormulaInventory()
        return FormulaInventory.model_validate_json(file.read_text(encoding="utf-8"))

    def save_lineage(self, job_id: str, lineage: LineageIR) -> None:
        """Write the column-level Lineage IR as JSON."""
        (self.path(job_id) / "lineage.json").write_text(
            lineage.model_dump_json(indent=2, by_alias=True), encoding="utf-8"
        )

    def load_lineage(self, job_id: str) -> LineageIR:
        """Read the Lineage IR (empty for jobs analyzed before it existed)."""
        file = self.path(job_id) / "lineage.json"
        if not file.is_file():
            return LineageIR()
        return LineageIR.model_validate_json(file.read_text(encoding="utf-8"))

    def save_status(self, job_id: str, status: JobStatus) -> None:
        """Write the status document."""
        (self.path(job_id) / "status.json").write_text(
            status.model_dump_json(indent=2), encoding="utf-8"
        )

    def load_status(self, job_id: str) -> JobStatus:
        """Read the status document.

        ``created_at`` falls back to the status file's mtime for jobs written
        before the field existed.
        """
        status_path = self.path(job_id) / "status.json"
        status = JobStatus.model_validate_json(status_path.read_text(encoding="utf-8"))
        if status.created_at is None:
            status.created_at = datetime.fromtimestamp(status_path.stat().st_mtime, tz=timezone.utc)
        return status

    def list_jobs(self) -> list[JobStatus]:
        """All jobs that have a status document, newest first."""
        out: list[JobStatus] = []
        for entry in self.root.iterdir():
            if entry.is_dir() and (entry / "status.json").is_file():
                out.append(self.load_status(entry.name))
        out.sort(
            key=lambda s: s.created_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True
        )
        return out

    def save_data(self, job_id: str, frames: list[pd.DataFrame]) -> None:
        """Cache sheet data as parquet, one file per table in IR table order.

        Columns are stored positionally (``c0``, ``c1``...) so that user renames
        of tables or columns in the IR never break the mapping back to data.
        """
        data_dir = self.path(job_id) / "data"
        for i, frame in enumerate(frames):
            _parquet_safe(frame).to_parquet(data_dir / f"{i}.parquet", index=False)
        (data_dir / "index.json").write_text(json.dumps(len(frames)), encoding="utf-8")

    def load_data(self, job_id: str) -> list[pd.DataFrame]:
        """Load cached sheet data in IR table order."""
        data_dir = self.path(job_id) / "data"
        count = int(json.loads((data_dir / "index.json").read_text(encoding="utf-8")))
        return [pd.read_parquet(data_dir / f"{i}.parquet") for i in range(count)]

    def delete(self, job_id: str) -> None:
        """Remove a job entirely."""
        shutil.rmtree(self.path(job_id), ignore_errors=True)


def _parquet_safe(frame: pd.DataFrame) -> pd.DataFrame:
    """Coerce mixed-type object columns to strings so pyarrow can serialise them."""
    out = pd.DataFrame(frame.copy())
    out.columns = [f"c{i}" for i in range(len(out.columns))]
    for col in out.columns:
        series = out[col]
        if series.dtype != object:
            continue
        kinds = {type(v) for v in series if v is not None and v == v}
        if len(kinds) > 1:
            out[col] = series.map(lambda v: None if v is None else str(v))
    return out
