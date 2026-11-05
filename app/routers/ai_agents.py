from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload
from starlette.responses import JSONResponse, RedirectResponse

from app.config import settings
from app.database import get_db
from app.models import (
    AIAgentProfile,
    AIKnowledgeItem,
    AIKnowledgeMetadata,
    AIQueueBinding,
    AIRun,
    Conversation,
    ServiceQueue,
    User,
)
from app.security import get_current_user, require_roles, validate_csrf
from app.services.ai_sdr import ai_metrics
from app.services.audit import write_audit
from app.services.zapi_instances import queue_instance
from app.web import flash, render


LOG = logging.getLogger("sulnet_ai_agents")

router = APIRouter(prefix="/ai")

HANDLING_MODES = {
    "AI_FIRST": "IA primeiro",
    "HYBRID": "Híbrido",
    "AI_ONLY": "IA exclusiva",
    "HUMAN": "Somente humano",
}
EXECUTION_MODES = {
    "SHADOW": "Shadow · analisa sem enviar",
    "APPROVAL": "Aprovação humana",
    "AUTO": "Autônomo",
}

KNOWLEDGE_CATEGORIES = {
    "INSTITUCIONAL": "Institucional e história da empresa",
    "DIFERENCIAIS": "Diferenciais competitivos",
    "PRODUTOS": "Produtos, planos e benefícios",
    "ATENDIMENTO": "Atendimento e suporte",
    "COBERTURA": "Cobertura e regiões atendidas",
    "ARGUMENTOS_COMERCIAIS": "Argumentos comerciais",
    "OBJECOES": "Objeções e respostas",
    "REGRAS_COMERCIAIS": "Regras e restrições comerciais",
}


def _actor(request: Request, db: Session) -> tuple[User | None, RedirectResponse | None]:
    user = get_current_user(request, db)
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    if user.role not in {"ADMIN", "GESTOR"}:
        flash(request, "A configuração dos agentes de IA é restrita a gestores.", "warning")
        return None, RedirectResponse("/dashboard", status_code=303)
    return user, None


def _agent_or_404(db: Session, agent_id: int) -> AIAgentProfile:
    agent = db.scalar(
        select(AIAgentProfile)
        .options(selectinload(AIAgentProfile.user))
        .where(AIAgentProfile.id == agent_id)
    )
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return agent


