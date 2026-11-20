"""Alembic migrations against an EMPTY PostgreSQL database."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import psycopg
from alembic import command
from conftest import ROOT, _libpq, alembic_config, create_database, drop_database

from oan_messaging.contracts import ALLOWED_TRANSITIONS


def migration_module() -> ModuleType:
    path = ROOT / "migrations" / "versions" / "0001_initial_schema.py"
    spec = importlib.util.spec_from_file_location("m0001", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def tables(url: str) -> set[str]:
    with psycopg.connect(_libpq(url)) as conn:
        rows = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        return {r[0] for r in rows}


def test_upgrade_check_downgrade_upgrade_from_empty(admin_url: str) -> None:
    url = create_database(admin_url)
    try:
        assert tables(url) == set()
        cfg = alembic_config(url)
        command.upgrade(cfg, "head")
        assert tables(url) == {
            "alembic_version",
            "dispatch_controls",
            "callers",
            "api_credentials",
            "messages",
            "message_content",
            "message_attempts",
            "receipts",
            "retired_key_versions",
            "stats_daily",
            "stats_failures",
            "stats_campaigns",
            "stats_state",
        }
        command.check(cfg)  # raises if the models and the migrated schema differ
        with psycopg.connect(_libpq(url)) as conn:
            trig = conn.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal").fetchall()
            assert [t[0] for t in trig] == ["trg_messages_status_transition"]
            idx = conn.execute(
                "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_messages_live_binding'"
            ).fetchone()
            assert idx and "UNIQUE" in idx[0] and "binding_released_at IS NULL" in idx[0]
        command.downgrade(cfg, "base")
        assert tables(url) == {"alembic_version"}
        with psycopg.connect(_libpq(url)) as conn:
            assert conn.execute(
                "SELECT count(*) FROM pg_proc WHERE proname LIKE 'oan_msg%'"
            ).fetchone() == (0,)
        command.upgrade(cfg, "head")
        command.check(cfg)
    finally:
        drop_database(admin_url, url)


def test_trigger_transitions_equal_contract() -> None:
    mod = migration_module()
    expected = {(a.value, b.value) for a, targets in ALLOWED_TRANSITIONS.items() for b in targets}
    assert set(mod.TRANSITIONS) == expected
    assert len(mod.TRANSITIONS) == len(expected)


SOURCE_SUFFIXES = {".py", ".ini", ".mako"}


def migration_sources() -> list[Path]:
    """Hand-maintained text sources only; generated caches and binaries are excluded."""
    return sorted(
        p
        for p in (ROOT / "migrations").rglob("*")
        if p.is_file() and p.suffix in SOURCE_SUFFIXES and "__pycache__" not in p.parts
    )


def test_migration_sources_have_no_secrets_or_local_paths() -> None:
    sources = migration_sources()
    names = {p.relative_to(ROOT).as_posix() for p in sources}
    assert {
        "migrations/alembic.ini",
        "migrations/env.py",
        "migrations/script.py.mako",
        "migrations/versions/0001_initial_schema.py",
    } <= names
    for path in sources:
        body = path.read_text(encoding="utf-8")
        assert "/Users/" not in body and "/Volumes/" not in body
        assert "password" not in body.lower()


def test_source_scan_ignores_generated_bytecode() -> None:
    # Importing the migration module (as Alembic does) writes __pycache__/*.pyc next to it;
    # the scan must still pass and must not read those binary files.
    migration_module()
    import compileall

    compileall.compile_dir(str(ROOT / "migrations"), quiet=1)
    assert any((ROOT / "migrations").rglob("*.pyc"))
    assert all(p.suffix != ".pyc" for p in migration_sources())
    test_migration_sources_have_no_secrets_or_local_paths()
