"""Regressao de campanhas de audio informativo sem encaminhamento a grupo SIP."""
from __future__ import annotations

import re
import wave
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import VoiceCampaign, VoiceDialerEngine, VoiceRecipient, VoiceSipAccount
from app.services.voice_runtime import check_campaign_ready


def _token(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def _login(client):
    csrf = _token(client.get("/login").text)
    response = client.post("/login", data={
        "email": "admin@sulnet.local", "password": "Sulnet@2026!",
        "csrf_token": csrf,
    }, follow_redirects=False)
    assert response.status_code == 303
    return _token(client.get("/voice").text)


def test_create_audio_campaign_without_group_and_check_readiness():
    with TestClient(app) as client:
        csrf = _login(client)
        with SessionLocal() as db:
            account = VoiceSipAccount(
                name="Conta áudio informativo", domain="sip.example.com",
                username="5889", password_encrypted="placeholder", transport="UDP",
                active=True,
            )
            db.add(account)
            db.commit()
            account_id = account.id

        page = client.get("/voice/campaigns/new").text
        assert "Não transferir para grupo" in page
        response = client.post("/voice/campaigns", data={
            "csrf_token": csrf, "name": "Áudio sem grupo", "sip_account_id": account_id,
            "group_id": "", "mode": "AUDIO", "dtmf_digit": "1",
            "window_start": "09:00", "window_end": "18:00",
            "daily_limit": 5, "interval_seconds": 60,
        }, follow_redirects=False)
        assert response.status_code == 303
        with SessionLocal() as db:
            campaign = db.scalar(select(VoiceCampaign).where(
                VoiceCampaign.name == "Áudio sem grupo"
            ))
            assert campaign is not None and campaign.group_id is None
            assert campaign.mode == "AUDIO"
            campaign_id = campaign.id
            errors = check_campaign_ready(db, campaign)
            assert any("audio" in x.lower() for x in errors)
            assert not any("transferencia" in x.lower() or "grupo" in x.lower() for x in errors)

        page = client.get("/voice/campaigns").text
        assert "Áudio sem transferência" in page
        assert f'data-campaign-id="{campaign_id}"' in page
        details = client.get(f"/voice/campaigns/{campaign_id}/details").text
        assert "Sem transferência — áudio e encerramento" in details
        invalid = client.post("/voice/campaigns", data={
            "csrf_token": csrf, "name": "Direto sem grupo", "sip_account_id": account_id,
            "group_id": "", "mode": "DIRECT", "dtmf_digit": "1",
        }, follow_redirects=False)
        assert invalid.status_code == 303
        with SessionLocal() as db:
            assert db.scalar(select(VoiceCampaign).where(
                VoiceCampaign.name == "Direto sem grupo"
            )) is None
            campaign = db.get(VoiceCampaign, campaign_id)
            account = db.get(VoiceSipAccount, account_id)
            moment = datetime.now(timezone.utc)
            account.sip_check_code = 200
            account.sip_check_at = moment
            campaign.legal_basis_verified = True
            campaign.audio_uri = "s3://privado/campaigns/voice.wav"
            db.add(VoiceRecipient(
                campaign_id=campaign_id, name="Cliente autorizado",
                phone="5555999887700", status="PENDENTE", attempts=0,
            ))
            engine = db.get(VoiceDialerEngine, 1)
            if engine is None:
                engine = VoiceDialerEngine(id=1)
                db.add(engine)
            engine.enabled = True
            engine.campaign_runtime_enabled = True
            engine.heartbeat_at = moment
            db.commit()
            assert check_campaign_ready(db, campaign) == []
            # O áudio concluído não pode ser contabilizado como transferência.
            recipient = db.scalar(select(VoiceRecipient).where(
                VoiceRecipient.campaign_id == campaign_id
            ))
            recipient.status = "AUDIO_CONCLUIDO"
            recipient.attempts = 1
            recipient.answered_at = moment
            db.commit()
            from app.services.voice_reporting import campaign_operational_report
            report = campaign_operational_report(db, campaign)
            assert report["stats"]["audio_completed"] == 1
            assert report["stats"]["answered"] == 1
            assert report["stats"]["transferred"] == 0
            engine.enabled = False
            db.commit()


def test_audio_without_group_never_sends_sip_refer(tmp_path, monkeypatch):
    """Teste isolado do driver com PJSUA2 simulado; nenhuma ligacao real."""
    import app.services.voice_pjsua2 as service

    audio = tmp_path / "reproducao.wav"
    with wave.open(str(audio), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(8000)
        f.writeframes(b"\x00\x00" * 8000)

    virtual_time = [100.0]
    monkeypatch.setattr(service.time, "monotonic", lambda: virtual_time[0])
    playback = []

    class FakeMediaPlayer:
        def createPlayer(self, path, flags):
            playback.append(("created", path))

        def startTransmit(self, target):
            playback.append(("started", target))

    class FakeCall:
        def __init__(self, account):
            self._info = SimpleNamespace(
                lastStatusCode=200,
                state=1,
                media=[SimpleNamespace(type=1, status=1)],
            )
            self.hung_up = False

        def getInfo(self):
            return self._info

        def getAudioMedia(self, index):
            return "audio-media"

        def makeCall(self, uri, prm):
            self.onCallState(None)
            self.onCallMediaState(None)

        def xfer(self, uri, prm):
            raise AssertionError("REFER nao pode ser enviado sem grupo")

        def hangup(self, prm):
            self.hung_up = True

    class FakeOpParam:
        def __init__(self, *args):
            self.statusCode = 0

    pj = SimpleNamespace(
        Call=FakeCall,
        AudioMediaPlayer=FakeMediaPlayer,
        CallOpParam=FakeOpParam,
        PJMEDIA_TYPE_AUDIO=1,
        PJSUA_CALL_MEDIA_ACTIVE=1,
        PJSIP_INV_STATE_CONFIRMED=1,
        PJSIP_INV_STATE_DISCONNECTED=2,
        PJMEDIA_FILE_NO_LOOP=0,
    )
    session = object.__new__(service.PjSession)
    session.pj = pj
    session.acc = object()
    session._player = None
    session._call = None
    session._registration = 200
    session.account_model = SimpleNamespace(
        domain="sip.example.com", dial_domain="", sip_port=5060,
        dial_prefix="", strip_brazil_ddi=False, transport="UDP",
    )
    def pump(milliseconds=100):
        virtual_time[0] += milliseconds / 1000
    session.pump = pump

    statuses = []
    outcome = session.perform_call(
        "5555999991100", mode="AUDIO", group=None, audio_wav=str(audio),
        on_status=lambda status, _code: statuses.append(status),
        should_abort=lambda: False,
    )
    assert outcome.status == "AUDIO_CONCLUIDO"
    assert outcome.answered
    assert not outcome.transferred
    assert outcome.media_active
    assert "REPRODUZINDO_AUDIO" in statuses
    assert "AUDIO_CONCLUIDO" in statuses
    assert playback and playback[0][0] == "created"
    assert session._player is None
