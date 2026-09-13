from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from flask import Flask
from flask.testing import FlaskClient

from exmigrate.web import create_app
from fixtures.clean_three_table import generate as generate_clean
from fixtures.cross_file import generate as generate_cross
from fixtures.large_sheet import generate as generate_large


@pytest.fixture(scope="session")
def clean_workbook(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return generate_clean(tmp_path_factory.mktemp("fx") / "clean.xlsx")


@pytest.fixture(scope="session")
def large_workbook(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return generate_large(tmp_path_factory.mktemp("fx") / "large.xlsx")


@pytest.fixture(scope="session")
def cross_file_workbooks(tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
    return generate_cross(tmp_path_factory.mktemp("fx") / "cross")


@pytest.fixture()
def app(tmp_path: Path) -> Flask:
    app = create_app(jobs_root=tmp_path / "jobs")
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app: Flask) -> Iterator[FlaskClient]:
    with app.test_client() as c:
        yield c


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    dsn = os.environ.get("PG_DSN_DEFAULT")
    if not dsn:
        pytest.skip("PG_DSN_DEFAULT not set")
    return dsn
