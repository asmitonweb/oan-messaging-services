"""SMS-03 broadcast ageing: claim non-OTP messages by age once they have waited.

The worker's aged pass reads accepted non-OTP messages oldest first; this partial index
keeps that pass index-driven when a large broadcast is queued.
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_messages_claimable_by_age",
        "messages",
        ["created_at", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'accepted' AND priority > 0"),
    )


def downgrade() -> None:
    op.drop_index("ix_messages_claimable_by_age", table_name="messages")
