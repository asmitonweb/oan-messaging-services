"""Initial schema for MSG-02 (contract 3.0.0).

Generated from ``oan_messaging/storage/schema.py`` with ``alembic revision --autogenerate``
and reviewed by hand. The status-transition trigger is hand-written; its transition list
must equal ``contracts.ALLOWED_TRANSITIONS`` (checked by tests/api/test_migrations.py).

Revision ID: 0001
Revises:
Create Date: 2026-11-14
"""
# ruff: noqa: E501

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Legal (from, to) status pairs; must equal contracts.ALLOWED_TRANSITIONS.
TRANSITIONS: tuple[tuple[str, str], ...] = (
    ("accepted", "sending"), ("accepted", "expired"),
    ("sending", "sent"), ("sending", "failed"), ("sending", "unknown"),
    ("sending", "accepted"), ("sending", "expired"),
    ("sent", "delivered"), ("sent", "failed"),
    ("unknown", "sent"), ("unknown", "delivered"), ("unknown", "failed"),
)
STATUS_TRIGGER_FUNCTION = (
    "CREATE FUNCTION oan_msg_check_status_transition() RETURNS trigger AS $$\n"
    "BEGIN\n"
    "  IF NEW.status = OLD.status THEN RETURN NEW; END IF;\n"
    "  IF (OLD.status, NEW.status) IN ("
    + ", ".join(f"('{a}', '{b}')" for a, b in TRANSITIONS)
    + ") THEN RETURN NEW; END IF;\n"
    "  RAISE EXCEPTION 'illegal message status transition' USING ERRCODE = 'check_violation';\n"
    "END\n"
    "$$ LANGUAGE plpgsql"
)


