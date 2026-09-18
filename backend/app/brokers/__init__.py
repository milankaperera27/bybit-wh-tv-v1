"""Outbound venue adapters (LAYER 3) + the venue registry."""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.brokers.base import BrokerClient, BrokerError
from app.brokers.kraken_client import KrakenClient, kraken_sign
from app.brokers.tradovate_client import TradovateClient

__all__ = [
    "BrokerClient",
    "BrokerError",
    "KrakenClient",
    "TradovateClient",
    "kraken_sign",
    "get_broker",
    "all_brokers",
    "set_broker",
    "reset_brokers",
]

_registry: Dict[str, BrokerClient] = {}


def get_broker(venue: str) -> BrokerClient:
    """Return the (lazily constructed, process-wide) client for ``venue``."""
    key = str(getattr(venue, "value", venue)).upper()
    existing = _registry.get(key)
    if existing is not None:
        return existing
    if key == "TRADOVATE":
        client: BrokerClient = TradovateClient()
    elif key == "KRAKEN":
        client = KrakenClient()
    else:
        raise BrokerError(key, f"unknown venue {key!r}")
    _registry[key] = client
    return client


def set_broker(venue: str, client: Optional[BrokerClient]) -> None:
    """Inject (or clear) a client — used by the tests' spy doubles."""
    key = str(getattr(venue, "value", venue)).upper()
    if client is None:
        _registry.pop(key, None)
    else:
        _registry[key] = client


def all_brokers() -> Dict[str, BrokerClient]:
    """Both venues, constructing whichever is missing."""
    return {"TRADOVATE": get_broker("TRADOVATE"), "KRAKEN": get_broker("KRAKEN")}


async def reset_brokers() -> None:
    """Close and drop every cached client (shutdown / test teardown)."""
    for client in list(_registry.values()):
        try:
            await client.aclose()
        except Exception:  # pragma: no cover
            pass
    _registry.clear()


def _unused(_: Any) -> None:  # pragma: no cover - keeps linters quiet on Any import
    return None
