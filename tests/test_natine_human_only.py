"""A fila Natine e exclusivamente humana, inclusive para contatos de teste."""

from types import SimpleNamespace

from app.services.ai_sdr import is_human_only_queue, binding_for_conversation


def test_fila_natine_id_4_e_humana():
    assert not is_human_only_queue(SimpleNamespace(id=4, name="Frente de Loja"))
    assert is_human_only_queue(SimpleNamespace(id=99, name="Natine"))
    assert is_human_only_queue(SimpleNamespace(id=99, name="  NATINE  "))
    assert not is_human_only_queue(SimpleNamespace(id=1, name="Fila IA"))


def test_fila_natine_nao_cria_binding_para_telefone_de_teste(monkeypatch):
    import app.services.ai_sdr as sdr

    monkeypatch.setattr(
        sdr,
        "queue_for_conversation",
        lambda db, conversation_id: SimpleNamespace(id=4, name="Natine"),
    )
    assert binding_for_conversation(None, 3636) is None
