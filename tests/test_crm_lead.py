"""Lead no CRM aberta na primeira mensagem do cliente (nome + telefone)."""

from __future__ import annotations

from types import SimpleNamespace

from app.services import crm_lead
from app.services.crm_sulnet import CRMIntegrationError


class _Client:
    created: list[dict] = []

    @classmethod
    def from_settings(cls):
        return cls()

    def create_opportunity(self, payload):
        self.created.append(payload)
        return {"opportunityId": 14501}


def _conversation(name="Loine Patia", phone="555581243140", crm=None):
    contact = SimpleNamespace(name=name, business_name="", phone=phone)
    return SimpleNamespace(id=7, lead_id=None, contact=contact, crm_opportunity_id=crm)


class _Db:
    def flush(self):
        return None


def test_abre_lead_com_nome_e_telefone(monkeypatch):
    _Client.created = []
    monkeypatch.setattr(crm_lead, "SulnetCRMClient", _Client)
    conversation = _conversation()
    assert crm_lead.ensure_crm_lead(_Db(), conversation) == 14501
    assert conversation.crm_opportunity_id == 14501
    assert _Client.created[0]["clientName"] == "Loine Patia"
    assert _Client.created[0]["clientPhone"] == "555581243140"


def test_nao_duplica_lead_da_conversa(monkeypatch):
    _Client.created = []
    monkeypatch.setattr(crm_lead, "SulnetCRMClient", _Client)
    assert crm_lead.ensure_crm_lead(_Db(), _conversation(crm=9)) == 9
    assert _Client.created == []


def test_sem_nome_nao_abre_lead(monkeypatch):
    _Client.created = []
    monkeypatch.setattr(crm_lead, "SulnetCRMClient", _Client)
    assert crm_lead.ensure_crm_lead(_Db(), _conversation(name="")) is None
    assert _Client.created == []


def test_falha_do_crm_nao_lanca_e_tenta_depois(monkeypatch):
    class _Down(_Client):
        def create_opportunity(self, payload):
            raise CRMIntegrationError("CRM fora", status_code=503)

    monkeypatch.setattr(crm_lead, "SulnetCRMClient", _Down)
    conversation = _conversation()
    assert crm_lead.ensure_crm_lead(_Db(), conversation) is None
    assert conversation.crm_opportunity_id is None
