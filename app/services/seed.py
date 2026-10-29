from __future__ import annotations

from datetime import timedelta
import secrets
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.constants import DEFAULT_TARGETS
from app.models import (
    Activity,
    AIAgentProfile,
    AIKnowledgeItem,
    AIQueueBinding,
    Branch,
    Campaign,
    Lead,
    LeadQueuePreference,
    ServiceQueue,
    Target,
    User,
    ZApiInstance,
)
from app.security import hash_password
from app.services.ai_sdr_policy import SYSTEM_KNOWLEDGE_ITEMS, SULIA_SYSTEM_PROMPT
from app.services.followups import initial_next_action
from app.services.scoring import calculate_sulnet_score
from app.services.zapi_instances import bind_queue_instance, seed_default_zapi_instance
from app.timeutils import utcnow


def _release_unclaimed_queue_prospecting_leads(db: Session) -> int:
    activity_exists = (
        select(Activity.id)
        .where(Activity.lead_id == Lead.id)
        .exists()
    )
    leads = list(
        db.scalars(
            select(Lead)
            .join(
                LeadQueuePreference,
                LeadQueuePreference.lead_id == Lead.id,
            )
            .where(
                LeadQueuePreference.source == "PROSPECTING",
                Lead.source == "APIFY",
                Lead.stage == "NOVO",
                Lead.assigned_user_id.is_not(None),
                Lead.created_by_id == Lead.assigned_user_id,
                ~activity_exists,
            )
        )
    )
    for lead in leads:
        lead.assigned_user_id = None
    if leads:
        db.flush()
    return len(leads)


BRANCHES = [
    ("SRO", "Sulnet Santa Rosa", "Santa Rosa"),
    ("SAN", "Sulnet Santo Ângelo", "Santo Ângelo"),
    ("TMA", "Sulnet Três de Maio", "Três de Maio"),
    ("HOR", "Sulnet Horizontina", "Horizontina"),
    ("CRI", "Sulnet Crissiumal", "Crissiumal"),
    ("BVB", "Sulnet Boa Vista do Buricá", "Boa Vista do Buricá"),
    ("CLA", "Sulnet Cerro Largo", "Cerro Largo"),
]


def _ensure_dedicated_ai_queue(db: Session, *, admin: User) -> ServiceQueue | None:
    """Materializa a fila operacional quando existe a instância Z-API dedicada 'Fila IA'."""
    instance = db.scalar(
        select(ZApiInstance)
        .where(
            ZApiInstance.name == "Fila IA",
            ZApiInstance.active.is_(True),
        )
        .order_by(ZApiInstance.id.asc())
        .limit(1)
    )
    if not instance:
        return None

    queue = db.scalar(
        select(ServiceQueue)
        .where(ServiceQueue.code == "FILA-IA")
        .limit(1)
    )
    if not queue:
        queue = db.scalar(
            select(ServiceQueue)
            .where(ServiceQueue.name == "Fila IA")
            .order_by(ServiceQueue.id.asc())
            .limit(1)
        )
    if not queue:
        queue = ServiceQueue(
            name="Fila IA",
            code="FILA-IA",
            team="DIGITAL",
            routing_strategy="MANUAL",
            is_default_inbound=False,
            active=True,
            created_by_id=admin.id,
        )
        db.add(queue)
        db.flush()

    bind_queue_instance(db, queue.id, instance.id)
    return queue


