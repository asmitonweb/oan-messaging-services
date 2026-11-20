"""OAN Messaging Service client and integration helpers (contract 3.0.0).

INTEGRATION-READY EXAMPLE CODE, NOT AN INSTALLED INTEGRATION.
"""

from oan_client.client import (
    CONTRACT_VERSION,
    STATUS_MEANINGS,
    ClientConfig,
    Delivery,
    MessageStatus,
    OanClient,
    OanClientError,
    OanConflict,
    OanProtocolError,
    OanRejected,
    OanRetryDeferred,
    OanUncertain,
    ReconciliationRequired,
    Secret,
    SmsDelivery,
    Submission,
    TelegramDelivery,
)
from oan_client.keys import KEY_ALGORITHM, delivery_key, key_material

__all__ = [
    "CONTRACT_VERSION",
    "KEY_ALGORITHM",
    "STATUS_MEANINGS",
    "ClientConfig",
    "Delivery",
    "MessageStatus",
    "OanClient",
    "OanClientError",
    "OanConflict",
    "OanProtocolError",
    "OanRejected",
    "OanRetryDeferred",
    "OanUncertain",
    "ReconciliationRequired",
    "Secret",
    "SmsDelivery",
    "Submission",
    "TelegramDelivery",
    "delivery_key",
    "key_material",
]
