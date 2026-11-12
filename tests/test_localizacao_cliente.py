"""Localização enviada pelo cliente vira link de mapa que o vendedor consegue abrir."""

import json
from uuid import uuid4

from app.database import SessionLocal
from app.models import Contact, Conversation, Message, MessageEvent
from app.services.conversation_engine import (
    location_map_url,
    location_urls_for,
    payload_message_content,
)

PAYLOAD = {"location": {"latitude": -27.8712, "longitude": -54.4813, "address": "Rua das Flores, 100"}}


def test_link_do_mapa_e_montado_so_com_coordenadas():
    assert location_map_url(PAYLOAD) == "https://www.google.com/maps?q=-27.871200,-54.481300"
    # URL vinda do cliente nunca é usada.
    hostile = {"location": {"latitude": "1", "longitude": "2", "url": "https://evil.example/x"}}
    assert location_map_url(hostile) == "https://www.google.com/maps?q=1.000000,2.000000"
    assert location_map_url({"location": {"latitude": 999, "longitude": 0}}) is None
    assert location_map_url({"location": {"name": "sem coordenadas"}}) is None


def test_corpo_da_mensagem_descreve_a_localizacao():
    kind, body, media_url, _ = payload_message_content(PAYLOAD)
    assert kind == "LOCATION" and media_url is None
    assert "Rua das Flores, 100" in body and "-27.871200,-54.481300" in body
    assert payload_message_content({"location": {}})[1] == "[Localizacao]"


def test_localizacao_antiga_recupera_o_link_pelo_payload_guardado():
    with SessionLocal() as db:
        contact = Contact(phone=f"5555{uuid4().int % 10**8:08d}", name="Darí")
        db.add(contact)
        db.flush()
        conversation = Conversation(contact_id=contact.id, team="B2B", source="WHATSAPP", status="AGUARDANDO_CLIENTE")
        db.add(conversation)
        db.flush()
        message = Message(
            conversation_id=conversation.id,
            contact_id=contact.id,
            direction="INBOUND",
            message_type="LOCATION",
            body="[Localizacao]",
            status="RECEIVED",
        )
        text = Message(
            conversation_id=conversation.id,
            contact_id=contact.id,
            direction="INBOUND",
            message_type="TEXT",
            body="Nao",
            status="RECEIVED",
        )
        db.add_all([message, text])
        db.flush()
        db.add(
            MessageEvent(
                message_id=message.id,
                event_type="RECEIVED_CALLBACK",
                status="RECEIVED",
                payload_json=json.dumps(PAYLOAD),
            )
        )
        db.flush()
        urls = location_urls_for(db, [message, text])
        assert urls == {message.id: "https://www.google.com/maps?q=-27.871200,-54.481300"}
        db.rollback()
