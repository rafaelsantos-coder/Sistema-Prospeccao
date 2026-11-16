"""Painel interno de inteligência territorial do crédito Sulnet.

Guarda somatórios por cidade/bairro/rua/mês/resultado/assinatura e, dos clientes
aprovados, o endereço (rua e número) sem nome nem CPF. Autorização usa a sessão
do Prospecta.
"""
from __future__ import annotations

import csv
import logging
import re

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import delete, distinct, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CreditMapAddress, CreditMapAggregate, CreditMapGeocode, CreditMapStreetAggregate
from app.security import get_current_user, require_roles, validate_csrf
from app.services.audit import write_audit
from app.services import geocoding
from app.services.credit_map import (
    address_key, aggregate_csv_addresses, city_name, my_maps_csv, summarize, summarize_addresses,
    summarize_streets,
)
from app.timeutils import utcnow
from app.web import render

router = APIRouter()
LOG = logging.getLogger('sulnet_credit_map')
READ_ROLES = ('ADMIN', 'GESTOR', 'SUPERVISOR')
WRITE_ROLES = ('ADMIN', 'GESTOR')
MAX_BYTES = 15 * 1024 * 1024


def allowed(request: Request, db: Session, *, write: bool = False):
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail='Entre no Sulnet Prospecta para acessar este painel.')
    require_roles(user, *(WRITE_ROLES if write else READ_ROLES))
    return user


@router.get('/reports/credit-map')
def map_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse('/login', status_code=303)
    require_roles(user, *READ_ROLES)
    return render(
        request, 'reports/credit_map.html', user=user, active_nav='credit-map',
        page_title='Mapa de calor de crédito',
        can_import=user.role in WRITE_ROLES,
    )


@router.get('/api/reports/credit-map')
def map_data(request: Request, city: str = 'SANTO ÂNGELO',
             date_from: str = '2026-08', date_to: str = '2026-11',
             db: Session = Depends(get_db)):
    allowed(request, db)
    if not city or len(city) > 120:
        raise HTTPException(status_code=400, detail='Cidade inválida.')
    entries = list(db.scalars(select(CreditMapAggregate).where(
        CreditMapAggregate.city == city_name(city),
        CreditMapAggregate.month >= date_from,
        CreditMapAggregate.month <= date_to,
    )))
    try:
        return summarize([
            {'city': e.city, 'neighborhood': e.neighborhood,
             'month': e.month, 'decision': e.decision,
             'signed': e.signed, 'count': e.count}
            for e in entries
        ], city=city, start_month=date_from, end_month=date_to, min_sample=10)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


GEOCODE_BATCH = 6  # ~7 s por chamada (1 consulta/s do serviço público); o navegador repete até acabar.
NOT_FOUND_RETRY_DAYS = 30


