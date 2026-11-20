"""Validação e carga da base de caixas NAP usada pelo mapa territorial."""
from __future__ import annotations

import json
import math
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, bindparam, text

from app.services.audit import write_audit
from app.timeutils import utcnow

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models import User

STATUSES = {'Online', 'Parcial', 'Offline', 'Vazia'}
COUNTS = ('portas', 'usadas', 'livres', 'online', 'offline')
MAX_NAP_BYTES = 15 * 1024 * 1024

# Prefixos da coluna "Prefixo" na base NAP importada.
NAP_CITIES = {
    'BVB': 'Boa Vista do Buricá',
    'CBT': 'Caibaté',
    'CRR': 'Cerro Largo',
    'CSU': 'Crissiumal',
    'DMC': 'Doutor Maurício Cardoso',
    'EIJ': 'Entre-Ijuís',
    'GDM': 'Guarani das Missões',
    'GIR': 'Giruá',
    'HZA': 'Horizontina',
    'IDD': 'Independência',
    'NVN': 'Nova Candelária',
    'SAN': 'Santo Ângelo',
    'SETE': 'Sete de Setembro',
    'SJI': 'São José do Inhacorá',
    'SRO': 'Santa Rosa',
    'SSF': 'Senador Salgado Filho',
    'STC': 'Santo Cristo',
    'TDU': 'Tucunduva',
    'TMI': 'Três de Maio',
    'TUD': 'Tuparendi',
}


def feasibility_snapshot(snapshot: dict) -> dict:
    """Projeta os campos de rede usados pelo mapa comercial."""
    naps = []
    fields = ('id', 'nome', 'status', 'lat', 'lng', 'original_lat', 'original_lng', 'portas', 'livres')
    for nap in snapshot.get('naps', []):
        prefix = nap['nome'].upper().removeprefix('PROJETO-').split('-', 1)[0]
        naps.append({
            **{field: nap[field] for field in fields},
            'city': NAP_CITIES.get(prefix, ''),
        })
    return {
        'reference_at': snapshot.get('sessions_at') or snapshot.get('registration_at'),
        'naps': naps,
    }


def _coordinates(lat: Any, lng: Any) -> tuple[float, float]:
    if not (
        type(lat) in (int, float) and type(lng) in (int, float)
        and -90 <= lat <= 90 and -180 <= lng <= 180
        and math.isfinite(lat) and math.isfinite(lng)
        and (lat != 0 or lng != 0)
    ):
        raise ValueError('Coordenadas inválidas.')
    return float(lat), float(lng)


def _timestamp(metadata: dict, field: str) -> str | None:
    value = metadata.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 40 or 'T' not in value.replace(' ', 'T', 1):
        raise ValueError('Data de referência da base NAP inválida.')
    return datetime.fromisoformat(value).isoformat(timespec='seconds')


def parse_nap_geojson(raw: bytes) -> dict:
    data = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(data, dict) or data.get('type') != 'FeatureCollection':
        raise ValueError('Envie a coleção GeoJSON de caixas NAP.')
    features, metadata = data.get('features'), data.get('metadata', {})
    if not isinstance(features, list) or not features or not isinstance(metadata, dict):
        raise ValueError('O GeoJSON precisa conter caixas NAP válidas.')

    naps = []
    seen = set()
    for index, feature in enumerate(features, 1):
        if not isinstance(feature, dict) or feature.get('type') != 'Feature':
            raise ValueError(f'Registro NAP {index} inválido.')
        geometry, props = feature.get('geometry'), feature.get('properties')
        if not isinstance(geometry, dict) or geometry.get('type') != 'Point' or not isinstance(props, dict):
            raise ValueError(f'Registro NAP {index} sem ponto ou propriedades.')
        coords = geometry.get('coordinates')
        if not isinstance(coords, list) or len(coords) not in (2, 3):
            raise ValueError(f'Registro NAP {index} sem coordenadas.')
        try:
            lat, lng = _coordinates(coords[1], coords[0])
            original_lat, original_lng = _coordinates(
                props.get('latitude_original', lat), props.get('longitude_original', lng),
            )
        except ValueError as exc:
            raise ValueError(f'Coordenadas inválidas no registro NAP {index}.') from exc
        nap_id = props.get('id', feature.get('id'))
        if type(nap_id) is not int or not 0 < nap_id <= 2**53 - 1 or nap_id in seen:
            raise ValueError(f'ID inválido ou repetido no registro NAP {index}.')
        name, status = props.get('nome'), props.get('status')
        if not isinstance(name, str) or not name.strip() or len(name) > 160:
            raise ValueError(f'Nome inválido no registro NAP {index}.')
        if not isinstance(status, str) or status not in STATUSES:
            raise ValueError(f'Status inválido no registro NAP {index}.')
        if any(type(props.get(key)) is not int or not 0 <= props[key] <= 2**31 - 1 for key in COUNTS):
            raise ValueError(f'Contagem inválida no registro NAP {index}.')
        if props['usadas'] + props['livres'] != props['portas']:
            raise ValueError(f'Total de portas inconsistente no registro NAP {index}.')
        seen.add(nap_id)
        naps.append({
            'id': nap_id, 'nome': name.strip(), 'status': status,
            'lat': lat, 'lng': lng, 'original_lat': original_lat, 'original_lng': original_lng,
            **{key: props[key] for key in COUNTS},
        })
    return {
        'sessions_at': _timestamp(metadata, 'sessoes_atualizadas_em'),
        'registration_at': _timestamp(metadata, 'cadastro_atualizado_em'),
        'naps': naps,
    }


def save_nap_snapshot(db: Session, snapshot: dict, *, user: User | None = None) -> bool:
    """Grava a leitura validada e o registro de auditoria na mesma transação."""
    statement = text("""
        INSERT INTO credit_map_network (id, snapshot_json, updated_at)
        VALUES (1, :snapshot_json, :updated_at)
        ON CONFLICT (id) DO UPDATE
        SET snapshot_json = excluded.snapshot_json, updated_at = excluded.updated_at
        WHERE credit_map_network.snapshot_json <> excluded.snapshot_json
    """).bindparams(bindparam('updated_at', type_=DateTime(timezone=True)))
    result = db.execute(statement, {
        'snapshot_json': json.dumps(snapshot, separators=(',', ':'), ensure_ascii=False),
        'updated_at': utcnow(),
    })
    if result.rowcount == 0:
        return False

    write_audit(
        db, user=user, action='CREDIT_MAP_NETWORK_IMPORTED', entity_type='report',
        entity_id='credit_map_network',
        payload={'naps': len(snapshot['naps']), 'sessions_at': snapshot['sessions_at']},
    )
    return True
