"""Tipos estruturados da decisão do SDR."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, PrivateAttr

from app.services.ai_quick_replies import QuickReply


class QualificationData(BaseModel):
    current_provider: str | None = None
    current_speed: str | None = None
    current_price: str | None = None
    pain_points: list[str] = Field(default_factory=list)
    desired_product: str | None = None
    customer_type: Literal["RESIDENCIAL", "EMPRESA", "NAO_INFORMADO"] = "NAO_INFORMADO"
    existing_customer: Literal["CLIENTE", "NAO_CLIENTE", "NAO_INFORMADO"] = "NAO_INFORMADO"
    decision_maker: Literal["SIM", "NAO", "NAO_INFORMADO"] = "NAO_INFORMADO"
    timeframe: str | None = None
    city: str | None = None
    address: str | None = None


class AIDecision(BaseModel):
    # O que as regras fixas mudaram (ou mudariam, em modo observação) nesta decisão.
    _policy_observations: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    # Tempo do modelo e ferramentas chamadas nesta decisão (diagnóstico de latência).
    _timing: dict[str, Any] = PrivateAttr(default_factory=dict)
    reply_text: str = Field(min_length=1, max_length=1800)
    quick_replies: list[QuickReply] = Field(default_factory=list, max_length=3)
    intent: Literal[
        "INFORMATION",
        "QUALIFYING",
        "INTEREST",
        "HIGH_INTENT",
        "PROPOSAL_REQUEST",
        "OBJECTION_PRICE",
        "OBJECTION_QUALITY",
        "OBJECTION_SUPPORT",
        "HUMAN_REQUEST",
        "NOT_INTERESTED",
        "DO_NOT_CONTACT",
        "UNKNOWN",
    ] = "UNKNOWN"
    qualification_score: int = Field(ge=0, le=100)
    temperature: Literal["FRIO", "MORNO", "QUENTE"]
    recommended_stage: Literal[
        "NOVO",
        "TENTATIVA_CONTATO",
        "CONTATO_REALIZADO",
        "QUALIFICADO",
        "PROPOSTA",
        "NEGOCIACAO",
        "NAO_CONTATAR",
    ]
    handoff_recommended: bool
    handoff_reason: str = Field(default="", max_length=600)
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(max_length=1800)
    next_action: str = Field(max_length=600)
    qualification: QualificationData = Field(default_factory=QualificationData)
    crm_notes: list[str] = Field(default_factory=list)
    objection: Literal[
        "NONE",
        "PRICE",
        "NO_INTEREST",
        "CURRENT_PROVIDER",
        "FIDELITY",
        "TIMING",
        "DECISION_MAKER",
        "TRUST",
        "INSTALLATION",
        "MONEY_NOW",
        "OTHER",
    ] = "NONE"
    sales_state: Literal[
        "NEW_LEAD",
        "DISCOVERY",
        "QUALIFIED",
        "OFFERING",
        "NEGOTIATING",
        "WAITING_CUSTOMER",
        "PURCHASE_INTENT",
        "READY_FOR_HUMAN_SALES",
        "OPT_OUT",
    ] = "DISCOVERY"
    offer_plan_code: str | None = None
    offer_price: float | None = None
    concession_level: int = Field(default=0, ge=0, le=3)
    free_months: int = Field(default=0, ge=0, le=3)
    customer_commitment: bool = False
    followup_needed: bool = True
    customer_needs: list[str] = Field(default_factory=list)
    benefits_valued: list[str] = Field(default_factory=list)
    commercial_arguments_used: list[str] = Field(default_factory=list)
    proposal_summary: str = Field(default="", max_length=800)
    unanswered_question: str | None = Field(default=None, max_length=500)
    journey_state: Literal[
        "IDENTIFY_CUSTOMER",
        "CHOOSE_PRODUCT",
        "CHOOSE_SEGMENT",
        "CONFIRM_CITY",
        "SELECT_OFFER",
        "HANDLE_OBJECTION",
        "CONFIRM_PURCHASE",
        "CLOSING",
        "HUMAN_HANDOFF",
        "OPT_OUT",
        "DISCOVERY",
    ] = "DISCOVERY"
    journey_pending_action: str = Field(default="", max_length=600)
    handoff_target: Literal["NONE", "CLOSER_AI", "HUMAN"] = "NONE"
    stagnation_count: int = Field(default=0, ge=0, le=20)
    service_redirect: bool = False


class AIConfigurationError(RuntimeError):
    pass


