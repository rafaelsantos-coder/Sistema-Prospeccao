from __future__ import annotations

from types import SimpleNamespace

from app.models import Activity, Conversation, Lead, Message
from app.services import ai_sdr_crm as sdr_crm
from app.services.ai_sdr_models import AIDecision, QualificationData
from app.services.crm_sulnet import CRMIntegrationError


def _decision(**kwargs) -> AIDecision:
    base = dict(
        reply_text="ok",
        intent="QUALIFYING",
        qualification_score=40,
        temperature="MORNO",
        recommended_stage="CONTATO_REALIZADO",
        handoff_recommended=False,
        handoff_reason="",
        confidence=0.8,
        summary="teste",
        next_action="seguir",
        qualification=QualificationData(),
        crm_notes=[],
        sales_state="DISCOVERY",
    )
    base.update(kwargs)
    return AIDecision(**base)


def test_is_automatic_reply_detects_business_away_templates():
    assert sdr_crm.is_automatic_reply(
        "Olá! Recebemos sua mensagem. Nosso horário de atendimento é de "
        "segunda a sexta, das 9h às 18h. Retornamos assim que abrirmos."
    )
    assert sdr_crm.is_automatic_reply(
        "Esta é uma mensagem automática. Estamos ausentes no momento."
    )
    assert not sdr_crm.is_automatic_reply("Já sou assinante. Obrigado.")
    assert not sdr_crm.is_automatic_reply("Quero conhecer os planos")


def test_resolve_loss_reason_maps_common_cases():
    code, label = sdr_crm.resolve_loss_reason(
        _decision(intent="DO_NOT_CONTACT", sales_state="OPT_OUT"),
        latest_customer_message="Número errado",
    )
    assert code == "NUMERO_ERRADO"

    code, label = sdr_crm.resolve_loss_reason(
        _decision(intent="DO_NOT_CONTACT", sales_state="OPT_OUT"),
        latest_customer_message="Pode parar de me mandar mensagem",
    )
    assert code == "OPT_OUT"

    code, label = sdr_crm.resolve_loss_reason(
        _decision(intent="NOT_INTERESTED", objection="NO_INTEREST", sales_state="WAITING_CUSTOMER"),
        latest_customer_message="Já sou assinante. Obrigado.",
    )
    assert code == "JA_E_CLIENTE"
    assert "cliente" in label.lower() or "assinante" in label.lower() or "Sulnet" in label


def test_ensure_sdr_crm_opportunity_skips_auto_reply(monkeypatch):
    monkeypatch.setattr(sdr_crm, "crm_configured", lambda: True)
    conversation = SimpleNamespace(id=1, lead_id=10, contact=SimpleNamespace(phone="55999999999", name="Ana"))
    message = SimpleNamespace(body="Mensagem automática: estamos ausentes no momento.")
    assert (
        sdr_crm.ensure_sdr_crm_opportunity(
            db=SimpleNamespace(),
            conversation=conversation,
            latest_message=message,
        )
        is None
    )


def test_ensure_sdr_crm_opportunity_creates_with_name_and_phone(monkeypatch):
    created = {}

    class FakeClient:
        @classmethod
        def from_settings(cls):
            return cls()

        def create_opportunity(self, payload):
            created["payload"] = payload
            return {"opportunityId": 555, "protocol": "CRM-555"}

    monkeypatch.setattr(sdr_crm, "crm_configured", lambda: True)
    monkeypatch.setattr(sdr_crm, "SulnetCRMClient", FakeClient)

    lead = Lead(
        id=10,
        business_name="José A. Rodrigues",
        contact_name="José A. Rodrigues",
        phone="5599712012",
        stage="TENTATIVA_CONTATO",
        raw_data="{}",
    )
    conversation = SimpleNamespace(
        id=77,
        lead_id=10,
        contact=SimpleNamespace(phone="5599712012", name="José A. Rodrigues", business_name=""),
    )
    message = SimpleNamespace(body="Oi, quero saber dos planos")

    class FakeDb:
        def get(self, model, pk):
            if model is Lead and pk == 10:
                return lead
            return None

        def add(self, obj):
            created.setdefault("activities", []).append(obj)

        def flush(self):
            return None

    meta = sdr_crm.ensure_sdr_crm_opportunity(
        FakeDb(),
        conversation=conversation,
        latest_message=message,
        agent_user_id=1,
    )
    assert meta["id"] == 555
    assert meta["status"] == "OPEN"
    assert created["payload"]["clientName"] == "José A. Rodrigues"
    assert created["payload"]["clientPhone"] == "5599712012"
    assert lead.stage == "CONTATO_REALIZADO"
    assert any(isinstance(item, Activity) for item in created["activities"])


