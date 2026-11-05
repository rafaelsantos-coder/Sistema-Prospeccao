from decimal import Decimal
from types import SimpleNamespace

from app.models import AIClosingState
from app.services import ai_campaign_black as black
from app.services import ai_seller
from app.services.crm_sulnet import CRMIntegrationError


def _state(**kw):
    base = dict(
        conversation_id=1, agent_id=1, step="COMPLETED", external_id="prospecta-conversation-1",
        opportunity_id=14153, protocol="P1", plan_label="Internet Fibra 800 Mega",
        offer_price=Decimal("119.90"), contract_status="assinado", sign_url="https://zap/x",
        credit_status="Aprovado", handoff_summary=black.with_marker("resumo"),
    )
    base.update(kw)
    return AIClosingState(**base)


def test_black_choice_le_resposta_as_opcoes():
    history = [
        {"direction": "OUTBOUND", "body": black.OPTIONS_TEXT},
        {"direction": "INBOUND", "body": "Combo fibra + móvel"},
    ]
    assert black.black_choice(history) == "COMBO"
    history[1]["body"] = "Só móvel"
    assert black.black_choice(history) == "MOBILE"


def test_primeira_assinatura_do_combo_abre_a_venda_do_movel():
    state = _state()
    collected = {
        "name": "Rafael Santos", "cep": "98900000", "_sale_accepted": True,
        "_black_combo": {"stage": "FIBER"}, "crmPlaybook": {"x": 1}, "categoryFormValues": {"a": 1},
        "_sdr_ia_funnel": True,
    }
    text = ai_seller._after_signed_text(state, collected)
    assert text.startswith("Contrato da internet assinado")
    assert state.step == "COMBO_MOBILE_PLAN"
    assert state.opportunity_id is None and state.sign_url == "" and state.contract_status == ""
    assert state.external_id.endswith("-movel")
    assert collected["name"] == "Rafael Santos" and collected["cep"] == "98900000"
    assert "_sale_accepted" not in collected and "crmPlaybook" not in collected
    assert collected["_sdr_ia_funnel"] is True
    assert collected["_black_combo"]["fiber"]["opportunity_id"] == 14153


def test_venda_sem_combo_encerra_com_mensagem_final():
    state = _state()
    assert ai_seller._after_signed_text(state, {}) == ai_seller.SIGNED_FINAL_MESSAGE
    assert state.step == "COMPLETED"


def test_escolha_do_plano_movel_vai_para_cpf():
    state = _state(step="COMBO_MOBILE_PLAN", opportunity_id=None)
    collected = {"_black_combo": {"stage": "MOBILE"}}
    reply = ai_seller._combo_choose_mobile(state, collected, "10GB")
    assert state.step == "CPF" and state.plan_label == "Sulmóvel 10GB"
    assert state.offer_price == Decimal("53.90")
    assert "CPF" in reply


def test_desistir_do_movel_conclui_sem_a_promocao():
    state = _state(step="COMBO_MOBILE_PLAN", opportunity_id=None)
    collected = {"_black_combo": {"stage": "MOBILE"}}
    reply = ai_seller._combo_choose_mobile(state, collected, "não quero")
    assert state.step == "COMPLETED" and "não será aplicada" in reply
    assert collected["_black_combo"]["stage"] == "DECLINED"


def test_pergunta_sem_plano_repete_as_opcoes():
    state = _state(step="COMBO_MOBILE_PLAN", opportunity_id=None)
    reply = ai_seller._combo_choose_mobile(state, {"_black_combo": {"stage": "MOBILE"}}, "como funciona?")
    assert state.step == "COMBO_MOBILE_PLAN" and "Qual plano móvel" in reply


def test_textos_fixos_do_combo_passam_sem_guardiao():
    state = _state(step="COMBO_MOBILE_PLAN")
    assert ai_seller._is_fixed_closing_text(state, black.combo_mobile_prompt())
    assert ai_seller._is_fixed_closing_text(state, black.COMBO_DECLINED_MESSAGE)


