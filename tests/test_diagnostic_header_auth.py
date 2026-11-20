from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import diagnostico


TOKEN = "synthetic-diagnostic-test-value"


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(diagnostico.router)
    app.dependency_overrides[diagnostico.get_db] = lambda: object()
    monkeypatch.setattr(diagnostico, "settings", SimpleNamespace(diagnostics_secret=TOKEN))
    monkeypatch.setattr(diagnostico, "_snapshot", lambda db: {"status": "ok"})
    return TestClient(app)


@pytest.mark.parametrize("path,headers", [
    ("/internal/diag", {"Authorization": f"Bearer {TOKEN}"}),
    (f"/internal/diag/{TOKEN}", {}),
])
def test_diagnostic_transports(client, path, headers):
    response = client.get(path, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer synthetic-invalid"},
    {"Authorization": f"Basic {TOKEN}"},
])
def test_diagnostic_requires_credential(client, headers):
    response = client.get("/internal/diag", headers=headers)
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"


def test_diagnostic_requires_configured_secret(client, monkeypatch):
    monkeypatch.setattr(diagnostico, "settings", SimpleNamespace(diagnostics_secret=""))
    response = client.get("/internal/diag", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 404
