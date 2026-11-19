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
from app.services.dispatch_monitor import daily_capacity
from app.services.dispatch_queue import (
    claim_next_dispatch_recipient,
    dispatch_counts,
    parse_dispatch_csv,
    process_dispatch_recipient,
)
from app.services.media_storage import MediaStorage
from app.services.zapi import ZApiService
from app.services.zapi_instances import bind_queue_instance, create_zapi_instance


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
    instance = create_zapi_instance(
        db,
        name=f"WhatsApp Disparo {suffix}",
        phone_label=f"Disparo {suffix}",
        base_url="https://api.z-api.io",
        instance_id=f"instance-dispatch-{suffix.lower()}",
        instance_token=f"token-dispatch-{suffix.lower()}",
        client_token=f"client-dispatch-{suffix.lower()}",
        created_by_id=None,
    )
    bind_queue_instance(db, queue.id, instance.id)
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


def test_cancelar_por_engano_e_retomar_devolve_os_contatos_para_a_fila():
    with TestClient(app) as client:
        csrf = _login_admin(client)
        db = SessionLocal()
        try:
            campaign, queue = _campaign_and_queue(db, "REOPEN")
            dispatch = CampaignDispatch(
                campaign_id=campaign.id, queue_id=queue.id, status="ATIVO",
                interval_seconds=45, daily_limit=100,
            )
            db.add(dispatch)
            db.flush()
            db.add_all(
                [
                    CampaignDispatchRecipient(
                        dispatch_id=dispatch.id, name=f"Cliente {i}", phone=f"55559999913{i:02d}",
                        message_body="Oferta", status="PENDENTE",
                    )
                    for i in range(3)
                ]
                + [
                    CampaignDispatchRecipient(
                        dispatch_id=dispatch.id, name="Já ignorado", phone="5555999991399",
                        message_body="Oferta", status="IGNORADO", error="Cliente em lista de bloqueio.",
                    )
                ]
            )
            db.commit()
            dispatch_id = dispatch.id
        finally:
            db.close()

        client.post(f"/dispatches/{dispatch_id}/cancel", data={"csrf_token": csrf}, follow_redirects=False)
        page = client.get(f"/dispatches/{dispatch_id}")
        assert "CANCELADO" in page.text and f"/dispatches/{dispatch_id}/reopen" in page.text

        client.post(f"/dispatches/{dispatch_id}/reopen", data={"csrf_token": csrf}, follow_redirects=False)
        db = SessionLocal()
        try:
            dispatch = db.get(CampaignDispatch, dispatch_id)
            assert dispatch.status == "ATIVO" and dispatch.finished_at is None
            counts = dispatch_counts(db, dispatch_id)
            assert counts["pending"] == 3
            other = db.scalar(
                select(CampaignDispatchRecipient).where(
                    CampaignDispatchRecipient.dispatch_id == dispatch_id,
                    CampaignDispatchRecipient.name == "Já ignorado",
                )
            )
            assert other.status == "IGNORADO", "quem foi ignorado por outro motivo não volta"
        finally:
            db.close()


def test_dispatch_live_monitor_shows_last_next_and_realistic_daily_capacity():
    from datetime import timedelta
    from app.timeutils import utcnow

    with TestClient(app) as client:
        _login_admin(client)
        db = SessionLocal()
        try:
            campaign, queue = _campaign_and_queue(db, "MONITOR")
            dispatch = CampaignDispatch(
                campaign_id=campaign.id,
                queue_id=queue.id,
                status="ATIVO",
                interval_seconds=600,
                daily_limit=100,
                window_start="07:00",
                window_end="20:00",
                next_send_at=utcnow() + timedelta(minutes=10),
            )
            db.add(dispatch)
            db.flush()
            dispatch_id = dispatch.id
            assert daily_capacity(dispatch) == 78
            db.add_all([
                CampaignDispatchRecipient(
                    dispatch_id=dispatch.id, name="Destinatário 1",
                    phone="5555999995011", message_body="Teste", status="ENVIADO",
                    sent_at=utcnow(),
                ),
                CampaignDispatchRecipient(
                    dispatch_id=dispatch.id, name="Destinatário 2",
                    phone="5555999995012", message_body="Teste", status="PENDENTE",
                ),
            ])
            db.commit()
        finally:
            db.close()

        page = client.get(f"/dispatches/{dispatch_id}")
        assert page.status_code == 200
        assert "MONITORAMENTO AO VIVO" in page.text
        assert "Aguardando intervalo" in page.text or "Fora do horário permitido" in page.text

        response = client.get(f"/dispatches/{dispatch_id}/live")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        data = response.json()
        assert data["counts"]["total"] == 2
        assert data["counts"]["sent"] == 1
        assert data["counts"]["pending"] == 1
        assert data["daily_capacity"] == 78
        assert data["estimated_days_remaining"] == 1
        assert data["last_sent"] is not None
        assert data["next_send"] is not None
        assert data["status"] == "ATIVO"


