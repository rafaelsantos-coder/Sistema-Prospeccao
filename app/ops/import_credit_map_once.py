"""Execucao unica, sem PII, da importacao de credito usando o parser oficial.

Uso exclusivo no servico temporario de importacao do Railway:
CREDIT_MAP_IMPORT_MODE=APPLY
CREDIT_MAP_PAYLOAD_PARTS=9
CREDIT_MAP_PAYLOAD_00 ... _08 = partes de um CSV higienizado em gzip+base64
CREDIT_MAP_IMPORT_SHA256 = checksum do CSV apos descompressao
CREDIT_MAP_IMPORT_EXPECTED_ROWS = quantidade de linhas fisicas processadas
CREDIT_MAP_IMPORT_EXPECTED_ELIGIBLE = consultas elegiveis
CREDIT_MAP_IMPORT_EXPECTED_GPS = quantidade de aprovados com GPS valido

O processo so escreve apos todas as validacoes, em uma unica transacao.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import sys

from sqlalchemy import delete, func, select, text

from app.database import SessionLocal
from app.models import (
    CreditMapAddress, CreditMapAggregate, CreditMapGeocode, CreditMapStreetAggregate,
)
from app.services.audit import write_audit
from app.services.credit_map import address_key, aggregate_csv_addresses


def fail(reason: str) -> None:
    print(json.dumps({"ok": False, "reason": reason}, ensure_ascii=False), flush=True)
    raise SystemExit(1)


def fetch_payload() -> bytes:
    parts = int(os.getenv("CREDIT_MAP_PAYLOAD_PARTS", "0"))
    if not 1 <= parts <= 60:
        fail("missing_parts_count")
    pieces = [os.getenv(f"CREDIT_MAP_PAYLOAD_{i:02}", "") for i in range(parts)]
    if any(not part for part in pieces):
        fail("payload_incomplete")
    try:
        data = gzip.decompress(base64.b64decode("".join(pieces), validate=True))
    except (ValueError, OSError) as exc:
        fail("payload_corrupted")
    checksum = hashlib.sha256(data).hexdigest()
    if checksum != os.getenv("CREDIT_MAP_IMPORT_SHA256", ""):
        fail("sha256_mismatch")
    return data


def main() -> None:
    if os.getenv("CREDIT_MAP_IMPORT_MODE") != "APPLY":
        print(json.dumps({"ok": False, "skipped": "not_authorized_for_this_deployment"}), flush=True)
        return
    data = fetch_payload()
    aggregates, streets, addresses, stats = aggregate_csv_addresses(data)

    expected = {
        "linhas_lidas": int(os.getenv("CREDIT_MAP_IMPORT_EXPECTED_ROWS", "-1")),
        "cpf_validos_formato": int(os.getenv("CREDIT_MAP_IMPORT_EXPECTED_ELIGIBLE", "-1")),
        "aprovados_com_coordenadas_validas": int(os.getenv("CREDIT_MAP_IMPORT_EXPECTED_GPS", "-1")),
    }
    for key, count in expected.items():
        if stats.get(key, 0) != count:
            fail(f"unexpected_{key}_{stats.get(key, 0)}_not_{count}")
    if not aggregates or not addresses:
        fail("no_aggregate_or_address_records")
    if sum(row["count"] for row in aggregates) != expected["cpf_validos_formato"]:
        fail("aggregate_sum_mismatch")
    if sum(row["count"] for row in addresses) != expected["aprovados_com_coordenadas_validas"]:
        fail("gps_sum_mismatch")
    if any(row["lat"] is None or row["lng"] is None for row in addresses):
        fail("unexpected_approximate_coordinates")
    sha = hashlib.sha256(data).hexdigest()

    # Evita a importacao concorrente de duas instancias da tarefa.
    db = SessionLocal()
    try:
        db.execute(text("SELECT pg_advisory_xact_lock(2026111601)"))
        # Idempotencia e backup transacional: qualquer falha desfaz o DELETE.
        db.execute(delete(CreditMapAggregate))
        db.execute(delete(CreditMapStreetAggregate))
        db.execute(delete(CreditMapAddress))
        db.execute(delete(CreditMapGeocode).where(CreditMapGeocode.neighborhood.like("RUA|%")))
        retained = {
            (row["city"], address_key(row["street"], row["number"], row["neighborhood"], row["cep"]))
            for row in addresses
        }
        for g in db.scalars(select(CreditMapGeocode).where(CreditMapGeocode.neighborhood.like("END|%"))):
            if (g.city, g.neighborhood) not in retained:
                db.delete(g)
        db.add_all(CreditMapAggregate(**row) for row in aggregates)
        db.add_all(CreditMapStreetAggregate(**row) for row in streets)
        db.add_all(CreditMapAddress(**row) for row in addresses)
        write_audit(
            db, user=None, action="CREDIT_MAP_IMPORTED",
            entity_type="report", entity_id="credit_map",
            payload={
                "source": "railway_one_time_import",
                "sanitized_sha256_prefix": sha[:16],
                "cpf_validos_formato": stats["cpf_validos_formato"],
                "enderecos_aprovados": stats["enderecos_aprovados"],
                "agregados": len(aggregates),
                "ruas": len(streets),
                "enderecos": len(addresses),
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        print(json.dumps({"ok": False, "reason": "db_transaction_failed"}), flush=True)
        raise
    finally:
        db.close()

    # Verificacao no mesmo banco apos o commit efetivo.
    with SessionLocal() as check:
        counts = {
            "aggregate_rows": check.scalar(select(func.count()).select_from(CreditMapAggregate)),
            "street_rows": check.scalar(select(func.count()).select_from(CreditMapStreetAggregate)),
            "address_rows": check.scalar(select(func.count()).select_from(CreditMapAddress)),
            "eligible": check.scalar(select(func.sum(CreditMapAggregate.count))) or 0,
            "gps": check.scalar(select(func.sum(CreditMapAddress.count))) or 0,
        }
    if (
        counts["aggregate_rows"] != len(aggregates)
        or counts["street_rows"] != len(streets)
        or counts["address_rows"] != len(addresses)
        or counts["eligible"] != expected["cpf_validos_formato"]
        or counts["gps"] != expected["aprovados_com_coordenadas_validas"]
    ):
        fail("post_commit_counts_mismatch")
    print(json.dumps({
        "ok": True, "import_finished": True,
        "source_sha256_prefix": sha[:16], "stats": stats,
        "database": counts,
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
