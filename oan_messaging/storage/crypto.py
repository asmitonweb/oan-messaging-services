"""Content encryption at rest (AES-256-GCM) with versioned keys.

Plaintext is a small JSON document holding the message text and the recipient (E.164) or
chat id. The associated data binds each ciphertext to its message id and channel, so a
ciphertext copied onto another row fails authentication.
"""

from __future__ import annotations

import base64
import json
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_VERSION_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_AAD_PREFIX = b"oan-msg/content/v1\x00"


class KeyringError(ValueError):
    """Invalid key configuration. Messages never include key material."""


def parse_keyring(spec: str, *, min_bytes: int = 32) -> dict[str, bytes]:
    """Parse ``"v1:<base64>,v2:<base64>"``. Keys must decode to >= ``min_bytes`` bytes."""
    keys: dict[str, bytes] = {}
    for item in (p.strip() for p in spec.split(",")):
        if not item:
            continue
        version, sep, encoded = item.partition(":")
        if not sep or not KEY_VERSION_RE.fullmatch(version):
            raise KeyringError("invalid key version in keyring")
        if version in keys:
            raise KeyringError("duplicate key version in keyring")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise KeyringError("key material is not valid base64") from None
        if len(raw) < min_bytes:
            raise KeyringError("key material too short")
        keys[version] = raw
    if not keys:
        raise KeyringError("keyring is empty")
    return keys


@dataclass(frozen=True)
class EncryptedContent:
    key_version: str
    nonce: bytes
    ciphertext: bytes


class ContentCipher:
    def __init__(self, keys: Mapping[str, bytes], current_version: str) -> None:
        if current_version not in keys:
            raise KeyringError("current content key version missing")
        for v, k in keys.items():
            if len(k) != 32:
                raise KeyringError("content keys must be exactly 32 bytes (AES-256)")
            if not KEY_VERSION_RE.fullmatch(v):
                raise KeyringError("invalid key version in keyring")
        self._keys = {v: AESGCM(k) for v, k in keys.items()}
        self._current = current_version

    @property
    def current_version(self) -> str:
        return self._current

    def versions(self) -> frozenset[str]:
        return frozenset(self._keys)

    @staticmethod
    def _aad(message_id: uuid.UUID, channel: str) -> bytes:
        return _AAD_PREFIX + message_id.bytes + channel.encode()

    def encrypt(
        self, message_id: uuid.UUID, channel: str, content: dict[str, Any]
    ) -> EncryptedContent:
        nonce = os.urandom(12)
        plaintext = json.dumps(content, separators=(",", ":"), ensure_ascii=False).encode()
        ct = self._keys[self._current].encrypt(nonce, plaintext, self._aad(message_id, channel))
        return EncryptedContent(self._current, nonce, ct)

    def decrypt(
        self, message_id: uuid.UUID, channel: str, blob: EncryptedContent
    ) -> dict[str, Any]:
        try:
            aead = self._keys[blob.key_version]
        except KeyError:
            raise KeyringError("content key version not available") from None
        try:
            plaintext = aead.decrypt(blob.nonce, blob.ciphertext, self._aad(message_id, channel))
        except InvalidTag:
            raise KeyringError("content failed authentication") from None
        data = json.loads(plaintext)
        if not isinstance(data, dict):
            raise KeyringError("content has an unexpected shape")
        return data

    def __repr__(self) -> str:
        return f"ContentCipher(versions={sorted(self._keys)}, current={self._current!r})"