def test_dispatch_claim_survives_crash_after_provider_ack(monkeypatch):
    """Se a resposta externa chegou e a transação final caiu, não reenviar."""
    calls = []

    def accepted(self, *, phone, message):
        calls.append((phone, message))
        return {"messageId": "ACK-OUTSIDE-DB"}

    monkeypatch.setattr(ZApiService, "send_text", accepted)

    with TestClient(app):
        from datetime import timedelta

        from app.services.dispatch_queue import ensure_dispatch_lead
        from app.timeutils import utcnow

        db = SessionLocal()
        try:
            campaign, queue = _campaign_and_queue(db, "FENCE-ACK")
            dispatch = CampaignDispatch(
                campaign_id=campaign.id, queue_id=queue.id, status="ATIVO",
                interval_seconds=5, daily_limit=100,
            )
            db.add(dispatch)
            db.flush()
            lead = ensure_dispatch_lead(
                db, name="Cliente teste", phone="5555999998861",
                queue=queue, created_by_id=None,
            )
            recipient = CampaignDispatchRecipient(
                dispatch_id=dispatch.id, lead_id=lead.id,
                name="Cliente teste", phone=lead.phone,
                message_body="Mensagem interna de teste", status="PENDENTE",
            )
            db.add(recipient)
            db.commit()
            dispatch_id, recipient_id = dispatch.id, recipient.id

            reserved = claim_next_dispatch_recipient(db, dispatch_id=dispatch_id)
            assert reserved and reserved.id == recipient_id
            db.commit()  # mesmo limite transacional do worker
            assert process_dispatch_recipient(db, reserved) == "sent"
            db.rollback()  # SIGKILL / falha no commit após a confirmação externa
        finally:
            db.close()

        with SessionLocal() as db:
            persisted = db.get(CampaignDispatchRecipient, recipient_id)
            assert persisted.status == "ENVIANDO"
            assert persisted.attempts == 1
            assert persisted.provider_message_id is None
            dispatch = db.get(CampaignDispatch, dispatch_id)
            dispatch.next_send_at = utcnow() - timedelta(seconds=1)
            db.commit()
            assert claim_next_dispatch_recipient(db, dispatch_id=dispatch_id) is None
            assert len(calls) == 1  # não houve segundo envio


def test_unknown_provider_result_never_requeues_dispatch(monkeypatch):
    """Timeout pode acontecer após aceitação; usuário precisa reconciliar."""
    from datetime import timedelta

    from app.services.dispatch_queue import ensure_dispatch_lead
    from app.services.zapi import ZApiError
    from app.timeutils import utcnow

    def timed_out(self, *, phone, message):
        raise ZApiError("Timeout após envio; status externo desconhecido")

    monkeypatch.setattr(ZApiService, "send_text", timed_out)
    with TestClient(app):
        with SessionLocal() as db:
            campaign, queue = _campaign_and_queue(db, "FENCE-TIMEOUT")
            dispatch = CampaignDispatch(
                campaign_id=campaign.id, queue_id=queue.id,
                status="ATIVO", interval_seconds=5, daily_limit=100,
            )
            db.add(dispatch)
            db.flush()
            lead = ensure_dispatch_lead(
                db, name="Cliente teste", phone="5555999998862",
                queue=queue, created_by_id=None,
            )
            row = CampaignDispatchRecipient(
                dispatch_id=dispatch.id, lead_id=lead.id, name="Cliente teste",
                phone=lead.phone, message_body="Oferta teste", status="PENDENTE",
            )
            db.add(row)
            db.commit()
            dispatch_id, recipient_id = dispatch.id, row.id

            claimed = claim_next_dispatch_recipient(db, dispatch_id=dispatch_id)
            assert claimed and claimed.id == recipient_id
            db.commit()
            assert process_dispatch_recipient(db, claimed) == "unknown"
            db.commit()
            assert db.get(CampaignDispatchRecipient, recipient_id).status == "INDETERMINADO"
            counts = dispatch_counts(db, dispatch_id)
            assert counts["unknown"] == 1
            assert counts["pending"] == 0
            assert db.get(CampaignDispatch, dispatch_id).status == "ATIVO"
            db.get(CampaignDispatch, dispatch_id).next_send_at = (
                utcnow() - timedelta(seconds=1)
            )
            db.commit()
            assert claim_next_dispatch_recipient(db, dispatch_id=dispatch_id) is None