def seed_database(db: Session, include_demo: bool = True) -> None:
    branches: dict[str, Branch] = {}
    for code, name, city in BRANCHES:
        branch = db.scalar(select(Branch).where(Branch.code == code))
        if not branch:
            branch = Branch(code=code, name=name, city=city)
            db.add(branch)
            db.flush()
        branches[code] = branch

    admin = db.scalar(select(User).where(User.email == settings.bootstrap_admin_email.lower()))
    if not admin:
        admin = User(
            name=settings.bootstrap_admin_name,
            email=settings.bootstrap_admin_email.lower(),
            password_hash=hash_password(settings.bootstrap_admin_password),
            role="ADMIN",
            team="LOJA",
            branch_id=branches["SRO"].id,
            must_change_password=settings.is_production,
        )
        db.add(admin)
        db.flush()

    ai_user = db.scalar(select(User).where(User.email == "sulia.sdr@system.local"))
    if not ai_user:
        ai_user = User(
            name="SulIA SDR",
            email="sulia.sdr@system.local",
            password_hash=hash_password(secrets.token_urlsafe(48)),
            role="AI_SDR",
            team="DIGITAL",
            branch_id=None,
            active=True,
            must_change_password=False,
        )
        db.add(ai_user)
        db.flush()

    # Vendedor IA: identidade comercial própria, sem login interativo.
    # Nasce em Shadow e inativo até homologação das integrações de fechamento.
    seller_user = db.scalar(select(User).where(User.email == "sulia.vendedor@system.local"))
    if not seller_user:
        seller_user = User(
            name="Vendedor IA Sulnet",
            email="sulia.vendedor@system.local",
            password_hash=hash_password(secrets.token_urlsafe(48)),
            role="CONSULTOR",
            team="DIGITAL",
            branch_id=None,
            active=False,
            must_change_password=False,
        )
        db.add(seller_user)
        db.flush()
    seller_agent = db.scalar(
        select(AIAgentProfile).where(AIAgentProfile.user_id == seller_user.id)
    )
    if not seller_agent:
        seller_agent = AIAgentProfile(
            user_id=seller_user.id,
            name="SulIA Vendedor de Fechamento",
            slug="sulia-vendedor-fechamento",
            role_type="CLOSER",
            execution_mode="SHADOW",
            model="gpt-6-luna",
            reasoning_effort="low",
            active=False,
            public_name="Sulnet",
            qualification_handoff_score=80,
            max_history_messages=80,
            max_knowledge_chars=20000,
            business_goal="Receber aceite do SDR e concluir cadastro, crédito, endereço, plano, faturamento e contrato.",
            system_prompt=(
                "Você é o Vendedor IA da Sulnet, especializado em fechamento. "
                "Receba o histórico completo do SDR e não repita dados conhecidos. "
                "Peça somente dados faltantes: nome, telefone, CPF, CEP, número e complemento, "
                "confirmação do plano, e-mail e faturamento. "
                "Valide crédito/CPF apenas pelo retorno real do CRM; consulte CEP pela integração oficial. "
                "Salve cada etapa na oportunidade e preserve o protocolo. "
                "Não exija caixa NAP/ANATEL, coordenadas, técnico, data de ativação, "
                "selfie ou fotografias de documentos no funil SDR IA. "
                "Nunca declare crédito aprovado, contrato assinado ou venda concluída "
                "sem confirmação da API. Prazo de até 48 horas somente quando autorizado. "
                "Escale exceções e nunca altere preço fora da política."
            ),
            created_by_id=admin.id,
        )
        db.add(seller_agent)
        db.flush()

    ai_agent = db.scalar(
        select(AIAgentProfile).where(AIAgentProfile.user_id == ai_user.id)
    )
    if not ai_agent:
        ai_agent = AIAgentProfile(
            user_id=ai_user.id,
            name="SulIA SDR Digital",
            slug="sulia-sdr-digital",
            role_type="SDR",
            execution_mode="SHADOW",
            model="gpt-6-luna",
            reasoning_effort="low",
            active=True,
            public_name="Sulnet",
            qualification_handoff_score=70,
            max_history_messages=24,
            max_knowledge_chars=12000,
            created_by_id=admin.id,
        )
        db.add(ai_agent)
        db.flush()

    if (
        not ai_agent.system_prompt
        or "OBJETIVO" not in ai_agent.system_prompt
        or "BOTÕES E RESPOSTAS RÁPIDAS" not in ai_agent.system_prompt
    ):
        ai_agent.system_prompt = SULIA_SYSTEM_PROMPT
    ai_agent.business_goal = (
        "Gerar vendas de internet: diagnosticar, ofertar do maior plano elegível para o menor, "
        "tratar objeções, negociar até 3 meses gratuitos em FTTH com fidelidade, manter follow-up "
        "contínuo e transferir para vendedor da mesma fila quando houver intenção clara de contratação."
    )
    ai_agent.qualification_handoff_score = 80
    ai_agent.max_history_messages = max(int(ai_agent.max_history_messages or 24), 40)
    ai_agent.max_knowledge_chars = max(int(ai_agent.max_knowledge_chars or 12000), 20000)

    for knowledge_data in SYSTEM_KNOWLEDGE_ITEMS:
        item = db.scalar(
            select(AIKnowledgeItem).where(
                AIKnowledgeItem.agent_id == ai_agent.id,
                AIKnowledgeItem.title == knowledge_data["title"],
            )
        )
        if not item:
            item = AIKnowledgeItem(
                agent_id=ai_agent.id,
                title=knowledge_data["title"],
                category=knowledge_data["category"],
                tags=knowledge_data["tags"],
                content=knowledge_data["content"],
                source_type="SULNET_POLICY_V1",
                active=True,
                created_by_id=admin.id,
            )
            db.add(item)
        else:
            item.category = knowledge_data["category"]
            item.tags = knowledge_data["tags"]
            item.content = knowledge_data["content"]
            item.source_type = "SULNET_POLICY_V1"
            item.active = True

    seed_default_zapi_instance(db, created_by_id=admin.id)
    dedicated_ai_queue = _ensure_dedicated_ai_queue(db, admin=admin)

    digital_queues = list(
        db.scalars(
            select(ServiceQueue)
            .where(
                ServiceQueue.team == "DIGITAL",
                ServiceQueue.active.is_(True),
            )
            .order_by(ServiceQueue.id.asc())
        )
    )
    if (
        dedicated_ai_queue
        and dedicated_ai_queue.active
        and all(item.id != dedicated_ai_queue.id for item in digital_queues)
    ):
        digital_queues.append(dedicated_ai_queue)

    for digital_queue in digital_queues:
        if db.scalar(
            select(AIQueueBinding.id).where(
                AIQueueBinding.queue_id == digital_queue.id
            )
        ):
            continue
        db.add(
            AIQueueBinding(
                queue_id=digital_queue.id,
                agent_id=ai_agent.id,
                handling_mode="AI_FIRST",
                active=True,
                fallback_to_human=True,
            )
        )

    _release_unclaimed_queue_prospecting_leads(db)

    for team, metric, goal in DEFAULT_TARGETS:
        exists = db.scalar(
            select(Target).where(
                Target.team == team, Target.metric == metric, Target.branch_id.is_(None)
            )
        )
        if not exists:
            db.add(Target(team=team, metric=metric, daily_target=goal))

    db.commit()

    if include_demo and not db.scalar(select(Lead.id).limit(1)):
        _seed_demo_data(db, admin, branches)


