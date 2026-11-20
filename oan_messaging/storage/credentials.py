"""Callers and separately issued service credentials.

* Each calling service is a ``callers`` row with its own allowed channels, allowed
  purposes and (optionally) the configured Telegram bot key it may use.
* Credentials are opaque bearer tokens ``oanmsg_<43 url-safe chars>`` issued per caller.
  Only ``HMAC-SHA256(pepper, token)`` is stored; the pepper lives in secret configuration,
  so a database dump alone cannot be used to test guessed tokens.
* Issuance returns the token exactly once; it is never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oan_messaging.contracts import CallerContext, Channel, Purpose
from oan_messaging.storage.db import rowcount
from oan_messaging.storage.schema import api_credentials, callers

TOKEN_PREFIX = "oanmsg_"  # noqa: S105 (public prefix, not a secret)
TOKEN_RE = re.compile(r"^oanmsg_[A-Za-z0-9_-]{43}$")
BOT_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_DOMAIN = b"oan-msg/credential/v1\x00"


def token_hmac(pepper: bytes, token: str) -> bytes:
    return hmac.new(pepper, _DOMAIN + token.encode(), hashlib.sha256).digest()


async def upsert_caller(
    sf: async_sessionmaker[AsyncSession],
    caller_id: str,
    *,
    channels: Iterable[Channel],
    purposes: Iterable[Purpose],
    telegram_bot: str | None = None,
    enabled: bool = True,
    can_read_stats: bool = False,
) -> None:
    if telegram_bot is not None and not BOT_KEY_RE.fullmatch(telegram_bot):
        raise ValueError("invalid telegram bot key")
    channels, purposes = list(channels), list(purposes)
    if can_read_stats and (channels or purposes):
        # A statistics token is read-only: it must never be able to send.
        raise ValueError("a statistics reader has no channels and no purposes")
    values = {
        "caller_id": caller_id,
        "allowed_channels": sorted(c.value for c in channels),
        "allowed_purposes": sorted(p.value for p in purposes),
        "telegram_bot": telegram_bot,
        "enabled": enabled,
        "can_read_stats": can_read_stats,
    }
    async with sf() as s, s.begin():
        await s.execute(
            pg_insert(callers)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[callers.c.caller_id],
                set_={k: v for k, v in values.items() if k != "caller_id"},
            )
        )


async def issue_credential_with_id(
    sf: async_sessionmaker[AsyncSession], pepper: bytes, caller_id: str, now: datetime
) -> tuple[uuid.UUID, str]:
    """Create a new credential; returns its id (not secret) and the token (shown once)."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    credential_id = uuid.uuid4()
    async with sf() as s, s.begin():
        await s.execute(
            api_credentials.insert().values(
                credential_id=credential_id,
                caller_id=caller_id,
                token_hmac=token_hmac(pepper, token),
                created_at=now,
            )
        )
    return credential_id, token


async def issue_credential(
    sf: async_sessionmaker[AsyncSession], pepper: bytes, caller_id: str, now: datetime
) -> str:
    """Create a new credential for ``caller_id``; returns the token (shown once)."""
    return (await issue_credential_with_id(sf, pepper, caller_id, now))[1]


async def revoke_credential(
    sf: async_sessionmaker[AsyncSession], caller_id: str, credential_id: uuid.UUID, now: datetime
) -> bool:
    """Revoke ONE credential of ``caller_id``; other credentials keep working. Returns
    False when no such active credential exists for that caller."""
    async with sf() as s, s.begin():
        res = await s.execute(
            update(api_credentials)
            .where(
                api_credentials.c.credential_id == credential_id,
                api_credentials.c.caller_id == caller_id,
                api_credentials.c.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        return rowcount(res) == 1


async def list_credentials(
    sf: async_sessionmaker[AsyncSession], caller_id: str
) -> list[dict[str, object]]:
    """Credential ids and timestamps for ``caller_id``; never token material."""
    async with sf() as s:
        rows = (
            await s.execute(
                select(
                    api_credentials.c.credential_id,
                    api_credentials.c.created_at,
                    api_credentials.c.revoked_at,
                )
                .where(api_credentials.c.caller_id == caller_id)
                .order_by(api_credentials.c.created_at)
            )
        ).all()
    return [
        {
            "credential_id": str(r[0]),
            "created_at": r[1].isoformat(),
            "revoked_at": r[2].isoformat() if r[2] is not None else None,
        }
        for r in rows
    ]


async def revoke_credentials(
    sf: async_sessionmaker[AsyncSession], caller_id: str, now: datetime
) -> int:
    async with sf() as s, s.begin():
        res = await s.execute(
            update(api_credentials)
            .where(api_credentials.c.caller_id == caller_id, api_credentials.c.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        return rowcount(res)


async def authenticate(
    sf: async_sessionmaker[AsyncSession], pepper: bytes, token: str
) -> CallerContext | None:
    """Resolve a bearer token to its caller, or None (unknown, revoked, disabled)."""
    if not TOKEN_RE.fullmatch(token):
        return None
    digest = token_hmac(pepper, token)
    async with sf() as s:
        row = (
            (
                await s.execute(
                    select(callers)
                    .join(api_credentials, api_credentials.c.caller_id == callers.c.caller_id)
                    .where(
                        api_credentials.c.token_hmac == digest,
                        api_credentials.c.revoked_at.is_(None),
                        callers.c.enabled.is_(True),
                    )
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        return None
    return CallerContext(
        caller_id=row["caller_id"],
        allowed_channels=frozenset(Channel(c) for c in row["allowed_channels"]),
        allowed_purposes=frozenset(Purpose(p) for p in row["allowed_purposes"]),
        telegram_bot=row["telegram_bot"],
        can_read_stats=bool(row["can_read_stats"]),
    )