def test_mark_sdr_opportunity_lost_calls_crm_and_sets_local_stage(monkeypatch):
    lost_calls = {}

    class FakeClient:
        @classmethod
        def from_settings(cls):
            return cls()

        def mark_lost(self, opportunity_id, **kwargs):
            lost_calls["id"] = opportunity_id
            lost_calls["kwargs"] = kwargs
            return {"alreadyLost": False}

    monkeypatch.setattr(sdr_crm, "crm_configured", lambda: True)
    monkeypatch.setattr(sdr_crm, "SulnetCRMClient", FakeClient)

    lead = Lead(
        id=10,
        business_name="Cliente",
        contact_name="Cliente",
        phone="5599712012",
        stage="CONTATO_REALIZADO",
        raw_data='{"crm_opportunity":{"id":555,"status":"OPEN"}}',
    )
    conversation = SimpleNamespace(id=77, lead_id=10, contact=None)

    class FakeDb:
        def get(self, model, pk):
            if model is Lead and pk == 10:
                return lead
            return None

        def add(self, obj):
            return None

    meta = sdr_crm.mark_sdr_opportunity_lost(
        FakeDb(),
        conversation=conversation,
        decision=_decision(intent="DO_NOT_CONTACT", sales_state="OPT_OUT"),
        latest_customer_message="Não quero mais mensagens, pode parar",
        agent_user_id=1,
    )
    assert meta["status"] == "LOST"
    assert meta["loss_reason_code"] == "OPT_OUT"
    assert lead.stage == "PERDIDO"
    assert lead.do_not_contact is True
    assert lost_calls["id"] == 555
    assert lost_calls["kwargs"]["reason_code"] == "OPT_OUT"


def test_mark_lost_crm_client_legacy_patch(monkeypatch):
    from app.services import crm_sulnet as crm_module

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"status": "lost", "id": 9}

    class FakeHttp:
        captured = {}

        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def request(self, method, url, **kwargs):
            self.__class__.captured = {"method": method, "url": url, "json": kwargs.get("json")}
            return FakeResponse()

    monkeypatch.setattr(crm_module.httpx, "Client", FakeHttp)
    monkeypatch.setattr(
        crm_module.SulnetCRMClient,
        "supports",
        lambda self, cap: False,
    )
    monkeypatch.setattr(
        crm_module.SulnetCRMClient,
        "_stage_id_by_name",
        lambda self, *names: 44,
    )
    client = crm_module.SulnetCRMClient(
        base_url="https://crm.example",
        bearer_token="service-token-value-1234567890",
    )
    result = client.mark_lost(
        9,
        reason_code="SEM_INTERESSE",
        reason_label="Sem interesse",
        notes="Cliente pediu para encerrar",
    )
    assert result["reasonCode"] == "SEM_INTERESSE"
    assert FakeHttp.captured["method"] == "PATCH"
    assert FakeHttp.captured["json"]["status"] == "lost"
    assert FakeHttp.captured["json"]["lossReason"] == "Sem interesse"
    assert FakeHttp.captured["json"]["stageId"] == 44
