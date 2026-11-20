"""Subprocess harness for test_int_process.py (not collected by pytest).

One REAL worker process: PostgreSQL repository from ``OAN_MSG_*`` settings, the production
``SharedRateLimiter`` (database clock), and the real ``Sms8028Adapter`` with mTLS to a
loopback fake gateway (``OAN_MSG_SMS8028_*``). Runs ``run_forever`` until it is killed.
Prints only fixed markers; never message content.
"""

from __future__ import annotations

import asyncio
import os
import socket
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from oan_messaging.adapters.sms8028 import Sms8028Adapter, Sms8028Settings, make_client
from oan_messaging.contracts import Channel
from oan_messaging.storage.config import ServiceSecrets, StorageSettings, load_policy
from oan_messaging.storage.db import make_engine, make_session_factory
from oan_messaging.storage.repository import PostgresMessageRepository
from oan_messaging.worker import SharedRateLimiter, Worker, WorkerConfig

_connect = socket.socket.connect


def _loopback_only(self: socket.socket, address: Any) -> Any:
    host = address[0] if isinstance(address, tuple) else address
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise OSError("harness refuses non-loopback connections")
    return _connect(self, address)


socket.socket.connect = _loopback_only  # type: ignore[assignment]


async def main() -> None:
    settings = StorageSettings()
    secrets = ServiceSecrets.from_settings(settings)
    engine = make_engine(settings.database_url.get_secret_value(), pool_size=5)
    repo = PostgresMessageRepository(
        make_session_factory(engine),
        cipher=secrets.cipher,
        fingerprinter=secrets.fingerprinter,
        policy=load_policy(settings),
    )
    sms = Sms8028Settings(
        client_secret=SecretStr(os.environ["OAN_MSG_SMS8028_CLIENT_SECRET"]),
        client_cert=Path(os.environ["OAN_MSG_SMS8028_CLIENT_CERT"]),
        client_key=Path(os.environ["OAN_MSG_SMS8028_CLIENT_KEY"]),
        ca_cert=Path(os.environ["OAN_MSG_SMS8028_CA_CERT"]),
        base_url=os.environ["OAN_MSG_SMS8028_BASE_URL"],
        connect_timeout=1.0,
        read_timeout=3.0,
    )
    adapter = Sms8028Adapter(sms, make_client(sms))
    limiter = SharedRateLimiter(engine)  # production default: database clock
    lease = timedelta(seconds=float(os.environ["IT_LEASE_SECONDS"]))
    worker = Worker(
        repo,
        {Channel.SMS: adapter},
        limiter,
        WorkerConfig(
            worker_id=os.environ["IT_WORKER_ID"],
            lease=lease,
            lease_safety_margin=lease / 2,
            batch_size=5,
            max_rate_wait=timedelta(milliseconds=200),
        ),
    )
    print("IT-WORKER-READY", flush=True)
    await worker.run_forever(asyncio.Event(), idle=0.1)


if __name__ == "__main__":
    asyncio.run(main())
