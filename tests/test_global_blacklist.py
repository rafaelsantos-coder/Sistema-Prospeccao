"""Blacklist global: o último emissor deve bloquear TODAS as instâncias/tipos."""
from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
import pytest

from app.database import Base
from app.models import Contact, Conversation, GlobalContactBlacklist, Lead
from app.services.global_blacklist import (
    BlacklistPhoneError,
    block_phone,
    canonical_phone,
    is_globally_blocked,
    parse_blacklist_csv,
    remove_phone_block,
)
from app.services.zapi import ZApiBlacklistedRecipient, ZApiService


def test_import_csv_accepts_variants_and_deduplicates():
    payload = (
        "telefone;nome;motivo\n"
        "(55) 99999-7700;Cliente 1;Não deseja contato\n"
        "55 99999 7700;Duplicado;\n"
        "55 99999 7701;Cliente 2;Solicitou remoção\n"
        "123;Invalido;\n"
    ).encode("utf-8-sig")
    items, summary = parse_blacklist_csv(payload)
    assert len(items) == 2
    assert summary == {"linhas": 4, "invalidos": 1, "duplicados_arquivo": 1}
    assert items[0]["phone"] == "5555999997700"
    bare, _ = parse_blacklist_csv(b"55999997700\n55999997701\n")
    assert len(bare) == 2
    with pytest.raises(BlacklistPhoneError):
        canonical_phone("sem telefone")
    with pytest.raises(ValueError):
        parse_blacklist_csv(b"nome,telefone\nCliente,5512345678" * 99999)


def test_block_is_global_across_queues_instances_and_message_types(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    number = "5555999887700"
    try:
        with Session(engine) as db:
            lead = Lead(business_name="Empresa teste", phone=number)
            contact = Contact(name="Cliente", phone=number)
            db.add_all([lead, contact])
            db.flush()
            conv = Conversation(contact_id=contact.id, lead_id=lead.id, status="EM_ATENDIMENTO")
            db.add(conv)
            db.flush()
            row, created = block_phone(db, phone="(55) 99988-7700", source="CONVERSA")
            assert created
            assert row.phone == number
            assert db.get(Lead, lead.id).do_not_contact is True
            assert db.get(Conversation, conv.id).status == "BLOQUEADO"
            same, created_again = block_phone(db, phone=number, source="CSV")
            assert same.id == row.id
            assert not created_again
            db.commit()

        import app.services.global_blacklist as blacklist_mod
        monkeypatch.setattr(blacklist_mod, "SessionLocal", lambda: Session(engine))

        def forbidden_http_client(*args, **kwargs):
            raise AssertionError("HTTP não pode ser iniciado para contato bloqueado")
        import app.services.zapi as zapi_mod
        monkeypatch.setattr(zapi_mod.httpx, "Client", forbidden_http_client)

        for instance in ("INSTANCIA_A", "INSTANCIA_B"):
            api = ZApiService(
                base_url="https://example.test", instance_id=instance,
                instance_token="TESTE", client_token="TESTE",
            )
            tests = [
                lambda api=api: api.send_text(phone=number, message="Olá"),
                lambda api=api: api.send_button_list(phone=number, message="Oferta", buttons=[{"label":"Sim"}]),
                lambda api=api: api.send_link_button(phone=number, message="Contrato", label="Assinar", url="https://example.test"),
                lambda api=api: api.send_image(phone=number, image_url="https://example.test/img.png"),
                lambda api=api: api.send_video(phone=number, video_url="https://example.test/video.mp4"),
                lambda api=api: api.send_audio(phone=number, audio_url="https://example.test/audio.mp3"),
                lambda api=api: api.send_document(
                    phone=number, document_url="https://example.test/a.pdf",
                    extension="pdf", file_name="a.pdf",
                ),
            ]
            for attempt in tests:
                with pytest.raises(ZApiBlacklistedRecipient, match="blacklist global"):
                    attempt()

        with Session(engine) as db:
            entry = db.scalar(select(GlobalContactBlacklist).where(
                GlobalContactBlacklist.phone == number
            ))
            assert is_globally_blocked(db, number)
            remove_phone_block(db, entry=entry, actor_id=1)
            # O registro permanece para auditoria, mas inativo após remoção autorizada.
            assert not is_globally_blocked(db, number)
    finally:
        engine.dispose()


def test_legacy_do_not_contact_is_respected_without_list_entry():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            db.add(Lead(business_name="Optout antigo", phone="5555999886600", do_not_contact=True))
            db.commit()
            assert is_globally_blocked(db, "(55) 99988-6600")
    finally:
        engine.dispose()
