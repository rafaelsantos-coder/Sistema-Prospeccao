"""O provedor Google Maps e opcional e preserva OpenStreetMap por padrao."""
from dataclasses import replace
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.routers import credit_map
from app import security_middleware
from tests.test_credit_map_integration import _login


def _switch_provider(monkeypatch, provider: str, key: str = "") -> None:
    # Settings e um dataclass frozen, portanto substituimos a referencia de cada modulo.
    conf = replace(settings, credit_map_provider=provider, google_maps_browser_key=key, google_maps_map_id="DEMO_MAP_ID")
    monkeypatch.setattr(credit_map, "settings", conf)
    monkeypatch.setattr(security_middleware, "settings", conf)


def _script_sources(response) -> list[str]:
    csp = response.headers["content-security-policy"]
    script = next(item for item in csp.split(";") if item.strip().startswith("script-src "))
    return script.strip().split()[1:]


def test_mapa_osm_por_padrao_sem_chave(monkeypatch):
    _switch_provider(monkeypatch, "openstreetmap")
    with TestClient(app) as client:
        _login(client)
        response = client.get("/reports/credit-map")
        assert response.status_code == 200
        assert 'data-map-provider="osm"' in response.text
        assert "data-google-maps-key" not in response.text
        assert "maps.googleapis.com" not in [urlsplit(source).hostname for source in _script_sources(response)]


def test_mapa_google_habilita_csp_somente_no_painel(monkeypatch):
    _switch_provider(monkeypatch, "google", "test-key-not-real")
    with TestClient(app) as client:
        _login(client)
        response = client.get("/reports/credit-map")
        assert response.status_code == 200
        assert 'data-map-provider="google"' in response.text
        assert 'data-google-maps-key="test-key-not-real"' in response.text
        assert "maps.googleapis.com" in [urlsplit(source).hostname for source in _script_sources(response)]
        assert "maps.gstatic.com" in [urlsplit(source).hostname for source in _script_sources(response)]
        assert "data-google-maps-nonce=" in response.text
        other = client.get("/dashboard")
        assert "maps.googleapis.com" not in [urlsplit(source).hostname for source in _script_sources(other)]


def test_google_pede_chave_sem_habilitar_por_engano(monkeypatch):
    _switch_provider(monkeypatch, "google")
    with TestClient(app) as client:
        _login(client)
        response = client.get("/reports/credit-map")
        assert 'data-map-provider="osm"' in response.text
