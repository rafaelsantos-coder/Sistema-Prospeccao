"""Mapa de crédito: colunas Rua/CEP/Número, camada de ruas e modo todas as cidades."""

import re

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import CreditMapGeocode
from app.services import geocoding
from app.services.credit_map import aggregate_csv, aggregate_csv_full, cep_digits, street_name

HEADER = "Protocolo;Documento;Cidade;Bairro;Rua;Número;CEP;Data da consulta;Status;Contrato assinado"


def _csv() -> bytes:
    rows = [HEADER]
    for i in range(11):
        rows.append(f"R-{i};00000000191;SANTA ROSA;CENTRO;Rua das Flores;{100 + i};98900-000;05/09/2026 10:00;Aprovado;Sim")
    rows.append("R-50;00000000191;SANTA ROSA;CENTRO;Rua Rara, 12;12;98900-111;05/09/2026 10:00;Aprovado;Não")
    return ("\n".join(rows) + "\n").encode()


def test_street_and_cep_helpers_drop_house_number():
    assert street_name("Rua das Flores, 123") == "RUA DAS FLORES"
    assert street_name("Av. Brasil 45A") == "AV. BRASIL"
    assert street_name("Rua 7 de Setembro") == "RUA 7 DE SETEMBRO"
    assert cep_digits("98900-000") == "98900000"
    assert cep_digits("123") == ""


def test_csv_without_street_columns_still_imports():
    legacy = (
        "Protocolo;Documento;Cidade;Bairro;Data da consulta;Status;Contrato assinado\n"
        "A-1;00000000191;SANTA ROSA;CENTRO;05/09/2026 10:00;Aprovado;Sim\n"
    ).encode()
    aggregates, streets, stats = aggregate_csv_full(legacy)
    assert aggregates and streets == []
    assert aggregate_csv(legacy)[0] == aggregates


def test_street_aggregates_never_keep_house_number_or_cpf():
    _aggregates, streets, stats = aggregate_csv_full(_csv())
    assert stats["com_rua"] == 12
    assert {row["street"] for row in streets} == {"RUA DAS FLORES", "RUA RARA"}
    assert "00000000191" not in str(streets)
    assert not any(re.search(r"\b10\d\b", row["street"]) for row in streets)


def test_streets_endpoint_only_returns_streets_with_ten_or_more(monkeypatch):
    monkeypatch.setattr(geocoding, "city_center", lambda city: (-27.87, -54.48))
    monkeypatch.setattr(geocoding, "street_point", lambda *args, **kwargs: (-27.871, -54.481))
    with TestClient(app) as client:
        page = client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        client.post(
            "/login",
            data={"email": "admin@sulnet.local", "password": "Sulnet@2026!", "csrf_token": token},
            follow_redirects=False,
        )
        with SessionLocal() as db:
            db.query(CreditMapGeocode).delete()
            db.commit()
        csrf = re.search(r'data-csrf="([^"]*)"', client.get("/reports/credit-map").text).group(1)
        ok = client.post(
            "/api/reports/credit-map/import",
            files={"file": ("a.csv", _csv(), "text/csv")},
            headers={"x-csrf-token": csrf},
        )
        assert ok.status_code == 200, ok.text
        query = "city=SANTA ROSA&date_from=2026-08&date_to=2026-11"
        client.get("/api/reports/credit-map/geo?" + query)  # grava o centro da cidade
        data = client.get("/api/reports/credit-map/streets?" + query).json()
        names = [row["street"] for row in data["streets"]]
        assert names == ["RUA DAS FLORES"]
        assert data["streets"][0]["cep"] == "98900000"
        assert "00000000191" not in str(data)
