from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from html.parser import HTMLParser

import pytest

from fastapi.testclient import TestClient

from app.main import app
from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import (
    Campaign,
    Contact, Conversation, ConversationAssignment, ConversationQueueLink,
    ConversationZApiInstance, Message, ServiceQueue, ServiceQueueMember, User,
)
from app.security import hash_password
from app.services.zapi_instances import bind_queue_instance, create_zapi_instance
from app.services.conversation_engine import conversation_visible_to_user, payload_message_content
from app.services.media_storage import MediaStorage
from app.services.zapi import ZApiService


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login(client: TestClient) -> None:
    page = client.get("/login")
    token = _csrf(page.text)
    response = client.post(
        "/login",
        data={
            "email": "admin@sulnet.local",
            "password": "Sulnet@2026!",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_open_conversation_from_lead():
    with TestClient(app) as client:
        _login(client)
        lead = client.get("/leads/1")
        token = _csrf(lead.text)
        response = client.post(
            "/leads/1/conversation",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/conversations/")
        inbox = client.get(response.headers["location"])
        assert inbox.status_code == 200
        assert "Caixa de entrada" in inbox.text
        assert "conversation-page-content" in inbox.text
        assert "Clínica Horizonte" in inbox.text
    assert 'class="chat-queue-context-name' in inbox.text
    assert 'class="chat-queue-chip' in inbox.text
    assert 'class="conversation-queue-row"' in inbox.text
    assert "Fila do atendimento" in inbox.text
    # Um botao Audio comum; autorizacao nativa so quando Chrome bloqueia.
    assert '<microphone data-native-microphone class="inbox-microphone-control"' in inbox.text
    assert 'class="inbox-microphone-dialog"' in inbox.text
    assert '<usermedia' not in inbox.text
    assert 'HTMLMicrophoneElement' in inbox.text
    assert 'data-record-standard title="Gravar áudio"' in inbox.text
    assert "element.addEventListener('track'" in inbox.text
    assert 'startRecorder(new MediaStream([track]), true)' in inbox.text
    assert 'getUserMedia(audioOnlyConstraints)' in inbox.text
    assert 'video: false' in inbox.text
    assert 'data-record-stop-button' in inbox.text
    assert 'data-media-audio-preview' in inbox.text
    assert 'data-mic-copy-settings' not in inbox.text
    assert 'chrome://settings/content/siteDetails?site=' not in inbox.text


def test_inbox_native_microphone_javascript_syntax():
    """Garante que o código inline do gravador continue executável no Chrome."""
    import pytest

    if not shutil.which("node"):
        pytest.skip("Node.js indisponível no ambiente de teste")
    class InlineScriptCollector(HTMLParser):
        def __init__(self):
            super().__init__()
            self.scripts = []
            self.in_script = False

        def handle_starttag(self, tag, attrs):
            if tag == "script":
                self.in_script = True
                self.scripts.append("")

        def handle_data(self, data):
            if self.in_script:
                self.scripts[-1] += data

        def handle_endtag(self, tag):
            if tag == "script":
                self.in_script = False

    template = Path(__file__).parents[1] / "app/templates/conversations/inbox.html"
    collector = InlineScriptCollector()
    collector.feed(template.read_text(encoding="utf-8"))
    assert collector.scripts, "O inbox precisa inicializar os controles de gravação"
    for script in collector.scripts:
        result = subprocess.run(
            ["node", "--check"], input=script, text=True,
            capture_output=True, check=False, timeout=20,
        )
        assert result.returncode == 0, result.stderr


def test_send_message_from_inbox(monkeypatch):
    # Este teste legado cobre o fluxo de envio imediato em conversa ativa.
    monkeypatch.setattr("app.routers.conversations.needs_pacing", lambda *_: False)
    monkeypatch.setattr(
        ZApiService,
        "send_text",
        lambda self, *, phone, message: {
            "messageId": "TEST-INBOX-MESSAGE-1",
            "zaapId": "TEST-ZAAP-1",
        },
    )
    with TestClient(app) as client:
        _login(client)
        lead = client.get("/leads/1")
        token = _csrf(lead.text)
        opened = client.post(
            "/leads/1/conversation",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        location = opened.headers["location"]
        inbox = client.get(location)
        token = _csrf(inbox.text)
        sent = client.post(
            f"{location}/send",
            data={
                "csrf_token": token,
                "message": "Mensagem automatizada de teste do Inbox.",
            },
            follow_redirects=False,
        )
        assert sent.status_code == 303
        page = client.get(location)
        assert "Mensagem automatizada de teste do Inbox." in page.text


def test_inbox_requires_login():
    with TestClient(app) as client:
        response = client.get("/conversations", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_consultant_cannot_see_another_consultants_conversation():
    owner = User(id=101, name="Consultor A", email="a@example.com", password_hash="x", role="CONSULTOR", team="B2B")
    other = User(id=102, name="Consultor B", email="b@example.com", password_hash="x", role="CONSULTOR", team="B2B")
    admin = User(id=1, name="Admin", email="admin@example.com", password_hash="x", role="ADMIN", team="LOJA")
    conversation = Conversation(id=900, contact_id=1, assigned_user_id=owner.id, team="B2B", status="EM_ATENDIMENTO")
    assert conversation_visible_to_user(conversation, owner) is True
    assert conversation_visible_to_user(conversation, other) is False
    assert conversation_visible_to_user(conversation, admin) is True



def test_button_reply_is_parsed_as_inbound_text():
    message_type, body, media_url, mime_type = payload_message_content(
        {
            "buttonsResponseMessage": {
                "selectedButtonId": "campaign_10_reply_1",
                "selectedDisplayText": "Tenho interesse",
            }
        }
    )
    assert message_type == "TEXT"
    assert body == "Tenho interesse"
    assert media_url is None
    assert mime_type is None


def test_realtime_inbox_api_lists_new_conversation():
    with TestClient(app) as client:
        _login(client)
        lead = client.get("/leads/1")
        token = _csrf(lead.text)
        opened = client.post(
            "/leads/1/conversation",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert opened.status_code == 303
        state = client.get("/api/conversations/inbox?view=active")
        assert state.status_code == 200
        payload = state.json()
        assert payload["counts"]["active"] >= 1
        assert any(item["name"] == "Clínica Horizonte" for item in payload["items"])
    assert all("queue_id" in item and "queue_name" in item for item in payload["items"])


def test_send_image_from_inbox(monkeypatch):
    # Este teste legado cobre o fluxo de envio imediato em conversa ativa.
    monkeypatch.setattr("app.routers.conversations.needs_pacing", lambda *_: False)
    monkeypatch.setattr(
        MediaStorage,
        "upload_bytes",
        lambda self, **kwargs: ("s3://conversationmedia/test/foto.jpg", "foto.jpg"),
    )
    monkeypatch.setattr(
        MediaStorage,
        "presigned_download_url",
        lambda self, value, **kwargs: "https://signed.example/foto.jpg",
    )
    monkeypatch.setattr(
        ZApiService,
        "send_image",
        lambda self, *, phone, image_url, caption="": {
            "messageId": "TEST-IMAGE-1",
            "zaapId": "TEST-ZAAP-IMAGE-1",
        },
    )
    with TestClient(app) as client:
        _login(client)
        lead = client.get("/leads/1")
        token = _csrf(lead.text)
        opened = client.post(
            "/leads/1/conversation",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        location = opened.headers["location"]
        inbox = client.get(location)
        token = _csrf(inbox.text)
        sent = client.post(
            f"{location}/media",
            data={"csrf_token": token, "caption": "Foto de teste"},
            files={"media": ("foto.jpg", b"\xff\xd8\xff\xe0" + b"x" * 32, "image/jpeg")},
        )
        assert sent.status_code == 200
        assert sent.json()["ok"] is True
        page = client.get(location)
        assert "Foto de teste" in page.text
        assert "message-media-preview" in page.text


@pytest.mark.parametrize(
    ("file_name", "file_payload", "file_mime"),
    [
        ("audio.ogg", b"OggS" + b"x" * 32, "audio/ogg"),
        ("audio.webm", b"\x1a\x45\xdf\xa3" + b"x" * 32, "audio/webm;codecs=opus"),
    ],
)
def test_send_audio_from_inbox(monkeypatch, file_name, file_payload, file_mime):
    # Ambos os formatos devem seguir ate a Z-API como mensagem de AUDIO.
    if file_name.endswith(".webm"):
        def fake_convert(data, *, filename, mime_type):
            assert filename == file_name
            assert mime_type == "audio/webm"
            assert data == file_payload
            return b"OggS" + b"x" * 32, "audio.ogg", "audio/ogg"
        monkeypatch.setattr("app.routers.conversations.normalize_audio", fake_convert)
    monkeypatch.setattr("app.routers.conversations.needs_pacing", lambda *_: False)
    monkeypatch.setattr(
        MediaStorage,
        "upload_bytes",
        lambda self, **kwargs: ("s3://conversationmedia/test/audio.ogg", "audio.ogg"),
    )
    monkeypatch.setattr(
        MediaStorage,
        "presigned_download_url",
        lambda self, value, **kwargs: "https://signed.example/audio.ogg",
    )
    monkeypatch.setattr(
        ZApiService,
        "send_audio",
        lambda self, *, phone, audio_url: {
            "messageId": "TEST-AUDIO-" + file_name,
            "zaapId": "TEST-ZAAP-AUDIO-" + file_name,
        },
    )
    with TestClient(app) as client:
        _login(client)
        lead = client.get("/leads/1")
        token = _csrf(lead.text)
        opened = client.post(
            "/leads/1/conversation",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        location = opened.headers["location"]
        inbox = client.get(location)
        token = _csrf(inbox.text)
        sent = client.post(
            f"{location}/media",
            data={"csrf_token": token, "caption": ""},
            files={"media": (file_name, file_payload, file_mime)},
        )
        assert sent.status_code == 200
        assert sent.json()["type"] == "AUDIO"
        page = client.get(location)
        assert "message-audio-wrap" in page.text


def test_quick_start_from_phone_opens_chat_without_template_or_message():
    """Vendedor digita só o número e vê o chat; primeiro envio continua manual."""
    with TestClient(app) as client:
        _login(client)
        with SessionLocal() as db:
            queue = ServiceQueue(
                name="Fila Rápida WhatsApp Teste",
                code="QUICK-CHAT-TEST-01",
                team="LOJA",
                routing_strategy="MANUAL",
                active=True,
            )
            db.add(queue)
            db.flush()
            instance = create_zapi_instance(
                db, name="Z-API Chat Rápido Teste",
                phone_label="Teste", base_url="https://api.z-api.io",
                instance_id="test-quick-chat-zapi-2026",
                instance_token="test-quick-chat-token",
                client_token="test-quick-chat-client-token",
                created_by_id=None,
            )
            bind_queue_instance(db, queue.id, instance.id)
            queue_id, instance_id = queue.id, instance.id
            db.commit()

        inbox = client.get("/conversations")
        assert inbox.status_code == 200
        assert "Nova conversa por telefone" in inbox.text
        assert 'action="/conversations/start"' in inbox.text
        assert 'id="inbox-quick-chat-template"' in inbox.text
        assert ">NENHUM<" in inbox.text
        assert 'name="template_id"' in inbox.text
        token = _csrf(inbox.text)
        created = client.post(
            "/conversations/start",
            data={"csrf_token": token, "queue_id": queue_id,
                  "phone": "(88) 99992-1143", "template_id": ""},
            follow_redirects=False,
        )
        assert created.status_code == 303
        assert created.headers["location"].startswith("/conversations/")
        assert "?quick=1" in created.headers["location"]
        conversation_id = int(created.headers["location"].split("/")[2].split("?")[0])
        page = client.get(created.headers["location"])
        assert page.status_code == 200
        assert 'class="chat-send-form"' in page.text
        assert "5588999921143" in page.text or "99992-1143" in page.text
        with SessionLocal() as db:
            convo = db.get(Conversation, conversation_id)
            assert convo.source == "MANUAL"
            assert convo.status == "ATRIBUIDO"
            assert convo.assigned_user_id is not None
            link = db.scalar(select(ConversationQueueLink).where(
                ConversationQueueLink.conversation_id == conversation_id
            ))
            assert link is not None and link.queue_id == queue_id
            conn = db.scalar(select(ConversationZApiInstance).where(
                ConversationZApiInstance.conversation_id == conversation_id
            ))
            assert conn is not None and conn.zapi_instance_id == instance_id
            assert db.scalar(select(func.count(Message.id)).where(
                Message.conversation_id == conversation_id
            )) == 0, "Abrir uma conversa não pode disparar mensagem automática"

        opened_again = client.post(
            "/conversations/start",
            data={"csrf_token": token, "queue_id": queue_id,
                  "phone": "5588999921143"},
            follow_redirects=False,
        )
        assert opened_again.status_code == 303
        assert opened_again.headers["location"] == created.headers["location"]
        with SessionLocal() as db:
            contact = db.scalar(select(Contact).where(Contact.phone == "5588999921143"))
            assert contact is not None
            assert db.scalar(select(func.count(Conversation.id)).where(
                Conversation.contact_id == contact.id
            )) == 1


def test_quick_start_rejects_unavailable_queue_and_bad_phone():
    """Apenas filas autorizadas podem ser usadas: sem desvio de instância."""
    with TestClient(app) as client:
        _login(client)
        token = _csrf(client.get("/conversations").text)
        unknown_queue = client.post(
            "/conversations/start",
            data={"csrf_token": token, "queue_id": 999999,
                  "phone": "(88) 99992-1155"},
            follow_redirects=False,
        )
        assert unknown_queue.status_code == 303
        assert unknown_queue.headers["location"] == "/conversations"
        with SessionLocal() as db:
            assert db.scalar(select(Contact.id).where(
                Contact.phone == "5588999921155"
            )) is None

        with SessionLocal() as db:
            queue = ServiceQueue(
                name="Fila Rápida Inválido", code="QUICK-CHAT-INVALID-01",
                team="LOJA", active=True, routing_strategy="MANUAL",
            )
            db.add(queue)
            db.flush()
            instance = create_zapi_instance(
                db, name="Z-API Chat Rápido Inválido",
                phone_label="Teste", base_url="https://api.z-api.io",
                instance_id="test-quick-chat-invalid-zapi-2026",
                instance_token="test-quick-chat-token-invalid",
                client_token="test-quick-chat-client-invalid",
                created_by_id=None,
            )
            bind_queue_instance(db, queue.id, instance.id)
            queue_id = queue.id
            db.commit()
        invalid = client.post(
            "/conversations/start",
            data={"csrf_token": token, "queue_id": queue_id, "phone": "123"},
            follow_redirects=False,
        )
        assert invalid.status_code == 303
        assert invalid.headers["location"] == "/conversations"


def test_consultant_quick_start_keeps_owner_and_stays_in_her_inbox():
    """Se a vendedora inicia Nova conversa, fica gravada com ela e permanece na caixa."""
    with TestClient(app) as client:
        with SessionLocal() as db:
            seller = User(
                name="Natine Teste",
                email="natine.quickstart@sulnet.local",
                password_hash=hash_password("SenhaNatine@2026"),
                role="CONSULTOR",
                team="DIGITAL",
                active=True,
                must_change_password=False,
            )
            db.add(seller)
            db.flush()
            queue = ServiceQueue(
                name="Fila Natine Quick",
                code="NATINE-QUICK-OWNER-01",
                team="DIGITAL",
                routing_strategy="MANUAL",
                active=True,
            )
            db.add(queue)
            db.flush()
            db.add(ServiceQueueMember(queue_id=queue.id, user_id=seller.id, active=True))
            instance = create_zapi_instance(
                db,
                name="Z-API Natine Quick",
                phone_label="Natine",
                base_url="https://api.z-api.io",
                instance_id="test-natine-quick-owner-zapi",
                instance_token="test-natine-quick-token",
                client_token="test-natine-quick-client",
                created_by_id=None,
            )
            bind_queue_instance(db, queue.id, instance.id)
            # Disparo antigo sem resposta: limbo que sumia da caixa após reabrir.
            contact = Contact(phone="5555984428413", name="Cliente Natine")
            db.add(contact)
            db.flush()
            orphan = Conversation(
                contact_id=contact.id,
                assigned_user_id=None,
                team="DIGITAL",
                source="DISPARO:99",
                status="AGUARDANDO_CLIENTE",
            )
            db.add(orphan)
            db.flush()
            db.add(ConversationQueueLink(
                conversation_id=orphan.id, queue_id=queue.id, origin="DISPATCH",
            ))
            db.add(ConversationZApiInstance(
                conversation_id=orphan.id, zapi_instance_id=instance.id,
            ))
            db.add(Message(
                conversation_id=orphan.id, contact_id=contact.id,
                direction="OUTBOUND", message_type="TEXT",
                body="Disparo antigo", status="SENT",
            ))
            seller_id, queue_id, orphan_id = seller.id, queue.id, orphan.id
            db.commit()

        login_page = client.get("/login")
        token = _csrf(login_page.text)
        assert client.post(
            "/login",
            data={
                "email": "natine.quickstart@sulnet.local",
                "password": "SenhaNatine@2026",
                "csrf_token": token,
            },
            follow_redirects=False,
        ).status_code == 303
        # Login inicia offline; consultora precisa entrar no WhatsApp para o inbox.
        dash = client.get("/dashboard")
        token = _csrf(dash.text)
        assert client.post(
            "/api/whatsapp-presence/join",
            data={"csrf_token": token},
        ).status_code == 200
        inbox = client.get("/conversations")
        assert inbox.status_code == 200
        token = _csrf(inbox.text)
        started = client.post(
            "/conversations/start",
            data={
                "csrf_token": token,
                "queue_id": queue_id,
                "phone": "55984428413",
            },
            follow_redirects=False,
        )
        assert started.status_code == 303
        assert started.headers["location"].startswith(f"/conversations/{orphan_id}")
        opened = client.get(started.headers["location"])
        assert opened.status_code == 200
        assert "Cliente Natine" in opened.text or "5555984428413" in opened.text

        with SessionLocal() as db:
            conversation = db.get(Conversation, orphan_id)
            assert conversation is not None
            assert conversation.assigned_user_id == seller_id
            assert conversation.status == "ATRIBUIDO"
            assert conversation.source == "MANUAL"
            claim = db.scalar(
                select(ConversationAssignment).where(
                    ConversationAssignment.conversation_id == orphan_id,
                    ConversationAssignment.event == "CLAIMED",
                    ConversationAssignment.user_id == seller_id,
                )
            )
            assert claim is not None

        active = client.get("/api/conversations/inbox?view=active").json()["items"]
        assert any(
            item["id"] == orphan_id and item.get("owner") == "Natine Teste"
            for item in active
        )


def test_quick_start_with_campaign_template_prefills_composer_without_sending():
    """Template opcional preenche o compositor; NENHUM e ausência não disparam envio."""
    with TestClient(app) as client:
        _login(client)
        with SessionLocal() as db:
            queue = ServiceQueue(
                name="Fila Template Quick",
                code="QUICK-CHAT-TEMPLATE-01",
                team="LOJA",
                routing_strategy="MANUAL",
                active=True,
            )
            db.add(queue)
            db.flush()
            instance = create_zapi_instance(
                db, name="Z-API Template Quick",
                phone_label="Template", base_url="https://api.z-api.io",
                instance_id="test-quick-chat-template-zapi",
                instance_token="test-quick-chat-template-token",
                client_token="test-quick-chat-template-client",
                created_by_id=None,
            )
            bind_queue_instance(db, queue.id, instance.id)
            campaign = Campaign(
                name="650Mb 3 meses - Template Teste",
                channel="WHATSAPP",
                status="ATIVA",
                sequence_json=(
                    '[{"day":1,"message":"Olá {{nome}}! Oferta 650Mb da Sulnet."},'
                    '{"day":3,"message":"Posso te ajudar com a oferta de 650Mb?"}]'
                ),
            )
            db.add(campaign)
            db.flush()
            queue_id, campaign_id = queue.id, campaign.id
            db.commit()

        inbox = client.get("/conversations")
        assert inbox.status_code == 200
        assert "650Mb 3 meses - Template Teste" in inbox.text
        assert "Mensagens padronizadas" in inbox.text
        assert "650Mb 3 meses - Template Teste · dia 3" in inbox.text
        token = _csrf(inbox.text)

        none_option = client.post(
            "/conversations/start",
            data={
                "csrf_token": token,
                "queue_id": queue_id,
                "phone": "(88) 99992-2201",
                "template_id": "",
            },
            follow_redirects=False,
        )
        assert none_option.status_code == 303
        empty_page = client.get(none_option.headers["location"])
        assert empty_page.status_code == 200
        assert "Oferta 650Mb da Sulnet" not in empty_page.text

        with_template = client.post(
            "/conversations/start",
            data={
                "csrf_token": token,
                "queue_id": queue_id,
                "phone": "(88) 99992-2202",
                "template_id": f"campaign:{campaign_id}",
            },
            follow_redirects=False,
        )
        assert with_template.status_code == 303
        page = client.get(with_template.headers["location"])
        assert page.status_code == 200
        assert "Oferta 650Mb da Sulnet" in page.text
        conversation_id = int(with_template.headers["location"].split("/")[2].split("?")[0])
        with SessionLocal() as db:
            assert db.scalar(select(func.count(Message.id)).where(
                Message.conversation_id == conversation_id
            )) == 0

        json_start = client.post(
            "/conversations/start",
            data={
                "csrf_token": token,
                "queue_id": queue_id,
                "phone": "(88) 99992-2203",
                "template_id": f"campaign:{campaign_id}:step:2",
            },
            headers={"Accept": "application/json"},
        )
        assert json_start.status_code == 200
        payload = json_start.json()
        assert payload["ok"] is True
        assert "oferta de 650Mb" in payload["draft"]
        with SessionLocal() as db:
            assert db.scalar(select(func.count(Message.id)).where(
                Message.conversation_id == payload["conversation_id"]
            )) == 0
