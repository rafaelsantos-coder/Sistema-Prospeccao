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
from app.services.zapi import ZApiService, ZApiError
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


def test_zapi_forbidden_pauses_only_affected_dispatch_preserving_recipient(monkeypatch):
    """A recusas 403 nao queimam tres tentativas nem mudam outras campanhas."""
    def forbidden(self, *, phone, message):
        raise ZApiError("Falha no envio pela Z-API: acesso recusado", status_code=403)

    monkeypatch.setattr(ZApiService, "send_text", forbidden)
    with TestClient(app):
        with SessionLocal() as db:
            campaign, queue = _campaign_and_queue(db, "HTTP403")
            affected = CampaignDispatch(
                campaign_id=campaign.id, queue_id=queue.id, status="ATIVO",
                interval_seconds=600, daily_limit=100,
            )
            other = CampaignDispatch(
                campaign_id=campaign.id, queue_id=queue.id, status="ATIVO",
                interval_seconds=600, daily_limit=100,
            )
            db.add_all([affected, other])
            db.flush()
            recipient = CampaignDispatchRecipient(
                dispatch_id=affected.id, name="Contato preservado",
                phone="5555999997011", message_body="Oferta de teste",
                status="ENVIANDO", attempts=0,
            )
            db.add(recipient)
            db.flush()
            outcome = process_dispatch_recipient(db, recipient)
            assert outcome == "provider_access_paused"
            assert affected.status == "PAUSADO"
            assert affected.next_send_at is None
            assert recipient.status == "PENDENTE"
            assert recipient.attempts == 0
            assert "HTTP 403" in recipient.error
            assert other.status == "ATIVO"
            db.commit()


def test_admin_can_review_and_prepare_only_rejected_undelivered_contacts():
    """Recuperacao requer pausa + CSRF + confirmacao; nao envia nenhum WhatsApp."""
    from app.timeutils import utcnow

    with TestClient(app) as client:
        csrf = _login_admin(client)
        with SessionLocal() as db:
            campaign, queue = _campaign_and_queue(db, "RETRY403")
            dispatch = CampaignDispatch(
                campaign_id=campaign.id, queue_id=queue.id, status="PAUSADO",
                interval_seconds=600, daily_limit=100,
            )
            db.add(dispatch)
            db.flush()
            dispatch_id = dispatch.id
            db.add_all([
                CampaignDispatchRecipient(
                    dispatch_id=dispatch.id, name="Recusado pela API",
                    phone="5555999997021", message_body="Oferta",
                    status="FALHOU", attempts=3,
                    error="RECUSA_CONFIRMADA_ZAPI: HTTP 422. API recusou a mensagem.",
                ),
                CampaignDispatchRecipient(
                    dispatch_id=dispatch.id, name="Enviado",
                    phone="5555999997022", message_body="Oferta",
                    status="ENVIADO", attempts=1,
                    sent_at=utcnow(), provider_message_id="MSG-JA-ENVIADA",
                ),
                CampaignDispatchRecipient(
                    dispatch_id=dispatch.id, name="Falha sem comprovacao de recusa",
                    phone="5555999997023", message_body="Oferta",
                    status="FALHOU", attempts=3,
                    error="Falha interna indeterminada",
                ),
            ])
            db.commit()

        page = client.get(f"/dispatches/{dispatch_id}")
        assert page.status_code == 200
        assert "2 contatos com falha" in page.text
        assert "Preparar 1 falha(s)" in page.text
        failures = client.get(f"/dispatches/{dispatch_id}?status_filter=FALHOU")
        assert failures.status_code == 200
        assert "Recusado pela API" in failures.text
        assert "Enviado</strong>" not in failures.text

        # Sem confirmacao explicita, nao altera a fila.
        refused = client.post(f"/dispatches/{dispatch_id}/retry-provider-failures",
                              data={"csrf_token": csrf}, follow_redirects=False)
        assert refused.status_code == 303
        with SessionLocal() as db:
            assert dispatch_counts(db, dispatch_id)["failed"] == 2

        accepted = client.post(f"/dispatches/{dispatch_id}/retry-provider-failures",
                               data={"csrf_token": csrf, "confirmation": "reprocessar"},
                               follow_redirects=False)
        assert accepted.status_code == 303
        with SessionLocal() as db:
            dispatch = db.get(CampaignDispatch, dispatch_id)
            counts = dispatch_counts(db, dispatch_id)
            assert dispatch.status == "PAUSADO"
            assert counts["pending"] == 1
            assert counts["failed"] == 1
            assert counts["sent"] == 1
            recovered = db.scalar(select(CampaignDispatchRecipient).where(
                CampaignDispatchRecipient.dispatch_id == dispatch_id,
                CampaignDispatchRecipient.phone == "5555999997021",
            ))
            assert recovered.status == "PENDENTE"
            assert recovered.attempts == 0
            assert recovered.error is None
            sent = db.scalar(select(CampaignDispatchRecipient).where(
                CampaignDispatchRecipient.dispatch_id == dispatch_id,
                CampaignDispatchRecipient.phone == "5555999997022",
            ))
            assert sent.status == "ENVIADO"
            assert sent.provider_message_id == "MSG-JA-ENVIADA"


