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
)
from app.security import hash_password
from app.services.followups import initial_next_action
from app.services.scoring import calculate_sulnet_score
from app.services.zapi_instances import seed_default_zapi_instance
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

    if not db.scalar(
        select(AIKnowledgeItem.id).where(AIKnowledgeItem.agent_id == ai_agent.id).limit(1)
    ):
        db.add_all(
            [
                AIKnowledgeItem(
                    agent_id=ai_agent.id,
                    title="Objetivo do SulIA SDR",
                    category="PROCESSO",
                    tags="sdr,qualificacao,handoff",
                    content=(
                        "O SulIA atua no topo do funil comercial. Deve entender a necessidade, "
                        "identificar dores e sinais de intenção, qualificar o prospect e recomendar "
                        "transferência para um vendedor humano quando houver intenção real de proposta, "
                        "contratação ou negociação."
                    ),
                    created_by_id=admin.id,
                ),
                AIKnowledgeItem(
                    agent_id=ai_agent.id,
                    title="Regras comerciais seguras",
                    category="REGRAS",
                    tags="preco,cobertura,promocao,seguranca",
                    content=(
                        "Nunca inventar preço, promoção, cobertura, prazo de instalação, desconto ou "
                        "condição comercial. Se a informação não estiver na base de conhecimento, informar "
                        "que um consultor da Sulnet confirmará. Pedido de desconto, proposta ou contratação "
                        "deve gerar recomendação de handoff."
                    ),
                    created_by_id=admin.id,
                ),
                AIKnowledgeItem(
                    agent_id=ai_agent.id,
                    title="Dados mínimos de qualificação",
                    category="QUALIFICACAO",
                    tags="provedor,velocidade,preco,dor,decisor,prazo",
                    content=(
                        "Quando fizer sentido na conversa, buscar de forma natural: provedor atual, "
                        "velocidade atual, valor aproximado, principais dores, uso da conexão, produto de "
                        "interesse, se a pessoa participa da decisão e prazo para mudança. Fazer uma pergunta "
                        "por vez e não transformar a conversa em interrogatório."
                    ),
                    created_by_id=admin.id,
                ),
            ]
        )

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

    seed_default_zapi_instance(db, created_by_id=admin.id)
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
