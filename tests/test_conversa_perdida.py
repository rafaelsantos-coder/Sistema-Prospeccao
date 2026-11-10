from app.routers import conversations


def test_conversa_reatribuida_volta_para_a_lista_sem_tela_de_erro(monkeypatch):
    avisos = []
    monkeypatch.setattr(conversations, "flash", lambda request, msg, level="info": avisos.append((msg, level)))
    resposta = conversations._conversation_unavailable(object())
    assert resposta.status_code == 303
    assert resposta.headers["location"] == "/conversations"
    assert avisos and avisos[0][1] == "warning"
    assert "reatribu" in avisos[0][0]
