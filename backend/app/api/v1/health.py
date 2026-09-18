"""Liveness / readiness.

A dependency outage reports ``status: "degraded"`` with HTTP 200 — never a 500 —
so an orchestrator can distinguish "process is wedged" from "Redis is down".
"""

from __future__ import annotations

from fastapi import APIRouter

from app import __version__
from app.core import db, redis_client
from app.core.config import get_settings
from app.models.schemas import HealthStatus

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthStatus)
async def health() -> HealthStatus:
    settings = get_settings()
    redis_ok = await redis_client.ping()
    postgres_ok = await db.ping()
    return HealthStatus(
        status="ok" if (redis_ok and postgres_ok) else "degraded",
        version=__version__,
        env=settings.env,
        mock_brokers=settings.is_mock,
        redis="up" if redis_ok else "down",
        postgres="up" if postgres_ok else "down",
    )


@router.get("/health/live")
async def liveness() -> dict:
    """Process-only probe — always 200 while the event loop is turning."""
    return {"status": "alive"}


@router.get("/health/ready")
async def readiness() -> dict:
    """Ready once Redis (the HITL hold store) is reachable."""
    redis_ok = await redis_client.ping()
    return {"status": "ready" if redis_ok else "not_ready", "redis": redis_ok}