@router.get('/api/reports/credit-map/geo')
def map_geo(request: Request, city: str = 'SANTO ÂNGELO',
            date_from: str = '2026-08', date_to: str = '2026-11',
            db: Session = Depends(get_db)):
    """Posições aproximadas dos bairros exibíveis (mín. 10 consultas), com cache no banco."""
    allowed(request, db)
    if not city or len(city) > 120:
        raise HTTPException(status_code=400, detail='Cidade inválida.')
    entries = list(db.scalars(select(CreditMapAggregate).where(
        CreditMapAggregate.city == city_name(city),
        CreditMapAggregate.month >= date_from,
        CreditMapAggregate.month <= date_to,
    )))
    try:
        summary = summarize([
            {'city': e.city, 'neighborhood': e.neighborhood, 'month': e.month,
             'decision': e.decision, 'signed': e.signed, 'count': e.count}
            for e in entries
        ], city=city, start_month=date_from, end_month=date_to, min_sample=10)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    names = [row['name'] for row in summary.get('neighborhoods', [])]
    cached = {g.neighborhood: g for g in db.scalars(
        select(CreditMapGeocode).where(CreditMapGeocode.city == city_name(city)))}
    retry_before = utcnow().timestamp() - NOT_FOUND_RETRY_DAYS * 86400

    def usable(item: CreditMapGeocode | None) -> bool:
        if item is None:
            return False
        return item.status == 'FOUND' or item.checked_at.timestamp() > retry_before

    todo = ([''] if not usable(cached.get('')) else []) + [n for n in names if not usable(cached.get(n))]
    error = ''
    for key in todo[:GEOCODE_BATCH]:
        try:
            if key == '':
                center = geocoding.city_center(city)
                point = center
            else:
                base = cached.get('')
                if not base or base.lat is None:
                    continue
                point = geocoding.neighborhood_center(key, city, (base.lat, base.lng))
        except geocoding.GeocodingError as exc:
            LOG.warning('credit_map_geocode_failed key=%s reason=%s', key or '(cidade)', str(exc)[:160])
            error = 'Serviço de endereços indisponível agora.'
            break
        record = cached.get(key) or CreditMapGeocode(city=city_name(city), neighborhood=key)
        record.lat, record.lng = (point if point else (None, None))
        record.status = 'FOUND' if point else 'NOT_FOUND'
        record.checked_at = utcnow()
        db.add(record)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()  # outro usuário gravou a mesma posição ao mesmo tempo
        cached[key] = record
    cached = {g.neighborhood: g for g in db.scalars(
        select(CreditMapGeocode).where(CreditMapGeocode.city == city_name(city)))}
    center = cached.get('')
    positions = {n: {'lat': cached[n].lat, 'lng': cached[n].lng}
                 for n in names if n in cached and cached[n].status == 'FOUND' and cached[n].lat is not None}
    if center is not None and center.lat is None:
        pending = 0  # cidade não localizada: sem centro não há como validar os bairros
    else:
        pending = len([k for k in ([''] + names) if not usable(cached.get(k))])
    return {
        'city_center': {'lat': center.lat, 'lng': center.lng} if center and center.lat is not None else None,
        'positions': positions,
        'not_found': [n for n in names if n in cached and cached[n].status == 'NOT_FOUND'],
        'pending': pending,
        'error': error,
    }


STREET_BATCH = 4
MAX_STREETS = 300


@router.get('/api/reports/credit-map/streets')
def map_streets(request: Request, city: str = 'SANTO ÂNGELO',
                date_from: str = '2026-08', date_to: str = '2026-11',
                db: Session = Depends(get_db)):
    """Ruas com 5+ consultas, posicionadas (cache no banco). Sem número de casa."""
    allowed(request, db)
    if not city or len(city) > 120:
        raise HTTPException(status_code=400, detail='Cidade inválida.')
    entries = list(db.scalars(select(CreditMapStreetAggregate).where(
        CreditMapStreetAggregate.city == city_name(city),
        CreditMapStreetAggregate.month >= date_from,
        CreditMapStreetAggregate.month <= date_to,
    )))
    try:
        rows = summarize_streets([
            {'city': e.city, 'neighborhood': e.neighborhood, 'street': e.street, 'cep': e.cep,
             'month': e.month, 'decision': e.decision, 'signed': e.signed, 'count': e.count}
            for e in entries
        ], city=city, start_month=date_from, end_month=date_to)[:MAX_STREETS]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cached = {g.neighborhood: g for g in db.scalars(
        select(CreditMapGeocode).where(CreditMapGeocode.city == city_name(city)))}
    base = cached.get('')
    retry_before = utcnow().timestamp() - NOT_FOUND_RETRY_DAYS * 86400

    def usable(item: CreditMapGeocode | None) -> bool:
        return item is not None and (item.status == 'FOUND' or item.checked_at.timestamp() > retry_before)

    error = ''
    if base is not None and base.lat is not None:
        todo = [r for r in rows if not usable(cached.get(r['key']))]
        for row in todo[:STREET_BATCH]:
            try:
                point = geocoding.street_point(
                    row['street'], row['neighborhood'], city, row['cep'], (base.lat, base.lng))
            except geocoding.GeocodingError as exc:
                LOG.warning('credit_map_street_geocode_failed reason=%s', str(exc)[:160])
                error = 'Serviço de endereços indisponível agora.'
                break
            record = cached.get(row['key']) or CreditMapGeocode(city=city_name(city), neighborhood=row['key'])
            record.lat, record.lng = (point if point else (None, None))
            record.status = 'FOUND' if point else 'NOT_FOUND'
            record.checked_at = utcnow()
            db.add(record)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
            cached[row['key']] = record
    streets = []
    pending = 0
    for row in rows:
        item = cached.get(row['key'])
        if item is not None and item.status == 'FOUND' and item.lat is not None:
            streets.append({**row, 'lat': item.lat, 'lng': item.lng})
        elif not usable(item):
            pending += 1
    return {'streets': streets, 'pending': pending if base is not None and base.lat is not None else 0,
            'total': len(rows), 'error': error}


