"""Verificacao de HTML e permissao dos novos modos de mapa de calor."""
from fastapi.testclient import TestClient

from app.main import app
from tests.test_credit_map_integration import _login


def test_pagina_do_mapa_contem_todas_as_camadas_com_avisos_de_precisao():
    with TestClient(app) as client:
        _login(client)
        response = client.get("/reports/credit-map")
        assert response.status_code == 200
        html = response.text
        for mode in ("approved", "rejected", "compare", "triangles"):
            assert 'data-map-mode="' + mode + '"' in html
        assert 'id="cm-neighborhood"' in html
        assert "credit_map_heat.js" in html
        assert html.index("credit_map_heat.js") < html.index("credit_map.js")
        assert "Calor estimado por centros de bairro e rua" in html
        assert "Triângulos = GPS informado no CSV" in html
        assert 'id="cm-streets" checked' not in html
        assert 'id="cm-addresses" checked' not in html


def test_camada_calor_nao_abre_api_sem_sessao():
    with TestClient(app) as client:
        for path in ("/api/reports/credit-map", "/api/reports/credit-map/geo",
                     "/api/reports/credit-map/streets", "/api/reports/credit-map/addresses"):
            response = client.get(path)
            assert response.status_code == 401, (path, response.status_code)
