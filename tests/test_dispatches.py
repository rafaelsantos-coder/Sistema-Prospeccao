from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    Campaign,
    CampaignDispatch,
    CampaignDispatchRecipient,
    ConversationQueueLink,
    Message,
    ServiceQueue,
)
from app.services.dispatch_queue import (
    claim_next_dispatch_recipient,
    dispatch_counts,
    parse_dispatch_csv,
    process_dispatch_recipient,
)
from app.services.media_storage import MediaStorage
from app.services.zapi import ZApiService


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _login_admin(client: TestClient) -> str:
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
    return _csrf(client.get("/dispatches").text)


def _campaign_and_queue(db, suffix: str):
    campaign = Campaign(
        name=f"Campanha Disparo CSV {suffix}",
        channel="WHATSAPP",
        status="APROVADA",
        daily_limit=100,
        team="DIGITAL",
        sequence_json=json.dumps(
            [{"message": "Olá {{nome}}, mensagem de teste da Sulnet."}],
            ensure_ascii=False,
        ),
    )
    queue = ServiceQueue(
        name=f"Fila Digital Disparo {suffix}",
        code=f"DIGITAL-DISPATCH-{suffix}",
        team="DIGITAL",
        routing_strategy="ROUND_ROBIN",
        active=True,
    )
    db.add_all([campaign, queue])
    db.flush()
    return campaign, queue


def test_parse_dispatch_csv_accepts_name_and_phone():
    rows, summary = parse_dispatch_csv(
        "nome;telefone\nEmpresa A;5555999991001\nEmpresa B;5555999991002\n".encode()
    )
    assert len(rows) == 2
    assert rows[0]["name"] == "Empresa A"
    assert rows[0]["phone"] == "5555999991001"
    assert summary["valid"] == 2
    assert summary["invalid"] == 0


