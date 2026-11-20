"""Statistics for GET /api/v1/charts: snapshot tables, a reader flag on callers, and an
index on messages.created_at for the snapshot's refresh of recent days.

Expand-only: new tables, a defaulted column and an index; earlier versions keep working.
deploy/sql/grants.sql grants the runtime role the four new tables.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def _counts() -> list[sa.Column]:
    return [
        sa.Column(n, sa.Integer(), nullable=False)
        for n in ("open", "sent", "delivered", "failed", "unknown", "expired")
    ]


def upgrade() -> None:
    op.add_column(
        "callers",
        sa.Column("can_read_stats", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_messages_created_at", "messages", ["created_at"], unique=False)
    op.create_table(
        "stats_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("caller_id", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("accepted", sa.Integer(), nullable=False),
        *_counts(),
        sa.Column("send_hist", postgresql.ARRAY(sa.Integer()), nullable=False),
        sa.Column("deliver_hist", postgresql.ARRAY(sa.Integer()), nullable=False),
        sa.PrimaryKeyConstraint("day", "caller_id", "channel", "purpose", name="pk_stats_daily"),
    )
    op.create_table(
        "stats_failures",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("caller_id", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("failure_code", sa.Text(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "day", "caller_id", "channel", "purpose", "failure_code", name="pk_stats_failures"
        ),
    )
    op.create_table(
        "stats_campaigns",
        sa.Column("campaign", sa.Text(), nullable=False),
        sa.Column("caller_id", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("first_at", TZ, nullable=False),
        sa.Column("last_at", TZ, nullable=False),
        sa.Column("recipients", sa.Integer(), nullable=False),
        *_counts(),
        sa.PrimaryKeyConstraint("caller_id", "campaign", "purpose", name="pk_stats_campaigns"),
    )
    op.create_table(
        "stats_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("as_of", TZ, nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_stats_state_stats_state_single_row")),
    )


def downgrade() -> None:
    op.drop_table("stats_state")
    op.drop_table("stats_campaigns")
    op.drop_table("stats_failures")
    op.drop_table("stats_daily")
    op.drop_index("ix_messages_created_at", table_name="messages")
    op.drop_column("callers", "can_read_stats")
