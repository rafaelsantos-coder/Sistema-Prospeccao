from datetime import datetime, timezone

from app.services import ai_campaign_black as black


def _h(*pairs):
    return [{"direction": d, "body": b} for d, b in pairs]


def test_validade_ate_30_de_novembro_de_2026():
    assert black.black_active(datetime(2026, 11, 30, 20, 0, tzinfo=timezone.utc))
    assert not black.black_active(datetime(2026, 12, 1, 4, 0, tzinfo=timezone.utc))


def test_respostas_da_pergunta_da_black():
    assert black.answer_to_offer("Sim") == "YES"
    assert black.answer_to_offer("quero sim") == "YES"
    assert black.answer_to_offer("Não") == "NO"
    assert black.answer_to_offer("qual o preço?") == ""


def test_escolha_movel_ou_combo():
    assert black.choice_from_options("Combo fibra + móvel") == "COMBO"
    assert black.choice_from_options("Só móvel") == "MOBILE"
    assert black.choice_from_options("650 mega") == ""


def test_textos_trazem_data_e_regra_do_combo():
    assert "10/01/2027" in black.OPTIONS_TEXT
    assert "30/11/2026" in black.OPTIONS_TEXT
    assert "internet e o móvel" in black.OPTIONS_TEXT
    assert black.OFFER_QUESTION.count("?") == 1


def test_black_aceita_so_depois_do_sim():
    offered = _h(("OUTBOUND", black.OFFER_QUESTION))
    assert not black.black_accepted(offered)
    assert not black.black_accepted(offered + _h(("INBOUND", "Não")))
    assert black.black_accepted(offered + _h(("INBOUND", "Sim")))
    opts = _h(("OUTBOUND", black.OPTIONS_TEXT), ("INBOUND", "Só móvel"))
    assert black.black_accepted(opts)


def test_marcador_do_resumo_e_idempotente():
    once = black.with_marker("resumo")
    assert black.has_marker(once)
    assert black.with_marker(once) == once


def test_guardiao_aprova_textos_fixos_da_black():
    from app.services.ai_guardian import GuardianReview, _apply_guardrails

    for text in (black.OFFER_QUESTION, black.OPTIONS_TEXT):
        result = _apply_guardrails(
            None,
            review=GuardianReview(verdict="REWRITE", risk="HIGH", issue_codes=["UNSUPPORTED_PROMOTION"],
                                  reply_text="Qual é a sua cidade?"),
            context={},
            original_text=text,
            original_buttons=[{"id": "a", "label": "Sim"}],
        )
        assert result.verdict == "APPROVE" and result.reply_text == text and result.should_send