def test_create_dispatch_from_csv():
    with TestClient(app) as client:
        token = _login_admin(client)
        db = SessionLocal()
        try:
            campaign, queue = _campaign_and_queue(db, "CREATE")
            db.commit()
            campaign_id = campaign.id
            queue_id = queue.id
        finally:
            db.close()

        response = client.post(
            "/dispatches",
            data={
                "csrf_token": token,
                "campaign_id": str(campaign_id),
                "queue_id": str(queue_id),
                "interval_seconds": "25",
                "daily_limit": "80",
            },
            files={
                "csv_file": (
                    "lista.csv",
                    "nome;telefone\nEmpresa A;5555999991101\nEmpresa B;5555999991102\n",
                    "text/csv",
                )
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/dispatches/")

        dispatch_id = int(response.headers["location"].rsplit("/", 1)[-1])
        db = SessionLocal()
        try:
            dispatch = db.get(CampaignDispatch, dispatch_id)
            assert dispatch is not None
            assert dispatch.status == "RASCUNHO"
            assert dispatch.queue_id == queue_id
            assert dispatch.interval_seconds == 25
            assert dispatch.daily_limit == 80
            counts = dispatch_counts(db, dispatch_id)
            assert counts["total"] == 2
            assert counts["pending"] == 2
        finally:
            db.close()


def test_dispatch_sends_via_zapi_and_preserves_queue(monkeypatch):
    monkeypatch.setattr(
        ZApiService,
        "send_text",
        lambda self, *, phone, message: {
            "messageId": "DISPATCH-MSG-1",
            "zaapId": "DISPATCH-ZAAP-1",
        },
    )

    with TestClient(app):
        db = SessionLocal()
        try:
            campaign, queue = _campaign_and_queue(db, "SEND")
            dispatch = CampaignDispatch(
                campaign_id=campaign.id,
                queue_id=queue.id,
                status="ATIVO",
                interval_seconds=45,
                daily_limit=100,
            )
            db.add(dispatch)
            db.flush()

            from app.services.dispatch_queue import ensure_dispatch_lead
            lead1 = ensure_dispatch_lead(
                db,
                name="Empresa Sequencial 1",
                phone="5555999991201",
                queue=queue,
                created_by_id=None,
            )
            lead2 = ensure_dispatch_lead(
                db,
                name="Empresa Sequencial 2",
                phone="5555999991202",
                queue=queue,
                created_by_id=None,
            )
            db.add_all(
                [
                    CampaignDispatchRecipient(
                        dispatch_id=dispatch.id,
                        lead_id=lead1.id,
                        name="Empresa Sequencial 1",
                        phone="5555999991201",
                        message_body="Mensagem 1",
                        status="PENDENTE",
                    ),
                    CampaignDispatchRecipient(
                        dispatch_id=dispatch.id,
                        lead_id=lead2.id,
                        name="Empresa Sequencial 2",
                        phone="5555999991202",
                        message_body="Mensagem 2",
                        status="PENDENTE",
                    ),
                ]
            )
            db.commit()
            dispatch_id = dispatch.id

            recipient = claim_next_dispatch_recipient(db)
            assert recipient is not None
            assert recipient.status == "ENVIANDO"
            result = process_dispatch_recipient(db, recipient)
            db.commit()
            assert result == "sent"
            assert recipient.status == "ENVIADO"
            assert recipient.conversation_id is not None

            link = db.scalar(
                select(ConversationQueueLink).where(
                    ConversationQueueLink.conversation_id == recipient.conversation_id
                )
            )
            assert link is not None
            assert link.queue_id == queue.id
            assert link.origin == "DISPATCH"

            immediate_next = claim_next_dispatch_recipient(db)
            assert immediate_next is None

            counts = dispatch_counts(db, dispatch_id)
            assert counts["sent"] == 1
            assert counts["pending"] == 1
        finally:
            db.close()


def test_dispatch_inherits_campaign_buttons_and_image(monkeypatch):
    calls: list[tuple] = []

    def send_button_list(self, *, phone, message, buttons):
        calls.append(("buttons", phone, message, buttons))
        return {
            "messageId": "DISPATCH-BUTTONS-1",
            "zaapId": "DISPATCH-BUTTONS-ZAAP-1",
        }

    def send_image(self, *, phone, image_url, caption=""):
        calls.append(("image", phone, image_url, caption))
        return {
            "messageId": "DISPATCH-IMAGE-1",
            "zaapId": "DISPATCH-IMAGE-ZAAP-1",
        }

    def fail_plain_text(self, *, phone, message):
        raise AssertionError("Disparo com botões não deve usar send_text.")

    monkeypatch.setattr(ZApiService, "send_button_list", send_button_list)
    monkeypatch.setattr(ZApiService, "send_image", send_image)
    monkeypatch.setattr(ZApiService, "send_text", fail_plain_text)
    monkeypatch.setattr(
        MediaStorage,
        "presigned_download_url",
        lambda self, value, **kwargs: "https://signed.example/oferta.jpg",
    )

    with TestClient(app):
        db = SessionLocal()
        try:
            campaign, queue = _campaign_and_queue(db, "MEDIA-BUTTON")
            campaign.sequence_json = json.dumps(
                [
                    {
                        "message": "Oferta especial",
                        "quick_replies": [
                            {"id": "interesse", "label": "Tenho interesse"},
                            {"id": "agora_nao", "label": "Agora não"},
                        ],
                        "media": [
                            {
                                "type": "IMAGE",
                                "storage_uri": "s3://campaignmedia/oferta.jpg",
                                "file_name": "oferta.jpg",
                                "mime_type": "image/jpeg",
                            }
                        ],
                    }
                ],
                ensure_ascii=False,
            )
            dispatch = CampaignDispatch(
                campaign_id=campaign.id,
                queue_id=queue.id,
                status="ATIVO",
                interval_seconds=30,
                daily_limit=100,
            )
            db.add(dispatch)
            db.flush()

            from app.services.dispatch_queue import ensure_dispatch_lead

            lead = ensure_dispatch_lead(
                db,
                name="Cliente Botão",
                phone="5555999991601",
                queue=queue,
                created_by_id=None,
            )
            recipient = CampaignDispatchRecipient(
                dispatch_id=dispatch.id,
                lead_id=lead.id,
                name="Cliente Botão",
                phone=lead.phone,
                message_body="Oferta especial",
                status="ENVIANDO",
            )
            db.add(recipient)
            db.commit()

            result = process_dispatch_recipient(db, recipient)
            db.commit()

            assert result == "sent"
            assert calls[0][0] == "buttons"
            assert calls[0][3] == [
                {"id": "interesse", "label": "Tenho interesse"},
                {"id": "agora_nao", "label": "Agora não"},
            ]
            assert calls[1] == (
                "image",
                "5555999991601",
                "https://signed.example/oferta.jpg",
                "",
            )

            messages = list(
                db.scalars(
                    select(Message)
                    .where(Message.conversation_id == recipient.conversation_id)
                    .order_by(Message.id)
                )
            )
            assert [item.message_type for item in messages] == ["BUTTONS", "IMAGE"]
            assert messages[1].media_url == "s3://campaignmedia/oferta.jpg"
        finally:
            db.close()


def test_draft_dispatch_recipient_can_be_edited_before_start():
    with TestClient(app) as client:
        token = _login_admin(client)
        db = SessionLocal()
        try:
            campaign, queue = _campaign_and_queue(db, "EDIT")
            dispatch = CampaignDispatch(
                campaign_id=campaign.id,
                queue_id=queue.id,
                status="RASCUNHO",
                interval_seconds=30,
                daily_limit=100,
            )
            db.add(dispatch)
            db.flush()
            from app.services.dispatch_queue import ensure_dispatch_lead
            lead = ensure_dispatch_lead(
                db,
                name="Empresa Original",
                phone="5555999991301",
                queue=queue,
                created_by_id=None,
            )
            recipient = CampaignDispatchRecipient(
                dispatch_id=dispatch.id,
                lead_id=lead.id,
                name="Empresa Original",
                phone="5555999991301",
                message_body="Mensagem original",
                status="PENDENTE",
            )
            db.add(recipient)
            db.commit()
            dispatch_id = dispatch.id
            recipient_id = recipient.id
        finally:
            db.close()

        response = client.post(
            f"/dispatches/{dispatch_id}/recipients/{recipient_id}/edit",
            data={
                "csrf_token": token,
                "name": "Empresa Revisada",
                "phone": "(55) 99999-1302",
                "message_body": "Mensagem revisada antes do envio.",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == f"/dispatches/{dispatch_id}"

        db = SessionLocal()
        try:
            recipient = db.get(CampaignDispatchRecipient, recipient_id)
            assert recipient is not None
            assert recipient.name == "Empresa Revisada"
            assert recipient.phone == "5555999991302"
            assert recipient.message_body == "Mensagem revisada antes do envio."
            assert recipient.lead_id is None
            assert recipient.status == "PENDENTE"
        finally:
            db.close()


def test_draft_dispatch_recipient_can_be_removed_before_start():
    with TestClient(app) as client:
        token = _login_admin(client)
        db = SessionLocal()
        try:
            campaign, queue = _campaign_and_queue(db, "REMOVE")
            dispatch = CampaignDispatch(
                campaign_id=campaign.id,
                queue_id=queue.id,
                status="RASCUNHO",
                interval_seconds=30,
                daily_limit=100,
                import_summary_json=json.dumps({"imported": 1}),
            )
            db.add(dispatch)
            db.flush()
            recipient = CampaignDispatchRecipient(
                dispatch_id=dispatch.id,
                name="Empresa Remover",
                phone="5555999991401",
                message_body="Mensagem",
                status="PENDENTE",
            )
            db.add(recipient)
            db.commit()
            dispatch_id = dispatch.id
            recipient_id = recipient.id
        finally:
            db.close()

        response = client.post(
            f"/dispatches/{dispatch_id}/recipients/{recipient_id}/remove",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 303

        db = SessionLocal()
        try:
            assert db.get(CampaignDispatchRecipient, recipient_id) is None
            dispatch = db.get(CampaignDispatch, dispatch_id)
            summary = json.loads(dispatch.import_summary_json)
            assert summary["removed_manual"] == 1
        finally:
            db.close()


def test_started_dispatch_recipient_cannot_be_edited():
    with TestClient(app) as client:
        token = _login_admin(client)
        db = SessionLocal()
        try:
            campaign, queue = _campaign_and_queue(db, "LOCKED")
            dispatch = CampaignDispatch(
                campaign_id=campaign.id,
                queue_id=queue.id,
                status="ATIVO",
                interval_seconds=30,
                daily_limit=100,
            )
            db.add(dispatch)
            db.flush()
            recipient = CampaignDispatchRecipient(
                dispatch_id=dispatch.id,
                name="Empresa Travada",
                phone="5555999991501",
                message_body="Mensagem original",
                status="PENDENTE",
            )
            db.add(recipient)
            db.commit()
            dispatch_id = dispatch.id
            recipient_id = recipient.id
        finally:
            db.close()

        response = client.post(
            f"/dispatches/{dispatch_id}/recipients/{recipient_id}/edit",
            data={
                "csrf_token": token,
                "name": "Não deve alterar",
                "phone": "5555999991502",
                "message_body": "Não deve alterar",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

        db = SessionLocal()
        try:
            recipient = db.get(CampaignDispatchRecipient, recipient_id)
            assert recipient.phone == "5555999991501"
            assert recipient.message_body == "Mensagem original"
        finally:
            db.close()


def test_draft_dispatch_configuration_can_be_updated():
    with TestClient(app) as client:
        token = _login_admin(client)
        db = SessionLocal()
        try:
            campaign, queue = _campaign_and_queue(db, "CONFIG")
            dispatch = CampaignDispatch(
                campaign_id=campaign.id,
                queue_id=queue.id,
                status="RASCUNHO",
                interval_seconds=10,
                daily_limit=300,
                import_summary_json=json.dumps({"imported": 5}),
            )
            db.add(dispatch)
            db.flush()
            for index in range(5):
                db.add(
                    CampaignDispatchRecipient(
                        dispatch_id=dispatch.id,
                        name=f"Empresa Config {index}",
                        phone=f"5555999982{index:03d}",
                        message_body=f"Mensagem {index}",
                        status="PENDENTE",
                    )
                )
            db.commit()
            dispatch_id = dispatch.id
        finally:
            db.close()

        response = client.post(
            f"/dispatches/{dispatch_id}/configuration",
            data={
                "csrf_token": token,
                "interval_seconds": "45",
                "daily_limit": "120",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == f"/dispatches/{dispatch_id}"

        db = SessionLocal()
        try:
            dispatch = db.get(CampaignDispatch, dispatch_id)
            assert dispatch.interval_seconds == 45
            assert dispatch.daily_limit == 120
            summary = json.loads(dispatch.import_summary_json)
            assert summary["estimated_duration_seconds"] == 180
        finally:
            db.close()


def test_paused_dispatch_configuration_can_be_updated():
    with TestClient(app) as client:
        token = _login_admin(client)
        db = SessionLocal()
        try:
            campaign, queue = _campaign_and_queue(db, "CONFIG-PAUSED")
            dispatch = CampaignDispatch(
                campaign_id=campaign.id,
                queue_id=queue.id,
                status="PAUSADO",
                interval_seconds=30,
                daily_limit=100,
            )
            db.add(dispatch)
            db.commit()
            dispatch_id = dispatch.id
        finally:
            db.close()

        response = client.post(
            f"/dispatches/{dispatch_id}/configuration",
            data={
                "csrf_token": token,
                "interval_seconds": "60",
                "daily_limit": "250",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

        db = SessionLocal()
        try:
            dispatch = db.get(CampaignDispatch, dispatch_id)
            assert dispatch.interval_seconds == 60
            assert dispatch.daily_limit == 250
            assert dispatch.next_send_at is None
        finally:
            db.close()


def test_active_dispatch_configuration_is_locked():
    with TestClient(app) as client:
        token = _login_admin(client)
        db = SessionLocal()
        try:
            campaign, queue = _campaign_and_queue(db, "CONFIG-LOCKED")
            dispatch = CampaignDispatch(
                campaign_id=campaign.id,
                queue_id=queue.id,
                status="ATIVO",
                interval_seconds=10,
                daily_limit=300,
            )
            db.add(dispatch)
            db.commit()
            dispatch_id = dispatch.id
        finally:
            db.close()

        response = client.post(
            f"/dispatches/{dispatch_id}/configuration",
            data={
                "csrf_token": token,
                "interval_seconds": "90",
                "daily_limit": "10",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

        db = SessionLocal()
        try:
            dispatch = db.get(CampaignDispatch, dispatch_id)
            assert dispatch.interval_seconds == 10
            assert dispatch.daily_limit == 300
        finally:
            db.close()
