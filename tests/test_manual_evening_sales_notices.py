"""Testes das salvaguardas dos alertas pontuais ao gestor desde 18h."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from app.services.manual_evening_sales_notices import _eligible, _timestamp, CUTOFF_UTC


def _conversation(status="EM_ATENDIMENTO"):
    return NS(status=status)


def _inbound(*, body="Quero falar com um vendedor", minutes=30, message_id=10):
    return NS(
        id=message_id,
        direction="INBOUND",
        message_type="TEXT",
        body=body,
        created_at=CUTOFF_UTC + timedelta(minutes=minutes),
        status="RECEIVED",
    )


def _outbound(message_id=11, status="SENT"):
    return NS(id=message_id, status=status)


def test_alerta_de_contato_recente_sem_resposta_elegivel():
    ok, reason = _eligible(
        conversation=_conversation(),
        inbound=_inbound(),
        outbound=None,
        lead=None,
    )
    assert ok is True
    assert reason == "pending_human_review"


def test_nao_avisar_que_ja_respondeu_ou_foi_encerrado():
    inbound = _inbound()
    assert _eligible(
        conversation=_conversation(),
        inbound=inbound,
        outbound=_outbound(status="DELIVERED"),
        lead=None,
    ) == (False, "already_replied")
    assert _eligible(
        conversation=_conversation("RESOLVIDO"),
        inbound=inbound,
        outbound=None,
        lead=None,
    ) == (False, "closed")


def test_avisa_falha_cancelada_de_envio():
    assert _eligible(
        conversation=_conversation(),
        inbound=_inbound(),
        outbound=_outbound(status="CANCELED"),
        lead=None,
    )[0] is True


def test_nao_avisar_respostas_automaticas_ou_opt_out():
    assert _eligible(
        conversation=_conversation(),
        inbound=_inbound(body="Agradecemos o contato. Nosso horário de funcionamento é das 8h às 18h."),
        outbound=None,
        lead=None,
    ) == (False, "automatic_reply")
    assert _eligible(
        conversation=_conversation(),
        inbound=_inbound(),
        outbound=None,
        lead=NS(stage="NAO_CONTATAR", do_not_contact=True),
    ) == (False, "do_not_contact")


def test_rejeita_mensagem_anterior_as_18h():
    assert _eligible(
        conversation=_conversation(),
        inbound=_inbound(minutes=-1),
        outbound=None,
        lead=None,
    ) == (False, "out_of_window")


def test_horario_naive_interpretado_como_utc():
    raw = datetime(2026, 11, 23, 21, 30)
    assert _timestamp(raw) == datetime(2026, 11, 23, 21, 30, tzinfo=timezone.utc)
