"""Explicit component entrypoints. Providers must be enabled by operator configuration.

    python -m oan_messaging.runtime worker|receipts|cleanup [--forever]
    python -m oan_messaging.runtime suspend|resume --channel sms|telegram
    python -m oan_messaging.runtime status [--fail-on-alert]
    python -m oan_messaging.runtime caller --caller ID --channels sms --purposes otp[,...]
    python -m oan_messaging.runtime issue-credential|list-credentials --caller ID
    python -m oan_messaging.runtime revoke-credentials --caller ID (--credential CID | --all)
    python -m oan_messaging.runtime check-heartbeat [--max-age SECONDS]

The API runs under uvicorn: ``uvicorn --factory oan_messaging.runtime.api:create_app``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import uuid
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from oan_messaging.adapters.sms8028 import Sms8028Adapter, Sms8028Settings
from oan_messaging.adapters.sms8028 import make_client as sms_client
from oan_messaging.adapters.telegram import TelegramAdapter, TelegramBotRegistry
from oan_messaging.adapters.telegram import make_client as telegram_client
from oan_messaging.contracts import Channel, MessageAdapter, Purpose
from oan_messaging.receipts.ingress import DEFAULT_PATH as DEFAULT_RECEIPTS_PATH
from oan_messaging.receipts.ingress import (
    CallbackServer,
    ReceiptProcessor,
    build_server_ssl_context,
    load_pins,
)
from oan_messaging.redaction import install_redaction
from oan_messaging.runtime.ops import (
    DEFAULT_HEARTBEAT,
    heartbeat_age,
    missing_key_versions,
    ops_status,
    set_scope,
    touch_heartbeat,
)
from oan_messaging.stats.snapshot import refresh as refresh_stats
from oan_messaging.stats.snapshot import refresh_forever
from oan_messaging.storage.config import ServiceSecrets, StorageSettings, load_policy
from oan_messaging.storage.credentials import (
    issue_credential_with_id,
    list_credentials,
    revoke_credential,
    revoke_credentials,
    upsert_caller,
)
from oan_messaging.storage.db import make_engine, make_session_factory
from oan_messaging.storage.repository import PostgresMessageRepository
from oan_messaging.storage.schema import callers
from oan_messaging.worker import SharedRateLimiter, Worker, WorkerConfig
from oan_messaging.worker.cleanup import cleanup_forever, run_cleanup
from oan_messaging.worker.rate_limit import TELEGRAM_DEFAULT, RateLimit, measure_dispatch_latency

log = logging.getLogger(__name__)


def heartbeat_path() -> Path:
    return Path(os.environ.get("OAN_MSG_WORKER_HEARTBEAT_FILE", str(DEFAULT_HEARTBEAT)))


def path_list(name: str) -> list[Path]:
    """Comma-separated file paths from an optional setting."""
    return [Path(p.strip()) for p in os.environ.get(name, "").split(",") if p.strip()]


def required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"required setting missing: {name}")
    return value


async def run(
    component: str,
    channel: str | None = None,
    *,
    fail_on_alert: bool = False,
    forever: bool = False,
    opts: argparse.Namespace | None = None,
) -> None:
    settings = StorageSettings()
    policy = load_policy(settings)
    secrets = ServiceSecrets.from_settings(settings)
    install_redaction(
        settings.database_url.get_secret_value(),
        settings.content_keys.get_secret_value(),
        settings.fingerprint_keys.get_secret_value(),
        settings.credential_pepper.get_secret_value(),
    )
    engine = make_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )
    sf = make_session_factory(engine)
    repo = PostgresMessageRepository(
        sf, cipher=secrets.cipher, fingerprinter=secrets.fingerprinter, policy=policy
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        async with AsyncExitStack() as stack:
            async with sf() as session:
                await session.execute(text("SELECT 1"))
            if component in ("suspend", "resume"):
                assert channel is not None
                await set_scope(
                    sf, Channel(channel), suspended=component == "suspend", now=datetime.now(UTC)
                )
                log.info("provider scope %s channel=%s", component, channel)
                return
            if component == "caller":
                assert opts is not None
                purposes = {Purpose(v) for v in opts.purposes.split(",") if v}
                stats_reader = bool(getattr(opts, "stats_reader", False))
                if Purpose.BROADCAST in purposes and not settings.broadcast_enabled:
                    raise ValueError("broadcast purpose refused: broadcasts disabled for pilot")
                await upsert_caller(
                    sf,
                    opts.caller,
                    channels=(
                        set()
                        if stats_reader
                        else {Channel(v) for v in opts.channels.split(",") if v}
                    ),
                    purposes=purposes,
                    telegram_bot=opts.bot,
                    enabled=not opts.disable,
                    can_read_stats=stats_reader,
                )
                log.info("caller upserted caller_id=%s enabled=%s", opts.caller, not opts.disable)
                return
            if component == "issue-credential":
                assert opts is not None
                credential_id, token = await issue_credential_with_id(
                    sf, secrets.credential_pepper, opts.caller, datetime.now(UTC)
                )
                # The token is shown exactly once, alone on stdout, for the caller's secret
                # store. The id is not secret: record it to revoke this token later.
                # nosemgrep: python-logger-credential-disclosure -- the id is public
                log.info(
                    "credential issued caller_id=%s credential_id=%s", opts.caller, credential_id
                )
                print(token)
                return
            if component == "list-credentials":
                assert opts is not None
                print(json.dumps(await list_credentials(sf, opts.caller)))
                return
            if component == "revoke-credentials":
                assert opts is not None
                now = datetime.now(UTC)
                if opts.credential:
                    if not await revoke_credential(
                        sf, opts.caller, uuid.UUID(opts.credential), now
                    ):
                        raise ValueError("no such active credential for this caller")
                    # nosemgrep: python-logger-credential-disclosure -- the id is public
                    log.info(
                        "credential revoked caller_id=%s credential_id=%s",
                        opts.caller,
                        opts.credential,
                    )
                else:
                    count = await revoke_credentials(sf, opts.caller, now)
                    # nosemgrep: python-logger-credential-disclosure -- a count, no secret
                    log.info("all credentials revoked caller_id=%s count=%s", opts.caller, count)
                return
            if component == "status":
                report = await ops_status(sf, datetime.now(UTC))
                print(json.dumps(report, sort_keys=True))
                if fail_on_alert and report["alerts"]:
                    raise SystemExit(2)
                return
            if component == "cleanup" and forever:
                # Deployment mode: one long-running process sweeping every
                # purge_sweep_interval, and rebuilding the statistics snapshot every
                # stats_refresh_seconds; a failed run is logged and retried next interval.
                await asyncio.gather(
                    cleanup_forever(repo, stop, interval=policy.purge_sweep_interval),
                    refresh_forever(
                        sf, stop, interval=timedelta(seconds=settings.stats_refresh_seconds)
                    ),
                )
                return
            if component == "stats-refresh":
                done = await refresh_stats(sf, datetime.now(UTC))
                log.info("statistics refresh %s", "done" if done else "skipped: another runs")
                return
            if component == "cleanup":
                stats = await run_cleanup(repo, datetime.now(UTC))
                if not stats.complete:
                    raise RuntimeError("cleanup batch budget exhausted; rerun required")
                return
            if component in ("worker", "receipts"):
                missing = await missing_key_versions(
                    repo, secrets.cipher, secrets.fingerprinter, datetime.now(UTC)
                )
                if missing:
                    # Version names only; never key material.
                    log.error("key versions in use but not configured: %s", ",".join(missing))
                    raise RuntimeError("key check failed")
            if component == "receipts":
                pins = path_list("OAN_MSG_RECEIPTS_PINNED_CERTS")
                cas = path_list("OAN_MSG_RECEIPTS_CLIENT_CA_CERTS")
                if not pins and not cas:
                    raise ValueError(
                        "required setting missing: OAN_MSG_RECEIPTS_CLIENT_CA_CERTS "
                        "or OAN_MSG_RECEIPTS_PINNED_CERTS"
                    )
                callback_secrets = [
                    required("OAN_MSG_RECEIPTS_CALLBACK_SECRET"),
                    os.environ.get("OAN_MSG_RECEIPTS_CALLBACK_SECRET_PREVIOUS", ""),
                ]
                install_redaction(*[s for s in callback_secrets if s])
                ssl_context = build_server_ssl_context(
                    Path(required("OAN_MSG_RECEIPTS_SERVER_CERT")),
                    Path(required("OAN_MSG_RECEIPTS_SERVER_KEY")),
                    pins,
                    cas,
                )
                processor = ReceiptProcessor(
                    repo,
                    load_pins(pins) if pins else frozenset(),
                    trust_ca=bool(cas),
                    callback_secrets=callback_secrets,
                    path=os.environ.get("OAN_MSG_RECEIPTS_PATH") or DEFAULT_RECEIPTS_PATH,
                )
                server = CallbackServer(processor, ssl_context)
                await server.start("0.0.0.0", int(os.environ.get("OAN_MSG_RECEIPTS_PORT", "8443")))  # noqa: S104 (container ingress)
                stack.push_async_callback(server.close)
                await stop.wait()
                return
            enabled = {Channel(v.strip()) for v in required("OAN_MSG_WORKER_CHANNELS").split(",")}
            adapters: dict[Channel, MessageAdapter] = {}
            if Channel.SMS in enabled:
                sms_settings = Sms8028Settings.from_environ(os.environ)
                client = await stack.enter_async_context(sms_client(sms_settings))
                adapters[Channel.SMS] = Sms8028Adapter(sms_settings, client)
            if Channel.TELEGRAM in enabled:
                registry = TelegramBotRegistry.from_environ(os.environ)
                async with sf() as session:
                    mapped = (
                        (
                            await session.execute(
                                select(callers.c.telegram_bot).where(
                                    callers.c.enabled, callers.c.allowed_channels.any("telegram")
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                if not registry.keys() or any(bot not in registry.keys() for bot in mapped):
                    raise ValueError("enabled Telegram caller missing worker bot token")
                install_redaction(*registry.secrets())
                client = await stack.enter_async_context(telegram_client())
                adapters[Channel.TELEGRAM] = TelegramAdapter(registry, client)
            limiter = SharedRateLimiter(
                engine,
                {
                    Channel.SMS: RateLimit(
                        requests=settings.sms_rate_per_second,
                        otp_reserve=settings.sms_otp_reserve,
                    ),
                    Channel.TELEGRAM: TELEGRAM_DEFAULT,
                },
            )
            stack.push_async_callback(limiter.aclose)
            # Start-up latency guard (remediation plan §3.3): refuse to run when the
            # database round trip cannot fit the dispatch window; otherwise every start
            # would be late and nothing would be sent.
            latency = await measure_dispatch_latency(engine.connect, limiter.dispatch_tolerance)
            log.info(
                "dispatch latency p95_ms=%.1f estimate_ms=%.1f tolerance_ms=%.0f",
                latency.p95.total_seconds() * 1000,
                latency.estimate.total_seconds() * 1000,
                latency.tolerance.total_seconds() * 1000,
            )
            if not latency.fits:
                log.error("database round trip too slow for the dispatch window; not starting")
                raise RuntimeError("dispatch latency guard")
            worker = Worker(
                repo,
                adapters,
                limiter,
                WorkerConfig(
                    lease=policy.worker_lease,
                    broadcast_enabled=settings.broadcast_enabled,
                    broadcast_max_wait=timedelta(seconds=settings.broadcast_max_wait_seconds),
                ),
            )
            heartbeat = heartbeat_path()
            touch_heartbeat(heartbeat)
            log.info("worker started channels=%s", ",".join(sorted(c.value for c in adapters)))
            await worker.run_forever(stop, on_cycle=lambda: touch_heartbeat(heartbeat))
    finally:
        await engine.dispose()


COMPONENTS = [
    "worker",
    "receipts",
    "cleanup",
    "stats-refresh",
    "suspend",
    "resume",
    "status",
    "check-heartbeat",
    "caller",
    "issue-credential",
    "list-credentials",
    "revoke-credentials",
]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=COMPONENTS)
    parser.add_argument("--channel", choices=["sms", "telegram"])
    parser.add_argument("--fail-on-alert", action="store_true")
    parser.add_argument("--max-age", type=float, default=90.0)
    parser.add_argument("--forever", action="store_true", help="cleanup: run continuously")
    parser.add_argument("--caller", help="caller id (caller, issue-credential, revoke)")
    parser.add_argument("--channels", default="sms", help="caller: comma-separated channels")
    parser.add_argument("--purposes", default="", help="caller: comma-separated purposes")
    parser.add_argument("--bot", help="caller: Telegram bot key")
    parser.add_argument("--disable", action="store_true", help="caller: disable the caller")
    parser.add_argument(
        "--stats-reader",
        action="store_true",
        help="caller: a read-only statistics reader (GET /api/v1/charts); cannot send",
    )
    parser.add_argument("--credential", help="revoke-credentials: the one credential id")
    parser.add_argument("--all", action="store_true", help="revoke-credentials: every token")
    args = parser.parse_args(argv)
    if args.component in ("suspend", "resume") and not args.channel:
        parser.error("--channel is required")
    if (
        args.component in ("caller", "issue-credential", "list-credentials", "revoke-credentials")
        and not args.caller
    ):
        parser.error("--caller is required")
    if args.component == "revoke-credentials" and bool(args.credential) == bool(args.all):
        parser.error("revoke-credentials needs exactly one of --credential ID or --all")
    if args.component == "caller" and not args.purposes and not args.stats_reader:
        parser.error("--purposes is required")
    if args.component == "caller" and args.stats_reader and args.purposes:
        parser.error("a --stats-reader has no --purposes (it cannot send)")
    if args.component == "check-heartbeat":
        # Liveness probe: no settings, no database. Exit 1 if the worker loop has not
        # completed a cycle within --max-age seconds.
        age = heartbeat_age(heartbeat_path())
        if age is None or age > args.max_age:
            print("worker heartbeat stale", file=sys.stderr)
            raise SystemExit(1)
        return
    install_redaction()
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(
            run(
                args.component,
                args.channel,
                fail_on_alert=args.fail_on_alert,
                forever=args.forever,
                opts=args,
            )
        )
    except (ValueError, RuntimeError, OSError, SQLAlchemyError) as exc:
        # Startup diagnostics never dump configuration values or exception locals.
        log.error("component failed error_type=%s", type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