def test_durable_dispatch_claim_blocks_duplicate_after_mock_ack_and_rollback(monkeypatch):
    """Z-API confirmou fora do banco; SIGKILL/rollback nao deve gerar replay."""
    from datetime import timedelta
    from app.services.dispatch_queue import ensure_dispatch_lead
    from app.timeutils import utcnow

    sends = []
    def accepted(self, *, phone, message):
        sends.append((phone, message))
        return {"messageId": "PROVIDER-ACCEPTED-FAKE"}
    monkeypatch.setattr(ZApiService, "send_text", accepted)

    with TestClient(app):
        with SessionLocal() as db:
            campaign, queue = _campaign_and_queue(db, "FENCE-DB-CRASH")
            dispatch = CampaignDispatch(
                campaign_id=campaign.id, queue_id=queue.id,
                status="ATIVO", interval_seconds=5, daily_limit=100,
            )
            db.add(dispatch)
            db.flush()
            lead = ensure_dispatch_lead(
                db, name="Destino simulado", phone="5555999998941",
                queue=queue, created_by_id=None,
            )
            row = CampaignDispatchRecipient(
                dispatch_id=dispatch.id, lead_id=lead.id,
                name="Destino simulado", phone=lead.phone,
                message_body="Somente simulacao interna", status="PENDENTE",
            )
            db.add(row)
            db.commit()
            dispatch_id, row_id = dispatch.id, row.id

            claimed = claim_next_dispatch_recipient(db, dispatch_id=dispatch_id)
            assert claimed and claimed.id == row_id
            assert claimed.attempts == 1
            db.commit()  # limite transacional do worker ANTES do envio externo
            assert process_dispatch_recipient(db, claimed) == "sent"
            db.rollback()  # falha apos ACK, antes do commit do resultado

        with SessionLocal() as db:
            row = db.get(CampaignDispatchRecipient, row_id)
            assert row.status == "ENVIANDO"
            assert row.attempts == 1
            assert row.provider_message_id is None
            dispatch = db.get(CampaignDispatch, dispatch_id)
            dispatch.next_send_at = utcnow() - timedelta(seconds=1)
            db.commit()
            assert claim_next_dispatch_recipient(db, dispatch_id=dispatch_id) is None
            assert len(sends) == 1


def test_zapi_http_503_remains_unknown_never_requeued(monkeypatch):
    """HTTP 503 nao comprova que mensagem nao foi aceita."""
    from datetime import timedelta
    from app.services.dispatch_queue import ensure_dispatch_lead
    from app.timeutils import utcnow

    def temporary_error(self, *, phone, message):
        raise ZApiError("Z-API sem resposta conclusiva", status_code=503)
    monkeypatch.setattr(ZApiService, "send_text", temporary_error)

    with TestClient(app):
        with SessionLocal() as db:
            campaign, queue = _campaign_and_queue(db, "FENCE-503")
            dispatch = CampaignDispatch(
                campaign_id=campaign.id, queue_id=queue.id, status="ATIVO",
                interval_seconds=5, daily_limit=100,
            )
            db.add(dispatch)
            db.flush()
            lead = ensure_dispatch_lead(
                db, name="Destino 503", phone="5555999998942",
                queue=queue, created_by_id=None,
            )
            row = CampaignDispatchRecipient(
                dispatch_id=dispatch.id, lead_id=lead.id, name="Destino 503",
                phone=lead.phone, message_body="Mensagem simulada", status="PENDENTE",
            )
            db.add(row)
            db.commit()
            dispatch_id, row_id = dispatch.id, row.id
            claimed = claim_next_dispatch_recipient(db, dispatch_id=dispatch_id)
            assert claimed.id == row_id
            db.commit()
            assert process_dispatch_recipient(db, claimed) == "unknown"
            db.commit()
            persisted = db.get(CampaignDispatchRecipient, row_id)
            assert persisted.status == "INDETERMINADO"
            assert persisted.attempts == 1
            assert dispatch_counts(db, dispatch_id)["unknown"] == 1
            dispatch = db.get(CampaignDispatch, dispatch_id)
            dispatch.next_send_at = utcnow() - timedelta(seconds=1)
            db.commit()
            assert claim_next_dispatch_recipient(db, dispatch_id=dispatch_id) is None