class _FakeClient:
    def __init__(self, reject_extras):
        self.calls = []
        self.reject_extras = reject_extras

    def create_opportunity(self, payload):
        self.calls.append(payload)
        if self.reject_extras and "tags" in payload:
            raise CRMIntegrationError("campo inválido", status_code=400)
        return {"opportunityId": 99, "protocol": "X"}

    def add_opportunity_tag(self, opportunity_id, tag_name):
        self.tagged = (opportunity_id, tag_name)
        return True


def _create(monkeypatch, reject):
    fake = _FakeClient(reject)
    monkeypatch.setattr(ai_seller.SulnetCRMClient, "from_settings", classmethod(lambda cls: fake))
    state = _state(step="CPF", opportunity_id=None, plan_label="Sulmóvel 10GB")
    state.collected_json = '{"name": "Rafael Santos Silva", "phone": "5555936188241"}'
    conversation = SimpleNamespace(id=1, lead_id=1, contact=SimpleNamespace(phone="5555936188241"))
    ai_seller._create_crm_if_ready(None, state=state, conversation=conversation)
    return fake, state


def test_venda_black_envia_tag_e_data(monkeypatch):
    fake, state = _create(monkeypatch, reject=False)
    assert fake.calls[0]["tags"] == ["Promoção Black 2027"]
    assert fake.calls[0]["firstDueDate"] == "2027-01-10"
    assert state.opportunity_id == 99
    assert fake.tagged == (99, "Promoção Black 2027")


def test_crm_que_recusa_campos_extras_nao_trava_a_venda(monkeypatch):
    fake, state = _create(monkeypatch, reject=True)
    assert len(fake.calls) == 2 and "tags" not in fake.calls[1]
    assert state.opportunity_id == 99


def test_resumo_traz_dados_do_cliente_endereco_e_portabilidade():
    state = _state(step="CONFIRM_SALE", plan_label="Sulmóvel 10GB", offer_price=Decimal("53.90"), opportunity_id=1)
    collected = {
        "name": "Rafael Santos", "cpf": "00380171058", "phone": "5555936188241",
        "street": "Rua A", "number": "10", "neighborhood": "Centro", "city": "Santa Rosa", "uf": "RS", "cep": "98900000",
        "categoryFormValues": {
            "tipo_contratacao": "Número Portabilidade", "numero_portado": "55999002508",
            "port_titular": "Sim", "forma_pagamento": "Boleto", "vencimento": "10",
        },
    }
    text = ai_seller._sale_acceptance_prompt(state, collected)
    for part in ("Nome: Rafael Santos", "CPF: 003.***.***-58", "Rua A 10", "Santa Rosa/RS", "Tipo: Portabilidade",
                 "Número a portar: 55999002508", "Titular da linha: você mesmo", "Promoção Black"):
        assert part in text
    assert text.endswith("assinatura digital.")


def test_titular_igual_ao_cliente_preenche_nome_e_cpf_sem_perguntar():
    from app.services.crm_sulnet import CRMPlaybookMissingItem

    state = _state(step="MOBILE_PORT_NAME")
    collected = {"name": "Rafael Santos", "cpf": "00380171058", "categoryFormValues": {"port_titular": "Sim"}}
    name_item = CRMPlaybookMissingItem(code="cf_port_nome_titular", label="Nome titular")
    doc_item = CRMPlaybookMissingItem(code="cf_port_cpf_cnpj", label="CPF titular")
    assert ai_seller._playbook_known_value(name_item, state, collected) == ("category:port_nome_titular", "Rafael Santos")
    assert ai_seller._playbook_known_value(doc_item, state, collected) == ("category:port_cpf_cnpj", "00380171058")
    collected["categoryFormValues"]["port_titular"] = "Não"
    assert ai_seller._playbook_known_value(name_item, state, collected) is None


def test_tipo_de_contratacao_vai_ao_crm_como_numero_portabilidade():
    assert ai_seller._crm_form_value("tipo_contratacao", "Portabilidade") == "Número Portabilidade"
    assert ai_seller._crm_form_value("tipo_contratacao", "Número Portabilidade") == "Número Portabilidade"
    assert ai_seller._crm_form_value("tipo_contratacao", "Número novo") == "Número novo"


