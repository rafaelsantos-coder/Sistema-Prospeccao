from __future__ import annotations

import json
import re
from dataclasses import replace
from secrets import token_urlsafe

from fastapi.testclient import TestClient

from app import security_middleware
from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import CreditMapNetwork, User
from app.routers import feasibility_map
from app.security import hash_password
from app.timeutils import utcnow


def _login_consultant(client: TestClient, permissions: list[str] | None = None) -> None:
    password = token_urlsafe(24)
    email = f'viabilidade-{token_urlsafe(12).lower()}@example.test'
    with SessionLocal() as db:
        db.add(User(
            name='Equipe de vendas', email=email, password_hash=hash_password(password),
            role='CONSULTOR', must_change_password=False,
            access_permissions=json.dumps(permissions) if permissions is not None else None,
        ))
        db.commit()
    token = re.search(r'name="csrf_token" value="([^"]+)"', client.get('/login').text)
    assert token
    response = client.post('/login', data={
        'email': email, 'password': password, 'csrf_token': token.group(1),
    }, follow_redirects=False)
    assert response.status_code == 303


def test_seller_uses_nap_snapshot_without_access_to_credit(monkeypatch):
    conf = replace(settings, credit_map_provider='openstreetmap', google_maps_browser_key='maps-test-fixture')
    monkeypatch.setattr(feasibility_map, 'settings', conf)
    monkeypatch.setattr(security_middleware, 'settings', conf)
    point = {
        'id': 741, 'nome': 'PROJETO-HZA-N-741', 'status': 'Parcial',
        'lat': -27.6254, 'lng': -54.309, 'original_lat': -27.625, 'original_lng': -54.309,
        'portas': 8, 'livres': 3, 'online': 5, 'vpn': 'campo fora da consulta comercial',
    }
    with TestClient(app) as client:
        with SessionLocal() as db:
            original = db.get(CreditMapNetwork, 1)
            original_values = (original.snapshot_json, original.updated_at) if original else None
            snapshot = CreditMapNetwork(
                id=1, updated_at=utcnow(),
                snapshot_json=json.dumps({
                    'sessions_at': '2026-11-20T10:32:08',
                    'naps': [point, {**point, 'id': 742, 'nome': '1X8 NAP'}],
                }),
            )
            db.merge(snapshot)
            db.commit()

        try:
            _login_consultant(client)
            page = client.get('/feasibility-map')
            assert page.status_code == 200
            assert 'aria-current="page"' in page.text
            assert 'Mapa de Viabilidade' in page.text
            assert 'id="vm-address"' in page.text
            assert 'id="vm-coordinate"' in page.text
            assert 'id="vm-city"' in page.text
            assert 'data-google-maps-key="maps-test-fixture"' in page.text
            assert 'https://maps.googleapis.com' in page.headers['content-security-policy']
            assert 'https://maps.googleapis.com' not in client.get('/dashboard').headers['content-security-policy']

            response = client.get('/api/feasibility-map')
            assert response.status_code == 200
            assert response.headers['cache-control'] == 'no-store'
            expected = {key: value for key, value in point.items() if key not in {'online', 'vpn'}}
            assert response.json() == {
                'reference_at': '2026-11-20T10:32:08',
                'naps': [
                    {**expected, 'city': 'Horizontina'},
                    {**expected, 'id': 742, 'nome': '1X8 NAP', 'city': ''},
                ],
            }
            assert client.get('/reports/credit-map').status_code == 403
            assert client.get('/api/reports/credit-map/network').status_code == 403
        finally:
            with SessionLocal() as db:
                snapshot = db.get(CreditMapNetwork, 1)
                if original_values is None:
                    db.delete(snapshot)
                else:
                    snapshot.snapshot_json, snapshot.updated_at = original_values
                db.commit()


def test_feasibility_routes_require_login_and_respect_custom_permissions():
    with TestClient(app) as client:
        assert client.get('/feasibility-map', follow_redirects=False).headers['location'] == '/login'
        assert client.get('/api/feasibility-map').status_code == 401
        _login_consultant(client, ['dashboard.view'])
        assert 'Mapa de Viabilidade' not in client.get('/dashboard').text
        assert client.get('/feasibility-map').status_code == 403
        assert client.get('/api/feasibility-map').status_code == 403

    with TestClient(app) as client:
        _login_consultant(client, ['feasibility_map.view'])
        assert client.get('/feasibility-map').status_code == 200
        assert client.get('/api/feasibility-map').status_code == 200
