"""Wiring: create the sandbox on disk and open a service over it."""

from __future__ import annotations

import shutil
from pathlib import Path

from .config import Settings
from .domain.approvals import Signer, Verifier, ensure_keys
from .domain.db import connect, init_schema
from .domain.seed import seed
from .domain.service import PaymentsService


def create_sandbox(settings: Settings, *, force: bool = False) -> dict[str, int]:
    """Create keys and a freshly seeded database under settings.home."""
    settings.home.mkdir(parents=True, exist_ok=True)
    ensure_keys(settings.keys_dir)
    if settings.db_path.exists():
        if not force:
            raise FileExistsError(f"{settings.db_path} exists; pass force=True to recreate it")
        settings.db_path.unlink()
    conn = connect(settings.db_path)
    try:
        return seed(conn, settings)
    finally:
        conn.close()


def clone_sandbox(template: Settings, target_home: Path) -> Settings:
    """Copy a seeded sandbox (database and keys) to a new home. Used by evals."""
    target_home.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template.db_path, target_home / "payments.db")
    shutil.copytree(template.keys_dir, target_home / "keys", dirs_exist_ok=True)
    return Settings(
        home=target_home,
        now=template.now,
        auto_approve_limit_minor=template.auto_approve_limit_minor,
        max_refund_minor=template.max_refund_minor,
        daily_refund_limit_minor=template.daily_refund_limit_minor,
        dispute_window_days=template.dispute_window_days,
        actor=template.actor,
    )


def open_service(settings: Settings) -> PaymentsService:
    """Open the service as the MCP server sees it: public keys only, plus the
    policy signer when (and only when) an auto-approve limit is configured."""
    if not settings.db_path.exists():
        raise FileNotFoundError(f"no sandbox at {settings.db_path}; run `make seed` first")
    conn = connect(settings.db_path)
    init_schema(conn)
    policy = (
        Signer.load(settings.keys_dir, "policy") if settings.auto_approve_limit_minor > 0 else None
    )
    return PaymentsService(conn, settings, Verifier.load(settings.keys_dir), policy)
