"""Persist the upper boundary of a bounded catch-up scan."""

import sqlalchemy as sa
from alembic import op

revision = "b19c24a0f001"
down_revision = "add7c228b9b9"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("channels", sa.Column("catchup_target", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("channels", "catchup_target")
