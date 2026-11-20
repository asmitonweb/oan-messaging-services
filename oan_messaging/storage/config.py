"""Service configuration (environment / vault-injected), validated at startup.

All secrets are ``SecretStr`` and are parsed into key objects once; they are never logged
or written to the database. Policy overrides are seconds; non-positive or incoherent values
fail ``ServicePolicy`` validation, so the service refuses to start (scenario A21).

Environment variables (prefix ``OAN_MSG_``)::

    OAN_MSG_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/db
    OAN_MSG_CONTENT_KEYS=v1:<base64 32 bytes>[,v0:<base64>]
    OAN_MSG_CONTENT_KEY_CURRENT=v1
    OAN_MSG_FINGERPRINT_KEYS=k1:<base64 >=32 bytes>[,k0:<base64>]
    OAN_MSG_FINGERPRINT_KEY_CURRENT=k1
    OAN_MSG_CREDENTIAL_PEPPER=<base64 >=32 bytes>
    OAN_MSG_POLICY_DEDUP_WINDOW_SECONDS=2592000   (optional overrides, all policy fields)
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import timedelta

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from oan_messaging.contracts import HmacFingerprinter, ServicePolicy
from oan_messaging.storage.crypto import ContentCipher, KeyringError, parse_keyring

_POLICY_FIELDS = (
    "max_otp_ttl",
    "max_expiry_horizon",
    "dedup_window",
    "receipt_window",
    "receipt_evidence_retention",
    "purge_sweep_interval",
    "worker_lease",
)


class StorageSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OAN_MSG_", extra="ignore", frozen=True)

    database_url: SecretStr
    content_keys: SecretStr
    content_key_current: str
    fingerprint_keys: SecretStr
    fingerprint_key_current: str
    credential_pepper: SecretStr
    db_pool_size: int = 10
    db_max_overflow: int = 10
    max_body_bytes: int = 16 * 1024
    broadcast_enabled: bool = False
    #: SMS-03: seconds after which a waiting message goes ahead of newer non-OTP traffic.
    broadcast_max_wait_seconds: int = Field(default=180, gt=0)
    #: SMS sent to 8028 per second across all workers (operator capacity 2-5/s), and the
    #: slots of each second only an OTP may use.
    sms_rate_per_second: int = Field(default=4, gt=0)
    sms_otp_reserve: int = Field(default=1, ge=0)
    #: How often the cleanup component rebuilds the statistics snapshot (GET /api/v1/charts).
    stats_refresh_seconds: int = Field(default=900, gt=0)

    policy_max_otp_ttl_seconds: int | None = None
    policy_max_expiry_horizon_seconds: int | None = None
    policy_dedup_window_seconds: int | None = None
    policy_receipt_window_seconds: int | None = None
    policy_receipt_evidence_retention_seconds: int | None = None
    policy_purge_sweep_interval_seconds: int | None = None
    policy_worker_lease_seconds: int | None = None


def load_policy(settings: StorageSettings) -> ServicePolicy:
    """Build the policy; raises (startup failure) on non-positive or incoherent values."""
    overrides: dict[str, timedelta] = {}
    for name in _POLICY_FIELDS:
        value = getattr(settings, f"policy_{name}_seconds")
        if value is not None:
            overrides[name] = timedelta(seconds=value)
    return ServicePolicy(**overrides)


@dataclass(frozen=True)
class ServiceSecrets:
    cipher: ContentCipher
    fingerprinter: HmacFingerprinter
    credential_pepper: bytes

    @classmethod
    def from_settings(cls, settings: StorageSettings) -> ServiceSecrets:
        cipher = ContentCipher(
            parse_keyring(settings.content_keys.get_secret_value()),
            settings.content_key_current,
        )
        fp_keys = parse_keyring(settings.fingerprint_keys.get_secret_value())
        if settings.fingerprint_key_current not in fp_keys:
            raise KeyringError("current fingerprint key version missing")
        try:
            pepper = base64.b64decode(settings.credential_pepper.get_secret_value(), validate=True)
        except ValueError:
            raise KeyringError("credential pepper is not valid base64") from None
        if len(pepper) < 32:
            raise KeyringError("credential pepper too short")
        return cls(cipher, HmacFingerprinter(fp_keys, settings.fingerprint_key_current), pepper)

    def __repr__(self) -> str:
        return "ServiceSecrets(<redacted>)"