ADDRESS_BATCH = 4
MAX_ADDRESSES = 2000


@router.get('/api/reports/credit-map/addresses')
def map_addresses(request: Request, city: str = 'SANTO ÂNGELO',
                  date_from: str = '2026-08', date_to: str = '2026-11',
                  db: Session = Depends(get_db)):
    """Endereços (rua e número) dos clientes aprovados: com contrato assinado ou ainda sem contrato.

    Não devolve nome, CPF nem telefone. A posição fica em cache no banco.
    """
    allowed(request, db)
    if not city or len(city) > 120:
        raise HTTPException(status_code=400, detail='Cidade inválida.')
    entries = list(db.scalars(select(CreditMapAddress).where(
        CreditMapAddress.city == city_name(city),
        CreditMapAddress.month >= date_from,
        CreditMapAddress.month <= date_to,
    )))
    rows = summarize_addresses([
        {'city': e.city, 'neighborhood': e.neighborhood, 'street': e.street, 'number': e.number,
         'cep': e.cep, 'month': e.month, 'signed': e.signed, 'count': e.count}
        for e in entries
    ], city=city, start_month=date_from, end_month=date_to)[:MAX_ADDRESSES]
    cached = {g.neighborhood: g for g in db.scalars(
        select(CreditMapGeocode).where(CreditMapGeocode.city == city_name(city)))}
    base = cached.get('')
    retry_before = utcnow().timestamp() - NOT_FOUND_RETRY_DAYS * 86400

    def usable(item: CreditMapGeocode | None) -> bool:
        return item is not None and (item.status in ('FOUND', 'STREET') or item.checked_at.timestamp() > retry_before)

    error = ''
    if base is not None and base.lat is not None:
        todo: list[dict] = []
        for row in rows:
            if not usable(cached.get(row['key'])) and row['key'] not in {t['key'] for t in todo}:
                todo.append(row)
        for row in todo[:ADDRESS_BATCH]:
            street_cache = cached.get(f"RUA|{row['street']}"[:180])
            try:
                if not row['number'] and street_cache is not None and street_cache.status == 'FOUND' \
                        and street_cache.lat is not None:
                    point = (street_cache.lat, street_cache.lng, False)  # sem número: usa a rua já localizada
                else:
                    point = geocoding.address_point(
                        row['street'], row['number'], row['neighborhood'], city, row['cep'], (base.lat, base.lng))
            except geocoding.GeocodingError as exc:
                LOG.warning('credit_map_address_geocode_failed reason=%s', str(exc)[:160])
                error = 'Serviço de endereços indisponível agora.'
                break
            record = cached.get(row['key']) or CreditMapGeocode(city=city_name(city), neighborhood=row['key'])
            record.lat, record.lng = ((point[0], point[1]) if point else (None, None))
            record.status = ('FOUND' if point[2] else 'STREET') if point else 'NOT_FOUND'
            record.checked_at = utcnow()
            db.add(record)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
            cached[row['key']] = record
    addresses = []
    pending = 0
    for row in rows:
        item = cached.get(row['key'])
        if item is not None and item.status in ('FOUND', 'STREET') and item.lat is not None:
            addresses.append({
                'street': row['street'], 'number': row['number'], 'neighborhood': row['neighborhood'],
                'signed': row['signed'], 'count': row['count'],
                'lat': item.lat, 'lng': item.lng, 'exact': item.status == 'FOUND'})
        elif not usable(item):
            pending += 1
    return {'addresses': addresses, 'pending': pending if base is not None and base.lat is not None else 0,
            'total': len(rows),
            'signed': sum(r['count'] for r in rows if r['signed']),
            'unsigned': sum(r['count'] for r in rows if not r['signed']),
            'error': error}


