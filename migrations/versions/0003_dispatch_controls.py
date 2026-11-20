"""Shared provider incident controls; no public status additions."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dispatch_controls",
        sa.Column("channel", sa.Text(), primary_key=True),
        sa.Column("suspended", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("ambiguous_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cooldown_until", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "channel IN ('sms', 'telegram')", name=op.f("ck_dispatch_controls_control_channel")
        ),
    )


def downgrade() -> None:
    op.drop_table("dispatch_controls")