def _seed_demo_data(db: Session, admin: User, branches: dict[str, Branch]) -> None:
    now = utcnow()
    demo = [
        {
            "business_name": "Clínica Horizonte",
            "contact_name": "Mariana",
            "phone": "5555999991001",
            "email": "contato@clinicahorizonte.com.br",
            "segment": "Clínica odontológica",
            "city": "Santa Rosa",
            "source": "APIFY",
            "origin_channel": "WHATSAPP",
            "team": "B2B",
            "stage": "QUALIFICADO",
            "estimated_mrr": Decimal("349.90"),
            "branch": "SRO",
            "reviews_count": 82,
            "website": "https://example.com",
            "next_action_at": now - timedelta(hours=2),
        },
        {
            "business_name": "Mercado Bom Vizinho",
            "contact_name": "Paulo",
            "phone": "5555988882002",
            "segment": "Mercado",
            "city": "Três de Maio",
            "source": "MANUAL",
            "origin_channel": "VISITA",
            "team": "PAP",
            "stage": "CONTATO_REALIZADO",
            "estimated_mrr": Decimal("199.90"),
            "branch": "TMA",
            "reviews_count": 34,
            "next_action_at": now + timedelta(hours=3),
        },
        {
            "business_name": "Agro Forte Máquinas",
            "contact_name": "Roberto",
            "phone": "5555977773003",
            "email": "compras@agroforte.com.br",
            "segment": "Agro e máquinas",
            "city": "Horizontina",
            "source": "INDICACAO",
            "origin_channel": "TELEFONE",
            "team": "B2B",
            "stage": "PROPOSTA",
            "estimated_mrr": Decimal("899.00"),
            "branch": "HOR",
            "reviews_count": 119,
            "website": "https://example.com",
            "next_action_at": now + timedelta(days=1),
            "priority": "ALTA",
        },
        {
            "business_name": "Loja Estação Casa",
            "contact_name": "Fernanda",
            "phone": "5555966664004",
            "segment": "Loja de móveis",
            "city": "Santa Rosa",
            "source": "TRAFEGO_PAGO",
            "origin_channel": "INSTAGRAM",
            "team": "DIGITAL",
            "stage": "NOVO",
            "estimated_mrr": Decimal("119.90"),
            "branch": "SRO",
            "reviews_count": 12,
            "intent_signal": True,
            "is_paid_traffic": True,
            "next_action_at": now - timedelta(minutes=40),
        },
        {
            "business_name": "Contabilidade União",
            "contact_name": "Carla",
            "phone": "5555955555005",
            "email": "carla@contabilidadeuniao.com.br",
            "segment": "Escritório de contabilidade",
            "city": "Santo Ângelo",
            "source": "APIFY",
            "origin_channel": "EMAIL",
            "team": "B2B",
            "stage": "NEGOCIACAO",
            "estimated_mrr": Decimal("549.90"),
            "branch": "SAN",
            "reviews_count": 55,
            "website": "https://example.com",
            "next_action_at": now + timedelta(days=2),
        },
        {
            "business_name": "Academia Movimento",
            "contact_name": "Diego",
            "phone": "5555944446006",
            "segment": "Academia",
            "city": "Crissiumal",
            "source": "BASE_ATIVA",
            "origin_channel": "WHATSAPP",
            "team": "LOJA",
            "stage": "GANHO",
            "estimated_mrr": Decimal("169.90"),
            "branch": "CRI",
            "reviews_count": 101,
            "website": "https://example.com",
            "next_action_at": None,
        },
    ]

    leads: list[Lead] = []
    for item in demo:
        score_result = calculate_sulnet_score(item)
        branch_code = item.pop("branch")
        reviews_count = item.pop("reviews_count", 0)
        item.pop("intent_signal", None)
        lead = Lead(
            **item,
            score=score_result.score,
            temperature=score_result.temperature,
            potential=", ".join(score_result.reasons),
            branch_id=branches[branch_code].id,
            assigned_user_id=admin.id,
            created_by_id=admin.id,
        )
        db.add(lead)
        db.flush()
        leads.append(lead)

    completed = [
        (leads[0], "LIGACAO", "Contato realizado; pediu retorno com proposta."),
        (leads[1], "VISITA", "Visita inicial concluída e necessidade identificada."),
        (leads[2], "PROPOSTA", "Proposta de link dedicado enviada."),
        (leads[3], "WHATSAPP", "Lead respondeu ao anúncio e solicitou condições."),
    ]
    for lead, kind, description in completed:
        db.add(
            Activity(
                lead_id=lead.id,
                user_id=admin.id,
                kind=kind,
                status="CONCLUIDA",
                channel=kind,
                description=description,
                completed_at=now - timedelta(minutes=30),
            )
        )

    db.add(
        Campaign(
            name="Empresas de Santa Rosa — Fibra + Voz",
            segment="Empresas locais",
            team="B2B",
            channel="WHATSAPP",
            status="RASCUNHO",
            daily_limit=30,
            branch_id=branches["SRO"].id,
            created_by_id=admin.id,
            sequence_json='[{"day":1,"message":"Abordagem inicial"},{"day":3,"message":"Follow-up"}]',
        )
    )
    db.commit()
