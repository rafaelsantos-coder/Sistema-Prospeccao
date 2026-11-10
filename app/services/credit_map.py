"""Normalização e agregação territorial de consultas de crédito.

Não guarda nem publica dados pessoais: CPF, nome, telefone, score e
informações de restrição são descartados durante o processamento.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections import Counter
from datetime import datetime
from typing import Any

REQUIRED_COLUMNS = {
    'Protocolo', 'Documento', 'Cidade', 'Bairro', 'Data da consulta',
    'Status', 'Contrato assinado',
}
NO_NEIGHBORHOOD = 'SEM BAIRRO INFORMADO'
NO_DATE = 'SEM_DATA'
APPROVED = 'APROVADO'
REJECTED = 'REPROVADO'
PENDING = 'PENDENTE'


def fold(value: str) -> str:
    normal = unicodedata.normalize('NFKD', str(value or '').strip().upper())
    return re.sub(r'\s+', ' ', ''.join(char for char in normal if not unicodedata.combining(char))).strip()


def city_name(value: str) -> str:
    raw = fold(value)
    if raw in {'SANTO ANGELO', 'SANTA ANGELO'}:
        return 'SANTO ÂNGELO'
    # Evita manter versões com acento diferente no mesmo município.
    return raw


def neighborhood_name(value: str) -> str:
    raw = fold(value)
    if not raw:
        return NO_NEIGHBORHOOD
    # Apenas diferenças ortográficas inequívocas; outros nomes são preservados.
    aliases = {
        'JD DAS PALMEIRAS': 'JARDIM DAS PALMEIRAS',
        'JARDIM DAS PALMEIRA': 'JARDIM DAS PALMEIRAS',
        'B. CENTRO': 'CENTRO',
        'BAIRRO CENTRO': 'CENTRO',
    }
    return aliases.get(raw, raw)


def credit_decision(value: str) -> str:
    name = fold(value)
    if name.startswith('REPROVADO') or name == 'REPROVACAO MANTIDA':
        return REJECTED
    if name.startswith('APROVADO') or name.startswith('LIBERADO'):
        return APPROVED
    return PENDING


def date_month(value: str) -> str:
    clean = (value or '').strip().replace(',', '')
    for fmt in ('%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M', '%d/%m/%Y'):
        try:
            return datetime.strptime(clean, fmt).strftime('%Y-%m')
        except ValueError:
            continue
    return NO_DATE


def _decoded(raw: bytes) -> str:
    if len(raw) > 15 * 1024 * 1024:
        raise ValueError('Arquivo maior do que o limite de 15 MB.')
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        return raw.decode('latin-1')


def aggregate_csv(raw: bytes) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Retorna agrupamentos sem PII e estatísticas de integridade da importação.

    Cada protocolo é contado uma única vez, e apenas documentos com 11
    dígitos são contabilizados. Datas sem data são mantidas em SEM_DATA.
    """
    text = _decoded(raw)
    first_line = text.partition('\n')[0]
    delim = ';' if first_line.count(';') > first_line.count(',') else ','
    reader = csv.DictReader(io.StringIO(text, newline=''), delimiter=delim)
    headers = {key.lstrip('\ufeff').strip() for key in (reader.fieldnames or [])}
    missing = REQUIRED_COLUMNS.difference(headers)
    if missing:
        raise ValueError('Colunas obrigatórias ausentes: ' + ', '.join(sorted(missing)))

    counts: Counter[tuple[str, str, str, str, bool]] = Counter()
    seen: set[str] = set()
    stats = Counter()
    for line in reader:
        stats['linhas_lidas'] += 1
        protocol = (line.get('Protocolo') or '').strip().upper()
        if protocol:
            if protocol in seen:
                stats['protocolos_duplicados'] += 1
                continue
            seen.add(protocol)
        document = re.sub(r'\D', '', line.get('Documento') or '')
        if len(document) != 11:
            stats['nao_cpf'] += 1
            continue
        city = city_name(line.get('Cidade') or '')
        if not city:
            stats['sem_cidade'] += 1
            continue
        neighborhood = neighborhood_name(line.get('Bairro') or '')
        month = date_month(line.get('Data da consulta') or '')
        decision = credit_decision(line.get('Status') or '')
        signed = fold(line.get('Contrato assinado') or '') == 'SIM'
        if month == NO_DATE:
            stats['sem_data'] += 1
        if neighborhood == NO_NEIGHBORHOOD:
            stats['sem_bairro'] += 1
        if decision == REJECTED and signed:
            stats['reprovados_assinados_inconsistentes'] += 1
        counts[(city, neighborhood, month, decision, signed)] += 1
        stats['cpf_validos_formato'] += 1

    aggregates = [
        {'city': city, 'neighborhood': bairro, 'month': month,
         'decision': decision, 'signed': signed, 'count': count}
        for (city, bairro, month, decision, signed), count in sorted(counts.items())
    ]
    return aggregates, dict(stats)


