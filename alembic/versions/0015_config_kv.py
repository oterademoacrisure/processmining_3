"""SOURCE-CONFIG: config_kv (key/value entries from the Source Config page)

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-06

SOURCE-CONFIG: one row per (tenant, key), written via core-service
/api/v1/source-configs. Only key, value and timestamps — kept separate from
the other API's source_config table. Key is immutable; only value is edited.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "config_kv",
        sa.Column("config_kv_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "tenant_id", sa.Integer(),
            sa.ForeignKey("tenant.tenant_id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.String(length=2048), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("tenant_id", "key", name="uq_config_kv_tenant_key"),
    )


def downgrade() -> None:
    op.drop_table("config_kv")
