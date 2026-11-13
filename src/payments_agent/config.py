"""Runtime settings, read from environment variables with safe defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

# The sandbox runs on a fixed clock so that seeded data, dispute windows and
# daily limits behave the same on every machine and in CI.
DEFAULT_SANDBOX_NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Settings:
    home: Path
    now: datetime = DEFAULT_SANDBOX_NOW
    # Refunds at or below this amount may be approved by the policy key.
    # 0 disables policy approval: every refund waits for a human.
    auto_approve_limit_minor: int = 0
    # Hard ceiling for a single refund, regardless of approval.
    max_refund_minor: int = 100_000
    # Ceiling for refunds requested per customer per sandbox day.
    daily_refund_limit_minor: int = 150_000
    dispute_window_days: int = 120
    # Identity recorded in the audit log for calls made through the MCP server.
    actor: str = "agent:mcp"

    @property
    def db_path(self) -> Path:
        return self.home / "payments.db"

    @property
    def keys_dir(self) -> Path:
        return self.home / "keys"

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"

    @classmethod
    def from_env(cls, home: Path | None = None) -> Settings:
        base = home or Path(os.environ.get("PAYMENTS_HOME", ".sandbox"))
        settings = cls(home=base.resolve())
        overrides: dict[str, int | str] = {}
        for env, field in (
            ("PAYMENTS_AUTO_APPROVE_LIMIT_MINOR", "auto_approve_limit_minor"),
            ("PAYMENTS_MAX_REFUND_MINOR", "max_refund_minor"),
            ("PAYMENTS_DAILY_REFUND_LIMIT_MINOR", "daily_refund_limit_minor"),
        ):
            raw = os.environ.get(env)
            if raw:
                overrides[field] = int(raw)
        actor = os.environ.get("PAYMENTS_ACTOR")
        if actor:
            overrides["actor"] = actor
        return replace(settings, **overrides)  # type: ignore[arg-type]
