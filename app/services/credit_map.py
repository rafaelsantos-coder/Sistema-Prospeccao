"""Normalização e agregação territorial de consultas de crédito.

CPF, nome, telefone, score e informações de restrição são descartados durante
o processamento. Dos clientes aprovados fica guardado o endereço (rua e número),
sem identificação da pessoa, para os marcadores do mapa.
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
STREET_MIN_SAMPLE = 5
STREET_HEADERS = ('Rua', 'Logradouro', 'Endereço', 'Endereco')
CEP_HEADERS = ('CEP',)
NUMBER_HEADERS = ('Número', 'Numero', 'Nº', 'N°', 'Num')
NO_NEIGHBORHOOD = 'SEM BAIRRO INFORMADO'
NOT_A_STREET = {'INTERIOR', 'ZONA RURAL', 'RURAL', 'SEM RUA', 'NAO INFORMADO', 'NAO INFORMADA', 'S/N'}
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


def street_name(value: str) -> str:
    """Nome da rua sem número da casa (o número nunca é guardado)."""
    raw = fold(value)
    raw = re.sub(r'\s*(,|-|\bN\b\.?|\bNUMERO\b|\bNO\b)?\s*\d+\s*[A-Z]?$', '', raw).strip(' ,-')
    # Abreviações comuns, para a mesma rua não se dividir em várias.
    prefixes = {'R': 'RUA', 'AV': 'AVENIDA', 'AVE': 'AVENIDA', 'TRAV': 'TRAVESSA', 'TV': 'TRAVESSA',
                'PCA': 'PRACA', 'ROD': 'RODOVIA', 'EST': 'ESTRADA', 'AL': 'ALAMEDA'}
    parts = raw.split(' ', 1)
    head = parts[0].rstrip('.')
    if head in prefixes and len(parts) > 1:
        raw = prefixes[head] + ' ' + parts[1]
    if raw in NOT_A_STREET:
        return ''
    return raw[:100]


def house_number(number: str, street: str = '') -> str:
    """Número da casa: da coluna própria ou, na falta dela, do fim do campo da rua."""
    raw = fold(number)
    if raw in {'S/N', 'SN', 'S N', 'SEM NUMERO', '0'}:
        return ''
    match = re.fullmatch(r'(\d{1,6})\s*([A-Z])?', raw)
    if not match:
        match = re.search(r'(?:,|-|\bN\b\.?|\bNUMERO\b|\bNO\b|\s)\s*(\d{1,6})\s*([A-Z])?$', fold(street)) if not raw else None
    if not match or int(match.group(1)) == 0:
        return ''
    return str(int(match.group(1))) + (match.group(2) or '')


def majority_color(approved: int, rejected: int) -> str:
    """Cor do território: verde se aprovados >= reprovados (empate fica verde), senão vermelho."""
    return 'green' if int(approved or 0) >= int(rejected or 0) else 'red'


def cep_digits(value: str) -> str:
    digits = re.sub(r'\D', '', value or '')
    return digits if len(digits) == 8 else ''


def _first_value(line: dict[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        if line.get(name):
            return str(line.get(name))
    return ''


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
    aggregates, _streets, stats = aggregate_csv_full(raw)
    return aggregates, stats


def aggregate_csv_full(
    raw: bytes,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    aggregates, streets, _addresses, stats = aggregate_csv_addresses(raw)
    return aggregates, streets, stats


def aggregate_csv_addresses(
    raw: bytes,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Retorna agrupamentos por bairro e por rua, endereços dos aprovados e estatísticas.

    Colunas opcionais Rua/Logradouro, CEP e Número. Nos agrupamentos por rua o
    número da casa é ignorado; ele só é mantido na lista de endereços dos
    clientes aprovados (rua, número, bairro, mês e se assinou contrato).

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
    street_counts: Counter[tuple[str, str, str, str, str, str, bool]] = Counter()
    address_counts: Counter[tuple[str, str, str, str, str, str, bool]] = Counter()
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
        street = street_name(_first_value(line, STREET_HEADERS))
        if street:
            cep = cep_digits(_first_value(line, CEP_HEADERS))
            street_counts[(city, neighborhood, street, cep, month, decision, signed)] += 1
            stats['com_rua'] += 1
            if decision == APPROVED:
                number = house_number(_first_value(line, NUMBER_HEADERS), _first_value(line, STREET_HEADERS))
                address_counts[(city, neighborhood, street, number, cep, month, signed)] += 1
                stats['enderecos_aprovados'] += 1

    aggregates = [
        {'city': city, 'neighborhood': bairro, 'month': month,
         'decision': decision, 'signed': signed, 'count': count}
        for (city, bairro, month, decision, signed), count in sorted(counts.items())
    ]
    streets = [
        {'city': city, 'neighborhood': bairro, 'street': street, 'cep': cep, 'month': month,
         'decision': decision, 'signed': signed, 'count': count}
        for (city, bairro, street, cep, month, decision, signed), count in sorted(street_counts.items())
    ]
    addresses = [
        {'city': city, 'neighborhood': bairro, 'street': street, 'number': number, 'cep': cep,
         'month': month, 'signed': signed, 'count': count}
        for (city, bairro, street, number, cep, month, signed), count in sorted(address_counts.items())
    ]
    return aggregates, streets, addresses, dict(stats)


def address_key(street: str, number: str) -> str:
    """Chave do cache de posição de um endereço (rua + número)."""
    return f"END|{street}|{number}"[:180]


def summarize_addresses(
    items: list[dict[str, Any]], *, city: str, start_month: str, end_month: str,
) -> list[dict[str, Any]]:
    """Endereços de clientes aprovados no período, separados em assinou / não assinou contrato."""
    city = city_name(city)
    groups: dict[tuple[str, str, bool], dict[str, Any]] = {}
    for item in items:
        if city_name(item['city']) != city or not (start_month <= item['month'] <= end_month):
            continue
        signed = bool(item['signed'])
        key = (item['street'], item.get('number') or '', signed)
        row = groups.setdefault(key, {
            'street': key[0], 'number': key[1], 'signed': signed, 'count': 0,
            'neighborhood': '', 'cep': '', 'key': address_key(key[0], key[1])})
        row['count'] += int(item['count'])
        hood = neighborhood_name(item['neighborhood'])
        if hood != NO_NEIGHBORHOOD and not row['neighborhood']:
            row['neighborhood'] = hood
        if item.get('cep') and not row['cep']:
            row['cep'] = item['cep']
    return sorted(groups.values(), key=lambda x: (x['street'], x['number'], not x['signed']))


def summarize_streets(
    items: list[dict[str, Any]], *, city: str, start_month: str, end_month: str,
    min_sample: int = STREET_MIN_SAMPLE,
) -> list[dict[str, Any]]:
    """Ruas com pelo menos ``min_sample`` consultas no período (a mesma rua agrupa todos os bairros)."""
    city = city_name(city)
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        if city_name(item['city']) != city or not (start_month <= item['month'] <= end_month):
            continue
        key = (item['street'], '')
        row = groups.setdefault(key, {
            'street': key[0], 'neighborhood': '', 'total': 0, 'approved': 0,
            'rejected': 0, 'pending': 0, 'signed_approved': 0, 'ceps': Counter(), 'hoods': Counter()})
        hood = neighborhood_name(item['neighborhood'])
        n = int(item['count'])
        row['total'] += n
        decision = item['decision']
        row[{'APROVADO': 'approved', 'REPROVADO': 'rejected', 'PENDENTE': 'pending'}[decision]] += n
        if decision == APPROVED and bool(item['signed']):
            row['signed_approved'] += n
        if item.get('cep'):
            row['ceps'][item['cep']] += n
        if hood != NO_NEIGHBORHOOD:
            row['hoods'][hood] += n
    result = []
    for row in groups.values():
        if row['total'] < min_sample:
            continue
        ceps = row.pop('ceps')
        hoods = row.pop('hoods')
        row['cep'] = ceps.most_common(1)[0][0] if ceps else ''
        row['neighborhood'] = hoods.most_common(1)[0][0] if hoods else ''
        decided = row['approved'] + row['rejected']
        row['approval_rate'] = round(row['approved'] / decided * 100, 1) if decided else None
        row['signed_rate'] = round(row['signed_approved'] / row['approved'] * 100, 1) if row['approved'] else None
        row['color'] = majority_color(row['approved'], row['rejected'])
        row['key'] = f"RUA|{row['street']}"[:180]
        result.append(row)
    result.sort(key=lambda x: (-x['total'], x['street']))
    return result


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
        row['color'] = majority_color(row['approved'], row['rejected'])
        neighborhoods.append(row)
    neighborhoods.sort(key=lambda x: (-x['approved'], -x['total'], x['name']))
    decided = totals['approved'] + totals['rejected']
    totals['approval_rate'] = round(totals['approved'] / decided * 100, 1) if decided else None
    totals['signed_rate'] = round(totals['signed_approved'] / totals['approved'] * 100, 1) if totals['approved'] else None
    return {'city': city, 'period': {'from': start_month, 'to': end_month},
        'totals': totals, 'neighborhoods': neighborhoods, 'months': sorted(by_month.values(), key=lambda x: x['month']),
        'suppressed_neighborhoods': hidden_neighborhoods, 'min_sample': min_sample,
        'geolocation': 'Bairros no centro aproximado; os endereços de aprovados vêm de consulta separada.'}


def _cell(value: str) -> str:
    """Evita que uma célula comece com =, +, - ou @ (fórmula ao abrir em planilha)."""
    return "'" + value if value[:1] in ('=', '+', '-', '@', '\t', '\r') else value


def my_maps_csv(addresses: list[dict[str, Any]], *, city: str) -> str:
    """CSV para importar no Google My Maps, que localiza cada endereço (com número).

    A coluna Situação serve para colorir os pontos. Sem nome, CPF ou telefone.
    """
    city_label = city_name(city).title()
    out = io.StringIO()
    writer = csv.writer(out, lineterminator='\r\n')
    writer.writerow(['Endereço', 'Situação', 'Bairro', 'Clientes'])
    for row in addresses:
        street = row['street'].title()
        line = f"{street}, {row['number']}" if row['number'] else street
        hood = row['neighborhood'].title() if row['neighborhood'] else ''
        full = ', '.join([line] + ([hood] if hood else []) + [city_label, 'RS', 'Brasil'])
        writer.writerow([_cell(full), 'Contrato assinado' if row['signed'] else 'Aprovado sem contrato',
                         _cell(hood), int(row['count'])])
    return out.getvalue()
