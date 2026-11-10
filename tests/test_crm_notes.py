import json
from decimal import Decimal
from types import SimpleNamespace

from app.services import crm_notes


def _state(**kw):
    base = dict(
        opportunity_id=9, step="CREDIT_REJECTED", plan_label="Fibra 800 Mega",
        offer_price=Decimal("119.90"), collected_json="{}",
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _DB:
    def flush(self):
        pass


class _Client:
    def __init__(self, fail=False):
        self.notes = []
        self.fail = fail

    def add_opportunity_note(self, opportunity_id, *, result, description, channel="WhatsApp"):
        if self.fail:
            raise crm_notes.CRMIntegrationError("fora", status_code=404)
        self.notes.append((opportunity_id, result, description))


def test_marcos_do_credito_reprovado():
    notes = crm_notes.pending_notes(_state(), {"_credit_alt": "FEE"})
    keys = [n[0] for n in notes]
    assert "plan:Fibra 800 Mega" in keys and "credit_rejected:FEE" in keys
    assert "119,90" in notes[0][2]
    assert "taxa" in notes[1][2]


def test_assinado_so_com_venda_concluida():
    assert not any(n[0] == "signed" for n in crm_notes.pending_notes(_state(step="WAITING_SIGNATURE"), {}))
    done = crm_notes.pending_notes(_state(step="COMPLETED"), {"_won_after_signature": True})
    assert any(n[0] == "signed" for n in done)


def test_nota_gravada_uma_vez(monkeypatch):
    client = _Client()
    monkeypatch.setattr(crm_notes.SulnetCRMClient, "from_settings", staticmethod(lambda: client))
    state = _state()
    assert crm_notes.record_milestones(_DB(), state) == 2
    assert crm_notes.record_milestones(_DB(), state) == 0
    assert len(client.notes) == 2
    assert len(json.loads(state.collected_json)[crm_notes.NOTES_KEY]) == 2


def test_falha_do_crm_nao_marca_como_feito(monkeypatch):
    monkeypatch.setattr(crm_notes.SulnetCRMClient, "from_settings", staticmethod(lambda: _Client(fail=True)))
    state = _state()
    assert crm_notes.record_milestones(_DB(), state) == 0
    assert crm_notes.NOTES_KEY not in json.loads(state.collected_json)