def test_pergunta_da_etapa_nunca_e_bloqueada_pelo_guardiao():
    state = _state(step="MOBILE_PORT_NUMBER")
    state.collected_json = '{"_playbook_question": "Qual é o número que será portado?"}'
    assert ai_seller._is_step_prompt(state, "Qual é o número que será portado?")
    assert not ai_seller._is_step_prompt(state, "Texto livre gerado pelo modelo sobre outra coisa")


def test_planos_moveis_do_combo_em_paginas_de_botoes():
    first = black.combo_page_buttons(1)
    assert [b["label"] for b in first] == ["2GB - R$ 33,90", "5GB - R$ 44,90", "Mais planos"]
    assert [b["label"] for b in black.combo_page_buttons(2)] == ["7GB - R$ 47,90", "10GB - R$ 53,90", "Mais planos"]
    assert [b["label"] for b in black.combo_page_buttons(3)] == ["15GB - R$ 69,80", "Voltar"]
    assert all(len(black.combo_page_buttons(p)) <= 3 for p in (1, 2, 3))


def test_mais_planos_avanca_e_botao_escolhe_o_plano():
    state = _state(step="COMBO_MOBILE_PLAN", opportunity_id=None)
    collected = {"_black_combo": {"stage": "MOBILE"}}
    reply = ai_seller._combo_choose_mobile(state, collected, "Mais planos")
    assert collected["_combo_page"] == 2 and "7GB" in reply and "2GB" not in reply
    assert ai_seller._seller_step_buttons("COMBO_MOBILE_PLAN", reply, collected)[0]["label"].startswith("7GB")
    ai_seller._combo_choose_mobile(state, collected, "10GB - R$ 53,90")
    assert state.plan_label == "Sulmóvel 10GB" and state.step == "CPF"


def test_segunda_venda_preenche_valor_mensal_sem_perguntar():
    from app.services.crm_sulnet import CRMPlaybookMissingItem

    state = _state(step="CEP", plan_label="Sulmóvel 2GB", offer_price=Decimal("33.90"))
    item = CRMPlaybookMissingItem(code="monthlyValue", label="Valor mensal")
    assert ai_seller._playbook_known_value(item, state, {}) == ("monthlyValue", 33.9)


def test_tag_black_e_adicionada_sem_apagar_as_outras():
    from app.services.crm_sulnet import SulnetCRMClient

    calls = []

    class _Client(SulnetCRMClient):
        def __init__(self):
            pass

        def _request(self, method, path, json_body=None, **kwargs):
            calls.append((method, path, json_body))
            if path == "/api/catalog/reference":
                return {"tags": [{"id": 3, "name": "VIP"}, {"id": 9, "name": "Promoção Black 2027"}]}
            if method == "GET":
                return {"id": 14373, "tagIds": [3]}
            return {}

    client = _Client()
    assert client.add_opportunity_tag(14373, "promoção black 2027")
    assert calls[-1] == ("PATCH", "/api/opportunities/14373", {"tagIds": [3, 9]})


def test_tag_inexistente_no_crm_nao_trava():
    from app.services.crm_sulnet import SulnetCRMClient

    class _Client(SulnetCRMClient):
        def __init__(self):
            pass

        def _request(self, method, path, json_body=None, **kwargs):
            return {"tags": []}

    assert _Client().add_opportunity_tag(1, "Promoção Black 2027") is False


