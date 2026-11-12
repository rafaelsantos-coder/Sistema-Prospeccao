"""Mapa de crédito com OpenStreetMap: geocodificação no servidor, com cache e validação de distância."""

import re

from fastapi.testclient import TestClient

from app.main import app
from app.services import geocoding
from tests.test_credit_map_integration import _csv, _login

CITY_CENTER = (-28.2990, -54.2630)
NEAR = (-28.3010, -54.2640)
FAR = (-23.5500, -46.6300)


def _import(client):
    page = client.get("/reports/credit-map")
    token = re.search(r'data-csrf="([^"]*)"', page.text).group(1)
    resp = client.post(
        "/api/reports/credit-map/import",
        files={"file": ("analises.csv", _csv(), "text/csv")},
        headers={"x-csrf-token": token},
    )
    assert resp.status_code == 200


def _fake(calls, neighborhood_point):
    def fake(query):
        calls.append(query)
        if query.startswith("CENTRO"):
            return (*neighborhood_point, "Centro")
        return (*CITY_CENTER, "Santo Ângelo")

    return fake


def test_pagina_nao_depende_de_chave_do_google():
    with TestClient(app) as client:
        _login(client)
        page = client.get("/reports/credit-map")
        assert "data-maps-key" not in page.text and "googleapis" not in page.text


def test_geo_localiza_bairro_e_guarda_no_banco(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(geocoding, "_throttle", lambda: None)
    monkeypatch.setattr(geocoding, "search", _fake(calls, NEAR))
    with TestClient(app) as client:
        _login(client)
        _import(client)
        query = "/api/reports/credit-map/geo?city=SANTO ANGELO&date_from=2026-08&date_to=2026-11"
        first = client.get(query).json()
        assert first["pending"] == 0 and first["error"] == ""
        assert first["positions"]["CENTRO"] == {"lat": NEAR[0], "lng": NEAR[1]}
        assert first["city_center"]["lat"] == CITY_CENTER[0]
        assert "BAIRRO PEQUENO" not in first["positions"]  # menos de 10 consultas: nunca exposto
        used = len(calls)
        second = client.get(query).json()
        assert second["positions"] == first["positions"]
        assert len(calls) == used, "resultado em cache não consulta o serviço de novo"


def test_geo_descarta_resultado_longe_da_cidade(monkeypatch):
    monkeypatch.setattr(geocoding, "_throttle", lambda: None)
    monkeypatch.setattr(geocoding, "search", _fake([], FAR))
    with TestClient(app) as client:
        _login(client)
        _import(client)
        data = client.get(
            "/api/reports/credit-map/geo?city=SANTO ANGELO&date_from=2026-08&date_to=2026-11"
        ).json()
        assert "CENTRO" not in data["positions"] and data["not_found"] == ["CENTRO"]
        assert data["pending"] == 0


def test_geo_exige_login():
    with TestClient(app) as client:
        assert client.get("/api/reports/credit-map/geo").status_code == 401


def test_distancia_em_km():
    assert geocoding.distance_km(CITY_CENTER, NEAR) < 1
    assert geocoding.distance_km(CITY_CENTER, FAR) > 700
