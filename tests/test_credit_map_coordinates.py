"""Coordenadas CSV da fonte: somente aprovados com GPS valido viram triangulos."""
import re

from fastapi.testclient import TestClient

from app.main import app
from app.services.credit_map import aggregate_csv_addresses, parse_coordinates
from tests.test_credit_map_integration import _login


def _source_csv():
    rows = [
        "Protocolo;Documento;Cidade;Bairro;Rua;Número;CEP;Coordenadas;Data da consulta;Status;Contrato assinado",
        "Q-1;00000000191;SANTO ANGELO;CENTRO;Rua das Flores;123;98800-000;-28.3012345, -54.2634567;05/09/2026;Aprovado;Sim",
        "Q-2;00000000191;SANTO ANGELO;CENTRO;Rua das Flores;124;98800-000;-28.3012350, -54.2634570;05/09/2026;Aprovado;Não",
        "Q-3;00000000191;SANTO ANGELO;CENTRO;Rua das Flores;125;98800-000;;05/09/2026;Aprovado;Não",
        "Q-4;00000000191;SANTO ANGELO;CENTRO;Rua das Flores;126;98800-000;00000;05/09/2026;Liberado com taxa;Não",
        "Q-5;00000000191;SANTO ANGELO;CENTRO;Rua das Flores;127;98800-000;-28.3012350, -54.2634570;05/09/2026;Reprovado;Não",
        "Q-6;00000000191;SANTO ANGELO;CENTRO;Rua das Flores;128;98800-000;28°08'52.7\"S 54°44'28.2\"W;05/09/2026;Liberado comprovante provedor;Sim",
    ]
    return ("\n".join(rows) + "\n").encode()


def test_coordinate_parser_accepts_decimal_dms_and_extra_altitude():
    assert parse_coordinates("-28.3012345, -54.2634567") == (-28.3012345, -54.2634567)
    assert parse_coordinates("-28.3066886,-54.2806614,831") == (-28.3066886, -54.2806614)
    assert parse_coordinates("28°08'52.7\"S 54°44'28.2\"W") is not None
    assert parse_coordinates('27°52\'25.7"S 54°29\'27.6"W · -27.873812, -54.491006') == (-27.873812, -54.491006)
    assert parse_coordinates("Não quero") is None
    assert parse_coordinates("00000") is None
    assert parse_coordinates("27.8648970, -54.4791130") is None
    assert parse_coordinates("-28.319020936824085-5426816024232819") is None


def test_import_with_q_only_gps_valid_from_approved_but_all_in_totals():
    aggregates, streets, addresses, stats = aggregate_csv_addresses(_source_csv())
    assert stats["cpf_validos_formato"] == 6
    assert stats["aprovados_com_coordenadas_validas"] == 3
    assert stats["aprovados_sem_coordenadas"] == 1
    assert stats["aprovados_coordenadas_invalidas"] == 1
    assert stats["enderecos_aprovados"] == 3
    assert len(addresses) == 3
    assert sum(r["count"] for r in aggregates) == 6
    assert all(r["lat"] is not None and r["lng"] is not None for r in addresses)
    assert "00000000191" not in str(addresses)
    assert "Q-1" not in str(addresses)
    assert any(r["number"] == "123" and r["signed"] for r in addresses)
    assert not any(r["number"] in {"125", "126", "127"} for r in addresses)


def test_api_uses_source_gps_without_geocoding(monkeypatch):
    from app.services import geocoding

    def forbidden(*args, **kwargs):
        raise AssertionError("Nao deve geocodificar o marcador ja fornecido pelo CRM")

    monkeypatch.setattr(geocoding, "address_point", forbidden)
    with TestClient(app) as client:
        _login(client)
        page = client.get("/reports/credit-map")
        csrf = re.search(r'data-csrf="([^"]*)"', page.text).group(1)
        upload = client.post(
            "/api/reports/credit-map/import",
            files={"file": ("dados.csv", _source_csv(), "text/csv")},
            headers={"x-csrf-token": csrf},
        )
        assert upload.status_code == 200, upload.text
        assert upload.json()["stats"]["aprovados_com_coordenadas_validas"] == 3
        api = client.get("/api/reports/credit-map/addresses?city=SANTO ANGELO&date_from=2026-08&date_to=2026-11")
        assert api.status_code == 200, api.text
        data = api.json()
        assert data["pending"] == 0
        assert data["total"] == 3
        assert data["source_coordinates"] == 3
        assert data["signed"] == 2 and data["unsigned"] == 1
        assert len(data["addresses"]) == 3
        assert all(r["source_coordinates"] for r in data["addresses"])
        assert set((r["lat"], r["lng"]) for r in data["addresses"]) >= {(-28.3012345, -54.2634567)}
        assert "00000000191" not in str(data)