def test_valor_mensal_vem_do_plano_e_cliente_pode_trocar_o_plano(monkeypatch):
    calls = []

    class _Client:
        def update_opportunity(self, opportunity_id, payload):
            calls.append((opportunity_id, payload))
            return {}

    monkeypatch.setattr(ai_seller, "_sync_plan_context", lambda client, state, collected: collected.update(planId=7, catalogPlanName="Sulmóvel 15GB CRM"))
    monkeypatch.setattr(ai_seller, "_advance_with_playbook", lambda client, state, collected, fallback_step: "próxima pergunta")
    state = _state(step="PLAYBOOK_DYNAMIC_FIELD", plan_label="Sulmóvel 2GB", offer_price=Decimal("33.90"), opportunity_id=14373)
    collected = {"_playbook_dynamic_code": "monthlyValue", "categoryName": "Telefonia Móvel"}
    reply = ai_seller._resolve_plan_value_field(_Client(), state, collected, "15GB")
    assert reply == "próxima pergunta"
    assert state.plan_label == "Sulmóvel 15GB" and state.offer_price == Decimal("69.80")
    assert calls == [(14373, {"monthlyValue": 69.8, "planId": 7, "planLabel": "Sulmóvel 15GB CRM"})]
    assert "_playbook_dynamic_code" not in collected


def test_produto_do_crm_de_outra_categoria_nao_vale_para_o_plano_escolhido():
    fibra = {"id": 800, "name": "SULNET RESID FIBRA - 800MB - 2-25", "categoryName": "FIBRA"}
    movel15 = {"id": 392, "name": "SULMOVEL MEU TRABALHO COMIGO 15GB - 2-26", "categoryName": "TELEFONIA MÓVEL"}
    movel2 = {"id": 390, "name": "Meu Plano Essencial 2GB", "categoryName": "Telefonia Móvel"}
    match = ai_seller._plan_matches_label
    assert not match(fibra, "Sulmóvel 15GB")
    assert match(movel15, "Sulmóvel 15GB")
    assert not match(movel2, "Sulmóvel 15GB")
    assert match(fibra, "800 Mega") and not match(movel15, "800 Mega")
    assert match(fibra, "plano combinado")


class _PlanClient:
    def __init__(self, crm_plan_id):
        self.crm_plan_id = crm_plan_id
        self.updated = []

    def get_opportunity(self, opportunity_id):
        return {"planId": self.crm_plan_id}

    def products(self, query=""):
        if query:
            return []
        return [
            {"id": 800, "name": "SULNET RESID FIBRA - 800MB - 2-25", "monthlyValue": 119.90, "categoryId": 10, "categoryName": "FIBRA"},
            {"id": 392, "name": "SULMOVEL MEU TRABALHO COMIGO 15GB - 2-26", "monthlyValue": 69.80, "categoryId": 83, "categoryName": "TELEFONIA MÓVEL"},
        ]

    def update_opportunity(self, opportunity_id, payload):
        self.updated.append((opportunity_id, payload))
        return {}


def test_segunda_venda_corrige_produto_herdado_da_fibra():
    client = _PlanClient(crm_plan_id=800)
    state = _state(step="CONFIRM_SALE", plan_label="Sulmóvel 15GB", offer_price=Decimal("69.80"), opportunity_id=14373)
    collected = {}
    ai_seller._sync_plan_context(client, state, collected)
    assert collected["planId"] == 392
    assert collected["contractProductName"] == "SULMOVEL MEU TRABALHO COMIGO 15GB - 2-26"
    assert client.updated and client.updated[0][1]["planId"] == 392
    assert client.updated[0][1]["categoryId"] == 83


def test_confirmo_com_resumo_errado_mostra_o_resumo_certo_antes_do_contrato():
    client = _PlanClient(crm_plan_id=800)
    state = _state(step="CONFIRM_SALE", plan_label="Sulmóvel 15GB", offer_price=Decimal("69.80"), opportunity_id=14373)
    collected = {"_sale_accepted": True, "catalogPlanName": "SULNET RESID FIBRA - 800MB - 2-25"}
    reply = ai_seller._finish_sale_contract(client, state, collected)
    assert state.step == "CONFIRM_SALE" and not collected["_sale_accepted"]
    assert "15GB" in reply and "Fibra" not in reply


def test_reinicio_do_combo_descarta_o_produto_da_fibra():
    state = _state()
    collected = {"_black_combo": {"stage": "FIBER"}, "planId": 800, "categoryId": 10, "contractProductName": "FIBRA 800"}
    ai_seller._after_signed_text(state, collected)
    assert "planId" not in collected and "categoryId" not in collected and "contractProductName" not in collected
