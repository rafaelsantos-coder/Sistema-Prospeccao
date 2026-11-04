"""Ajustes do teste de venda ao vivo de 04/11 (conversas 449, 459 e 472)."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import Contact, Conversation, Message, User
from app.security import hash_password
from app.services import ai_sdr
from app.services.ai_quick_replies import QuickReply
from app.services.ai_seller import SALE_ACCEPTANCE_STEP, _friendly_plan_name, _sale_acceptance_prompt


def test_nome_do_plano_sem_codigo_do_crm():
    assert _friendly_plan_name("Sulnet RESID FIBRA - 800MB - 2-25") == "Internet Fibra 800 Mega"
    assert _friendly_plan_name("SULMOVEL 15GB NACIONAL") == "Sulmóvel 15GB"
    assert _friendly_plan_name("Plano Especial") == "Plano Especial"
    assert _friendly_plan_name("") == ""


def test_resumo_mostra_pagamento_e_vencimento_do_formulario():
    state = SimpleNamespace(step=SALE_ACCEPTANCE_STEP, plan_label="", offer_price=119.9)
    collected = {
        "catalogPlanName": "Sulnet RESID FIBRA - 800MB - 2-25",
        "categoryFormValues": {"forma_pagamento": "Boleto", "vencimento": "10"},
    }
    resumo = _sale_acceptance_prompt(state, collected)
    assert "Internet Fibra 800 Mega" in resumo
    assert "Pagamento: Boleto" in resumo
    assert "Vencimento: dia 10" in resumo
    assert "RESID" not in resumo


def _decision(text: str, labels: list[str]) -> ai_sdr.AIDecision:
    decision = ai_sdr.AIDecision(
        reply_text=text,
        qualification_score=40,
        temperature="MORNO",
        recommended_stage="CONTATO_REALIZADO",
        handoff_recommended=False,
        confidence=0.9,
        summary="",
        next_action="",
    )
    decision.quick_replies = [QuickReply(id=f"b{i}", label=label) for i, label in enumerate(labels)]
    return decision


def test_uma_pergunta_por_mensagem():
    decision = ai_sdr._one_question_per_message(_decision(
        "Legal! Em qual cidade você precisa de internet? E seria para sua casa ou para uma empresa?",
        ["Para casa", "Para empresa"],
    ))
    assert decision.reply_text == "Legal! Em qual cidade você precisa de internet?"
    assert decision.quick_replies == []

    single = ai_sdr._one_question_per_message(_decision(
        "Em Santa Rosa temos fibra de 800 Mega por R$ 119,90. Quer contratar esse plano?",
        ["Quero contratar", "Tenho uma dúvida"],
    ))
    assert single.reply_text.endswith("Quer contratar esse plano?")
    assert len(single.quick_replies) == 2


def _user(db, email: str) -> User:
    user = User(
        name=email.split("@")[0], email=email, password_hash=hash_password("SenhaAjuste@2026"),
        role="CONSULTOR", team="AJUSTE", active=True, must_change_password=False,
    )
    db.add(user)
    db.flush()
    return user


def _conversation(db, phone: str, owner: User | None) -> Conversation:
    contact = Contact(phone=phone, name=phone)
    db.add(contact)
    db.flush()
    conversation = Conversation(
        contact_id=contact.id, assigned_user_id=owner.id if owner else None,
        team="AJUSTE", source="WHATSAPP", status="EM_ATENDIMENTO",
    )
    db.add(conversation)
    db.flush()
    return conversation


def test_ia_nao_entra_em_conversa_com_vendedor_atendendo():
    with TestClient(app):
        db = SessionLocal()
        try:
            fabiano = _user(db, "ajuste.fabiano@sulnet.local")
            atendendo = _conversation(db, "5555990000101", fabiano)
            db.add(Message(
                conversation_id=atendendo.id, contact_id=atendendo.contact_id, sender_user_id=fabiano.id,
                direction="OUTBOUND", message_type="TEXT", body="sim sim, posso lhe apresentar?", status="SENT",
            ))
            so_atribuida = _conversation(db, "5555990000102", fabiano)
            sem_dono = _conversation(db, "5555990000103", None)
            db.flush()
            assert ai_sdr.human_is_attending(db, atendendo) is True
            assert ai_sdr.human_is_attending(db, so_atribuida) is False
            assert ai_sdr.human_is_attending(db, sem_dono) is False
        finally:
            db.rollback()
            db.close()