def summarize(
    items: list[dict[str, Any]], *, city: str = 'SANTO ÂNGELO',
    start_month: str = '2026-08', end_month: str = '2026-11',
    min_sample: int = 10,
) -> dict[str, Any]:
    """Constrói resposta da API sem bairros com amostra inferior a k.

    Totais incluem bairros não localizáveis e pequenos grupos. Nenhum detalhe
    de bairros com < min_sample consultas é exposto ao navegador.
    """
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', start_month):
        raise ValueError('Mês inicial inválido')
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', end_month):
        raise ValueError('Mês final inválido')
    if start_month > end_month:
        raise ValueError('Período inicial maior que final')
    city = city_name(city)
    totals = {'total': 0, 'approved': 0, 'rejected': 0, 'pending': 0,
              'signed_approved': 0, 'signed_rejected_inconsistent': 0}
    by_hood: dict[str, dict[str, Any]] = {}
    by_month: dict[str, dict[str, int]] = {}
    for item in items:
        if city_name(item['city']) != city or not (start_month <= item['month'] <= end_month):
            continue
        n = int(item['count'])
        decision = item['decision']
        signed = bool(item['signed'])
        neighborhood = neighborhood_name(item['neighborhood'])
        row = by_hood.setdefault(neighborhood, {'name': neighborhood, 'total': 0,
            'approved': 0, 'rejected': 0, 'pending': 0, 'signed_approved': 0})
        monthly = by_month.setdefault(item['month'], {'month': item['month'],
            'total': 0, 'approved': 0, 'rejected': 0, 'pending': 0, 'signed_approved': 0})
        for bucket in (row, totals, monthly):
            bucket['total'] += n
            bucket[{'APROVADO': 'approved', 'REPROVADO': 'rejected', 'PENDENTE': 'pending'}[decision]] += n
            if decision == APPROVED and signed:
                bucket['signed_approved'] += n
        if decision == REJECTED and signed:
            totals['signed_rejected_inconsistent'] += n

    neighborhoods = []
    hidden_neighborhoods = 0
    for row in by_hood.values():
        if row['name'] == NO_NEIGHBORHOOD or row['total'] < min_sample:
            hidden_neighborhoods += 1
            continue
        decided = row['approved'] + row['rejected']
        row['approval_rate'] = round(row['approved'] / decided * 100, 1) if decided else None
        row['signed_rate'] = round(row['signed_approved'] / row['approved'] * 100, 1) if row['approved'] else None
        neighborhoods.append(row)
    neighborhoods.sort(key=lambda x: (-x['approved'], -x['total'], x['name']))
    decided = totals['approved'] + totals['rejected']
    totals['approval_rate'] = round(totals['approved'] / decided * 100, 1) if decided else None
    totals['signed_rate'] = round(totals['signed_approved'] / totals['approved'] * 100, 1) if totals['approved'] else None
    return {'city': city, 'period': {'from': start_month, 'to': end_month},
        'totals': totals, 'neighborhoods': neighborhoods, 'months': sorted(by_month.values(), key=lambda x: x['month']),
        'suppressed_neighborhoods': hidden_neighborhoods, 'min_sample': min_sample,
        'geolocation': 'Geocodificação aproximada no centro do bairro; não representa endereços individuais.'}
