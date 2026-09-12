"""Flask application factory."""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask

from exmigrate.web.api import api_bp
from exmigrate.web.jobs import JobStore
from exmigrate.web.pages import pages_bp

MAX_UPLOAD_BYTES = 200 * 1024 * 1024


def create_app(jobs_root: str | Path | None = None) -> Flask:
    """Build the Flask app; jobs live under ``jobs_root`` (default ``./jobs``)."""
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    root = Path(jobs_root or os.environ.get("EXMIGRATE_JOBS_DIR") or "jobs")
    app.extensions["job_store"] = JobStore(root)
    app.register_blueprint(api_bp)
    app.register_blueprint(pages_bp)
    return app


def main() -> None:
    """Run the development server."""
    create_app().run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)


if __name__ == "__main__":
    main()