def upgrade() -> None:
    op.create_table('callers',
    sa.Column('caller_id', sa.Text(), nullable=False),
    sa.Column('allowed_channels', postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column('allowed_purposes', postgresql.ARRAY(sa.Text()), nullable=False),
    sa.Column('telegram_bot', sa.Text(), nullable=True),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('char_length(caller_id) BETWEEN 1 AND 64', name=op.f('ck_callers_caller_id_length')),
    sa.PrimaryKeyConstraint('caller_id', name=op.f('pk_callers'))
    )
    op.create_table('retired_key_versions',
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('key_version', sa.Text(), nullable=False),
    sa.Column('retired_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("kind IN ('fingerprint', 'content')", name=op.f('ck_retired_key_versions_kind')),
    sa.PrimaryKeyConstraint('kind', 'key_version', name=op.f('pk_retired_key_versions'))
    )
    op.create_table('api_credentials',
    sa.Column('credential_id', sa.UUID(), nullable=False),
    sa.Column('caller_id', sa.Text(), nullable=False),
    sa.Column('token_hmac', sa.LargeBinary(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('octet_length(token_hmac) = 32', name=op.f('ck_api_credentials_token_hmac_length')),
    sa.ForeignKeyConstraint(['caller_id'], ['callers.caller_id'], name=op.f('fk_api_credentials_caller_id_callers')),
    sa.PrimaryKeyConstraint('credential_id', name=op.f('pk_api_credentials')),
    sa.UniqueConstraint('token_hmac', name=op.f('uq_api_credentials_token_hmac'))
    )
    op.create_table('messages',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('caller_id', sa.Text(), nullable=False),
    sa.Column('channel', sa.Text(), nullable=False),
    sa.Column('idempotency_key', sa.Text(), nullable=False),
    sa.Column('purpose', sa.Text(), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('source_reference', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('failure_code', sa.Text(), nullable=True),
    sa.Column('provider_status', sa.Text(), nullable=True),
    sa.Column('provider_message_id', sa.Text(), nullable=True),
    sa.Column('payload_fp_version', sa.Text(), nullable=True),
    sa.Column('payload_fp_digest', sa.Text(), nullable=True),
    sa.Column('binding_released_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('recipient_fp_version', sa.Text(), nullable=True),
    sa.Column('recipient_fp_digest', sa.Text(), nullable=True),
    sa.Column('recipient_masked', sa.Text(), nullable=False),
    sa.Column('authorized_bot', sa.Text(), nullable=True),
    sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('not_before', sa.DateTime(timezone=True), nullable=True),
    sa.Column('content_purged_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('lease_token', sa.UUID(), nullable=True),
    sa.Column('lease_owner', sa.Text(), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("NOT (status IN ('accepted', 'sending')) OR content_purged_at IS NULL", name=op.f('ck_messages_content_while_sendable')),
    sa.CheckConstraint("binding_released_at IS NULL OR (payload_fp_digest IS NULL AND status IN ('delivered', 'expired', 'failed', 'sent'))", name=op.f('ck_messages_released_binding')),
    sa.CheckConstraint("channel <> 'sms' OR status NOT IN ('accepted', 'sending') OR recipient_fp_digest IS NOT NULL", name=op.f('ck_messages_recipient_fp_while_sendable')),
    sa.CheckConstraint("channel <> 'telegram' OR status <> 'delivered'", name=op.f('ck_messages_telegram_never_delivered')),
    sa.CheckConstraint("(channel = 'telegram') = (authorized_bot IS NOT NULL)", name=op.f('ck_messages_authorized_bot_telegram')),
    sa.CheckConstraint("channel = 'sms' OR recipient_fp_digest IS NULL", name=op.f('ck_messages_recipient_fp_sms_only')),
    sa.CheckConstraint("channel IN ('sms', 'telegram')", name=op.f('ck_messages_channel')),
    sa.CheckConstraint("failure_code IS NULL OR failure_code IN ('chat_not_found', 'expired_before_send', 'interrupted', 'invalid_recipient', 'malformed_response', 'provider_auth', 'provider_config', 'provider_delivery_expired', 'provider_rejected', 'provider_server_error', 'provider_unavailable', 'rate_limited', 'recipient_blocked_bot', 'text_rejected', 'timeout')", name=op.f('ck_messages_failure_code')),
    sa.CheckConstraint("payload_fp_digest IS NULL OR payload_fp_digest ~ '^[0-9a-f]{64}$'", name=op.f('ck_messages_payload_fp_format')),
    sa.CheckConstraint("purpose IN ('alert', 'broadcast', 'otp', 'transactional')", name=op.f('ck_messages_purpose')),
    sa.CheckConstraint("recipient_fp_digest IS NULL OR recipient_fp_digest ~ '^[0-9a-f]{64}$'", name=op.f('ck_messages_recipient_fp_format')),
    sa.CheckConstraint("status <> 'delivered' OR delivered_at IS NOT NULL", name=op.f('ck_messages_delivered_at')),
    sa.CheckConstraint("status <> 'sending' OR lease_token IS NOT NULL", name=op.f('ck_messages_sending_lease')),
    sa.CheckConstraint("status <> 'sent' OR sent_at IS NOT NULL", name=op.f('ck_messages_sent_at')),
    sa.CheckConstraint("status IN ('accepted', 'delivered', 'expired', 'failed', 'sending', 'sent', 'unknown')", name=op.f('ck_messages_status')),
    sa.CheckConstraint("status NOT IN ('failed', 'expired') OR failure_code IS NOT NULL", name=op.f('ck_messages_failure_reason')),
    sa.CheckConstraint('(attempts > 0) = (last_attempt_at IS NOT NULL)', name=op.f('ck_messages_last_attempt')),
    sa.CheckConstraint('(payload_fp_version IS NULL) = (payload_fp_digest IS NULL)', name=op.f('ck_messages_payload_fp_pair')),
    sa.CheckConstraint('(recipient_fp_version IS NULL) = (recipient_fp_digest IS NULL)', name=op.f('ck_messages_recipient_fp_pair')),
    sa.CheckConstraint('attempts >= 0', name=op.f('ck_messages_attempts_non_negative')),
    sa.CheckConstraint('binding_released_at IS NOT NULL OR payload_fp_digest IS NOT NULL', name=op.f('ck_messages_live_binding_fingerprint')),
    sa.ForeignKeyConstraint(['caller_id'], ['callers.caller_id'], name=op.f('fk_messages_caller_id_callers')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_messages'))
    )
    op.create_index('ix_messages_caller_id_id', 'messages', ['caller_id', 'id'], unique=False)
    op.execute(STATUS_TRIGGER_FUNCTION)
    op.execute(
        "CREATE TRIGGER trg_messages_status_transition BEFORE UPDATE OF status ON messages "
        "FOR EACH ROW EXECUTE FUNCTION oan_msg_check_status_transition()"
    )
    op.create_index('ix_messages_claimable', 'messages', ['priority', 'created_at'], unique=False, postgresql_where=sa.text("status = 'accepted'"))
    op.create_index('ix_messages_provider_message_id', 'messages', ['channel', 'provider_message_id'], unique=False, postgresql_where=sa.text('provider_message_id IS NOT NULL'))
    op.create_index('ix_messages_sending_lease', 'messages', ['lease_expires_at'], unique=False, postgresql_where=sa.text("status = 'sending'"))
    op.create_index('uq_messages_live_binding', 'messages', ['caller_id', 'channel', 'idempotency_key'], unique=True, postgresql_where=sa.text('binding_released_at IS NULL'))
    op.create_table('message_attempts',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('message_id', sa.UUID(), nullable=False),
    sa.Column('attempt_no', sa.Integer(), nullable=False),
    sa.Column('claim_token', sa.UUID(), nullable=False),
    sa.Column('worker_id', sa.Text(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('outcome', sa.Text(), nullable=True),
    sa.Column('provider_status', sa.Text(), nullable=True),
    sa.Column('failure_code', sa.Text(), nullable=True),
    sa.Column('provider_message_id', sa.Text(), nullable=True),
    sa.Column('retryable', sa.Boolean(), nullable=True),
    sa.Column('retry_after_seconds', sa.Integer(), nullable=True),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.CheckConstraint('attempt_no >= 1', name=op.f('ck_message_attempts_attempt_no_positive')),
    sa.ForeignKeyConstraint(['message_id'], ['messages.id'], name=op.f('fk_message_attempts_message_id_messages'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_message_attempts')),
    sa.UniqueConstraint('message_id', 'attempt_no', name=op.f('uq_message_attempts_message_id_attempt_no'))
    )
    op.create_table('message_content',
    sa.Column('message_id', sa.UUID(), nullable=False),
    sa.Column('key_version', sa.Text(), nullable=False),
    sa.Column('nonce', sa.LargeBinary(), nullable=False),
    sa.Column('ciphertext', sa.LargeBinary(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('octet_length(nonce) = 12', name=op.f('ck_message_content_nonce_length')),
    sa.ForeignKeyConstraint(['message_id'], ['messages.id'], name=op.f('fk_message_content_message_id_messages'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('message_id', name=op.f('pk_message_content'))
    )
    op.create_table('receipts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('provider', sa.Text(), nullable=False),
    sa.Column('provider_message_id', sa.Text(), nullable=False),
    sa.Column('recipient_fp_version', sa.Text(), nullable=False),
    sa.Column('recipient_fp_digest', sa.Text(), nullable=False),
    sa.Column('recipient_masked', sa.Text(), nullable=False),
    sa.Column('raw_status', sa.Text(), nullable=False),
    sa.Column('event_time', sa.DateTime(timezone=True), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('disposition', sa.Text(), nullable=False),
    sa.Column('message_id', sa.UUID(), nullable=True),
    sa.CheckConstraint("disposition IN ('applied', 'mismatched', 'orphaned', 'recorded_no_change', 'unmatched_late')", name=op.f('ck_receipts_disposition')),
    sa.CheckConstraint("provider = 'sms8028'", name=op.f('ck_receipts_provider_8028_only')),
    sa.CheckConstraint("recipient_fp_digest ~ '^[0-9a-f]{64}$'", name=op.f('ck_receipts_recipient_fp_format')),
    sa.ForeignKeyConstraint(['message_id'], ['messages.id'], name=op.f('fk_receipts_message_id_messages'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_receipts'))
    )
    op.create_index('ix_receipts_provider_message_id', 'receipts', ['provider', 'provider_message_id'], unique=False)
    op.create_index('ix_receipts_received_at', 'receipts', ['received_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_receipts_received_at', table_name='receipts')
    op.drop_index('ix_receipts_provider_message_id', table_name='receipts')
    op.drop_table('receipts')
    op.drop_table('message_content')
    op.drop_table('message_attempts')
    op.drop_index('uq_messages_live_binding', table_name='messages', postgresql_where=sa.text('binding_released_at IS NULL'))
    op.drop_index('ix_messages_sending_lease', table_name='messages', postgresql_where=sa.text("status = 'sending'"))
    op.drop_index('ix_messages_provider_message_id', table_name='messages', postgresql_where=sa.text('provider_message_id IS NOT NULL'))
    op.drop_index('ix_messages_claimable', table_name='messages', postgresql_where=sa.text("status = 'accepted'"))
    op.drop_index('ix_messages_caller_id_id', table_name='messages')
    op.execute("DROP TRIGGER IF EXISTS trg_messages_status_transition ON messages")
    op.drop_table('messages')
    op.execute("DROP FUNCTION IF EXISTS oan_msg_check_status_transition()")
    op.drop_table('api_credentials')
    op.drop_table('retired_key_versions')
    op.drop_table('callers')
