"""Baseline do schema existente do Sulnet Prospecta.

Esta primeira revisão é deliberadamente idempotente: em produção ela reconhece
o banco existente e cria somente tabelas que eventualmente estiverem ausentes.
A partir da próxima revisão, alterações de schema devem ser declaradas
explicitamente com operações Alembic.
"""

from alembic import op

from app.database import Base
import app.models  # noqa: F401


revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    # Baseline de adoção: nunca derrubar o schema existente automaticamente.
    pass
