"""Regressões do fechamento SulIA: conversa de Santo Ângelo, 16/11/2026.

Os cenários não fazem chamadas externas, não usam dados de clientes reais e
asseguram que campos cadastrais internos não sejam solicitados por WhatsApp.
"""

from types import SimpleNamespace

from app.services.ai_campaign_recall import OFFER_MARK, SUMMARY_MARKER
from app.services.ai_seller import (
    _customer_confirmed_city,
    _recall_offer_accepted,
    _seller_free_months,
    _prompt_for_step,
    _seller_followup_prompt,
    _query_playbook_guidance,
)
from app.services.crm_sulnet import CRMPlaybookMissingItem, CRMPlaybookResult


def _state(step="CPF", summary=""):
    return SimpleNamespace(
        opportunity_id=15566,
        plan_label="Fibra 800 Mega",
        handoff_summary=summary,
        offer_price=119.90,
        credit_status="Aprovado",
        last_error="",
        step=step,
    )


def _playbook(*missing):
    return CRMPlaybookResult(
        opportunity_id=15566,
        category="fibra",
        credit_status="Aprovado",
        missing=list(missing),
    )


def test_ultima_cidade_do_cliente_supera_primeira_transcricao():
    history = [
        {"direction": "OUTBOUND", "body": "Em qual cidade será a instalação?"},
        {"direction": "INBOUND", "body": "[Áudio transcrito] Santa Rita, Alto Paraná"},
        {"direction": "INBOUND", "body": "[Áudio transcrito] Santo Ângelo, Rio Grande do Sul."},
    ]
    assert _customer_confirmed_city(history) == "Santo Ângelo"


def test_cidade_do_atendente_nao_e_fato_do_cliente():
    assert _customer_confirmed_city([
        {"direction": "OUTBOUND", "body": "Santo Ângelo"},
        {"direction": "INBOUND", "body": "Sim, quero contratar"},
    ]) == ""


def test_promocao_exige_oferta_e_aceite_na_ordem_correta():
    assert _recall_offer_accepted([
        {"direction": "INBOUND", "body": "Sim"},
        {"direction": "OUTBOUND", "body": "Olá, tudo bem?"},
    ]) is False
    assert _recall_offer_accepted([
        {"direction": "OUTBOUND", "body": f"Oferta especial: {OFFER_MARK}!"},
        {"direction": "INBOUND", "body": "Sim, vamos fechar"},
    ]) is True
    assert _recall_offer_accepted([
        {"direction": "OUTBOUND", "body": f"Oferta especial: {OFFER_MARK}!"},
        {"direction": "INBOUND", "body": "Sim, vamos fechar"},
        {"direction": "INBOUND", "body": "Não, desisti"},
    ]) is False


def test_meses_gratis_nao_somem_por_zero_em_decisao_intermediaria():
    state = _state(summary=SUMMARY_MARKER)
    assert _seller_free_months(state, {"freeMonths": 0}) == 3
    assert _seller_free_months(state, {"freeMonths": 3}) == 3
    assert _seller_free_months(_state(), {"freeMonths": 0}) == 0


def test_nascimento_nunca_vira_pergunta_ao_cliente():
    state = _state(step="BIRTH_DATE")
    prompt = _prompt_for_step(state, {})
    followup = _seller_followup_prompt(state, {}, followup_step=1)
    assert "data de nascimento" not in prompt.lower()
    assert "data de nascimento" not in followup.lower()
    assert "dd/mm/aaaa" not in (prompt + followup).lower()


class _FakeCrm:
    def __init__(self, results, opportunity=None):
        self.results = list(results)
        self.opportunity = opportunity or {}
        self.patches = []
        self.lookups = 0

    def playbook(self, opportunity_id, *, category):
        assert opportunity_id == 15566
        assert category == "fibra"
        return self.results.pop(0)

    def get_opportunity(self, opportunity_id):
        self.lookups += 1
        return self.opportunity

    def update_playbook_field(self, opportunity_id, *, patch_key, value):
        self.patches.append((opportunity_id, patch_key, value))

    def update_opportunity(self, opportunity_id, payload):
        self.patches.append((opportunity_id, payload))


def test_nascimento_ausente_no_crm_fica_pendente_sem_pergunta():
    client = _FakeCrm([
        _playbook(CRMPlaybookMissingItem(
            code="clientBirthDate",
            question="Qual é a sua data de nascimento?",
            patch_key="clientBirthDate",
        )),
    ])
    state = _state()
    collected = {}
    step, question = _query_playbook_guidance(
        client, state, collected, fallback_step="CEP",
    )
    assert step == "WAITING_CRM_READY"
    assert question is None
    assert collected["_crm_birth_date_blocked"] is True
    assert client.lookups == 1


def test_nascimento_existente_no_crm_e_sincronizado_sem_pergunta():
    client = _FakeCrm(
        [
            _playbook(CRMPlaybookMissingItem(
                code="clientBirthDate",
                question="Qual é sua data de nascimento?",
                patch_key="clientBirthDate",
            )),
            _playbook(CRMPlaybookMissingItem(
                code="cep", question="Qual é seu CEP?", patch_key="cep",
            )),
        ],
        opportunity={"client": {"clientBirthDate": "1990-05-10"}},
    )
    state = _state()
    collected = {}
    step, question = _query_playbook_guidance(
        client, state, collected, fallback_step="CEP",
    )
    assert client.patches == [(15566, "clientBirthDate", "1990-05-10")]
    assert step == "CEP"
    assert question == "Qual é seu CEP?"
    assert "_crm_birth_date_blocked" not in collected


def test_cep_depois_de_aceite_e_a_proxima_pergunta():
    client = _FakeCrm([
        _playbook(CRMPlaybookMissingItem(
            code="cep", question="Qual é o CEP de instalação?", patch_key="cep",
        )),
    ])
    state = _state()
    collected = {"_sale_intent_confirmed": True, "city": "Santo Ângelo"}
    step, question = _query_playbook_guidance(
        client, state, collected, fallback_step="CEP",
    )
    assert step == "CEP"
    assert question == "Qual é o CEP de instalação?"
