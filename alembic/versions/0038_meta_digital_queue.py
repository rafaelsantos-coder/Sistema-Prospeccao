"""Vinculo exclusivo do numero oficial Sulnet a fila Digital."""
from alembic import op
import sqlalchemy as sa
import logging
import re

revision = "0038_meta_digital_queue"
down_revision = "0037_meta_whatsapp"
branch_labels = None
depends_on = None
LOG = logging.getLogger("alembic.runtime.migration")


def upgrade():
    db = op.get_bind()
    md = sa.MetaData()
    def table(name):
        return sa.Table(name, md, autoload_with=db)
    accounts = table("meta_whatsapp_accounts")
    queues = table("service_queues")
    links = table("service_queue_meta_accounts")
    zapi = table("service_queue_zapi_instances")
    target = [a for a in db.execute(sa.select(accounts).where(accounts.c.active.is_(True))).mappings()
              if re.sub(r"\D", "", a["phone_label"] or "") in {"555535118003", "5535118003"}
              or a["name"].strip().casefold() == "sulnet - meta oficial 01"]
    digital = [q for q in db.execute(sa.select(queues).where(queues.c.active.is_(True), queues.c.team == "DIGITAL")).mappings()
               if q["name"].strip().casefold() in {"digital", "fila digital"}
               or q["code"].strip().upper() in {"DIGITAL", "FILA-DIGITAL", "FILA_DIGITAL"}]
    if len(target) != 1 or len(digital) != 1:
        LOG.warning("meta_digital_binding_blocked accounts=%s digital_candidates=%s", len(target), [(q["id"], q["name"]) for q in digital])
        return
    aid, qid = target[0]["id"], digital[0]["id"]
    occupant = db.execute(sa.select(links.c.meta_account_id).where(links.c.queue_id == qid)).scalar()
    if occupant is not None and occupant != aid:
        LOG.warning("meta_digital_binding_blocked reason=other_meta_account")
        return
    db.execute(links.delete().where(links.c.meta_account_id == aid, links.c.queue_id != qid))
    db.execute(zapi.delete().where(zapi.c.queue_id == qid))
    if occupant is None:
        db.execute(links.insert().values(queue_id=qid, meta_account_id=aid, created_at=sa.func.now(), updated_at=sa.func.now()))
    # Repair only open, automatically routed conversations of this Meta account.
    conv = table("conversations")
    cl = table("conversation_queue_links")
    cm = table("conversation_meta_accounts")
    members = table("service_queue_members")
    ids = list(db.execute(sa.select(conv.c.id).join(cm, cm.c.conversation_id == conv.c.id)
        .join(cl, cl.c.conversation_id == conv.c.id)
        .where(cm.c.meta_account_id == aid, cl.c.queue_id != qid,
               cl.c.origin.in_(["INBOUND", "META_INSTANCE", "META_ACCOUNT"]),
               conv.c.status.in_(["NOVO", "EM_FILA", "ATRIBUIDO", "EM_ATENDIMENTO", "AGUARDANDO_CLIENTE", "AGUARDANDO_INTERNO", "FOLLOW_UP"]))).scalars())
    if ids:
        db.execute(cl.update().where(cl.c.conversation_id.in_(ids)).values(queue_id=qid, origin="META_INSTANCE", updated_at=sa.func.now()))
        db.execute(conv.update().where(conv.c.id.in_(ids)).values(team="DIGITAL", updated_at=sa.func.now()))
        db.execute(conv.update().where(conv.c.id.in_(ids), conv.c.assigned_user_id.is_not(None),
            ~conv.c.assigned_user_id.in_(sa.select(members.c.user_id).where(members.c.queue_id == qid, members.c.active.is_(True))))
            .values(assigned_user_id=None, status="EM_FILA"))
    LOG.info("meta_digital_binding account_id=%s queue_id=%s queue_name=%s repaired=%s", aid, qid, digital[0]["name"], len(ids))
    duplicate = db.execute(sa.select(links.c.meta_account_id).group_by(links.c.meta_account_id).having(sa.func.count() > 1).limit(1)).first()
    if not duplicate:
        op.create_index("uq_service_queue_meta_account", links.name, ["meta_account_id"], unique=True)
    else:
        LOG.warning("meta_unique_index_skipped legacy duplicate; app blocks duplicate routes")


def downgrade():
    if any(i["name"] == "uq_service_queue_meta_account" for i in sa.inspect(op.get_bind()).get_indexes("service_queue_meta_accounts")):
        op.drop_index("uq_service_queue_meta_account", table_name="service_queue_meta_accounts")
