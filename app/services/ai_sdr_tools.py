"""Ferramentas que o SDR consulta durante a decisão.

O modelo deixa de depender de preços e regras copiados no prompt: ele chama
consultar_catalogo para planos e preços oficiais (mesma fonte do Guardião) e
buscar_manual para trechos aprovados da base de conhecimento.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from app.models import AIAgentProfile, AIKnowledgeItem, AIKnowledgeMetadata
from app.services.ai_sdr_policy import catalog_for


_STOPWORDS = {
    "que", "qual", "quais", "como", "quanto", "quantos", "quando", "onde", "para",
    "pra", "com", "sem", "uma", "por", "mais", "tem", "voce", "isso", "esse",
    "essa", "meu", "minha", "sulnet", "plano",
}


def _fold(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def load_manual(db: Session | None, agent: AIAgentProfile) -> list[dict[str, str]]:
    """Itens aprovados do manual do agente, carregados antes da execução.

    Sem sessão de banco (avaliação offline), usa a base comercial aprovada V2.
    """
    if db is None:
        from app.services.ai_sdr_policy import SYSTEM_KNOWLEDGE_ITEMS

        return [
            {"title": item["title"], "tags": item["tags"], "content": item["content"]}
            for item in SYSTEM_KNOWLEDGE_ITEMS
        ]
    items = list(
        db.scalars(
            select(AIKnowledgeItem).where(
                AIKnowledgeItem.agent_id == agent.id,
                AIKnowledgeItem.active.is_(True),
            )
        )
    )
    if not items:
        return []
    blocked = set(
        db.scalars(
            select(AIKnowledgeMetadata.knowledge_item_id).where(
                AIKnowledgeMetadata.knowledge_item_id.in_([item.id for item in items]),
                AIKnowledgeMetadata.approval_status != "APPROVED",
            )
        )
    )
    return [
        {
            "title": item.title,
            "tags": item.tags or "",
            "content": item.content,
        }
        for item in items
        if item.id not in blocked
    ]


def search_manual(manual: list[dict[str, str]], question: str, limit: int = 4) -> list[dict[str, str]]:
    terms = {
        token for token in re.findall(r"[a-z0-9]{3,}", _fold(question))
        if token not in _STOPWORDS
    }
    if not terms:
        return []

    def score(item: dict[str, str]) -> int:
        title = _fold(f"{item['title']} {item['tags']}")
        body = _fold(item["content"])
        return sum(3 if term in title else 1 for term in terms if term in title or term in body)

    ranked = sorted(manual, key=score, reverse=True)
    return [item for item in ranked[:limit] if score(item) > 0]


def build_sdr_tools(agent: AIAgentProfile) -> list[Any]:
    from agents import function_tool

    try:
        session = object_session(agent)
    except Exception:  # objeto fora do SQLAlchemy (avaliação offline)
        session = None
    manual = load_manual(session, agent)

    @function_tool
    def consultar_catalogo(cidade: str, segmento: str) -> str:
        """Planos e preços oficiais vigentes da Sulnet.

        Args:
            cidade: cidade do cliente, ou "" se ainda não foi informada.
            segmento: RESIDENCIAL, EMPRESA ou MOVEL.
        """
        return json.dumps(catalog_for(cidade, segmento), ensure_ascii=False)

    @function_tool
    def buscar_manual(pergunta: str) -> str:
        """Trechos aprovados do manual comercial da Sulnet para responder dúvidas.

        Args:
            pergunta: a dúvida ou o assunto a procurar.
        """
        found = search_manual(manual, pergunta)
        if not found:
            return json.dumps(
                {"found": False, "note": "Sem trecho aprovado; não invente a resposta."},
                ensure_ascii=False,
            )
        return json.dumps({"found": True, "items": found}, ensure_ascii=False)

    @function_tool
    def calcular_multa(velocidade_mega: int, meses_gratis: int, meses_usados: int) -> str:
        """Multa de cancelamento antecipado de um plano de fibra residencial.

        Args:
            velocidade_mega: velocidade do plano (800, 650, 500 ou 250).
            meses_gratis: mensalidades grátis concedidas na contratação (0 a 3).
            meses_usados: quantos meses de fidelidade o cliente terá usado ao cancelar.
        """
        return json.dumps(
            ftth_cancellation_penalty(velocidade_mega, meses_gratis, meses_usados),
            ensure_ascii=False,
        )

    return [consultar_catalogo, buscar_manual, calcular_multa]


def ftth_cancellation_penalty(speed: int, free_months: int, months_used: int) -> dict[str, Any]:
    """Multa proporcional da fidelidade FTTH: (benefícios / 12) × meses restantes.

    Benefícios = habilitação isenta (R$ 860) + mensalidades grátis concedidas,
    a mesma regra que o Vendedor IA usa no fechamento.
    """
    from app.services.ai_sdr_policy import (
        FTTH_ACTIVATION_FEE,
        FTTH_FIDELITY_MONTHS,
        FTTH_MAX_FREE_MONTHS,
        FTTH_PLANS,
    )

    plan = next((item for item in FTTH_PLANS if int(item["download_mbps"]) == int(speed)), None)
    if not plan:
        return {"ok": False, "note": "Plano não encontrado no catálogo de fibra residencial."}
    free = max(0, min(FTTH_MAX_FREE_MONTHS, int(free_months)))
    used = max(0, int(months_used))
    remaining = max(0, FTTH_FIDELITY_MONTHS - used)
    benefits = round(FTTH_ACTIVATION_FEE + free * float(plan["price_fidelity"]), 2)
    penalty = round(benefits / FTTH_FIDELITY_MONTHS * remaining, 2)

    def brl(value: float) -> str:
        return f"R$ {value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    return {
        "ok": True,
        "plan": plan["name"],
        "months_remaining": remaining,
        "benefits_total": benefits,
        "penalty": penalty,
        "answer": (
            "Depois dos 12 meses de fidelidade não há multa."
            if remaining == 0
            else (
                f"Cancelando com {used} meses de uso, faltam {remaining} meses da fidelidade; "
                f"a multa fica em {brl(penalty)} (proporcional aos benefícios de "
                f"{brl(benefits)}: habilitação isenta"
                + (f" e {free} mensalidade(s) grátis" if free else "")
                + ")."
            )
        ),
    }
