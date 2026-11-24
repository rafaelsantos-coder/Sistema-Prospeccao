"""Link leads to the prospecting job that imported them.

Revision ID: 0035_lead_prospecting_job
Revises: 0034_private_zapi
"""
from alembic import op
import sqlalchemy as sa

revision = "0035_lead_prospecting_job"
down_revision = "0034_private_zapi"
branch_labels = None
depends_on = None


def upgrade() -> None:
    db = op.get_bind()
    columns = {c["name"] for c in sa.inspect(db).get_columns("leads")}
    if "prospecting_job_id" not in columns:
        op.add_column(
            "leads",
            sa.Column("prospecting_job_id", sa.Integer(), nullable=True),
        )
    indexes = {i["name"] for i in sa.inspect(db).get_indexes("leads")}
    if "ix_leads_prospecting_job_id" not in indexes:
        op.create_index("ix_leads_prospecting_job_id", "leads", ["prospecting_job_id"])
    keys = sa.inspect(db).get_foreign_keys("leads")
    if not any("prospecting_job_id" in fk.get("constrained_columns", []) for fk in keys):
        with op.batch_alter_table("leads") as batch:
            batch.create_foreign_key(
                "fk_leads_prospecting_job_id",
                "prospecting_jobs",
                ["prospecting_job_id"],
                ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    db = op.get_bind()
    keys = sa.inspect(db).get_foreign_keys("leads")
    for fk in keys:
        if "prospecting_job_id" in fk.get("constrained_columns", []):
            with op.batch_alter_table("leads") as batch:
                batch.drop_constraint(fk["name"], type_="foreignkey")
            break
    indexes = {i["name"] for i in sa.inspect(db).get_indexes("leads")}
    if "ix_leads_prospecting_job_id" in indexes:
        op.drop_index("ix_leads_prospecting_job_id", table_name="leads")
    columns = {c["name"] for c in sa.inspect(db).get_columns("leads")}
    if "prospecting_job_id" in columns:
        op.drop_column("leads", "prospecting_job_id")
