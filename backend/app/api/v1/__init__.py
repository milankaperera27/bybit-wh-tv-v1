"""Aggregate v1 router — mounted at ``/api/v1`` by the app factory."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import confirmation, emergency, health, webhooks

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(webhooks.router)
api_router.include_router(confirmation.router)
api_router.include_router(emergency.router)

__all__ = ["api_router"]
