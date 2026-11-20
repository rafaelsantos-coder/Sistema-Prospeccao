from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import CreditMapNetwork
from app.security import get_current_user, require_user
from app.services.credit_map_network import feasibility_snapshot
from app.web import render


router = APIRouter()


@router.get('/feasibility-map')
def map_page(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    return render(
        request, 'reports/feasibility_map.html', user=user, active_nav='feasibility-map',
        page_title='Mapa de Viabilidade',
        google_maps_key=settings.google_maps_browser_key,
        google_map_id=settings.google_maps_map_id,
    )


@router.get('/api/feasibility-map')
def map_data(request: Request, db: Session = Depends(get_db)):
    if not get_current_user(request, db):
        raise HTTPException(status_code=401, detail='Entre no Prospecta para consultar a viabilidade.')
    snapshot = db.get(CreditMapNetwork, 1)
    data = feasibility_snapshot(json.loads(snapshot.snapshot_json) if snapshot else {})
    return JSONResponse(data, headers={'Cache-Control': 'no-store'})
