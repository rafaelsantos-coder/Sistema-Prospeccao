"""Mapa de crédito: cor por maioria (verde/vermelho) e endereços dos clientes aprovados."""

import re

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import CreditMapAddress, CreditMapGeocode
from app.services import geocoding
from app.services.credit_map import (
    aggregate_csv_addresses,
    house_number,
    majority_color,
    my_maps_csv,
    summarize,
    summarize_addresses,
)
from tests.test_credit_map_integration import _login

HEADER = "Protocolo;Documento;Cidade;Bairro;Rua;Número;CEP;Data da consulta;Status;Contrato assinado"
CITY = "CIDADE TESTE"


def _csv() -> bytes:
    # Valores fictícios; nenhum dado real.
    rows = [HEADER]
    for i in range(6):  # empate no CENTRO: 6 aprovados, 6 reprovados
        signed = "Sim" if i < 4 else "Não"
        rows.append(f"E-{i};00000000191;{CITY};CENTRO;Rua das Flores;{100 + i};98900-000;05/09/2026 10:00;Aprovado;{signed}")
    for i in range(6, 12):
        rows.append(f"E-{i};00000000191;{CITY};CENTRO;Rua das Flores;{100 + i};98900-000;05/09/2026 10:00;Reprovado;Não")
    for i in range(12, 15):  # VILA: 3 aprovados, 8 reprovados
        rows.append(f"E-{i};00000000191;{CITY};VILA;Av. Brasil, {i};;98900-111;05/09/2026 10:00;Aprovado;Não")
    for i in range(15, 23):
        rows.append(f"E-{i};00000000191;{CITY};VILA;Av. Brasil;{i};98900-111;05/09/2026 10:00;Reprovado;Não")
    return ("\n".join(rows) + "\n").encode()


def test_numero_da_casa_vem_da_coluna_ou_do_fim_da_rua():
    assert house_number("123") == "123"
    assert house_number("45a") == "45A"
    assert house_number("", "Rua Rara, 12") == "12"
    assert house_number("", "Rua 7 de Setembro") == ""
    assert house_number("S/N", "Rua X") == ""


def test_cor_por_maioria_e_empate_fica_verde():
    assert majority_color(9, 9) == "green"
    assert majority_color(10, 9) == "green"
    assert majority_color(4, 11) == "red"
    aggregates, _streets, _addresses, _stats = aggregate_csv_addresses(_csv())
    report = summarize(aggregates, city=CITY)
    colors = {row["name"]: row["color"] for row in report["neighborhoods"]}
    assert colors == {"CENTRO": "green", "VILA": "red"}


def test_enderecos_so_de_aprovados_e_sem_cpf():
    _aggregates, _streets, addresses, stats = aggregate_csv_addresses(_csv())
    assert stats["enderecos_aprovados"] == 9
    rows = summarize_addresses(addresses, city=CITY, start_month="2026-08", end_month="2026-11")
    assert len(rows) == 9
    assert sum(1 for row in rows if row["signed"]) == 4
    assert {row["number"] for row in rows if row["street"] == "AVENIDA BRASIL"} == {"12", "13", "14"}
    assert not any(row["number"] in {"106", "111", "15", "22"} for row in rows), "reprovado não vira marcador"
    assert "00000000191" not in str(addresses)
    assert summarize_addresses(addresses, city=CITY, start_month="2026-10", end_month="2026-11") == []


