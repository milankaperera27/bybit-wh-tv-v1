"""Outbound broker adapter contract (LAYER 3)."""

from __future__ import annotations

import abc
from typing import Any, Dict, Optional


class BrokerError(RuntimeError):
    """Raised when a venue rejects a request or the transport fails."""

    def __init__(self, venue: str, message: str, payload: Optional[Dict[str, Any]] = None):
        self.venue = venue
        self.payload = payload or {}
        super().__init__(f"[{venue}] {message}")


class BrokerClient(abc.ABC):
    """Every venue bridge implements this surface.

    In mock mode (``ATLAS_MOCK_BROKERS=true``) implementations still build and
    sign a byte-identical real payload — only the network call is skipped.
    """

    venue: str = "UNKNOWN"

    @abc.abstractmethod
    async def place_bracket_order(
        self,
        *,
        symbol: str,
        action: str,
        qty: float,
        limit_price: float,
        stop_loss: float,
        take_profit: float,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Submit an entry with exchange-side protective legs."""

    @abc.abstractmethod
    async def cancel_all(self, **kwargs: Any) -> Dict[str, Any]:
        """Cancel every working order on the venue."""

    @abc.abstractmethod
    async def flatten_all(self, **kwargs: Any) -> Dict[str, Any]:
        """Close every open position on the venue."""

    @abc.abstractmethod
    async def get_account(self, **kwargs: Any) -> Dict[str, Any]:
        """Account snapshot (equity, balances, open position count)."""

    async def aclose(self) -> None:
        """Release transport resources.  Default: nothing to do."""
        return None
