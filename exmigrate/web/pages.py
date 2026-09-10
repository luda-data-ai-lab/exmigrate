"""HTML pages: upload → review → target → report."""

from __future__ import annotations

from flask import Blueprint, render_template

pages_bp = Blueprint("pages", __name__)


@pages_bp.get("/")
def upload_page() -> str:
    """Drag-and-drop upload."""
    return render_template("upload.html")


@pages_bp.get("/jobs/<job_id>/review")
def review_page(job_id: str) -> str:
    """Editable schema review."""
    return render_template("review.html", job_id=job_id)


@pages_bp.get("/jobs/<job_id>/target")
def target_page(job_id: str) -> str:
    """Target selection and migration trigger."""
    return render_template("target.html", job_id=job_id)


@pages_bp.get("/jobs/<job_id>/report")
def report_page(job_id: str) -> str:
    """Migration report and artifact downloads."""
    return render_template("report.html", job_id=job_id)
