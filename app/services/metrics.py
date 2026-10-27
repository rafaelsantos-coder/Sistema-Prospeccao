from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.constants import DEFAULT_TARGETS, PIPELINE_STAGES, STAGES, TEAMS
from app.models import Activity, Lead, Target, User
from app.timeutils import local_day_start_utc, utcnow


def _day_start() -> datetime:
    return local_day_start_utc()


def apply_user_scope(stmt, user: User):
    if user.role in {"ADMIN", "GESTOR"}:
        return stmt
    if user.role == "SUPERVISOR":
        return stmt.where(Lead.branch_id == user.branch_id)
    return stmt.where(Lead.assigned_user_id == user.id)


def dashboard_metrics(db: Session, user: User) -> dict:
    today = _day_start()
    now = utcnow()
    month_start = today.replace(day=1)

    total_stmt = apply_user_scope(select(func.count(Lead.id)), user)
    total_leads = db.scalar(total_stmt) or 0

    new_stmt = apply_user_scope(select(func.count(Lead.id)).where(Lead.created_at >= today), user)
    new_today = db.scalar(new_stmt) or 0

    overdue_stmt = apply_user_scope(
        select(func.count(Lead.id)).where(
            Lead.next_action_at.is_not(None),
            Lead.next_action_at < now,
            Lead.stage.not_in(["GANHO", "PERDIDO", "NAO_CONTATAR"]),
        ),
        user,
    )
    overdue = db.scalar(overdue_stmt) or 0

    won_stmt = apply_user_scope(
        select(func.count(Lead.id)).where(Lead.stage == "GANHO", Lead.updated_at >= month_start), user
    )
    won_month = db.scalar(won_stmt) or 0

    mrr_stmt = apply_user_scope(
        select(func.coalesce(func.sum(Lead.estimated_mrr), 0)).where(
            Lead.stage == "GANHO", Lead.updated_at >= month_start
        ),
        user,
    )
    won_mrr = float(db.scalar(mrr_stmt) or 0)

    activities_stmt = select(func.count(Activity.id)).where(
        Activity.status == "CONCLUIDA", Activity.completed_at >= today
    )
    if user.role == "CONSULTOR":
        activities_stmt = activities_stmt.where(Activity.user_id == user.id)
    elif user.role == "SUPERVISOR":
        activities_stmt = activities_stmt.join(Lead).where(Lead.branch_id == user.branch_id)
    activities_today = db.scalar(activities_stmt) or 0

    denominator = max(total_leads, 1)
    conversion = round((won_month / denominator) * 100, 1)

    pipeline = []
    for stage in PIPELINE_STAGES:
        stmt = apply_user_scope(select(func.count(Lead.id)).where(Lead.stage == stage), user)
        count = db.scalar(stmt) or 0
        if count or stage in {"NOVO", "QUALIFICADO", "PROPOSTA", "NEGOCIACAO", "GANHO"}:
            pipeline.append({"key": stage, "label": STAGES[stage], "count": count})

    paid_stmt = apply_user_scope(select(func.count(Lead.id)).where(Lead.is_paid_traffic.is_(True)), user)
    paid_traffic = db.scalar(paid_stmt) or 0

    return {
        "total_leads": total_leads,
        "new_today": new_today,
        "activities_today": activities_today,
        "overdue": overdue,
        "won_month": won_month,
        "won_mrr": won_mrr,
        "conversion": conversion,
        "paid_traffic": paid_traffic,
        "pipeline": pipeline,
        "targets": target_progress(db, user, today),
    }


def target_progress(db: Session, user: User, today: datetime) -> list[dict]:
    targets = list(db.scalars(select(Target).where(Target.active.is_(True)).order_by(Target.team)))
    if not targets:
        targets = [Target(team=t, metric=m, daily_target=v) for t, m, v in DEFAULT_TARGETS]

    rows: list[dict] = []
    for target in targets:
        if user.role == "CONSULTOR" and target.team != user.team:
            continue
        if target.metric == "ATIVIDADES":
            stmt = (
                select(func.count(Activity.id))
                .join(Lead)
                .where(
                    Activity.status == "CONCLUIDA",
                    Activity.completed_at >= today,
                    Lead.team == target.team,
                )
            )
            if user.role == "CONSULTOR":
                stmt = stmt.where(Activity.user_id == user.id)
            elif user.role == "SUPERVISOR":
                stmt = stmt.where(Lead.branch_id == user.branch_id)
        elif target.metric == "VISITAS":
            stmt = (
                select(func.count(Activity.id))
                .join(Lead)
                .where(
                    Activity.status == "CONCLUIDA",
                    Activity.kind == "VISITA",
                    Activity.completed_at >= today,
                    Lead.team == target.team,
                )
            )
            if user.role == "CONSULTOR":
                stmt = stmt.where(Activity.user_id == user.id)
            elif user.role == "SUPERVISOR":
                stmt = stmt.where(Lead.branch_id == user.branch_id)
        else:
            stmt = select(func.count(Lead.id)).where(
                Lead.created_at >= today, Lead.team == target.team
            )
            stmt = apply_user_scope(stmt, user)

        done = db.scalar(stmt) or 0
        goal = target.daily_target
        rows.append(
            {
                "team": target.team,
                "team_label": TEAMS.get(target.team, target.team),
                "metric": target.metric,
                "done": done,
                "goal": goal,
                "percent": min(round(done / goal * 100) if goal else 0, 100),
            }
        )
    return rows


def recent_activities(db: Session, user: User, limit: int = 8) -> list[Activity]:
    stmt = select(Activity).join(Lead).order_by(Activity.created_at.desc()).limit(limit)
    if user.role == "CONSULTOR":
        stmt = stmt.where(Activity.user_id == user.id)
    elif user.role == "SUPERVISOR":
        stmt = stmt.where(Lead.branch_id == user.branch_id)
    return list(db.scalars(stmt))


def urgent_leads(db: Session, user: User, limit: int = 8) -> list[Lead]:
    now = utcnow()
    stmt = (
        select(Lead)
        .where(
            Lead.stage.not_in(["GANHO", "PERDIDO", "NAO_CONTATAR"]),
            or_(Lead.next_action_at < now, Lead.priority == "ALTA"),
        )
        .order_by(Lead.next_action_at.asc(), Lead.score.desc())
        .limit(limit)
    )
    stmt = apply_user_scope(stmt, user)
    return list(db.scalars(stmt))
