"""SQLAlchemy Core schema. ``migrations/versions/0001_initial.py`` creates exactly this
(``alembic check`` verifies parity); the status-transition trigger lives in the migration.

Invariants from contract 3.0.0 are enforced as CHECK constraints so that no code path —
including a buggy one — can persist a live binding without a fingerprint, release an
outstanding binding, keep content while a send is impossible to need it, and so on.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from oan_messaging.contracts import (
    COMPLETED_BINDING_STATUSES,
    CONTENT_REQUIRED_STATUSES,
    Channel,
    FailureCode,
    MessageStatus,
    Provider,
    Purpose,
    ReceiptDisposition,
)

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = MetaData(naming_convention=NAMING)


def _in(column: str, values: object) -> str:
    items = sorted(v.value for v in values)  # type: ignore[attr-defined]
    return f"{column} IN ({', '.join(repr(v) for v in items)})"


TZ = DateTime(timezone=True)
_FP = "^[0-9a-f]{64}$"

callers = Table(
    "callers",
    metadata,
    Column("caller_id", Text, primary_key=True),
    Column("allowed_channels", ARRAY(Text), nullable=False),
    Column("allowed_purposes", ARRAY(Text), nullable=False),
    Column("telegram_bot", Text, nullable=True),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("created_at", TZ, nullable=False, server_default=text("now()")),
    #: A statistics reader (Reports Central, the OAN dashboards): may read /api/v1/charts.
    #: Readers are created with no channels and no purposes, so they can never send.
    Column("can_read_stats", Boolean, nullable=False, server_default=text("false")),
    CheckConstraint("char_length(caller_id) BETWEEN 1 AND 64", name="caller_id_length"),
)

api_credentials = Table(
    "api_credentials",
    metadata,
    Column("credential_id", UUID(as_uuid=True), primary_key=True),
    Column("caller_id", Text, ForeignKey("callers.caller_id"), nullable=False),
    #: HMAC-SHA256(pepper, token). The token itself is never stored.
    Column("token_hmac", LargeBinary, nullable=False, unique=True),
    Column("created_at", TZ, nullable=False),
    Column("revoked_at", TZ, nullable=True),
    CheckConstraint("octet_length(token_hmac) = 32", name="token_hmac_length"),
)

messages = Table(
    "messages",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("caller_id", Text, ForeignKey("callers.caller_id"), nullable=False),
    Column("channel", Text, nullable=False),
    Column("idempotency_key", Text, nullable=False),
    Column("purpose", Text, nullable=False),
    Column("priority", Integer, nullable=False),
    Column("source_reference", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("failure_code", Text, nullable=True),
    Column("provider_status", Text, nullable=True),
    Column("provider_message_id", Text, nullable=True),
    Column("payload_fp_version", Text, nullable=True),
    Column("payload_fp_digest", Text, nullable=True),
    Column("binding_released_at", TZ, nullable=True),
    Column("recipient_fp_version", Text, nullable=True),
    Column("recipient_fp_digest", Text, nullable=True),
    Column("recipient_masked", Text, nullable=False),
    #: Telegram only: the caller's bot key authorized at acceptance. Dispatch requires the
    #: caller to still map to this same bot; another bot is never substituted.
    Column("authorized_bot", Text, nullable=True),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("created_at", TZ, nullable=False),
    Column("updated_at", TZ, nullable=False),
    Column("expires_at", TZ, nullable=True),
    Column("sent_at", TZ, nullable=True),
    Column("delivered_at", TZ, nullable=True),
    Column("last_attempt_at", TZ, nullable=True),
    Column("not_before", TZ, nullable=True),
    Column("content_purged_at", TZ, nullable=True),
    Column("lease_token", UUID(as_uuid=True), nullable=True),
    Column("lease_owner", Text, nullable=True),
    Column("lease_expires_at", TZ, nullable=True),
    CheckConstraint(_in("channel", Channel), name="channel"),
    CheckConstraint(_in("purpose", Purpose), name="purpose"),
    CheckConstraint(_in("status", MessageStatus), name="status"),
    CheckConstraint(
        f"failure_code IS NULL OR {_in('failure_code', FailureCode)}", name="failure_code"
    ),
    CheckConstraint("attempts >= 0", name="attempts_non_negative"),
    CheckConstraint("(attempts > 0) = (last_attempt_at IS NOT NULL)", name="last_attempt"),
    CheckConstraint(
        "(payload_fp_version IS NULL) = (payload_fp_digest IS NULL)", name="payload_fp_pair"
    ),
    CheckConstraint(
        "(recipient_fp_version IS NULL) = (recipient_fp_digest IS NULL)", name="recipient_fp_pair"
    ),
    CheckConstraint(
        f"payload_fp_digest IS NULL OR payload_fp_digest ~ '{_FP}'", name="payload_fp_format"
    ),
    CheckConstraint(
        f"recipient_fp_digest IS NULL OR recipient_fp_digest ~ '{_FP}'", name="recipient_fp_format"
    ),
    # A live binding always has its fingerprint (never "new" by accident).
    CheckConstraint(
        "binding_released_at IS NOT NULL OR payload_fp_digest IS NOT NULL",
        name="live_binding_fingerprint",
    ),
    # Only completed messages may be released, and released rows keep no fingerprint.
    CheckConstraint(
        "binding_released_at IS NULL OR (payload_fp_digest IS NULL AND "
        f"{_in('status', COMPLETED_BINDING_STATUSES)})",
        name="released_binding",
    ),
    CheckConstraint(
        f"NOT ({_in('status', CONTENT_REQUIRED_STATUSES)}) OR content_purged_at IS NULL",
        name="content_while_sendable",
    ),
    CheckConstraint("status <> 'sent' OR sent_at IS NOT NULL", name="sent_at"),
    CheckConstraint("status <> 'delivered' OR delivered_at IS NOT NULL", name="delivered_at"),
    CheckConstraint(
        "status NOT IN ('failed', 'expired') OR failure_code IS NOT NULL", name="failure_reason"
    ),
    CheckConstraint("status <> 'sending' OR lease_token IS NOT NULL", name="sending_lease"),
    CheckConstraint("channel = 'sms' OR recipient_fp_digest IS NULL", name="recipient_fp_sms_only"),
    CheckConstraint(
        "channel <> 'sms' OR status NOT IN ('accepted', 'sending') "
        "OR recipient_fp_digest IS NOT NULL",
        name="recipient_fp_while_sendable",
    ),
    CheckConstraint(
        "channel <> 'telegram' OR status <> 'delivered'", name="telegram_never_delivered"
    ),
    CheckConstraint(
        "(channel = 'telegram') = (authorized_bot IS NOT NULL)", name="authorized_bot_telegram"
    ),
)

Index(
    "uq_messages_live_binding",
    messages.c.caller_id,
    messages.c.channel,
    messages.c.idempotency_key,
    unique=True,
    postgresql_where=text("binding_released_at IS NULL"),
)
Index(
    "ix_messages_claimable",
    messages.c.priority,
    messages.c.created_at,
    postgresql_where=text("status = 'accepted'"),
)
# SMS-03 broadcast ageing: the worker's aged pass reads accepted non-OTP messages by age.
Index(
    "ix_messages_claimable_by_age",
    messages.c.created_at,
    messages.c.id,
    postgresql_where=text("status = 'accepted' AND priority > 0"),
)
Index("ix_messages_caller_id_id", messages.c.caller_id, messages.c.id)
Index(
    "ix_messages_provider_message_id",
    messages.c.channel,
    messages.c.provider_message_id,
    postgresql_where=text("provider_message_id IS NOT NULL"),
)
Index(
    "ix_messages_sending_lease",
    messages.c.lease_expires_at,
    postgresql_where=text("status = 'sending'"),
)

message_content = Table(
    "message_content",
    metadata,
    Column(
        "message_id",
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("key_version", Text, nullable=False),
    Column("nonce", LargeBinary, nullable=False),
    Column("ciphertext", LargeBinary, nullable=False),
    Column("created_at", TZ, nullable=False),
    CheckConstraint("octet_length(nonce) = 12", name="nonce_length"),
)

message_attempts = Table(
    "message_attempts",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column(
        "message_id",
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("attempt_no", Integer, nullable=False),
    Column("claim_token", UUID(as_uuid=True), nullable=False),
    Column("worker_id", Text, nullable=False),
    Column("started_at", TZ, nullable=False),
    Column("finished_at", TZ, nullable=True),
    Column("outcome", Text, nullable=True),
    Column("provider_status", Text, nullable=True),
    Column("failure_code", Text, nullable=True),
    Column("provider_message_id", Text, nullable=True),
    Column("retryable", Boolean, nullable=True),
    Column("retry_after_seconds", Integer, nullable=True),
    Column("detail", Text, nullable=True),
    UniqueConstraint("message_id", "attempt_no"),
    CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
)

Index("ix_message_attempts_started_at", message_attempts.c.started_at)

receipts = Table(
    "receipts",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("provider", Text, nullable=False),
    Column("provider_message_id", Text, nullable=False),
    Column("recipient_fp_version", Text, nullable=False),
    Column("recipient_fp_digest", Text, nullable=False),
    Column("recipient_masked", Text, nullable=False),
    Column("raw_status", Text, nullable=False),
    Column("event_time", TZ, nullable=False),
    Column("received_at", TZ, nullable=False),
    Column("disposition", Text, nullable=False),
    Column(
        "message_id",
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    ),
    CheckConstraint(f"provider = '{Provider.SMS_8028.value}'", name="provider_8028_only"),
    CheckConstraint(_in("disposition", ReceiptDisposition), name="disposition"),
    CheckConstraint(f"recipient_fp_digest ~ '{_FP}'", name="recipient_fp_format"),
)
Index("ix_receipts_provider_message_id", receipts.c.provider, receipts.c.provider_message_id)
Index("ix_receipts_received_at", receipts.c.received_at)

retired_key_versions = Table(
    "retired_key_versions",
    metadata,
    Column("kind", Text, nullable=False),
    Column("key_version", Text, nullable=False),
    Column("retired_at", TZ, nullable=False),
    PrimaryKeyConstraint("kind", "key_version"),
    CheckConstraint("kind IN ('fingerprint', 'content')", name="kind"),
)

# Shared provider incident gate. Resume is an explicit operator action, never inferred
# from a different credential value or a late successful in-flight request.
dispatch_controls = Table(
    "dispatch_controls", metadata,
    Column("channel", Text, primary_key=True),
    Column("suspended", Boolean, nullable=False, server_default="false"),
    Column("ambiguous_count", Integer, nullable=False, server_default="0"),
    Column("cooldown_until", TZ, nullable=True),
    Column("updated_at", TZ, nullable=False),
    CheckConstraint("channel IN ('sms', 'telegram')", name="control_channel"),
)

# ----------------------------------------------------------------------------- statistics
# Snapshot behind GET /api/v1/charts (oan_messaging.stats), rebuilt every 15 minutes by the
# cleanup component. Counts and latency histograms only: no recipient, text or reference.
# Days are calendar days in Ethiopia (Africa/Addis_Ababa) of the message's acceptance.

stats_daily = Table(
    "stats_daily",
    metadata,
    Column("day", Date, nullable=False),
    Column("caller_id", Text, nullable=False),
    Column("channel", Text, nullable=False),
    Column("purpose", Text, nullable=False),
    Column("accepted", Integer, nullable=False),
    Column("open", Integer, nullable=False),
    Column("sent", Integer, nullable=False),
    Column("delivered", Integer, nullable=False),
    Column("failed", Integer, nullable=False),
    Column("unknown", Integer, nullable=False),
    Column("expired", Integer, nullable=False),
    #: Counts per latency bucket (oan_messaging.stats.snapshot.BUCKET_EDGES_S).
    Column("send_hist", ARRAY(Integer), nullable=False),
    Column("deliver_hist", ARRAY(Integer), nullable=False),
    PrimaryKeyConstraint("day", "caller_id", "channel", "purpose", name="pk_stats_daily"),
)

stats_failures = Table(
    "stats_failures",
    metadata,
    Column("day", Date, nullable=False),
    Column("caller_id", Text, nullable=False),
    Column("channel", Text, nullable=False),
    Column("purpose", Text, nullable=False),
    Column("failure_code", Text, nullable=False),
    Column("total", Integer, nullable=False),
    PrimaryKeyConstraint(
        "day", "caller_id", "channel", "purpose", "failure_code", name="pk_stats_failures"
    ),
)

stats_campaigns = Table(
    "stats_campaigns",
    metadata,
    #: The first two ``:``-separated parts of source_reference (e.g. ``da_broadcast:BC-1``).
    Column("campaign", Text, nullable=False),
    Column("caller_id", Text, nullable=False),
    Column("purpose", Text, nullable=False),
    Column("first_at", TZ, nullable=False),
    Column("last_at", TZ, nullable=False),
    Column("recipients", Integer, nullable=False),
    Column("open", Integer, nullable=False),
    Column("sent", Integer, nullable=False),
    Column("delivered", Integer, nullable=False),
    Column("failed", Integer, nullable=False),
    Column("unknown", Integer, nullable=False),
    Column("expired", Integer, nullable=False),
    PrimaryKeyConstraint("caller_id", "campaign", "purpose", name="pk_stats_campaigns"),
)

stats_state = Table(
    "stats_state",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("as_of", TZ, nullable=False),
    CheckConstraint("id = 1", name="stats_state_single_row"),
)

Index("ix_messages_created_at", messages.c.created_at)

