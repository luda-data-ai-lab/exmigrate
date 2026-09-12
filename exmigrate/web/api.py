"""REST API implementing ``contracts/openapi.yaml``."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from flask import Blueprint, Response, current_app, jsonify, request, send_from_directory
from pydantic import ValidationError
from werkzeug.utils import secure_filename

from exmigrate.analyzer import analyze_with_data, bind_data
from exmigrate.contracts.ir import SchemaIR
from exmigrate.erd import to_mermaid
from exmigrate.service import TARGETS, run_migration
from exmigrate.web.jobs import JobNotFound, JobStatus, JobStore

api_bp = Blueprint("api", __name__, url_prefix="/api")
ALLOWED_SUFFIXES = {".xlsx", ".xlsm"}


def store() -> JobStore:
    """The app's job store."""
    return cast(JobStore, current_app.extensions["job_store"])


@api_bp.errorhandler(JobNotFound)
def _not_found(_exc: JobNotFound) -> tuple[Response, int]:
    return jsonify({"error": "job not found"}), 404


@api_bp.post("/upload")
def upload() -> tuple[Response, int]:
    """Accept workbooks, analyze them and create a job."""
    files = [f for f in request.files.getlist("files") if f.filename]
    if not files:
        return jsonify({"error": "no files supplied"}), 400
    job_store = store()
    job_id = job_store.create()
    uploads = job_store.uploads_dir(job_id)
    saved: list[Path] = []
    for f in files:
        name = secure_filename(f.filename or "") or "workbook.xlsx"
        if Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
            continue
        dest = uploads / name
        f.save(dest)
        saved.append(dest)
    if not saved:
        job_store.delete(job_id)
        return jsonify({"error": "no .xlsx files supplied"}), 400

    result = analyze_with_data(saved)
    job_store.save_ir(job_id, result.ir)
    job_store.save_data(job_id, result.frames)
    status = JobStatus(
        job_id=job_id,
        state="analyzed",
        created_at=datetime.now(tz=timezone.utc),
        files=[p.name for p in saved],
        tables=[t.name for t in result.ir.tables],
        issues=result.issues,
    )
    job_store.save_status(job_id, status)
    return jsonify({"job_id": job_id, "issues": [i.model_dump() for i in result.issues]}), 201


@api_bp.get("/jobs")
def list_jobs() -> Response:
    """Return a summary of every job, newest first."""
    return jsonify([_summary(s) for s in store().list_jobs()])


@api_bp.delete("/jobs/<job_id>")
def delete_job(job_id: str) -> tuple[str, int]:
    """Remove a job and all of its files."""
    job_store = store()
    job_store.path(job_id)
    job_store.delete(job_id)
    return "", 204


@api_bp.get("/jobs/<job_id>/schema")
def get_schema(job_id: str) -> Response:
    """Return the Schema IR."""
    return jsonify(store().load_ir(job_id).model_dump(mode="json"))


@api_bp.put("/jobs/<job_id>/schema")
def put_schema(job_id: str) -> tuple[Response, int]:
    """Persist user edits to the Schema IR."""
    job_store = store()
    job_store.path(job_id)
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"error": "body must be JSON"}), 400
    try:
        ir = SchemaIR.model_validate(payload)
    except ValidationError as exc:
        return jsonify({"error": "invalid schema", "details": exc.errors()}), 400
    frames = job_store.load_data(job_id)
    try:
        bind_data(ir, frames)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    job_store.save_ir(job_id, ir)
    status = job_store.load_status(job_id)
    status.tables = [t.name for t in ir.tables]
    job_store.save_status(job_id, status)
    return jsonify(ir.model_dump(mode="json")), 200


@api_bp.get("/jobs/<job_id>/erd")
def get_erd(job_id: str) -> Response:
    """Return the Mermaid ER diagram for the current (possibly edited) IR."""
    text = to_mermaid(store().load_ir(job_id))
    return Response(text, mimetype="text/plain; charset=utf-8")


@api_bp.post("/jobs/<job_id>/migrate")
def migrate(job_id: str) -> tuple[Response, int]:
    """Run the migration for the requested targets."""
    job_store = store()
    status = job_store.load_status(job_id)
    payload = request.get_json(silent=True) or {}
    targets = payload.get("targets")
    configs = payload.get("configs") or {}
    if not isinstance(targets, list) or not targets:
        return jsonify({"error": "targets must be a non-empty list"}), 400
    unknown = [t for t in targets if t not in TARGETS]
    if unknown:
        return jsonify({"error": f"unknown targets: {unknown}"}), 400
    if not isinstance(configs, dict):
        return jsonify({"error": "configs must be an object"}), 400

    status.state = "migrating"
    status.reports = []
    status.error = None
    job_store.save_status(job_id, status)
    try:
        ir = job_store.load_ir(job_id)
        data = bind_data(ir, job_store.load_data(job_id))
        status.reports = run_migration(ir, data, targets, configs, job_store.artifacts_dir(job_id))
        status.state = "done" if all(r.ok for r in status.reports) else "failed"
    except Exception as exc:  # noqa: BLE001 - surfaced in status
        status.state = "failed"
        status.error = str(exc)
    job_store.save_status(job_id, status)
    return jsonify(_status_payload(status)), 202


@api_bp.get("/jobs/<job_id>/status")
def get_status(job_id: str) -> Response:
    """Return job state and reports."""
    return jsonify(_status_payload(store().load_status(job_id)))


@api_bp.get("/jobs/<job_id>/artifacts/<name>")
def get_artifact(job_id: str, name: str) -> Response:
    """Download a produced artifact."""
    artifacts = store().artifacts_dir(job_id)
    return send_from_directory(artifacts, name, as_attachment=True)


def _summary(status: JobStatus) -> dict[str, object]:
    return {
        "job_id": status.job_id,
        "state": status.state,
        "created_at": status.created_at.isoformat() if status.created_at else None,
        "files": status.files,
        "tables": status.tables,
        "targets": [r.target for r in status.reports],
        "error": status.error,
    }


def _status_payload(status: JobStatus) -> dict[str, object]:
    payload = status.model_dump(mode="json")
    payload["reports"] = [
        {**r.model_dump(mode="json"), "artifacts": [Path(a).name for a in r.artifacts]}
        for r in status.reports
    ]
    return payload