@router.get("")
def ai_dashboard(request: Request, db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect

    agents = list(
        db.scalars(
            select(AIAgentProfile)
            .options(selectinload(AIAgentProfile.user))
            .where(AIAgentProfile.role_type == "SDR")
            .order_by(AIAgentProfile.id)
        )
    )
    queues = list(
        db.scalars(
            select(ServiceQueue)
            .options(selectinload(ServiceQueue.branch))
            .where(ServiceQueue.active.is_(True))
            .order_by(ServiceQueue.name)
        )
    )
    bindings = list(
        db.scalars(
            select(AIQueueBinding)
            .options(
                selectinload(AIQueueBinding.queue),
                selectinload(AIQueueBinding.agent),
            )
            .order_by(AIQueueBinding.queue_id)
        )
    )
    queue_instances = {
        queue.id: queue_instance(db, queue.id)
        for queue in queues
    }
    knowledge = list(
        db.scalars(
            select(AIKnowledgeItem)
            .options(selectinload(AIKnowledgeItem.agent))
            .order_by(AIKnowledgeItem.updated_at.desc(), AIKnowledgeItem.id.desc())
            .limit(200)
        )
    )
    recent_runs = list(
        db.scalars(
            select(AIRun)
            .options(
                selectinload(AIRun.agent),
                selectinload(AIRun.conversation),
            )
            .order_by(AIRun.created_at.desc())
            .limit(80)
        )
    )
    metrics = {agent.id: ai_metrics(db, agent.id) for agent in agents}
    knowledge_counts = {
        agent.id: int(
            db.scalar(
                select(func.count(AIKnowledgeItem.id)).where(
                    AIKnowledgeItem.agent_id == agent.id,
                    AIKnowledgeItem.active.is_(True),
                )
            )
            or 0
        )
        for agent in agents
    }

    return render(
        request,
        "ai/index.html",
        user=actor,
        active_nav="ai",
        ai_section="agent",
        page_title="IA & Agentes",
        agents=agents,
        queues=queues,
        bindings=bindings,
        queue_instances=queue_instances,
        knowledge=knowledge,
        recent_runs=recent_runs,
        metrics=metrics,
        knowledge_counts=knowledge_counts,
        handling_modes=HANDLING_MODES,
        execution_modes=EXECUTION_MODES,
        openai_configured=bool(settings.openai_api_key),
        ai_sdr_enabled=settings.ai_sdr_enabled,
        autonomous_enabled=settings.ai_autonomous_enabled,
    )


@router.get("/quality")
def ai_quality_dashboard(request: Request, days: int = 7, db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    from app.services.ai_quality import conversation_quality_report

    window = days if days in {1, 7, 14, 30} else 7
    report = conversation_quality_report(db, days=window)
    return render(
        request,
        "ai/quality.html",
        user=actor,
        active_nav="ai",
        ai_section="quality",
        page_title="Qualidade das conversas",
        report=report,
        days=window,
    )


@router.get("/quality/{conversation_id}/case.json")
def ai_quality_case(conversation_id: int, request: Request, db: Session = Depends(get_db)):
    """Histórico anonimizado de uma conversa, no formato de caso de teste."""
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    from app.models import Message
    from app.services.pii import redact_pii_text

    conversation = db.get(Conversation, conversation_id)
    if not conversation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    messages = list(
        db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
    )
    case = {
        "id": f"conv{conversation_id}-descreva-o-caso",
        "conversation": conversation_id,
        "description": "Descreva o que deu errado e o comportamento esperado.",
        "kind": "conversation_replay",
        "input": {
            "history": [
                {"direction": item.direction, "body": redact_pii_text(item.body)}
                for item in messages
            ],
        },
        "expect": {"notes": "Comportamento esperado da IA neste ponto da conversa."},
    }
    write_audit(
        db,
        user=actor,
        action="AI_TEST_CASE_EXPORTED",
        entity_type="conversation",
        entity_id=conversation_id,
        payload={"messages": len(messages)},
    )
    db.commit()
    return JSONResponse(
        case,
        headers={
            "Content-Disposition": f'attachment; filename="conv{conversation_id}.json"'
        },
    )


@router.get("/weekly")
def ai_weekly_dashboard(request: Request, db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    from app.models import AIKnowledgeMetadata
    from app.services.ai_weekly_analyst import ANALYST_SOURCE, RUN_ACTION, weekly_report

    report = weekly_report(db, days=7)
    drafts = list(
        db.scalars(
            select(AIKnowledgeItem)
            .join(AIKnowledgeMetadata, AIKnowledgeMetadata.knowledge_item_id == AIKnowledgeItem.id)
            .where(
                AIKnowledgeMetadata.approval_status != "APPROVED",
                AIKnowledgeMetadata.source_reference.like(f"{ANALYST_SOURCE}%"),
            )
            .order_by(AIKnowledgeItem.created_at.desc())
            .limit(50)
        )
    )
    from app.models import AuditLog

    last_run = db.scalar(
        select(AuditLog).where(AuditLog.action == RUN_ACTION).order_by(AuditLog.created_at.desc()).limit(1)
    )
    return render(
        request,
        "ai/weekly.html",
        user=actor,
        active_nav="ai",
        ai_section="weekly",
        page_title="Analista semanal",
        report=report,
        drafts=drafts,
        last_run=last_run,
    )


@router.post("/weekly/run")
def ai_weekly_run(
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    from app.services.ai_weekly_analyst import run_weekly_analyst

    result = run_weekly_analyst(db, user=actor)
    db.commit()
    flash(
        request,
        f"Analista executado: {result['drafts_created']} dúvida(s) nova(s) enviada(s) para revisão do manual.",
        "success",
    )
    return RedirectResponse("/ai/weekly", status_code=303)


@router.get("/simulator")
def ai_simulator_page(request: Request, db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    return render(
        request,
        "ai/simulator.html",
        user=actor,
        active_nav="ai",
        ai_section="simulator",
        page_title="Simulador do SDR",
    )


@router.post("/simulator/turn")
async def ai_simulator_turn(request: Request, db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return JSONResponse({"error": "Acesso restrito a gestores."}, status_code=403)
    payload = await request.json()
    validate_csrf(request, str(payload.get("csrf_token") or ""))
    message = str(payload.get("message") or "").strip()[:2000]
    if not message:
        return JSONResponse({"error": "Mensagem vazia."}, status_code=400)
    history = [
        {"direction": str(row.get("direction") or ""), "body": str(row.get("body") or "")}
        for row in (payload.get("history") or [])[-40:]
        if isinstance(row, dict)
    ]
    from starlette.concurrency import run_in_threadpool

    from app.services.ai_simulator import simulate_sdr_turn

    def _run() -> dict:
        try:
            return simulate_sdr_turn(
                db,
                history=history,
                message=message,
                city=str(payload.get("city") or "")[:80],
                variant=payload.get("variant") if isinstance(payload.get("variant"), dict) else None,
            )
        finally:
            # Simulação nunca grava nada: descarta qualquer alteração da sessão.
            db.rollback()

    try:
        result = await run_in_threadpool(_run)
    except Exception:
        LOG.exception("ai_simulator_failed")
        return JSONResponse({"error": "Falha ao simular a resposta do SDR."}, status_code=500)
    write_audit(
        db,
        user=actor,
        action="AI_SDR_SIMULATED",
        entity_type="ai",
        entity_id="simulator",
        payload={"turn": len(history) // 2 + 1, "journey_state": result.get("journey_state")},
    )
    db.commit()
    return JSONResponse(result)


@router.get("/seller")
def seller_dashboard(request: Request, db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect

    agent = db.scalar(
        select(AIAgentProfile)
        .options(selectinload(AIAgentProfile.user))
        .where(AIAgentProfile.role_type == "CLOSER")
        .order_by(AIAgentProfile.id)
    )
    knowledge = []
    metrics = {}
    if agent:
        knowledge = list(
            db.scalars(
                select(AIKnowledgeItem)
                .where(AIKnowledgeItem.agent_id == agent.id)
                .order_by(AIKnowledgeItem.updated_at.desc(), AIKnowledgeItem.id.desc())
            )
        )
        metrics = ai_metrics(db, agent.id)

    return render(
        request,
        "ai/seller.html",
        user=actor,
        active_nav="ai",
        ai_section="seller",
        page_title="Vendedor IA · Fechamento",
        agent=agent,
        knowledge=knowledge,
        metrics=metrics,
        execution_modes=EXECUTION_MODES,
        autonomous_enabled=settings.ai_autonomous_enabled,
        seller_enabled=settings.ai_seller_enabled,
        crm_configured=bool(settings.sulnet_crm_base_url and settings.sulnet_crm_bearer_token),
    )


@router.get("/knowledge")
def knowledge_dashboard(request: Request, db: Session = Depends(get_db)):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect

    agents = list(
        db.scalars(
            select(AIAgentProfile)
            .options(selectinload(AIAgentProfile.user))
            .order_by(AIAgentProfile.id)
        )
    )
    items = list(
        db.scalars(
            select(AIKnowledgeItem)
            .options(selectinload(AIKnowledgeItem.agent))
            .order_by(AIKnowledgeItem.category, AIKnowledgeItem.updated_at.desc())
            .limit(500)
        )
    )
    metadata_rows = list(db.scalars(select(AIKnowledgeMetadata)))
    metadata = {row.knowledge_item_id: row for row in metadata_rows}

    category_counts = {
        key: sum(1 for item in items if item.category == key and item.active)
        for key in KNOWLEDGE_CATEGORIES
    }
    approved_count = sum(
        1
        for item in items
        if item.active
        and (
            item.id not in metadata
            or metadata[item.id].approval_status == "APPROVED"
        )
    )
    recent_runs = list(
        db.scalars(
            select(AIRun)
            .order_by(AIRun.created_at.desc())
            .limit(250)
        )
    )
    failed_runs = sum(1 for run in recent_runs if run.status == "FALHOU")
    handoffs = sum(1 for run in recent_runs if run.handoff_recommended)
    interested = sum(
        1 for run in recent_runs
        if run.intent in {"INTEREST", "HIGH_INTENT", "PROPOSAL_REQUEST"}
    )
    qualified = sum(1 for run in recent_runs if int(run.qualification_score or 0) >= 70)
    human_requests = sum(1 for run in recent_runs if run.intent == "HUMAN_REQUEST")
    attended = len({run.conversation_id for run in recent_runs})

    objection_counts: Counter[str] = Counter()
    argument_counts: Counter[str] = Counter()
    unanswered_questions: list[str] = []
    handoff_conversation_ids: set[int] = set()
    for run in recent_runs:
        if run.handoff_recommended:
            handoff_conversation_ids.add(run.conversation_id)
        try:
            output = json.loads(run.output_json or "{}")
        except (TypeError, json.JSONDecodeError):
            output = {}
        objection = str(output.get("objection") or "").strip()
        if objection and objection != "NONE":
            objection_counts[objection] += 1
        for argument in output.get("commercial_arguments_used") or []:
            clean = str(argument).strip()
            if clean:
                argument_counts[clean] += 1
        unanswered = str(output.get("unanswered_question") or "").strip()
        if unanswered and unanswered not in unanswered_questions:
            unanswered_questions.append(unanswered)

    conversions_after_handoff = 0
    if handoff_conversation_ids:
        transferred = list(
            db.scalars(
                select(Conversation)
                .options(selectinload(Conversation.lead))
                .where(Conversation.id.in_(handoff_conversation_ids))
            )
        )
        conversions_after_handoff = sum(
            1 for conversation in transferred
            if conversation.lead and conversation.lead.stage == "GANHO"
        )

    return render(
        request,
        "ai/knowledge.html",
        user=actor,
        active_nav="ai",
        ai_section="knowledge",
        page_title="Conhecimento Sulnet",
        agents=agents,
        knowledge=items,
        metadata=metadata,
        knowledge_categories=KNOWLEDGE_CATEGORIES,
        category_counts=category_counts,
        approved_count=approved_count,
        training_metrics={
            "runs": len(recent_runs),
            "attended": attended,
            "interested": interested,
            "qualified": qualified,
            "handoffs": handoffs,
            "human_requests": human_requests,
            "converted_after_handoff": conversions_after_handoff,
            "failed": failed_runs,
        },
        top_objections=objection_counts.most_common(8),
        top_arguments=argument_counts.most_common(8),
        unanswered_questions=unanswered_questions[:12],
    )


@router.post("/agents/{agent_id}")
def update_agent(
    agent_id: int,
    request: Request,
    csrf_token: str = Form(...),
    name: str = Form(...),
    public_name: str = Form("Sulnet"),
    model: str = Form("gpt-6-luna"),
    reasoning_effort: str = Form("low"),
    execution_mode: str = Form("SHADOW"),
    qualification_handoff_score: int = Form(70),
    max_history_messages: int = Form(24),
    business_goal: str = Form(""),
    system_prompt: str = Form(""),
    active: bool = Form(False),
    db: Session = Depends(get_db),
):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(actor, "ADMIN", "GESTOR")

    agent = _agent_or_404(db, agent_id)
    mode = execution_mode.strip().upper()
    if mode not in EXECUTION_MODES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    if mode != "SHADOW" and not settings.ai_autonomous_enabled:
        flash(
            request,
            "A Fase 1 está protegida em Shadow Mode. Aprovação/Autônomo só serão liberados após a homologação.",
            "warning",
        )
        mode = "SHADOW"

    effort = reasoning_effort.strip().lower()
    if effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
        effort = "low"

    agent.name = name.strip() or agent.name
    agent.public_name = public_name.strip() or "Sulnet"
    agent.model = model.strip() or "gpt-6-luna"
    agent.reasoning_effort = effort
    agent.execution_mode = mode
    agent.qualification_handoff_score = max(0, min(100, qualification_handoff_score))
    agent.max_history_messages = max(4, min(80, max_history_messages))
    agent.business_goal = business_goal.strip()
    agent.system_prompt = system_prompt.strip()
    agent.active = active
    if agent.role_type == "CLOSER" and agent.user:
        # O Vendedor IA possui identidade comercial própria. O cadastro de
        # usuário acompanha o status do agente para relatórios/atribuições.
        agent.user.active = active

    write_audit(
        db,
        user=actor,
        action="AI_AGENT_UPDATED",
        entity_type="ai_agent",
        entity_id=agent.id,
        payload={
            "name": agent.name,
            "model": agent.model,
            "execution_mode": agent.execution_mode,
            "active": agent.active,
            "handoff_score": agent.qualification_handoff_score,
        },
    )
    db.commit()
    flash(request, f"Agente {agent.name} atualizado.")
    return RedirectResponse("/ai/seller" if agent.role_type == "CLOSER" else "/ai", status_code=303)


@router.post("/knowledge")
def create_knowledge(
    request: Request,
    csrf_token: str = Form(...),
    agent_id: int = Form(...),
    title: str = Form(...),
    category: str = Form("GERAL"),
    tags: str = Form(""),
    content: str = Form(...),
    subject: str = Form(""),
    product_service: str = Form(""),
    region: str = Form(""),
    source_reference: str = Form(""),
    priority: int = Form(50),
    valid_until: str = Form(""),
    approval_status: str = Form("APPROVED"),
    redirect_to: str = Form("/ai"),
    db: Session = Depends(get_db),
):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(actor, "ADMIN", "GESTOR")
    agent = _agent_or_404(db, agent_id)

    clean_title = title.strip()
    clean_content = content.strip()
    if not clean_title or not clean_content:
        flash(request, "Informe título e conteúdo do conhecimento.", "warning")
        target = redirect_to if redirect_to.startswith("/ai") else "/ai"
        return RedirectResponse(target, status_code=303)

    item = AIKnowledgeItem(
        agent_id=agent.id,
        title=clean_title[:180],
        category=(category.strip().upper() or "GERAL")[:80],
        tags=tags.strip()[:500],
        content=clean_content,
        source_type="MANUAL",
        active=True,
        created_by_id=actor.id,
    )
    db.add(item)
    db.flush()

    valid_until_dt = None
    if valid_until.strip():
        try:
            valid_until_dt = datetime.fromisoformat(valid_until.strip()).replace(tzinfo=timezone.utc)
        except ValueError:
            valid_until_dt = None

    db.add(
        AIKnowledgeMetadata(
            knowledge_item_id=item.id,
            subject=subject.strip()[:120],
            product_service=product_service.strip()[:120],
            region=region.strip()[:180],
            source_reference=source_reference.strip()[:500],
            priority=max(0, min(100, priority)),
            approval_status=(
                approval_status.strip().upper()
                if approval_status.strip().upper() in {"APPROVED", "DRAFT", "REVIEW"}
                else "APPROVED"
            ),
            valid_until=valid_until_dt,
        )
    )
    write_audit(
        db,
        user=actor,
        action="AI_KNOWLEDGE_CREATED",
        entity_type="ai_knowledge",
        entity_id=item.id,
        payload={"agent_id": agent.id, "title": item.title, "category": item.category},
    )
    db.commit()
    flash(request, "Conhecimento adicionado à base do SulIA.")
    target = redirect_to if redirect_to.startswith("/ai") else "/ai"
    return RedirectResponse(target, status_code=303)


@router.post("/knowledge/{item_id}")
def update_knowledge(
    item_id: int,
    request: Request,
    csrf_token: str = Form(...),
    title: str = Form(...),
    category: str = Form("GERAL"),
    tags: str = Form(""),
    content: str = Form(...),
    subject: str = Form(""),
    product_service: str = Form(""),
    region: str = Form(""),
    source_reference: str = Form(""),
    priority: int = Form(50),
    valid_until: str = Form(""),
    approval_status: str = Form("APPROVED"),
    db: Session = Depends(get_db),
):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(actor, "ADMIN", "GESTOR")

    item = db.get(AIKnowledgeItem, item_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    clean_title = title.strip()
    clean_content = content.strip()
    if not clean_title or not clean_content:
        flash(request, "Informe título e conteúdo do conhecimento.", "warning")
        return RedirectResponse("/ai/knowledge", status_code=303)

    item.title = clean_title[:180]
    item.category = (category.strip().upper() or "GERAL")[:80]
    item.tags = tags.strip()[:500]
    item.content = clean_content

    meta = db.scalar(
        select(AIKnowledgeMetadata).where(
            AIKnowledgeMetadata.knowledge_item_id == item.id
        )
    )
    if not meta:
        meta = AIKnowledgeMetadata(knowledge_item_id=item.id)
        db.add(meta)

    meta.subject = subject.strip()[:120]
    meta.product_service = product_service.strip()[:120]
    meta.region = region.strip()[:180]
    meta.source_reference = source_reference.strip()[:500]
    meta.priority = max(0, min(100, priority))
    clean_status = approval_status.strip().upper()
    meta.approval_status = clean_status if clean_status in {"APPROVED", "DRAFT", "REVIEW"} else "APPROVED"
    meta.valid_until = None
    if valid_until.strip():
        try:
            meta.valid_until = datetime.fromisoformat(valid_until.strip()).replace(tzinfo=timezone.utc)
        except ValueError:
            meta.valid_until = None

    write_audit(
        db,
        user=actor,
        action="AI_KNOWLEDGE_UPDATED",
        entity_type="ai_knowledge",
        entity_id=item.id,
        payload={
            "title": item.title,
            "category": item.category,
            "priority": meta.priority,
            "approval_status": meta.approval_status,
        },
    )
    db.commit()
    flash(request, "Conhecimento atualizado.")
    return RedirectResponse("/ai/knowledge", status_code=303)


@router.post("/knowledge/{item_id}/toggle")
def toggle_knowledge(
    item_id: int,
    request: Request,
    csrf_token: str = Form(...),
    redirect_to: str = Form("/ai"),
    db: Session = Depends(get_db),
):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    item = db.get(AIKnowledgeItem, item_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    item.active = not item.active
    write_audit(
        db,
        user=actor,
        action="AI_KNOWLEDGE_ACTIVATED" if item.active else "AI_KNOWLEDGE_DEACTIVATED",
        entity_type="ai_knowledge",
        entity_id=item.id,
    )
    db.commit()
    target = redirect_to if redirect_to.startswith("/ai") else "/ai"
    return RedirectResponse(target, status_code=303)


@router.post("/queues/bind")
def bind_queue(
    request: Request,
    csrf_token: str = Form(...),
    queue_id: int = Form(...),
    agent_id: int = Form(...),
    handling_mode: str = Form("AI_FIRST"),
    fallback_to_human: bool = Form(False),
    db: Session = Depends(get_db),
):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    require_roles(actor, "ADMIN", "GESTOR")

    queue = db.get(ServiceQueue, queue_id)
    agent = _agent_or_404(db, agent_id)
    mode = handling_mode.strip().upper()
    if not queue or not queue.active:
        flash(request, "Fila inválida ou inativa.", "warning")
        return RedirectResponse("/ai", status_code=303)
    if agent.role_type != "SDR":
        flash(
            request,
            "O Vendedor IA recebe handoff do SDR e não pode ser vinculado como agente inicial de uma fila.",
            "warning",
        )
        return RedirectResponse("/ai/seller", status_code=303)
    if mode not in HANDLING_MODES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)

    binding = db.scalar(
        select(AIQueueBinding).where(AIQueueBinding.queue_id == queue.id)
    )
    if not binding:
        binding = AIQueueBinding(
            queue_id=queue.id,
            agent_id=agent.id,
            handling_mode=mode,
            active=True,
            fallback_to_human=fallback_to_human,
        )
        db.add(binding)
    else:
        binding.agent_id = agent.id
        binding.handling_mode = mode
        binding.active = True
        binding.fallback_to_human = fallback_to_human

    write_audit(
        db,
        user=actor,
        action="AI_QUEUE_BOUND",
        entity_type="service_queue",
        entity_id=queue.id,
        payload={
            "agent_id": agent.id,
            "handling_mode": mode,
            "fallback_to_human": fallback_to_human,
            "effective_execution_mode": "SHADOW" if not settings.ai_autonomous_enabled else agent.execution_mode,
        },
    )
    db.commit()
    flash(
        request,
        f"Fila {queue.name} vinculada ao {agent.name}. Em Shadow Mode o atendimento humano continua funcionando normalmente.",
    )
    return RedirectResponse("/ai", status_code=303)


@router.post("/queues/{queue_id}/toggle")
def toggle_queue_binding(
    queue_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
):
    actor, redirect = _actor(request, db)
    if redirect:
        return redirect
    validate_csrf(request, csrf_token)
    binding = db.scalar(
        select(AIQueueBinding).where(AIQueueBinding.queue_id == queue_id)
    )
    if not binding:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    binding.active = not binding.active
    write_audit(
        db,
        user=actor,
        action="AI_QUEUE_ACTIVATED" if binding.active else "AI_QUEUE_DEACTIVATED",
        entity_type="service_queue",
        entity_id=queue_id,
        payload={"agent_id": binding.agent_id},
    )
    db.commit()
    return RedirectResponse("/ai", status_code=303)


@router.post("/test-chat/inbound")
async def ai_test_chat_inbound(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Teste de ponta a ponta sem WhatsApp Web: a mensagem entra como se o
    telefone de teste tivesse escrito no WhatsApp da fila (mesmo caminho do
    webhook da Z-API). As respostas da IA saem pelo WhatsApp real para esse
    telefone. Só administradores e só telefones de AI_TEST_PHONES.
    """
    actor, redirect = _actor(request, db)
    if redirect:
        return JSONResponse({"error": "Acesso restrito."}, status_code=403)
    payload = await request.json()
    validate_csrf(request, str(payload.get("csrf_token") or ""))
    require_roles(actor, "ADMIN")

    from uuid import uuid4

    from app.routers.zapi import _process_zapi_payload
    from app.services.ai_sdr import is_ai_test_phone

    phone = "".join(ch for ch in str(payload.get("phone") or "") if ch.isdigit())
    text = str(payload.get("text") or "").strip()[:2000]
    if not text:
        return JSONResponse({"error": "Mensagem vazia."}, status_code=400)
    if not is_ai_test_phone(phone):
        return JSONResponse(
            {"error": "Telefone fora de AI_TEST_PHONES: o teste só pode falar como um telefone de teste."},
            status_code=400,
        )
    try:
        queue_id = int(payload.get("queue_id") or 0)
    except (TypeError, ValueError):
        queue_id = 0
    instance = queue_instance(db, queue_id) if queue_id else None
    if not instance:
        return JSONResponse({"error": "Fila sem instância de WhatsApp."}, status_code=400)

    message_id = f"TESTE-{uuid4().hex}"
    result = _process_zapi_payload(
        {
            "type": "ReceivedCallback",
            "instanceId": instance.instance_id,
            "phone": phone,
            "fromMe": False,
            "messageId": message_id,
            "momment": int(datetime.now(timezone.utc).timestamp() * 1000),
            "senderName": "Teste IA",
            "text": {"message": text},
        },
        db=db,
        background_tasks=background_tasks,
        expected_instance_id=instance.instance_id,
        queue_id=queue_id,
        zapi_instance_id=instance.id,
    )
    write_audit(
        db,
        user=actor,
        action="AI_TEST_CHAT_INBOUND",
        entity_type="conversation",
        entity_id=result.get("conversation_id") or "-",
        payload={"queue_id": queue_id, "message_id": message_id},
    )
    db.commit()
    return JSONResponse(result)
