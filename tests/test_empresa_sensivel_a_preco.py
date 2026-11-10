from app.services import ai_sdr_policy


def test_prompt_ensina_ancora_de_preco_para_empresa():
    source = open(ai_sdr_policy.__file__, encoding="utf-8").read()
    assert "EMPRESA QUE ACHA CARO OU SÓ QUER PREÇO" in source
    assert "Fibra 650 Mega por R$ 99,90" in source
    assert "1 mês grátis" in source
