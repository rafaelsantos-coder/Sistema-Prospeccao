"""API oficial do WhatsApp (Meta Cloud API) convivendo com a Z-API."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    Campaign,
    CampaignDispatch,
    CampaignDispatchRecipient,
    Contact,
    Conversation,
    ConversationMetaNumber,
    ConversationQueueLink,
    ConversationZApiInstance,
    Message,
    MetaMessageTemplate,
    ServiceQueue,
    ServiceQueueMetaNumber,
    ServiceQueueZApiInstance,
    User,
)
from app.services import meta_cloud
from app.services.conversation_engine import ensure_contact, ensure_open_conversation
from app.services.meta_cloud import (
    MetaCloudService,
    MetaTemplateRequired,
    MetaWindowClosed,
)
from app.services.meta_numbers import (
    bind_queue_meta_number,
    build_template_message,
    create_meta_number,
    meta_verify_token,
    meta_window_open,
    template_variables,
    unbind_queue_meta_number,
    whatsapp_id_to_phone,
)
from app.services.secret_store import decrypt_secret
from app.services.zapi import ZApiService
from app.services.zapi_instances import (
    bind_queue_instance,
    create_zapi_instance,
    service_for_conversation,
    service_for_queue,
)
from app.timeutils import utcnow


APP_SECRET = "meta-app-secret-for-tests"


class FakeResponse:
    def __init__(self, status_code: int, payload: dict, content: bytes = b"", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeClient:
    """Substitui httpx.Client dentro do módulo meta_cloud."""

    calls: list[dict] = []
    responses: list[FakeResponse] = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, method, url, *, headers=None, json=None, params=None):
        FakeClient.calls.append(
            {"method": method, "url": url, "headers": headers, "json": json, "params": params}
        )
        if FakeClient.responses:
            return FakeClient.responses.pop(0)
        return FakeResponse(
            200,
            {
                "messaging_product": "whatsapp",
                "contacts": [{"input": "x", "wa_id": "5555999990000"}],
                "messages": [{"id": f"wamid.TEST{len(FakeClient.calls)}"}],
            },
        )


@pytest.fixture
def fake_meta(monkeypatch):
    FakeClient.calls = []
    FakeClient.responses = []
    monkeypatch.setattr(meta_cloud.httpx, "Client", FakeClient)
    return FakeClient


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login_admin(client: TestClient) -> str:
    page = client.get("/login")
    token = _csrf(page.text)
    response = client.post(
        "/login",
        data={"email": "admin@sulnet.local", "password": "Sulnet@2026!", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return _csrf(client.get("/settings").text)


def _meta_number(db, suffix: str):
    admin = db.scalar(select(User).where(User.email == "admin@sulnet.local"))
    item = create_meta_number(
        db,
        name=f"Meta {suffix}",
        phone_number_id=f"10{abs(hash(suffix)) % 10**12}",
        waba_id=f"20{abs(hash(suffix)) % 10**12}",
        access_token=f"EAAG-token-{suffix}",
        app_secret=APP_SECRET,
        created_by_id=admin.id if admin else None,
    )
    db.commit()
    db.refresh(item)
    return item


def _zapi_instance(db, suffix: str):
    item = create_zapi_instance(
        db,
        name=f"Z {suffix}",
        phone_label="",
        base_url="https://api.z-api.io",
        instance_id=f"meta-coexist-{suffix}",
        instance_token=f"token-{suffix}",
        client_token="",
        created_by_id=None,
    )
    db.commit()
    return item


def _queue(db, code: str, *, default_inbound: bool = False) -> ServiceQueue:
    queue = ServiceQueue(
        name=f"Fila {code}",
        code=code,
        team="B2B",
        routing_strategy="ROUND_ROBIN",
        is_default_inbound=default_inbound,
        active=True,
    )
    db.add(queue)
    db.flush()
    return queue


def _signed_post(client: TestClient, payload: dict, secret: str = APP_SECRET):
    raw = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/meta",
        content=raw,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
    )


def _inbound_payload(phone_number_id: str, *, wa_id: str, message: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"display_phone_number": "555530000000", "phone_number_id": phone_number_id},
                            "contacts": [{"profile": {"name": "Cliente Meta"}, "wa_id": wa_id}],
                            "messages": [{"from": wa_id, "timestamp": "1700000000", **message}],
                        },
                    }
                ],
            }
        ],
    }


# ------------------------------------------------------------------ unitários


def test_whatsapp_id_adds_brazilian_ninth_digit():
    assert whatsapp_id_to_phone("555599991234") == "5555999991234"
    assert whatsapp_id_to_phone("5555999991234") == "5555999991234"
    # Fixo (começa com 2-5) não ganha 9.
    assert whatsapp_id_to_phone("555532221234") == "555532221234"


def test_send_text_uses_bearer_header_and_returns_message_id(fake_meta):
    service = MetaCloudService(phone_number_id="123", access_token="SECRET-TOKEN")
    result = service.send_text(phone="55 55 99999-1234", message="Olá")
    call = fake_meta.calls[0]
    assert call["url"].endswith("/123/messages")
    assert "SECRET-TOKEN" not in call["url"]
    assert call["headers"]["Authorization"] == "Bearer SECRET-TOKEN"
    assert call["json"]["to"] == "5555999991234"
    assert call["json"]["text"]["body"] == "Olá"
    assert result["messageId"].startswith("wamid.")
    assert result["id"] == result["messageId"]


def test_window_closed_error_is_typed_and_friendly(fake_meta):
    fake_meta.responses = [
        FakeResponse(400, {"error": {"code": 131047, "message": "Re-engagement message"}})
    ]
    service = MetaCloudService(phone_number_id="123", access_token="t")
    with pytest.raises(MetaWindowClosed) as exc:
        service.send_text(phone="5555999991234", message="Oi")
    assert "24h" in str(exc.value)


def test_button_list_over_interactive_limit_sends_text_first(fake_meta):
    service = MetaCloudService(phone_number_id="123", access_token="t")
    long_text = "x" * 1500
    result = service.send_button_list(
        phone="5555999991234",
        message=long_text,
        buttons=[{"id": "a", "label": "Tenho interesse"}],
    )
    assert fake_meta.calls[0]["json"]["type"] == "text"
    interactive = fake_meta.calls[1]["json"]["interactive"]
    assert interactive["type"] == "button"
    assert interactive["action"]["buttons"][0]["reply"]["title"] == "Tenho interesse"
    assert result["messageId"] == "wamid.TEST1"


def test_template_message_positional_named_and_header():
    template = MetaMessageTemplate(
        name="boas_vindas",
        language="pt_BR",
        status="APPROVED",
        parameter_format="POSITIONAL",
        header_format="",
        body_text="Olá {{1}}, aqui é da Sulnet sobre a {{2}}.",
        components_json="[]",
    )
    components, rendered = build_template_message(template, ["Ana", "Padaria\nCentral"])
    assert components == [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": "Ana"},
                {"type": "text", "text": "Padaria Central"},
            ],
        }
    ]
    assert rendered == "Olá Ana, aqui é da Sulnet sobre a Padaria Central."

    named = MetaMessageTemplate(
        name="nomeado",
        language="pt_BR",
        status="APPROVED",
        parameter_format="NAMED",
        header_format="IMAGE",
        body_text="Oi {{first_name}}!",
        components_json="[]",
    )
    assert template_variables(named.body_text) == ["first_name"]
    with pytest.raises(meta_cloud.MetaCloudError):
        build_template_message(named, ["Ana"])  # cabeçalho de imagem exige mídia
    components, _ = build_template_message(named, ["Ana"], header_media_url="https://cdn/img.png")
    assert components[0]["parameters"][0]["image"]["link"] == "https://cdn/img.png"
    assert components[1]["parameters"][0]["parameter_name"] == "first_name"


# ----------------------------------------------------------------- integração


def test_meta_credentials_are_encrypted_at_rest():
    with TestClient(app):
        db = SessionLocal()
        try:
            item = _meta_number(db, "ENCRYPT")
            assert "EAAG-token-ENCRYPT" not in item.access_token_encrypted
            assert APP_SECRET not in item.app_secret_encrypted
            assert decrypt_secret(item.access_token_encrypted) == "EAAG-token-ENCRYPT"
            assert len(meta_verify_token(item)) >= 32
        finally:
            db.close()


def test_queue_with_meta_number_sends_by_meta_and_keeps_zapi_binding():
    with TestClient(app):
        db = SessionLocal()
        try:
            zapi = _zapi_instance(db, "ROUTE")
            number = _meta_number(db, "ROUTE")
            queue = _queue(db, "META-ROUTE")
            bind_queue_instance(db, queue.id, zapi.id)
            db.commit()

            assert isinstance(service_for_queue(db, queue.id), ZApiService)

            bind_queue_meta_number(db, queue.id, number.id)
            db.commit()
            service = service_for_queue(db, queue.id)
            assert isinstance(service, MetaCloudService)
            assert service.phone_number_id == number.phone_number_id
            # O vínculo Z-API continua salvo.
            assert db.scalar(
                select(ServiceQueueZApiInstance).where(ServiceQueueZApiInstance.queue_id == queue.id)
            )

            assert unbind_queue_meta_number(db, queue.id) is True
            db.commit()
            assert isinstance(service_for_queue(db, queue.id), ZApiService)
        finally:
            db.close()


def test_meta_webhook_verification_challenge():
    with TestClient(app) as client:
        db = SessionLocal()
        try:
            number = _meta_number(db, "VERIFY")
            token = meta_verify_token(number)
        finally:
            db.close()
        ok = client.get(
            "/webhooks/meta",
            params={"hub.mode": "subscribe", "hub.verify_token": token, "hub.challenge": "12345"},
        )
        assert ok.status_code == 200
        assert ok.text == "12345"
        bad = client.get(
            "/webhooks/meta",
            params={"hub.mode": "subscribe", "hub.verify_token": "errado", "hub.challenge": "12345"},
        )
        assert bad.status_code == 403


def test_meta_webhook_rejects_bad_signature():
    with TestClient(app) as client:
        db = SessionLocal()
        try:
            number = _meta_number(db, "SIGNATURE")
            phone_number_id = number.phone_number_id
        finally:
            db.close()
        payload = _inbound_payload(
            phone_number_id,
            wa_id="555599990001",
            message={"id": "wamid.BADSIG", "type": "text", "text": {"body": "oi"}},
        )
        response = _signed_post(client, payload, secret="outro-segredo")
        assert response.status_code == 403
        db = SessionLocal()
        try:
            assert db.scalar(select(Message).where(Message.provider_message_id == "wamid.BADSIG")) is None
        finally:
            db.close()


def test_meta_webhook_ignores_unknown_number_with_200():
    with TestClient(app) as client:
        payload = _inbound_payload(
            "999000999000",
            wa_id="555599990009",
            message={"id": "wamid.UNKNOWN", "type": "text", "text": {"body": "oi"}},
        )
        response = _signed_post(client, payload, secret="qualquer")
        assert response.status_code == 200
        assert response.json()["ignored"] == "unknown_number"


def test_meta_inbound_text_creates_conversation_on_meta_queue_and_status_updates():
    with TestClient(app) as client:
        db = SessionLocal()
        try:
            number = _meta_number(db, "INBOUND")
            queue = _queue(db, "META-INBOUND", default_inbound=True)
            bind_queue_meta_number(db, queue.id, number.id)
            db.commit()
            phone_number_id = number.phone_number_id
            number_id = number.id
            queue_id = queue.id
        finally:
            db.close()

        payload = _inbound_payload(
            phone_number_id,
            wa_id="555599990002",
            message={"id": "wamid.INBOUND1", "type": "text", "text": {"body": "Quero fibra"}},
        )
        response = _signed_post(client, payload)
        assert response.status_code == 200, response.text
        assert response.json()["processed"] == 1

        status_payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {"phone_number_id": phone_number_id},
                                "statuses": [{"id": "wamid.INBOUND1", "status": "read"}],
                            },
                        }
                    ],
                }
            ],
        }
        assert _signed_post(client, status_payload).status_code == 200

        db = SessionLocal()
        try:
            message = db.scalar(select(Message).where(Message.provider_message_id == "wamid.INBOUND1"))
            assert message is not None
            assert message.body == "Quero fibra"
            assert message.direction == "INBOUND"
            assert message.status == "READ"
            conversation = db.get(Conversation, message.conversation_id)
            contact = db.get(Contact, conversation.contact_id)
            assert contact.phone == "5555999990002"  # nono dígito acrescentado
            link = db.scalar(
                select(ConversationMetaNumber).where(ConversationMetaNumber.conversation_id == conversation.id)
            )
            assert link is not None and link.meta_number_id == number_id
            queue_link = db.scalar(
                select(ConversationQueueLink).where(ConversationQueueLink.conversation_id == conversation.id)
            )
            assert queue_link is not None and queue_link.queue_id == queue_id
            assert meta_window_open(db, number_id, "5555999990002") is True
            # Resposta da conversa sai pela Meta.
            assert isinstance(service_for_conversation(db, conversation.id), MetaCloudService)
        finally:
            db.close()


def test_zapi_inbound_does_not_take_over_meta_conversation():
    with TestClient(app):
        db = SessionLocal()
        try:
            number = _meta_number(db, "ISOLATE")
            zapi = _zapi_instance(db, "ISOLATE")
            contact = ensure_contact(db, "5555999990003")
            meta_conversation = ensure_open_conversation(db, contact, meta_number_id=number.id)
            db.flush()
            zapi_conversation = ensure_open_conversation(db, contact, zapi_instance_id=zapi.id)
            db.flush()
            assert zapi_conversation.id != meta_conversation.id
            assert db.scalar(
                select(ConversationZApiInstance).where(
                    ConversationZApiInstance.conversation_id == meta_conversation.id
                )
            ) is None
            db.rollback()
        finally:
            db.close()


def _dispatch_setup(db, code: str, sequence: list[dict], phone: str):
    number = _meta_number(db, code)
    queue = _queue(db, code)
    bind_queue_meta_number(db, queue.id, number.id)
    campaign = Campaign(
        name=f"Campanha {code}",
        channel="WHATSAPP",
        status="APROVADA",
        daily_limit=100,
        team="B2B",
        sequence_json=json.dumps(sequence),
    )
    db.add(campaign)
    db.flush()
    dispatch = CampaignDispatch(
        campaign_id=campaign.id,
        queue_id=queue.id,
        status="ATIVO",
        interval_seconds=10,
        daily_limit=100,
    )
    db.add(dispatch)
    db.flush()
    recipient = CampaignDispatchRecipient(
        dispatch_id=dispatch.id,
        name="Ana",
        phone=phone,
        message_body="Olá Ana",
        status="ENVIANDO",
    )
    db.add(recipient)
    db.commit()
    return number, queue, campaign, recipient


def test_meta_dispatch_without_template_outside_window_fails_without_sending(fake_meta):
    from app.services.dispatch_queue import process_dispatch_recipient

    with TestClient(app):
        db = SessionLocal()
        try:
            _, _, _, recipient = _dispatch_setup(
                db, "META-NOTPL", [{"message": "Olá {{nome}}"}], "5555999990004"
            )
            outcome = process_dispatch_recipient(db, recipient)
            db.commit()
            assert outcome == "failed"
            assert recipient.status == "FALHOU"
            assert "template" in (recipient.error or "")
            assert fake_meta.calls == []
        finally:
            db.close()


def test_meta_dispatch_with_template_sends_template(fake_meta):
    from app.services.dispatch_queue import process_dispatch_recipient

    with TestClient(app):
        db = SessionLocal()
        try:
            number, _, campaign, recipient = _dispatch_setup(
                db, "META-TPL", [{"message": "Olá {{nome}}"}], "5555999990005"
            )
            template = MetaMessageTemplate(
                meta_number_id=number.id,
                name="prospeccao_inicial",
                language="pt_BR",
                status="APPROVED",
                parameter_format="POSITIONAL",
                body_text="Olá {{1}}! Falo da Sulnet.",
                components_json="[]",
            )
            db.add(template)
            db.flush()
            campaign.sequence_json = json.dumps(
                [
                    {
                        "message": "Olá {{nome}}",
                        "quick_replies": [{"id": "r1", "label": "Tenho interesse"}],
                        "meta_template": {"template_id": template.id, "params": ["{{nome}}|cliente"]},
                    }
                ]
            )
            db.commit()
            outcome = process_dispatch_recipient(db, recipient)
            db.commit()
            assert outcome == "sent", recipient.error
            assert len(fake_meta.calls) == 1
            sent = fake_meta.calls[0]["json"]
            assert sent["type"] == "template"
            assert sent["template"]["name"] == "prospeccao_inicial"
            assert sent["template"]["components"][0]["parameters"][0]["text"] == "Ana"
            message = db.scalar(
                select(Message).where(Message.conversation_id == recipient.conversation_id)
            )
            assert message.body == "Olá Ana! Falo da Sulnet."
            assert db.scalar(
                select(ConversationMetaNumber).where(
                    ConversationMetaNumber.conversation_id == recipient.conversation_id
                )
            )
        finally:
            db.close()


def test_meta_dispatch_inside_window_sends_free_text(fake_meta):
    from app.services.dispatch_queue import process_dispatch_recipient

    with TestClient(app):
        db = SessionLocal()
        try:
            number, queue, _, recipient = _dispatch_setup(
                db, "META-WINDOW", [{"message": "Olá {{nome}}"}], "5555999990006"
            )
            contact = ensure_contact(db, "5555999990006")
            conversation = ensure_open_conversation(db, contact, queue_id=queue.id, meta_number_id=number.id)
            db.add(
                Message(
                    conversation_id=conversation.id,
                    contact_id=contact.id,
                    direction="INBOUND",
                    message_type="TEXT",
                    body="oi",
                    status="RECEIVED",
                    created_at=utcnow() - timedelta(hours=2),
                )
            )
            db.commit()
            outcome = process_dispatch_recipient(db, recipient)
            db.commit()
            assert outcome == "sent", recipient.error
            assert fake_meta.calls[0]["json"]["type"] == "text"
        finally:
            db.close()


def test_queue_channel_route_switches_between_zapi_and_meta():
    with TestClient(app) as client:
        token = _login_admin(client)
        db = SessionLocal()
        try:
            number = _meta_number(db, "CHANNEL")
            queue = _queue(db, "META-CHANNEL")
            db.commit()
            number_id, queue_id = number.id, queue.id
        finally:
            db.close()

        response = client.post(
            f"/settings/queues/{queue_id}/channel",
            data={"csrf_token": token, "channel": f"META:{number_id}"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        db = SessionLocal()
        try:
            link = db.scalar(select(ServiceQueueMetaNumber).where(ServiceQueueMetaNumber.queue_id == queue_id))
            assert link is not None and link.meta_number_id == number_id
        finally:
            db.close()

        response = client.post(
            f"/settings/queues/{queue_id}/channel",
            data={"csrf_token": token, "channel": "ZAPI"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        db = SessionLocal()
        try:
            assert db.scalar(
                select(ServiceQueueMetaNumber).where(ServiceQueueMetaNumber.queue_id == queue_id)
            ) is None
        finally:
            db.close()


def test_build_template_payload_valid_and_rejections():
    from app.services.meta_numbers import build_template_payload

    payload = build_template_payload(
        name="Oferta_Fibra",
        category="marketing",
        language="pt_BR",
        body="Olá {{1}}, a fibra chegou em {{2}}!",
        header="Sulnet",
        footer="Responda SAIR",
        body_examples=["Maria", "Santa Maria"],
    )
    assert payload["name"] == "oferta_fibra"
    assert payload["category"] == "MARKETING"
    assert [c["type"] for c in payload["components"]] == ["HEADER", "BODY", "FOOTER"]
    assert payload["components"][1]["example"] == {"body_text": [["Maria", "Santa Maria"]]}

    base = {"name": "t", "category": "MARKETING", "language": "pt_BR"}
    invalid = [
        {**base, "name": "oferta fibra", "body": "x"},
        {**base, "category": "AUTHENTICATION", "body": "x"},
        {**base, "language": "portugues", "body": "x"},
        {**base, "body": "   "},
        {**base, "body": "Oi {{1}} e {{3}} ok", "body_examples": ["a", "b"]},
        {**base, "body": "Oi {{1}} e {{2}} ok", "body_examples": ["a"]},
        {**base, "body": "{{1}}, temos fibra", "body_examples": ["a"]},
        {**base, "body": "Temos fibra para {{1}}.", "body_examples": ["a"]},
        {**base, "body": "x", "header": "Oi {{1}}"},
        {**base, "body": "x", "footer": "y" * 61},
    ]
    for fields in invalid:
        with pytest.raises(meta_cloud.MetaCloudError):
            build_template_payload(**fields)


def test_create_template_route_submits_to_meta_and_stores_pending(fake_meta):
    from app.models import MetaMessageTemplate

    fake_meta.responses.append(
        FakeResponse(200, {"id": "998877", "status": "PENDING", "category": "MARKETING"})
    )
    with TestClient(app) as client:
        token = _login_admin(client)
        db = SessionLocal()
        try:
            number = _meta_number(db, "TPLNEW")
            number_id, waba_id = number.id, number.waba_id
        finally:
            db.close()

        response = client.post(
            "/settings/meta/templates/new",
            data={
                "csrf_token": token,
                "meta_number_id": str(number_id),
                "name": "oferta_teste_rota",
                "category": "MARKETING",
                "language": "pt_BR",
                "body": "Olá {{1}}, temos fibra na sua rua.",
                "examples": "Maria",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        call = fake_meta.calls[-1]
        assert call["method"] == "POST"
        assert call["url"].endswith(f"{waba_id}/message_templates")
        assert call["json"]["name"] == "oferta_teste_rota"
        db = SessionLocal()
        try:
            record = db.scalar(
                select(MetaMessageTemplate).where(
                    MetaMessageTemplate.meta_number_id == number_id,
                    MetaMessageTemplate.name == "oferta_teste_rota",
                )
            )
            assert record is not None
            assert record.status == "PENDING"
            assert record.meta_template_id == "998877"
        finally:
            db.close()


def test_create_template_route_invalid_does_not_call_meta(fake_meta):
    from app.models import MetaMessageTemplate

    with TestClient(app) as client:
        token = _login_admin(client)
        db = SessionLocal()
        try:
            number = _meta_number(db, "TPLBAD")
            number_id = number.id
        finally:
            db.close()
        calls_before = len(fake_meta.calls)
        response = client.post(
            "/settings/meta/templates/new",
            data={
                "csrf_token": token,
                "meta_number_id": str(number_id),
                "name": "Nome Inválido",
                "category": "MARKETING",
                "language": "pt_BR",
                "body": "Texto",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert len(fake_meta.calls) == calls_before
        db = SessionLocal()
        try:
            assert db.scalar(
                select(MetaMessageTemplate).where(MetaMessageTemplate.meta_number_id == number_id)
            ) is None
        finally:
            db.close()


def test_httpx_client_is_restored_after_fake():
    # Garante que o monkeypatch do fixture não vaza para outros testes.
    assert meta_cloud.httpx.Client is httpx.Client


def test_migration_0009_creates_meta_tables_on_existing_database(tmp_path):
    """Caminho de produção: banco já existente (sem as tabelas Meta) recebe só as novas."""
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    from app.database import Base

    meta_tables = {
        "meta_whatsapp_numbers",
        "service_queue_meta_numbers",
        "conversation_meta_numbers",
        "meta_message_templates",
    }
    engine = create_engine(f"sqlite:///{tmp_path / 'upgrade.db'}")
    existing = [table for table in Base.metadata.sorted_tables if table.name not in meta_tables]
    Base.metadata.create_all(engine, tables=existing)

    spec = importlib.util.spec_from_file_location(
        "migration_0009",
        str(__import__("pathlib").Path(__file__).resolve().parents[1] / "alembic/versions/0009_meta_cloud_api.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            module.upgrade()
            module.upgrade()  # idempotente

    inspector = inspect(engine)
    assert meta_tables <= set(inspector.get_table_names())
    for table in meta_tables:
        model_columns = {column.name for column in Base.metadata.tables[table].columns}
        db_columns = {column["name"] for column in inspector.get_columns(table)}
        assert model_columns == db_columns, table

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            module.downgrade()
    assert not (meta_tables & set(inspect(engine).get_table_names()))