def test_cancel_reopen_never_replays_inflight_or_uncertain_rows():
    """Cancelar/reabrir recupera APENAS pendentes que nao foram enviados."""
    with TestClient(app) as client:
        token = _login_admin(client)
        with SessionLocal() as db:
            campaign, queue = _campaign_and_queue(db, "FENCE-CANCEL")
            dispatch = CampaignDispatch(
                campaign_id=campaign.id, queue_id=queue.id,
                status="ATIVO", interval_seconds=30, daily_limit=100,
            )
            db.add(dispatch)
            db.flush()
            rows = [
                CampaignDispatchRecipient(
                    dispatch_id=dispatch.id, name=f"Teste {i}",
                    phone=f"55559999989{i:02d}", message_body="Somente simulado",
                    status=state,
                )
                for i, state in enumerate(
                    ("PENDENTE", "ENVIANDO", "INDETERMINADO"), start=71
                )
            ]
            db.add_all(rows)
            db.commit()
            dispatch_id = dispatch.id
            ids = [r.id for r in rows]

        cancel = client.post(
            f"/dispatches/{dispatch_id}/cancel",
            data={"csrf_token": token}, follow_redirects=False,
        )
        assert cancel.status_code == 303
        with SessionLocal() as db:
            assert [db.get(CampaignDispatchRecipient, k).status for k in ids] == [
                "IGNORADO", "ENVIANDO", "INDETERMINADO",
            ]
        reopen = client.post(
            f"/dispatches/{dispatch_id}/reopen",
            data={"csrf_token": token}, follow_redirects=False,
        )
        assert reopen.status_code == 303
        with SessionLocal() as db:
            assert [db.get(CampaignDispatchRecipient, k).status for k in ids] == [
                "PENDENTE", "ENVIANDO", "INDETERMINADO",
            ]


def test_legacy_campaign_claim_is_durable_before_outbound_request():
    from app.models import CampaignRecipient
    from app.services.campaign_queue import claim_next_recipient
    from app.services.dispatch_queue import ensure_dispatch_lead

    with TestClient(app):
        with SessionLocal() as db:
            campaign, queue = _campaign_and_queue(db, "LEGACY-CLAIM")
            campaign.status = "ATIVA"
            lead = ensure_dispatch_lead(
                db, name="Destino legado", phone="5555999998944",
                queue=queue, created_by_id=None,
            )
            row = CampaignRecipient(
                campaign_id=campaign.id, lead_id=lead.id,
                message_body="Somente simulado", status="PENDENTE",
            )
            db.add(row)
            db.commit()
            id_ = row.id
            reserved = claim_next_recipient(db, campaign_id=campaign.id)
            assert reserved and reserved.id == id_
            assert reserved.attempts == 1
            db.commit()
            db.rollback()
        with SessionLocal() as db:
            persisted = db.get(CampaignRecipient, id_)
            assert persisted.status == "ENVIANDO"
            assert persisted.attempts == 1
            assert claim_next_recipient(db, campaign_id=campaign.id) is None


def test_old_ambiguous_http_error_cannot_be_restored_by_retry_endpoint():
    """Historico antigo nao informa certeza de rejeicao, mesmo sem ID externo."""
    with TestClient(app) as client:
        token = _login_admin(client)
        with SessionLocal() as db:
            campaign, queue = _campaign_and_queue(db, "OLD-HTTP500")
            dispatch = CampaignDispatch(
                campaign_id=campaign.id, queue_id=queue.id,
                status="PAUSADO", interval_seconds=20, daily_limit=100,
            )
            db.add(dispatch)
            db.flush()
            row = CampaignDispatchRecipient(
                dispatch_id=dispatch.id, name="Retorno indeterminado antigo",
                phone="5555999998945", message_body="Somente simulacao",
                status="FALHOU", attempts=3,
                error="Falha no envio pela Z-API: HTTP 500",
            )
            db.add(row)
            db.commit()
            dispatch_id, row_id = dispatch.id, row.id

        page = client.get(f"/dispatches/{dispatch_id}")
        assert page.status_code == 200
        assert "Preparar 1 falha(s)" not in page.text
        resp = client.post(
            f"/dispatches/{dispatch_id}/retry-provider-failures",
            data={"csrf_token": token, "confirmation": "reprocessar"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        with SessionLocal() as db:
            assert db.get(CampaignDispatchRecipient, row_id).status == "FALHOU"
