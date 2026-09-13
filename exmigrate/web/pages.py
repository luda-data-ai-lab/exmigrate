"""HTML pages: upload → review → target → report, plus the job history and manual."""

from __future__ import annotations

from typing import cast

from flask import Blueprint, abort, current_app, render_template

from exmigrate.web.jobs import JobStore

pages_bp = Blueprint("pages", __name__)


def _require_job(job_id: str) -> None:
    store = cast(JobStore, current_app.extensions["job_store"])
    if not store.exists(job_id):
        abort(404, description="job not found")


@pages_bp.get("/")
def upload_page() -> str:
    """Drag-and-drop upload."""
    return render_template("upload.html")


@pages_bp.get("/jobs")
def history_page() -> str:
    """List of past jobs."""
    return render_template("history.html")


@pages_bp.get("/manual")
def manual_page() -> str:
    """User manual rendered from ``static/manual.md``."""
    return render_template("manual.html")


@pages_bp.get("/jobs/<job_id>/review")
def review_page(job_id: str) -> str:
    """Editable schema review."""
    _require_job(job_id)
    return render_template("review.html", job_id=job_id)


@pages_bp.get("/jobs/<job_id>/target")
def target_page(job_id: str) -> str:
    """Target selection and migration trigger."""
    _require_job(job_id)
    return render_template("target.html", job_id=job_id)


@pages_bp.get("/jobs/<job_id>/report")
def report_page(job_id: str) -> str:
    """Migration report and artifact downloads."""
    _require_job(job_id)
    return render_template("report.html", job_id=job_id)
