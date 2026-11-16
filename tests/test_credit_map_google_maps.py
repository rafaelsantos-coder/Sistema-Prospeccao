"""O provedor Google Maps eh opcional e preserva OpenStreetMap por padrao."""
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from tests.test_credit_map_integration import _login


def test_mapa_osm_por_padrao_sem_chave(monkeypatch):
    monkeypatch.setattr(settings, "credit_map_provider", "openstreetmap")
    monkeypatch.setattr(settings, "google_maps_browser_key", "")
    with TestClient(app) as client:
        _login(client)
        response = client.get("/reports/credit-map")
        assert response.status_code == 200
        assert 'data-map-provider="osm"' in response.text
        assert "data-google-maps-key" not in response.text
        assert "https://maps.googleapis.com" not in response.headers["content-security-policy"]


def test_mapa_google_habilita_csp_somente_no_painel(monkeypatch):
    monkeypatch.setattr(settings, "credit_map_provider", "google")
    monkeypatch.setattr(settings, "google_maps_browser_key", "test-key-not-real")
    monkeypatch.setattr(settings, "google_maps_map_id", "DEMO_MAP_ID")
    with TestClient(app) as client:
        _login(client)
        response = client.get("/reports/credit-map")
        assert response.status_code == 200
        assert 'data-map-provider="google"' in response.text
        assert 'data-google-maps-key="test-key-not-real"' in response.text
        assert "https://maps.googleapis.com" in response.headers["content-security-policy"]
        assert "https://maps.gstatic.com" in response.headers["content-security-policy"]
        assert "data-google-maps-nonce=" in response.text
        other = client.get("/dashboard")
        assert "https://maps.googleapis.com" not in other.headers["content-security-policy"]


def test_google_pede_chave_sem_habilitar_por_engano(monkeypatch):
    monkeypatch.setattr(settings, "credit_map_provider", "google")
    monkeypatch.setattr(settings, "google_maps_browser_key", "")
    with TestClient(app) as client:
        _login(client)
        response = client.get("/reports/credit-map")
        assert 'data-map-provider="osm"' in response.text
