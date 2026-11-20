"""Index the shared rate-window scan; additive migration, initial snapshot unchanged."""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_message_attempts_started_at", "message_attempts", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_message_attempts_started_at", table_name="message_attempts")