def test_legacy_campaign_claim_survives_worker_crash():
    """Fluxo legado também deve persistir ENVIANDO antes de chamar Z-API."""
    from app.models import CampaignRecipient
    from app.services.campaign_queue import claim_next_recipient
    from app.services.dispatch_queue import ensure_dispatch_lead

    with TestClient(app):
        with SessionLocal() as db:
            campaign, queue = _campaign_and_queue(db, "FENCE-LEGACY")
            campaign.status = "ATIVA"
            lead = ensure_dispatch_lead(
                db, name="Cliente legado", phone="5555999998863",
                queue=queue, created_by_id=None,
            )
            row = CampaignRecipient(
                campaign_id=campaign.id, lead_id=lead.id,
                message_body="Mensagem teste", status="PENDENTE",
            )
            db.add(row)
            db.commit()
            row_id = row.id
            reserved = claim_next_recipient(db)
            assert reserved and reserved.id == row_id
            db.commit()
            db.rollback()  # simula um reinício inesperado
        with SessionLocal() as db:
            assert db.get(CampaignRecipient, row_id).status == "ENVIANDO"
            assert db.get(CampaignRecipient, row_id).attempts == 1
            assert claim_next_recipient(db) is None


def test_cancel_reopen_never_replays_in_flight_or_unknown_rows():
    """Cancelar/reabrir deve restaurar só os contatos realmente não enviados."""
    with TestClient(app) as client:
        csrf = _login_admin(client)
        with SessionLocal() as db:
            campaign, queue = _campaign_and_queue(db, "FENCE-CANCEL")
            dispatch = CampaignDispatch(
                campaign_id=campaign.id, queue_id=queue.id,
                status="ATIVO", interval_seconds=15, daily_limit=100,
            )
            db.add(dispatch)
            db.flush()
            rows = [
                CampaignDispatchRecipient(
                    dispatch_id=dispatch.id, name=f"Teste {i}",
                    phone=f"55559999988{i:02d}", message_body="Teste",
                    status=status,
                )
                for i, status in enumerate(
                    ("PENDENTE", "ENVIANDO", "INDETERMINADO"), start=71
                )
            ]
            db.add_all(rows)
            db.commit()
            dispatch_id = dispatch.id
            ids = [r.id for r in rows]

        result = client.post(
            f"/dispatches/{dispatch_id}/cancel",
            data={"csrf_token": csrf}, follow_redirects=False,
        )
        assert result.status_code == 303

        with SessionLocal() as db:
            states = [db.get(CampaignDispatchRecipient, i).status for i in ids]
            assert states == ["IGNORADO", "ENVIANDO", "INDETERMINADO"]

        result = client.post(
            f"/dispatches/{dispatch_id}/reopen",
            data={"csrf_token": csrf}, follow_redirects=False,
        )
        assert result.status_code == 303
        with SessionLocal() as db:
            states = [db.get(CampaignDispatchRecipient, i).status for i in ids]
            assert states == ["PENDENTE", "ENVIANDO", "INDETERMINADO"]



