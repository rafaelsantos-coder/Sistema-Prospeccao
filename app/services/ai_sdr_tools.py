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

    return [consultar_catalogo, buscar_manual]
