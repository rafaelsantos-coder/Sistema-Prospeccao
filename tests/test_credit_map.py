import pytest

from app.services.credit_map import aggregate_csv, credit_decision, summarize


def example_csv():
    lines = ['Protocolo;Documento;Cidade;Bairro;Data da consulta;Status;Contrato assinado']
    for i in range(1, 14):
        lines.append(f'SAC-2026-{i};001.111.111-11;SANTO ÂNGELO;CENTRO;01/08/2026 12:01;Aprovado;Sim')
    for i in range(14, 17):
        lines.append(f'SAC-2026-{i};001.111.111-11;SANTO ÂNGELO;CENTRO;01/08/2026 12:01;Reprovado;Não')
    for i in range(17, 22):
        lines.append(f'SAC-2026-{i};002.222.222-22;SANTO ÂNGELO;HORTÊNCIA;01/08/2026 12:01;Reprovado;Não')
    lines.append(lines[-1])
    lines.append('SAC-2026-22;10.220.038/0001-23;SANTO ÂNGELO;CENTRO;01/08/2026 12:01;Aprovado;Não')
    return ('\n'.join(lines) + '\n').encode('utf-8')


def test_counts_suppression_and_signed():
    aggregate, stats = aggregate_csv(example_csv())
    report = summarize(aggregate)
    assert report['totals']['total'] == 21
    assert report['totals']['approved'] == 13
    assert report['totals']['rejected'] == 8
    assert report['totals']['signed_approved'] == 13
    assert stats['protocolos_duplicados'] == 1
    assert stats['nao_cpf'] == 1
    assert len(report['neighborhoods']) == 1
    assert report['neighborhoods'][0]['name'] == 'CENTRO'
    assert report['suppressed_neighborhoods'] == 1
    assert report['neighborhoods'][0]['approval_rate'] == 81.2


def test_status_and_period_validation():
    assert credit_decision('Pré-aprovado com taxa') == 'PENDENTE'
    assert credit_decision('Liberado comprovante provedor') == 'APROVADO'
    assert credit_decision('Reprovação mantida') == 'REPROVADO'
    with pytest.raises(ValueError):
        summarize([], start_month='2026-11', end_month='2026-08')