def test_sigkill_after_fake_provider_ack_never_automatically_resends():
    """Encerra processo real com SIGKILL após ACK simulado, sem acessar Z-API."""
    import os
    import signal
    import subprocess
    import sys
    import textwrap
    from datetime import timedelta

    import pytest

    from app.services.dispatch_queue import ensure_dispatch_lead
    from app.timeutils import utcnow

    if os.name != "posix":
        pytest.skip("SIGKILL exige ambiente POSIX")

    with TestClient(app):
        with SessionLocal() as db:
            campaign, queue = _campaign_and_queue(db, "SIGKILL-FENCE")
            dispatch = CampaignDispatch(
                campaign_id=campaign.id, queue_id=queue.id,
                status="ATIVO", interval_seconds=1, daily_limit=100,
            )
            db.add(dispatch)
            db.flush()
            lead = ensure_dispatch_lead(
                db, name="Teste de falha abrupta",
                phone="5555999998874", queue=queue, created_by_id=None,
            )
            recipient = CampaignDispatchRecipient(
                dispatch_id=dispatch.id, lead_id=lead.id,
                name="Teste de falha abrupta", phone=lead.phone,
                message_body="Somente teste interno sem envio real",
                status="PENDENTE",
            )
            db.add(recipient)
            db.commit()
            dispatch_id, recipient_id = dispatch.id, recipient.id

        child_code = textwrap.dedent(
            """
            import sys
            import time
            from app.database import SessionLocal
            from app.services.dispatch_queue import (
                claim_next_dispatch_recipient, process_dispatch_recipient,
            )
            from app.services.zapi import ZApiService

            def fake_send(self, *, phone, message):
                print("FAKE_PROVIDER_ACK", flush=True)
                time.sleep(60)
                return {"messageId": "ONLY-A-FAKE-ACK"}

            ZApiService.send_text = fake_send
            with SessionLocal() as db:
                row = claim_next_dispatch_recipient(db, dispatch_id=int(sys.argv[1]))
                assert row is not None, "No recipient was reserved"
                db.commit()
                print("CLAIM_DURABLE", flush=True)
                process_dispatch_recipient(db, row)
                db.commit()
            """
        )
        child = subprocess.Popen(
            [sys.executable, "-u", "-c", child_code, str(dispatch_id)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "CLAIM_DURABLE"
            assert child.stdout.readline().strip() == "FAKE_PROVIDER_ACK"
            child.send_signal(signal.SIGKILL)
            child.communicate(timeout=10)
            assert child.returncode == -signal.SIGKILL
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=10)

        with SessionLocal() as db:
            persisted = db.get(CampaignDispatchRecipient, recipient_id)
            assert persisted.status == "ENVIANDO"
            assert persisted.attempts == 1
            assert persisted.provider_message_id is None
            dispatch = db.get(CampaignDispatch, dispatch_id)
            dispatch.next_send_at = utcnow() - timedelta(seconds=1)
            db.commit()
            assert claim_next_dispatch_recipient(db, dispatch_id=dispatch_id) is None



def test_reconciliation_is_read_only_and_covers_both_campaign_flows():
    """Gestor visualiza envios incertos sem novos contatos nem mudanças no banco."""
    from datetime import timedelta

    from app.models import CampaignRecipient
    from app.services.dispatch_queue import ensure_dispatch_lead
    from app.timeutils import utcnow

    with TestClient(app) as client:
        _login_admin(client)
        with SessionLocal() as db:
            campaign, queue = _campaign_and_queue(db, "REVIEW-SAFE")
            dispatch = CampaignDispatch(
                campaign_id=campaign.id, queue_id=queue.id, status="ATIVO",
                interval_seconds=30, daily_limit=100,
            )
            db.add(dispatch)
            db.flush()
            lead = ensure_dispatch_lead(
                db, name="Contato de conciliação",
                phone="5555999998811", queue=queue, created_by_id=None,
            )
            unknown = CampaignDispatchRecipient(
                dispatch_id=dispatch.id, lead_id=lead.id,
                name="A conferir CSV", phone=lead.phone,
                message_body="Mensagem fictícia", status="INDETERMINADO",
                error="Timeout fictício do teste", attempts=1,
            )
            pending = CampaignDispatchRecipient(
                dispatch_id=dispatch.id, name="Em processamento CSV",
                phone="5555999998812", message_body="Mensagem fictícia",
                status="ENVIANDO", attempts=1,
                updated_at=utcnow() - timedelta(minutes=20),
            )
            legacy = CampaignRecipient(
                campaign_id=campaign.id, lead_id=lead.id,
                message_body="Mensagem fictícia", status="INDETERMINADO",
                attempts=1, error="Resposta fictícia não confirmada",
            )
            db.add_all([unknown, pending, legacy])
            db.commit()
            ids = unknown.id, pending.id, legacy.id

        page = client.get("/dispatches/reconciliation")
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        assert "Envios a conferir" in page.text
        assert "A conferir CSV" in page.text
        assert "Em processamento CSV" in page.text
        assert "Contato de conciliação" in page.text
        assert "Processamento acima de 10 min" in page.text

        csv_only = client.get("/dispatches/reconciliation?source=DISPATCH")
        assert csv_only.status_code == 200
        assert "A conferir CSV" in csv_only.text
        assert "Em processamento CSV" in csv_only.text
        assert "Contato de conciliação" not in csv_only.text

        old_campaigns = client.get(
            "/dispatches/reconciliation?source=CAMPAIGN&status=INDETERMINADO"
        )
        assert old_campaigns.status_code == 200
        assert "Contato de conciliação" in old_campaigns.text
        assert "A conferir CSV" not in old_campaigns.text
        assert "Em processamento CSV" not in old_campaigns.text

        with SessionLocal() as db:
            assert db.get(CampaignDispatchRecipient, ids[0]).status == "INDETERMINADO"
            assert db.get(CampaignDispatchRecipient, ids[1]).status == "ENVIANDO"
            assert db.get(CampaignRecipient, ids[2]).status == "INDETERMINADO"
            assert db.get(CampaignDispatchRecipient, ids[0]).attempts == 1


def test_reconciliation_requires_authentication():
    with TestClient(app) as client:
        result = client.get("/dispatches/reconciliation", follow_redirects=False)
        assert result.status_code == 303
        assert result.headers["location"] == "/login"
