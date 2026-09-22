from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo-data"


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """Every test gets a fresh SQLite database and upload dir."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("DELETE_UPLOADS_AFTER_PROCESSING", "true")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from app import config, db

    config.get_settings.cache_clear()
    db.reset_engine_for_tests()
    yield
    db.reset_engine_for_tests()
    config.get_settings.cache_clear()


@pytest.fixture
def db_session():
    from app.db import get_session_factory, init_db
    from app.services.schema_service import seed_builtin_schemas

    init_db()
    session = get_session_factory()()
    seed_builtin_schemas(session)
    yield session
    session.close()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def customer_schema(db_session):
    from app.services.schema_service import list_schemas

    return next(s for s in list_schemas(db_session) if s.name == "Customer Import v1")


def make_xlsx(
    headers: list[str], rows: list[list], sheets: dict[str, list[list]] | None = None
) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(headers)
    for r in rows:
        ws.append(r)
    for name, data in (sheets or {}).items():
        extra = wb.create_sheet(name)
        for r in data:
            extra.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_csv(
    headers: list[str], rows: list[list], delimiter: str = ",", encoding: str = "utf-8"
) -> bytes:
    import csv

    buf = io.StringIO(newline="")
    w = csv.writer(buf, delimiter=delimiter)
    w.writerow(headers)
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode(encoding)


os.environ.setdefault("PYTHONHASHSEED", "0")
