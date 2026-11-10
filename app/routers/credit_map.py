"""Painel interno de inteligência territorial do crédito Sulnet.

Não armazena registros pessoais de consultas, apenas somatórios por
cidade/bairro/mês/resultado/assinatura. Autorização usa a sessão do Prospecta.
"""
from __future__ import annotations

import csv
import os

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, distinct, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import CreditMapAggregate
from app.security import get_current_user, require_roles, validate_csrf
from app.services.audit import write_audit
from app.services.credit_map import aggregate_csv, city_name, summarize
from app.web import render

router = APIRouter()
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
    # Chave de navegador: NÃO é uma credencial secreta. Restringir HTTP referrers
    # ao domínio do Prospecta e APIS ao Maps JavaScript API no Google Cloud.
    return render(
        request, 'reports/credit_map.html', user=user, active_nav='credit-map',
        page_title='Mapa de calor de crédito',
        google_maps_browser_key=os.getenv('GOOGLE_MAPS_BROWSER_KEY', '').strip(),
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
        aggregates, stats = aggregate_csv(raw)
    except (ValueError, UnicodeError, csv.Error) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not aggregates:
        raise HTTPException(status_code=422, detail='Nenhum CPF válido encontrado na exportação.')

    # Substituição atômica. Nem o CSV nem campos pessoais entram no banco.
    db.execute(delete(CreditMapAggregate))
    db.add_all(CreditMapAggregate(**row) for row in aggregates)
    write_audit(db, user=user, action='CREDIT_MAP_IMPORTED',
                entity_type='report', entity_id='credit_map',
                payload={'cpf_validos_formato': stats.get('cpf_validos_formato', 0),
                         'agrupamentos': len(aggregates),
                         'sem_data': stats.get('sem_data', 0),
                         'protocolos_duplicados': stats.get('protocolos_duplicados', 0)})
    db.commit()
    return {'ok': True, 'stats': stats, 'aggregates': len(aggregates)}
