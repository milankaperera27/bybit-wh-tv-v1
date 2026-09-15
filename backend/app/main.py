"""ATLAS FastAPI gateway & WebSocket hub (LAYER 4).

* app factory + lifespan (Redis pool and DB engine up/down)
* every v1 router mounted under ``/api/v1``
* CORS for the Capacitor/PWA client
* ``WS /ws/dashboard`` broadcasting the live regime + pending setups
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Set

from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.v1 import api_router
from app.core import db, redis_client
from app.core.config import get_settings

logger = logging.getLogger(__name__)

#: How often the hub pushes an unsolicited snapshot.
DASHBOARD_SNAPSHOT_INTERVAL_SECONDS = 5.0


class DashboardHub:
    """Fan-out for ``/ws/dashboard``.

    Broadcasts are best-effort: a dead socket is dropped silently rather than
    propagating an exception back into a webhook or confirmation handler.
    """

    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._clients.add(websocket)
        logger.info("dashboard client connected (%d total)", len(self._clients))

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(websocket)

    async def broadcast(self, frame: Dict[str, Any]) -> int:
        """Send one JSON frame to every connected client; returns the fan-out."""
        if not self._clients:
            return 0
        payload = json.dumps(frame, default=str)
        async with self._lock:
            targets = list(self._clients)
        delivered = 0
        for client in targets:
            try:
                await client.send_text(payload)
                delivered += 1
            except Exception:
                await self.disconnect(client)
        return delivered

    async def snapshot(self) -> Dict[str, Any]:
        """Current regime + live pending setups + kill-switch flag."""
        pending: List[Dict[str, Any]] = []
        regime = None
        kill = False
        try:
            pending = await redis_client.list_pending_setups()
            regime_doc = await redis_client.get_current_regime()
            regime = (regime_doc or {}).get("regime")
            kill = await redis_client.is_kill_switch_engaged()
        except Exception:
            logger.debug("dashboard snapshot degraded", exc_info=True)
        return {
            "type": "snapshot",
            "regime": regime,
            "pending": pending,
            "kill_switch": kill,
            "emitted_at": datetime.now(timezone.utc).isoformat(),
        }


#: Module-level singleton so routers can `from app.main import dashboard_hub`.
dashboard_hub = DashboardHub()

ws_router = APIRouter()


@ws_router.websocket("/ws/dashboard")
async def dashboard_socket(websocket: WebSocket) -> None:
    """Live regime + pending-setup stream for the PWA."""
    await dashboard_hub.connect(websocket)
    try:
        await websocket.send_text(json.dumps(await dashboard_hub.snapshot(), default=str))
        while True:
            try:
                message = await asyncio.wait_for(
                    websocket.receive_text(), timeout=DASHBOARD_SNAPSHOT_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                await websocket.send_text(
                    json.dumps(await dashboard_hub.snapshot(), default=str)
                )
                continue
            if message.strip().lower() in {"ping", '"ping"'}:
                await websocket.send_text(json.dumps({"type": "pong"}))
            else:
                await websocket.send_text(
                    json.dumps(await dashboard_hub.snapshot(), default=str)
                )
    except WebSocketDisconnect:
        pass
    except Exception:  # pragma: no cover - transport level
        logger.debug("dashboard socket error", exc_info=True)
    finally:
        await dashboard_hub.disconnect(websocket)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Bring the Redis pool and the DB engine up, then tear them down."""
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s :: %(message)s",
    )
    logger.info(
        "ATLAS backend %s starting (env=%s, mock_brokers=%s)",
        __version__,
        settings.env,
        settings.is_mock,
    )

    try:
        await redis_client.init_redis()
        if not await redis_client.ping():
            logger.warning("Redis unreachable at startup; HITL holds will fail")
    except Exception:
        logger.warning("Redis pool init failed; continuing degraded", exc_info=True)

    try:
        await db.init_db()
        if not await db.ping():
            logger.warning("PostgreSQL unreachable at startup; audit ledger disabled")
    except Exception:
        logger.warning("DB engine init failed; continuing degraded", exc_info=True)

    try:
        yield
    finally:
        logger.info("ATLAS backend shutting down")
        from app.brokers import reset_brokers

        with contextlib.suppress(Exception):
            await reset_brokers()
        with contextlib.suppress(Exception):
            await redis_client.close_redis()
        with contextlib.suppress(Exception):
            await db.close_db()


def create_app() -> FastAPI:
    """Application factory."""
    settings = get_settings()

    app = FastAPI(
        title="ATLAS Execution Middleware",
        description=(
            "TradingView webhook ingest, HITL 1-tap confirmation state machine, "
            "Tradovate + Kraken execution bridges, and the universal kill switch."
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins or ["*"],
        allow_origin_regex=r"^(capacitor|ionic)://.*$",
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Atlas-Signature"],
    )

    app.include_router(api_router, prefix="/api/v1")
    app.include_router(ws_router)

    @app.get("/", tags=["meta"])
    async def root() -> Dict[str, Any]:
        return {
            "service": "atlas-backend",
            "version": __version__,
            "env": settings.env,
            "mock_brokers": settings.is_mock,
            "api": "/api/v1",
            "docs": "/docs",
            "websocket": "/ws/dashboard",
        }

    return app


app = create_app()
