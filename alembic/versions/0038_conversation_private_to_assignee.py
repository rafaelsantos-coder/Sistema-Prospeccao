"""Conversa particular: só o responsável enxerga o atendimento.

Revision ID: 0038_private_assignee
Revises: 0037_meta_whatsapp
"""
from alembic import op
import sqlalchemy as sa

revision = "0038_private_assignee"
down_revision = "0037_meta_whatsapp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    db = op.get_bind()
    columns = {c["name"] for c in sa.inspect(db).get_columns("conversations")}
    if "private_to_assignee" not in columns:
        op.add_column(
            "conversations",
            sa.Column(
                "private_to_assignee",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    indexes = {i["name"] for i in sa.inspect(db).get_indexes("conversations")}
    if "ix_conversations_private_to_assignee" not in indexes:
        op.create_index(
            "ix_conversations_private_to_assignee",
            "conversations",
            ["private_to_assignee"],
        )

    # Instâncias exclusivas já existentes: o histórico delas é particular.
    if "zapi_instances" in sa.inspect(db).get_table_names():
        zapi_cols = {c["name"] for c in sa.inspect(db).get_columns("zapi_instances")}
        if "exclusive_access" in zapi_cols:
            db.execute(
                sa.text(
                    """
                    UPDATE conversations
                    SET private_to_assignee = true
                    WHERE id IN (
                        SELECT czi.conversation_id
                        FROM conversation_zapi_instances czi
                        JOIN zapi_instances zi ON zi.id = czi.zapi_instance_id
                        WHERE zi.exclusive_access = true
                    )
                    """
                )
            )


def downgrade() -> None:
    op.drop_index("ix_conversations_private_to_assignee", table_name="conversations")
    op.drop_column("conversations", "private_to_assignee")
