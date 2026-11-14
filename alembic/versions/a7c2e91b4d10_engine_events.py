"""engine audit events

Revision ID: a7c2e91b4d10
Revises: f1458dd15d19
"""
from alembic import op
import sqlalchemy as sa

revision = "a7c2e91b4d10"
down_revision = "f1458dd15d19"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "engine_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("run", sa.Integer, index=True),
        sa.Column("at", sa.String),
        sa.Column("kind", sa.String, index=True),
        sa.Column("incident", sa.Integer, nullable=True),
        sa.Column("simulated", sa.Boolean),
        sa.Column("payload", sa.JSON),
        sa.Column("created_at", sa.DateTime),
    )


def downgrade():
    op.drop_table("engine_events")
