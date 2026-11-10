from app.services import ai_sdr


def _ctx(msg, previous=()):
    return {
        "latest_customer_message": msg,
        "confirmed_facts": {},
        "lead": {},
        "history": [
            {"direction": "OUTBOUND", "body": body}
            for body in (list(previous) or ["Você ainda quer conhecer as opções?"])
        ],
    }


def test_nao_agora_nao_vira_pergunta_de_cidade():
    text, buttons = ai_sdr._ai_recovery_reply(_ctx("Não agora"))
    assert "cidade" not in text.lower()
    assert "o que pesa" in text.lower()
    assert [b.label for b in buttons] == ["O valor", "Não é o momento", "Já tenho internet"]


def test_variacoes_de_adiamento():
    for msg in ("Agora não", "mais tarde, obrigado", "vou pensar", "Depois"):
        assert ai_sdr._is_postponement(ai_sdr._fold_sales_text(msg)), msg
    assert not ai_sdr._is_postponement(ai_sdr._fold_sales_text("Santa Rosa"))
    assert not ai_sdr._is_postponement(ai_sdr._fold_sales_text("Quanto custa depois da promoção?"))


def test_nao_repete_a_mesma_quebra():
    primeira, _ = ai_sdr._ai_recovery_reply(_ctx("Não agora"))
    segunda, _ = ai_sdr._ai_recovery_reply(_ctx("Não agora", previous=[primeira]))
    assert primeira != segunda
