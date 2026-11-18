"""CRUD seguro de campanhas do discador: edicao, exclusao logica e restauracao."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.database import SessionLocal
from app.main import app
from app.models import AuditLog, VoiceCallEvent, VoiceCampaign, VoiceRecipient, VoiceSipAccount


def _csrf(page: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match
    return match.group(1)


def _login(client: TestClient) -> str:
    token = _csrf(client.get("/login").text)
    response = client.post("/login", data={
        "email": "admin@sulnet.local", "password": "Sulnet@2026!",
        "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303
    return _csrf(client.get("/voice/campaigns").text)


def _campaign(status: str = "PAUSADA", recipient_status: str = "FALHOU", attempts: int = 1):
    key = uuid4().hex[:10]
    with SessionLocal() as db:
        account = VoiceSipAccount(
            name=f"Conta crud {key}", domain="sip.example.com", username="200",
            password_encrypted="placeholder", transport="UDP", active=True,
        )
        db.add(account)
        db.flush()
        camp = VoiceCampaign(
            name=f"Campanha crud {key}", mode="AUDIO", status=status,
            sip_account_id=account.id, group_id=None,
            window_start="09:00", window_end="18:00", daily_limit=10,
            interval_seconds=30, dial_format="NATIONAL", dtmf_digit="1",
            audio_uri="s3://audio-placeholder/campaign.wav",
            legal_basis_verified=True, started_at=datetime.now(timezone.utc),
        )
        db.add(camp)
        db.flush()
        person = VoiceRecipient(
            campaign_id=camp.id, name="Cliente teste",
            phone="5555999002508", status=recipient_status,
            attempts=attempts, last_attempt_at=datetime.now(timezone.utc),
        )
        db.add(person)
        db.flush()
        db.add(VoiceCallEvent(
            recipient_id=person.id, event_id=uuid4().hex,
            event="SEM_ATENDIMENTO", detail="Teste de historico",
        ))
        db.commit()
        return camp.id, camp.name, person.id, account.id


def _edit_payload(token: str, name: str, account_id: int) -> dict:
    return {
        "csrf_token": token, "name": name, "sip_account_id": str(account_id),
        "group_id": "", "mode": "AUDIO", "dtmf_digit": "1",
        "window_start": "10:00", "window_end": "17:00",
        "daily_limit": "15", "interval_seconds": "40",
        "dial_format": "E164",
    }


def _purge_test_campaign(campaign_id: int, account_id: int) -> None:
    """Limpa registros sintéticos para evitar contaminação entre testes de campanha."""
    with SessionLocal() as db:
        target_recipients = select(VoiceRecipient.id).where(
            VoiceRecipient.campaign_id == campaign_id
        )
        db.execute(delete(VoiceCallEvent).where(
            VoiceCallEvent.recipient_id.in_(target_recipients)
        ))
        db.execute(delete(VoiceRecipient).where(VoiceRecipient.campaign_id == campaign_id))
        db.execute(delete(AuditLog).where(
            AuditLog.entity_type == "voice_campaign",
            AuditLog.entity_id == str(campaign_id),
        ))
        db.execute(delete(VoiceCampaign).where(VoiceCampaign.id == campaign_id))
        db.execute(delete(VoiceSipAccount).where(VoiceSipAccount.id == account_id))
        db.commit()


def test_edit_and_archive_campaign_with_call_history_preserved():
    camp_id, name, recipient_id, account_id = _campaign()
    with TestClient(app) as client:
        assert client.get(f"/voice/campaigns/{camp_id}/edit", follow_redirects=False).status_code == 303
        assert client.get(f"/voice/campaigns/{camp_id}/delete", follow_redirects=False).status_code == 303
        token = _login(client)
        listing = client.get("/voice/campaigns").text
        assert f'data-campaign-id="{camp_id}"' in listing
        assert f"/voice/campaigns/{camp_id}/edit" not in listing
        details = client.get(f"/voice/campaigns/{camp_id}/details")
        assert details.status_code == 200
        assert f"/voice/campaigns/{camp_id}/edit" in details.text
        assert f"/voice/campaigns/{camp_id}/delete" in details.text
        page = client.get(f"/voice/campaigns/{camp_id}/edit")
        assert page.status_code == 200
        assert name in page.text
        assert "1 contato(s) com tentativas anteriores" in page.text

        renamed = name + " - atualizado"
        payload = _edit_payload(token, renamed, account_id)
        updated = client.post(f"/voice/campaigns/{camp_id}/update",
                              data=payload, follow_redirects=False)
        assert updated.status_code == 303
        with SessionLocal() as db:
            campaign = db.get(VoiceCampaign, camp_id)
            assert campaign.name == renamed
            assert campaign.daily_limit == 15
            assert campaign.interval_seconds == 40
            assert campaign.dial_format == "E164"
            assert campaign.status == "PAUSADA"
            assert campaign.legal_basis_verified is False
            assert db.get(VoiceRecipient, recipient_id).attempts == 1

        bad = {**payload, "mode": "DIRECT"}
        assert client.post(f"/voice/campaigns/{camp_id}/update",
                           data=bad, follow_redirects=False).status_code == 303
        with SessionLocal() as db:
            assert db.get(VoiceCampaign, camp_id).mode == "AUDIO"

        confirm = client.get(f"/voice/campaigns/{camp_id}/delete")
        assert confirm.status_code == 200
        assert "1 tentativa(s) anterior(es)" in confirm.text
        wrong = client.post(f"/voice/campaigns/{camp_id}/delete",
                            data={"csrf_token": token, "confirm_value": "nome errado"},
                            follow_redirects=False)
        assert wrong.status_code == 303
        with SessionLocal() as db:
            assert db.get(VoiceCampaign, camp_id).status == "PAUSADA"

        removed = client.post(f"/voice/campaigns/{camp_id}/delete",
                              data={"csrf_token": token, "confirm_value": renamed},
                              follow_redirects=False)
        assert removed.status_code == 303
        with SessionLocal() as db:
            assert db.get(VoiceCampaign, camp_id).status == "EXCLUIDA"
            assert db.get(VoiceRecipient, recipient_id) is not None
            assert db.scalar(select(func.count(VoiceCallEvent.id)).where(
                VoiceCallEvent.recipient_id == recipient_id)) == 1
            assert db.scalar(select(AuditLog.id).where(
                AuditLog.entity_type == "voice_campaign",
                AuditLog.entity_id == str(camp_id),
                AuditLog.action == "VOICE_CAMPAIGN_ARCHIVED",
            ).limit(1)) is not None

        operational_page = client.get("/voice/campaigns").text
        assert renamed not in operational_page
        archive_page = client.get("/voice/campaigns?archived=1").text
        assert renamed in archive_page
        assert f"/voice/campaigns/{camp_id}/restore" not in archive_page
        archived_details = client.get(f"/voice/campaigns/{camp_id}/details")
        assert f"/voice/campaigns/{camp_id}/restore" in archived_details.text
        assert str(camp_id) not in client.get("/voice/campaigns/status").json()["campaigns"]
        assert client.get(f"/voice/campaigns/{camp_id}/edit", follow_redirects=False).status_code == 303

        restored = client.post(f"/voice/campaigns/{camp_id}/restore",
                               data={"csrf_token": token}, follow_redirects=False)
        assert restored.status_code == 303
        with SessionLocal() as db:
            item = db.get(VoiceCampaign, camp_id)
            assert item.status == "PAUSADA"
            assert item.legal_basis_verified is False
            assert db.get(VoiceRecipient, recipient_id).attempts == 1
    _purge_test_campaign(camp_id, account_id)


def test_running_and_in_flight_campaign_cannot_be_edited_or_archived():
    for status, recipient_status in [
        ("EM_EXECUCAO", "PENDENTE"),
        ("PAUSADA", "TOCANDO"),
    ]:
        camp_id, name, _, account_id = _campaign(status, recipient_status)
        with TestClient(app) as client:
            token = _login(client)
            assert client.get(f"/voice/campaigns/{camp_id}/edit", follow_redirects=False).status_code == 303
            assert client.get(f"/voice/campaigns/{camp_id}/delete", follow_redirects=False).status_code == 303
            assert client.post(f"/voice/campaigns/{camp_id}/update",
                               data=_edit_payload(token, name + " novo", account_id),
                               follow_redirects=False).status_code == 303
            assert client.post(f"/voice/campaigns/{camp_id}/delete",
                               data={"csrf_token": token, "confirm_value": name},
                               follow_redirects=False).status_code == 303
            with SessionLocal() as db:
                record = db.get(VoiceCampaign, camp_id)
                assert record.status == status
                assert record.name == name
        _purge_test_campaign(camp_id, account_id)


def test_paused_campaign_allows_new_csv_without_resetting_previous_attempts():
    camp_id, name, recipient_id, account_id = _campaign()
    with TestClient(app) as client:
        token = _login(client)
        page = client.get("/voice/campaigns").text
        assert f'data-campaign-id="{camp_id}"' in page
        assert f"/voice/campaigns/{camp_id}/contacts" not in page
        details = client.get(f"/voice/campaigns/{camp_id}/details")
        assert details.status_code == 200
        assert f"/voice/campaigns/{camp_id}/contacts" in details.text
        result = client.post(
            f"/voice/campaigns/{camp_id}/contacts",
            data={"csrf_token": token},
            files={"csv_file": ("novos.csv", "nome,telefone\nNovo contato,51999981234\n", "text/csv")},
            follow_redirects=False,
        )
        assert result.status_code == 303
        with SessionLocal() as db:
            camp = db.get(VoiceCampaign, camp_id)
            assert camp.status == "PAUSADA"
            assert camp.legal_basis_verified is False
            assert db.get(VoiceRecipient, recipient_id).attempts == 1
            assert db.scalar(select(func.count(VoiceRecipient.id)).where(
                VoiceRecipient.campaign_id == camp_id,
                VoiceRecipient.status == "PENDENTE",
            )) == 1
    _purge_test_campaign(camp_id, account_id)
