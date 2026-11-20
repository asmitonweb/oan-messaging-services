"""PostgreSQL storage for the OAN Messaging Service (MSG-02).

Implements the frozen ``MessageRepository`` contract (contract 3.0.0) with SQLAlchemy
(asyncio, psycopg 3). Secrets (content encryption keys, fingerprint keys, credential
pepper) come from configuration only and never touch the database.
"""

from oan_messaging.storage.config import ServiceSecrets, StorageSettings, load_policy
from oan_messaging.storage.crypto import ContentCipher, KeyringError
from oan_messaging.storage.errors import (
    BindingIntegrityError,
    DispatchNotAuthorized,
    ExpiryRejected,
    KeyRetirementRefused,
    MessageExpired,
)
from oan_messaging.storage.repository import PostgresMessageRepository

__all__ = [
    "BindingIntegrityError",
    "ContentCipher",
    "DispatchNotAuthorized",
    "ExpiryRejected",
    "KeyRetirementRefused",
    "KeyringError",
    "MessageExpired",
    "PostgresMessageRepository",
    "ServiceSecrets",
    "StorageSettings",
    "load_policy",
]
