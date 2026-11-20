"""Mapa de calor de crédito: permissões, importação sem dado pessoal e privacidade da API."""

import json
import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import CreditMapAggregate


def _login(client: TestClient) -> None:
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    client.post(
        "/login",
        data={"email": "admin@sulnet.local", "password": "Sulnet@2026!", "csrf_token": token},
        follow_redirects=False,
    )


def _csv() -> bytes:
    # Valores fictícios; nenhum dado real.
    rows = ["Protocolo;Documento;Cidade;Bairro;Data da consulta;Status;Contrato assinado"]
    for i in range(12):
        rows.append(f"T-{i};00000000191;SANTO ANGELO;CENTRO;05/09/2026 10:00;Aprovado;Sim")
    for i in range(12, 14):
        rows.append(f"T-{i};00000000191;SANTO ANGELO;CENTRO;05/09/2026 10:00;Reprovado;Não")
    rows.append("T-90;00000000191;SANTO ANGELO;BAIRRO PEQUENO;05/09/2026 10:00;Aprovado;Não")
    return ("\n".join(rows) + "\n").encode()


def test_api_exige_login_e_pagina_redireciona():
    with TestClient(app) as client:
        assert client.get("/api/reports/credit-map").status_code == 401
        page = client.get("/reports/credit-map", follow_redirects=False)
        assert page.status_code == 303 and page.headers["location"] == "/login"


def test_admin_importa_e_api_oculta_bairro_pequeno_sem_guardar_dado_pessoal():
    with TestClient(app) as client:
        _login(client)
        page = client.get("/reports/credit-map")
        assert page.status_code == 200
        csp = page.headers["content-security-policy"]
        directives = {d.split()[0]: d.split()[1:] for d in csp.split(";") if d.strip()}
        assert "script-src" in directives
        assert any(src == "https://unpkg.com" for src in directives["script-src"])
        assert not any(src.startswith("https://maps.") for src in directives["script-src"])
        token = re.search(r'data-csrf="([^"]*)"', page.text).group(1)

        sem_csrf = client.post(
            "/api/reports/credit-map/import",
            files={"file": ("a.csv", _csv(), "text/csv")},
        )
        assert sem_csrf.status_code in (400, 403)

        ok = client.post(
            "/api/reports/credit-map/import",
            files={"file": ("a.csv", _csv(), "text/csv")},
            headers={"x-csrf-token": token},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["aggregates"] >= 1

        data = client.get("/api/reports/credit-map?city=SANTO ANGELO&date_from=2026-08&date_to=2026-11").json()
        assert data["totals"]["total"] == 15
        names = [row["name"] for row in data["neighborhoods"]]
        assert names == ["CENTRO"]
        assert data["suppressed_neighborhoods"] == 1
        assert "00000000191" not in str(data)

    with SessionLocal() as db:
        rows = list(db.scalars(select(CreditMapAggregate)))
        assert rows and all(set(vars(r)) >= {"city", "neighborhood", "count"} for r in rows)
        assert "00000000191" not in str([vars(r) for r in rows])


def test_importacao_rejeita_arquivo_que_nao_e_csv():
    with TestClient(app) as client:
        _login(client)
        page = client.get("/reports/credit-map")
        token = re.search(r'data-csrf="([^"]*)"', page.text).group(1)
        resp = client.post(
            "/api/reports/credit-map/import",
            files={"file": ("a.txt", b"x", "text/plain")},
            headers={"x-csrf-token": token},
        )
        assert resp.status_code == 400


def test_base_nap_exige_sessao_e_preserva_leitura_anterior_se_importacao_falhar():
    endpoint = "/api/reports/credit-map/network"
    point = {
        "type": "Feature", "id": 42,
        "geometry": {"type": "Point", "coordinates": [-54.261, -28.301]},
        "properties": {
            "nome": "NAP EXEMPLO", "status": "Parcial",
            "latitude_original": -28.3, "longitude_original": -54.26,
            "portas": 8, "usadas": 3, "livres": 5, "online": 2, "offline": 1,
            "ssid": "CAMPO NÃO UTILIZADO NO MAPA",
        },
    }
    dataset = {
        "type": "FeatureCollection",
        "metadata": {"sessoes_atualizadas_em": "2026-11-20 10:32:08"},
        "features": [point],
    }

    def upload(client, headers=None):
        return client.post(
            endpoint, files={"file": ("nap.geojson", json.dumps(dataset), "application/geo+json")},
            headers=headers,
        )

    with TestClient(app) as client:
        assert client.get(endpoint).status_code == 401
        assert upload(client).status_code == 401
        _login(client)
        page = client.get("/reports/credit-map")
        assert 'id="cm-network-layer"' in page.text
        token = re.search(r'data-csrf="([^"]+)"', page.text).group(1)
        assert upload(client).status_code in (400, 403)
        headers = {"x-csrf-token": token}
        assert upload(client, headers).json()["naps"] == 1
        snapshot = client.get(endpoint).json()
        assert snapshot["sessions_at"] == "2026-11-20T10:32:08"
        assert snapshot["naps"][0]["original_lat"] == -28.3
        assert snapshot["naps"][0]["lat"] == -28.301
        assert snapshot["naps"][0]["livres"] == 5
        assert "ssid" not in snapshot["naps"][0]

        dataset["features"] = [point, point]
        assert upload(client, headers).status_code == 400
        assert client.get(endpoint).json() == snapshot

        dataset["features"] = [point]
        point["properties"].update({"usadas": 4, "livres": 4})
        assert upload(client, headers).status_code == 200
        assert client.get(endpoint).json()["naps"][0]["livres"] == 4
        assert client.get("/api/reports/credit-map").status_code == 200
