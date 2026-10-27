from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Branch, Lead, User
from app.services.followups import initial_next_action
from app.services.scoring import calculate_sulnet_score, normalize_phone


def import_legacy_sqlite(
    db: Session,
    *,
    path: str | Path,
    user: User,
    branch_id: int | None = None,
) -> dict[str, int]:
    source_path = Path(path).expanduser()
    if not source_path.exists():
        raise FileNotFoundError(f"Banco legado não encontrado: {source_path}")

    connection = sqlite3.connect(str(source_path))
    connection.row_factory = sqlite3.Row
    rows = connection.execute("SELECT * FROM prospects ORDER BY id").fetchall()

    imported = 0
    duplicates = 0
    skipped = 0

    for row in rows:
        data = dict(row)
        phone = normalize_phone(data.get("phone"))
        email = (data.get("email") or "").strip() or None
        duplicate_stmt = select(Lead.id).where(
            or_(
                Lead.phone == phone if phone else False,
                Lead.email == email if email else False,
            )
        )
        if db.scalar(duplicate_stmt):
            duplicates += 1
            continue
        business_name = (data.get("business_name") or "").strip()
        if not business_name:
            skipped += 1
            continue

        mapped = {
            "business_name": business_name,
            "contact_name": data.get("contact_name") or "",
            "phone": phone,
            "email": email,
            "website": data.get("website"),
            "segment": data.get("segment") or "Não informado",
            "city": data.get("location") or (user.branch.city if user.branch else "Santa Rosa"),
            "source": "IMPORTACAO",
            "origin_channel": (data.get("channel") or "WHATSAPP").upper(),
            "team": "B2B",
            "stage": _legacy_stage(data),
            "legacy_score": float(data.get("score") or 0),
            "potential": data.get("potential") or "",
            "notes": data.get("notes") or "",
            "raw_data": data.get("raw_data") or "{}",
            "branch_id": branch_id or user.branch_id,
            "assigned_user_id": user.id,
            "created_by_id": user.id,
        }
        result = calculate_sulnet_score(mapped)
        mapped["score"] = result.score
        mapped["temperature"] = result.temperature
        mapped["next_action_at"] = initial_next_action(mapped["team"])
        db.add(Lead(**mapped))
        imported += 1

    db.commit()
    connection.close()
    return {"total": len(rows), "imported": imported, "duplicates": duplicates, "skipped": skipped}


def _legacy_stage(data: dict) -> str:
    if bool(data.get("converted")):
        return "GANHO"
    if bool(data.get("responded")):
        return "CONTATO_REALIZADO"
    if int(data.get("current_step") or 0) > 0:
        return "TENTATIVA_CONTATO"
    return "NOVO"
