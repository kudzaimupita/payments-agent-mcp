from __future__ import annotations

from pathlib import Path

import pytest

from payments_agent.config import Settings
from payments_agent.domain.approvals import Signer
from payments_agent.domain.models import RefundRequest
from payments_agent.domain.service import PaymentsService
from payments_agent.runtime import create_sandbox, open_service


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings(home=tmp_path / "sandbox")
    create_sandbox(s)
    return s


@pytest.fixture
def service(settings: Settings) -> PaymentsService:
    svc = open_service(settings)
    yield svc
    svc.conn.close()


@pytest.fixture
def human(settings: Settings) -> Signer:
    return Signer.load(settings.keys_dir, "human")


def refund(txn: str, amount: int, key: str = "key-00001", reason: str = "test refund"):
    return RefundRequest(
        transaction_id=txn, amount_minor=amount, reason=reason, idempotency_key=key
    )