def test_endpoint_de_enderecos_posiciona_e_guarda_em_cache(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_address(street, number, neighborhood, city, cep, center):
        calls.append((street, number))
        exact = street == "RUA DAS FLORES"
        return (-27.87 - len(calls) / 10000, -54.48, exact)

    monkeypatch.setattr(geocoding, "city_center", lambda city: (-27.87, -54.48))
    monkeypatch.setattr(geocoding, "neighborhood_center", lambda *args, **kwargs: (-27.871, -54.481))
    monkeypatch.setattr(geocoding, "street_point", lambda *args, **kwargs: (-27.871, -54.481))
    monkeypatch.setattr(geocoding, "address_point", fake_address)
    query = f"city={CITY}&date_from=2026-08&date_to=2026-11"
    with TestClient(app) as client:
        assert client.get("/api/reports/credit-map/addresses?" + query).status_code == 401
        _login(client)
        with SessionLocal() as db:
            db.query(CreditMapGeocode).delete()
            db.commit()
        page = client.get("/reports/credit-map")
        assert 'id="cm-addresses"' in page.text and "Aprovado sem contrato" in page.text
        csrf = re.search(r'data-csrf="([^"]*)"', page.text).group(1)
        ok = client.post(
            "/api/reports/credit-map/import",
            files={"file": ("a.csv", _csv(), "text/csv")},
            headers={"x-csrf-token": csrf},
        )
        assert ok.status_code == 200, ok.text
        client.get("/api/reports/credit-map/geo?" + query)  # grava o centro da cidade
        data = {}
        for _ in range(6):  # o servidor localiza poucos endereços por chamada
            data = client.get("/api/reports/credit-map/addresses?" + query).json()
            if not data["pending"]:
                break
        assert data["pending"] == 0 and data["error"] == ""
        assert data["total"] == 9 and data["signed"] == 4 and data["unsigned"] == 5
        assert len(data["addresses"]) == 9
        flores = [row for row in data["addresses"] if row["street"] == "RUA DAS FLORES"]
        assert len(flores) == 6 and all(row["exact"] for row in flores)
        assert not any(row["exact"] for row in data["addresses"] if row["street"] == "AVENIDA BRASIL")
        assert "00000000191" not in str(data)
        used = len(calls)
        assert used == 9
        again = client.get("/api/reports/credit-map/addresses?" + query).json()
        assert len(again["addresses"]) == 9 and len(calls) == used, "posição em cache não consulta de novo"

    with SessionLocal() as db:
        saved = db.query(CreditMapAddress).filter(CreditMapAddress.city == CITY).all()
        assert len(saved) == 9
        assert "00000000191" not in str([vars(row) for row in saved])


def test_endereco_usa_meio_da_rua_quando_o_mapa_nao_conhece_o_numero(monkeypatch):
    def fake_detail(query):
        return (-28.30, -54.26, "Rua das Flores", "highway")

    monkeypatch.setattr(geocoding, "search_detail", fake_detail)
    assert geocoding.address_point("RUA DAS FLORES", "100", "CENTRO", "SANTO ÂNGELO", "", (-28.299, -54.263)) == (
        -28.30, -54.26, False)

    monkeypatch.setattr(geocoding, "search_detail", lambda query: (-28.30, -54.26, "Rua das Flores, 100", "building"))
    assert geocoding.address_point("RUA DAS FLORES", "100", "CENTRO", "SANTO ÂNGELO", "", (-28.299, -54.263)) == (
        -28.30, -54.26, True)


def test_csv_do_my_maps_tem_endereco_completo_e_situacao():
    _aggregates, _streets, addresses, _stats = aggregate_csv_addresses(_csv())
    rows = summarize_addresses(addresses, city=CITY, start_month="2026-08", end_month="2026-11")
    lines = my_maps_csv(rows, city=CITY).strip().splitlines()
    assert lines[0] == "Endereço,Situação,Bairro,Clientes"
    assert len(lines) == 1 + 9
    assert '"Rua Das Flores, 100, Centro, Cidade Teste, RS, Brasil",Contrato assinado,Centro,1' in lines
    assert '"Rua Das Flores, 104, Centro, Cidade Teste, RS, Brasil",Aprovado sem contrato,Centro,1' in lines
    assert "00000000191" not in "\n".join(lines)


def test_csv_do_my_maps_neutraliza_formula():
    rows = [{"street": "=CMD", "number": "", "neighborhood": "VILA", "signed": True, "count": 1}]
    assert my_maps_csv(rows, city=CITY).splitlines()[1].startswith("\"'=Cmd")


def test_endpoint_exporta_csv_so_para_quem_importa():
    query = f"city={CITY}&date_from=2026-08&date_to=2026-11"
    with TestClient(app) as client:
        assert client.get("/api/reports/credit-map/export-my-maps?" + query).status_code == 401
        _login(client)
        page = client.get("/reports/credit-map")
        assert 'id="cm-export-mymaps"' in page.text
        csrf = re.search(r'data-csrf="([^"]*)"', page.text).group(1)
        ok = client.post(
            "/api/reports/credit-map/import",
            files={"file": ("a.csv", _csv(), "text/csv")},
            headers={"x-csrf-token": csrf},
        )
        assert ok.status_code == 200, ok.text
        resp = client.get("/api/reports/credit-map/export-my-maps?" + query)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert "attachment" in resp.headers["content-disposition"]
        assert len(resp.text.strip().splitlines()) == 1 + 9
        assert "00000000191" not in resp.text
        assert client.get("/api/reports/credit-map/export-my-maps?city=OUTRA&date_from=2026-08&date_to=2026-11").status_code == 404
        assert client.get("/api/reports/credit-map/export-my-maps?city=X&date_from=abc&date_to=2026-11").status_code == 400


def test_csv_usa_endereco_completo_mesmo_quando_rua_e_coluna_separada():
    header = ("Protocolo;Documento;Cidade;Bairro;Rua;Endereço;CEP;"
              "Data da consulta;Status;Contrato assinado")
    rows = [
        header,
        "A-1;00000000191;CIDADE TESTE;CENTRO;Rua das Flores;Rua das Flores, 123, Centro;98900-000;05/09/2026;Aprovado;Não",
        "A-2;00000000191;CIDADE TESTE;VILA;Rua das Flores;Rua das Flores, 123, Vila;98900-100;05/09/2026;Aprovado;Sim",
        "A-3;00000000191;CIDADE TESTE;CENTRO;Rua das Flores;Rua das Flores, 123;98900-000;05/09/2026;Reprovado;Não",
    ]
    _groups, streets, addresses, _stats = aggregate_csv_addresses(("\n".join(rows) + "\n").encode())
    assert len(addresses) == 2
    assert {row["number"] for row in addresses} == {"123"}
    assert {row["cep"] for row in addresses} == {"98900000", "98900100"}
    result = summarize_addresses(addresses, city=CITY, start_month="2026-08", end_month="2026-11")
    assert len(result) == 2, "Rua e numero iguais de bairros diferentes sao enderecos distintos"
    assert len({row["key"] for row in result}) == 2
    assert len({row["street_key"] for row in result}) == 2
    assert len(streets) == 3


def test_geocodificacao_de_endereco_consulta_numero_bairro_cidade_e_cep(monkeypatch):
    calls = []

    def fake(query):
        calls.append(query)
        return (-28.30, -54.26, "Rua das Flores, 123, Centro, Santo Ângelo", "building")

    monkeypatch.setattr(geocoding, "search_detail", fake)
    found = geocoding.address_point("RUA DAS FLORES", "123", "CENTRO", "SANTO ÂNGELO",
                                   "98800000", (-28.299, -54.263))
    assert found == (-28.30, -54.26, True)
    assert len(calls) == 1
    assert "RUA DAS FLORES, 123, CENTRO" in calls[0]
    assert "98800-000" in calls[0]


def test_geocodificador_nao_marca_centro_da_cidade_como_imovel_exato(monkeypatch):
    monkeypatch.setattr(
        geocoding, "search_detail",
        lambda _query: (-28.30, -54.26, "Centro, Santo Ângelo", "place"),
    )
    monkeypatch.setattr(geocoding, "street_point", lambda *_args: (-28.301, -54.262))
    found = geocoding.address_point("RUA DAS FLORES", "123", "CENTRO", "SANTO ÂNGELO",
                                   "98800000", (-28.299, -54.263))
    assert found == (-28.301, -54.262, False)
