"""Exclusive per-instance conversation access; bind Maria's known Z-API instance safely.

Revision ID: 0034_private_zapi
Revises: 0033_geral_comercial_sulia
"""
from alembic import op
import sqlalchemy as sa

revision = "0034_private_zapi"
down_revision = "0033_geral_comercial_sulia"
branch_labels = None
depends_on = None


def upgrade() -> None:
    db = op.get_bind()
    # Tests may create the current ORM schema before applying Alembic.
    # Production databases normally have neither column yet.
    columns = {c["name"] for c in sa.inspect(db).get_columns("zapi_instances")}
    if "exclusive_access" not in columns:
        op.add_column("zapi_instances", sa.Column("exclusive_access", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "exclusive_user_id" not in columns:
        op.add_column("zapi_instances", sa.Column("exclusive_user_id", sa.Integer(), nullable=True))
    indexes = {i["name"] for i in sa.inspect(db).get_indexes("zapi_instances")}
    if "ix_zapi_instances_exclusive_access" not in indexes:
        op.create_index("ix_zapi_instances_exclusive_access", "zapi_instances", ["exclusive_access"])
    if "ix_zapi_instances_exclusive_user_id" not in indexes:
        op.create_index("ix_zapi_instances_exclusive_user_id", "zapi_instances", ["exclusive_user_id"])
    keys = sa.inspect(db).get_foreign_keys("zapi_instances")
    if not any("exclusive_user_id" in fk.get("constrained_columns", []) for fk in keys):
        with op.batch_alter_table("zapi_instances") as batch:
            batch.create_foreign_key("fk_zapi_instances_exclusive_user_id", "users", ["exclusive_user_id"], ["id"])

    # The known instance is identified by its *full* provider ID using its
    # screenshot suffix AND its registered number/name. Never assign a guessed user.
    matches = db.execute(sa.text(
        "SELECT id, name, phone_label, instance_id FROM zapi_instances "
        "WHERE UPPER(instance_id) LIKE '%A50443D0' "
        "AND regexp_replace(COALESCE(phone_label, ''), '[^0-9]', '', 'g') LIKE '%55999000524'"
    )).mappings().all() if db.dialect.name == "postgresql" else []
    # Legacy instances may have no phone label; require name and ID instead.
    if not matches:
        matches = db.execute(sa.text(
            "SELECT id, name, phone_label, instance_id FROM zapi_instances "
            "WHERE UPPER(instance_id) LIKE '%A50443D0' "
            "AND lower(name) LIKE '%maria gabriela%'"
        )).mappings().all()
    if not matches:
        return
    if len(matches) != 1:
        raise RuntimeError("Ambiguous Maria Z-API instances; exclusive access cannot be safely configured")

    # Turn on the privacy flag first. When the user is missing/ambiguous it
    # fails closed rather than exposing private conversations to everyone.
    instance_db_id = matches[0]["id"]
    db.execute(sa.text(
        "UPDATE zapi_instances SET exclusive_access = true, exclusive_user_id = NULL "
        "WHERE id = :instance_id"
    ), {"instance_id": instance_db_id})
    users = db.execute(sa.text(
        "SELECT id FROM users WHERE active = true AND "
        "(lower(trim(name)) = 'maria gabriela' OR lower(trim(name)) = 'maria gabriela - sulnet')"
    )).mappings().all()
    if len(users) == 1:
        owner = int(users[0]["id"])
        db.execute(sa.text(
            "UPDATE zapi_instances SET exclusive_user_id = :user_id WHERE id = :instance_id"
        ), {"user_id": owner, "instance_id": instance_db_id})
        # Existing threads must be available to Maria immediately, regardless
        # of the previously assigned consultant or queue. Preserve history.
        db.execute(sa.text(
            "UPDATE conversations SET assigned_user_id = :user_id "
            "WHERE id IN (SELECT conversation_id FROM conversation_zapi_instances "
            "WHERE zapi_instance_id = :instance_id)"
        ), {"user_id": owner, "instance_id": instance_db_id})


def downgrade() -> None:
    with op.batch_alter_table("zapi_instances") as batch:
        batch.drop_constraint("fk_zapi_instances_exclusive_user_id", type_="foreignkey")
    op.drop_index("ix_zapi_instances_exclusive_user_id", table_name="zapi_instances")
    op.drop_index("ix_zapi_instances_exclusive_access", table_name="zapi_instances")
    op.drop_column("zapi_instances", "exclusive_user_id")
    op.drop_column("zapi_instances", "exclusive_access")