@router.get('/api/reports/credit-map/export-my-maps')
def export_my_maps(request: Request, city: str = 'SANTO ÂNGELO',
                   date_from: str = '2026-08', date_to: str = '2026-11',
                   db: Session = Depends(get_db)):
    """CSV dos endereços de clientes aprovados, pronto para importar no Google My Maps (ADMIN/GESTOR)."""
    user = allowed(request, db, write=True)
    if not city or len(city) > 120:
        raise HTTPException(status_code=400, detail='Cidade inválida.')
    if not (re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', date_from) and re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', date_to)):
        raise HTTPException(status_code=400, detail='Período inválido.')
    entries = list(db.scalars(select(CreditMapAddress).where(
        CreditMapAddress.city == city_name(city),
        CreditMapAddress.month >= date_from,
        CreditMapAddress.month <= date_to,
    )))
    rows = summarize_addresses([
        {'city': e.city, 'neighborhood': e.neighborhood, 'street': e.street, 'number': e.number,
         'cep': e.cep, 'month': e.month, 'signed': e.signed, 'count': e.count}
        for e in entries
    ], city=city, start_month=date_from, end_month=date_to)
    if not rows:
        raise HTTPException(status_code=404, detail='Sem endereços de aprovados neste período. Importe o CSV com Rua e Número.')
    write_audit(db, user=user, action='CREDIT_MAP_EXPORTED_MY_MAPS',
                entity_type='report', entity_id='credit_map',
                payload={'cidade': city_name(city), 'periodo': f'{date_from}/{date_to}', 'linhas': len(rows)})
    db.commit()
    return Response(
        content=my_maps_csv(rows, city=city),
        media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename="mapa-credito-my-maps.csv"',
                 'Cache-Control': 'no-store'},
    )


@router.get('/api/reports/credit-map/cities')
def map_cities(request: Request, db: Session = Depends(get_db)):
    allowed(request, db)
    return {'cities': sorted(db.scalars(select(distinct(CreditMapAggregate.city))).all())}


@router.post('/api/reports/credit-map/import')
async def import_file(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    user = allowed(request, db, write=True)
    validate_csrf(request, request.headers.get('x-csrf-token'))
    if not (file.filename or '').lower().endswith('.csv'):
        raise HTTPException(status_code=400, detail='Envie um arquivo .csv.')
    raw = await file.read(MAX_BYTES + 1)
    await file.close()
    if len(raw) > MAX_BYTES:
        raise HTTPException(status_code=413, detail='CSV excede 15 MB.')
    try:
        aggregates, street_rows, address_rows, stats = aggregate_csv_addresses(raw)
    except (ValueError, UnicodeError, csv.Error) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not aggregates:
        raise HTTPException(status_code=422, detail='Nenhum CPF válido encontrado na exportação.')

    # Substituição atômica. O CSV, o CPF e o nome não entram no banco; dos aprovados fica rua e número.
    db.execute(delete(CreditMapAggregate))
    db.execute(delete(CreditMapStreetAggregate))
    db.execute(delete(CreditMapAddress))
    db.execute(delete(CreditMapGeocode).where(CreditMapGeocode.neighborhood.like('RUA|%')))
    # Posição de endereço é reaproveitada entre importações; a de quem saiu da base é apagada.
    kept = {(row['city'], address_key(row['street'], row['number'])) for row in address_rows}
    for item in db.scalars(select(CreditMapGeocode).where(CreditMapGeocode.neighborhood.like('END|%'))):
        if (item.city, item.neighborhood) not in kept:
            db.delete(item)
    db.add_all(CreditMapAggregate(**row) for row in aggregates)
    db.add_all(CreditMapStreetAggregate(**row) for row in street_rows)
    db.add_all(CreditMapAddress(**row) for row in address_rows)
    write_audit(db, user=user, action='CREDIT_MAP_IMPORTED',
                entity_type='report', entity_id='credit_map',
                payload={'cpf_validos_formato': stats.get('cpf_validos_formato', 0),
                         'agrupamentos': len(aggregates),
                         'sem_data': stats.get('sem_data', 0),
                         'protocolos_duplicados': stats.get('protocolos_duplicados', 0),
                         'com_rua': stats.get('com_rua', 0),
                         'enderecos_aprovados': stats.get('enderecos_aprovados', 0)})
    db.commit()
    return {'ok': True, 'stats': stats, 'aggregates': len(aggregates)}
