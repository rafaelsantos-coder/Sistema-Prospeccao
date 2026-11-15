"""Rechamada dos clientes atendidos pela IA (pedido do comercial, 15/11).

Roteiro fixo, em código, para a IA nunca inventar condição:

1. Pergunta: "Se eu tivesse uma promoção para você fechar hoje comigo, você
   iria fechar?" com os botões Sim / Agora mesmo / Não.
2. "Não" -> uma insistência ("Você tem certeza?") com os mesmos botões.
   Segundo "Não" -> a IA agradece e encerra, sem novo follow-up.
3. "Sim" ou "Agora mesmo" -> oferta: 800 Mega com 3 mensalidades grátis
   (economia de 3 x R$ 119,90), roteador e instalação gratuitos, instalação
   em até 48 horas, e a pergunta de fechamento.
4. "Sim" na oferta -> o Vendedor IA fecha com 3 mensalidades grátis. No CRM a
   oportunidade leva a tag "Desconto 3 mensalidades", que escolhe o contrato.

Qualquer resposta fora do sim/não devolve a conversa ao fluxo normal da IA.
Este módulo não acessa banco nem rede.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone

CAMPAIGN_NAME = "Rechamada IA: promoção 800 Mega com 3 meses grátis"
PLAN_SPEED = 800
PLAN_LABEL = "Fibra 800 Mega"
MONTHLY_PRICE = 119.90
FREE_MONTHS = 3
SUMMARY_MARKER = "[Rechamada 3 mensalidades grátis]"

_BRASILIA = timezone(timedelta(hours=-3))


def _brl(value: float) -> str:
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


SAVINGS = round(MONTHLY_PRICE * FREE_MONTHS, 2)
SAVINGS_LABEL = _brl(SAVINGS)

QUESTION_TEXT = (
    "Olá! Aqui é a Sulnet. Se eu tivesse uma promoção para você fechar hoje comigo, você iria fechar?"
)
QUESTION_MARK = "promocao para voce fechar hoje comigo"
INSIST_TEXT = "Você tem certeza? Essa promoção vai fazer você economizar dinheiro. Quer conhecer?"
INSIST_MARK = "essa promocao vai fazer voce economizar dinheiro"
OFFER_MARK = "800 mega com 3 meses gratuitos"
CITY_TEXT = "Perfeito! Para garantir a promoção, em qual cidade será a instalação?"
CITY_MARK = "para garantir a promocao em qual cidade"
END_TEXT = "Tudo bem, obrigado pela atenção! Se mudar de ideia, é só me chamar por aqui."

QUESTION_BUTTONS = (
    {"id": "recall_sim", "label": "Sim"},
    {"id": "recall_agora", "label": "Agora mesmo"},
    {"id": "recall_nao", "label": "Não"},
)
OFFER_BUTTONS = (
    {"id": "recall_fechar", "label": "Sim, vamos fechar"},
    {"id": "recall_agora_nao", "label": "Agora não"},
)


def offer_text(now: datetime | None = None) -> str:
    current = (now or datetime.now(timezone.utc)).astimezone(_BRASILIA)
    promo = "essa promoção do nosso domingo" if current.weekday() == 6 else "essa promoção"
    return (
        f"Tenho uma promoção exclusiva para você: {PLAN_SPEED} Mega com {FREE_MONTHS} meses gratuitos, "
        f"sem pagar nada, gerando uma economia de R$ {SAVINGS_LABEL} ({FREE_MONTHS} x R$ {_brl(MONTHLY_PRICE)}). "
        "Roteador e instalação gratuitos, com instalação em até 48 horas. "
        f"Depois dos {FREE_MONTHS} meses, a mensalidade é de R$ {_brl(MONTHLY_PRICE)}, com fidelidade de 12 meses. "
        f"Podemos fechar esse plano agora com você para garantir {promo}?"
    )


def fold(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


_YES = {
    "sim", "s", "agora mesmo", "agora", "claro", "com certeza", "quero", "quero sim", "fecharia",
    "fecho", "fecharia sim", "iria", "iria sim", "pode ser", "bora", "ok", "sim vamos fechar",
    "vamos fechar", "quero fechar", "podemos", "podemos sim", "pode", "pode sim", "fechado",
}
_NO = {
    "nao", "n", "agora nao", "nao quero", "nao obrigado", "nao obrigada", "nao iria", "nao fecharia",
    "nao tenho interesse", "sem interesse", "nao valeu",
}
# Depois de "Você tem certeza?", confirmar a certeza é manter a recusa.
_SURE = {"tenho certeza", "tenho", "certeza", "certeza absoluta", "sim tenho certeza", "tenho sim"}


def answer(text: str | None) -> str:
    """YES, NO ou vazio (resposta livre: volta para o fluxo normal da IA)."""
    folded = fold(text)
    if not folded:
        return ""
    if folded in _NO or folded.startswith(("nao ", "agora nao")):
        return "NO"
    if folded in _YES or folded.startswith(("sim ", "quero ", "agora mesmo")):
        return "YES"
    return ""


def step_of(outbound: str | None) -> str:
    """Etapa do roteiro a que a última mensagem enviada pertence."""
    folded = fold(outbound)
    if not folded:
        return ""
    if OFFER_MARK in folded:
        return "OFFER"
    if CITY_MARK in folded:
        return "CITY"
    if INSIST_MARK in folded:
        return "INSIST"
    if QUESTION_MARK in folded:
        return "QUESTION"
    return ""


def next_action(last_outbound: str | None, latest_inbound: str | None) -> str:
    """Próximo passo do roteiro.

    OFFER (enviar a oferta), INSIST (insistir uma vez), END (encerrar),
    CLOSE (levar ao fechamento), CITY (cidade respondida) ou vazio (fora do
    roteiro: o fluxo normal da IA responde).
    """
    step = step_of(last_outbound)
    if not step:
        return ""
    if step == "CITY":
        return "CITY" if fold(latest_inbound) else ""
    reply = answer(latest_inbound)
    if step == "QUESTION":
        return {"YES": "OFFER", "NO": "INSIST"}.get(reply, "")
    if step == "INSIST":
        if fold(latest_inbound) in _SURE:
            return "END"
        return {"YES": "OFFER", "NO": "END"}.get(reply, "")
    return "CLOSE" if reply == "YES" else ""


def with_marker(summary: str | None) -> str:
    text = (summary or "").strip()
    if SUMMARY_MARKER in text:
        return text
    return f"{text} {SUMMARY_MARKER}".strip()


def has_marker(summary: str | None) -> bool:
    return SUMMARY_MARKER in (summary or "")


def discount_tag(free_months: int) -> str:
    """Tag do CRM que escolhe o contrato com mensalidades grátis (650 ou 800 Mega)."""
    months = int(free_months or 0)
    if months == 1:
        return "Desconto 1 mensalidade"
    if months in (2, 3):
        return f"Desconto {months} mensalidades"
    return ""
