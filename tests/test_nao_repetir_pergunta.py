from app.services import ai_sdr_policy


def test_prompt_proibe_repetir_pergunta_ja_respondida():
    source = open(ai_sdr_policy.__file__, encoding="utf-8").read()
    assert "NUNCA REPITA UMA PERGUNTA JÁ FEITA OU JÁ RESPONDIDA" in source
    assert "Moramos agora" in source
