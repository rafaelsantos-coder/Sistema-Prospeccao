"""Carrega um GeoJSON NAP diretamente no banco do mapa de crédito.

Execute da raiz do projeto com DATABASE_URL apontando para o banco do Prospecta:
    python -m scripts.import_credit_map_network <arquivo.geojson>
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description='Importa a base NAP no banco do Prospecta.')
    parser.add_argument('geojson', type=Path, help='Arquivo GeoJSON extraído das caixas NAP.')
    args = parser.parse_args()
    if not os.environ.get('DATABASE_URL', '').strip():
        parser.error('Configure DATABASE_URL com a conexão do banco do Prospecta.')

    from sqlalchemy.exc import SQLAlchemyError

    from app.database import SessionLocal
    from app.services.credit_map_network import MAX_NAP_BYTES, parse_nap_geojson, save_nap_snapshot

    try:
        with args.geojson.open('rb') as source:
            raw = source.read(MAX_NAP_BYTES + 1)
        if len(raw) > MAX_NAP_BYTES:
            raise ValueError('GeoJSON excede 15 MB.')
        snapshot = parse_nap_geojson(raw)
    except (OSError, RecursionError, ValueError) as exc:
        parser.error(str(exc))

    try:
        with SessionLocal.begin() as db:
            saved = save_nap_snapshot(db, snapshot)
    except SQLAlchemyError as exc:
        parser.exit(1, f'Falha ao gravar a base NAP no banco configurado ({type(exc).__name__}).\n')

    nap_count = len(snapshot['naps'])
    if saved:
        print(f'Base NAP gravada com {nap_count} caixas.')
    else:
        print(f'A leitura de {nap_count} caixas já está gravada no banco.')


if __name__ == '__main__':
    main()
