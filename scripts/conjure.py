#!/usr/bin/env python3
"""conjure — a synthetic TradingView alert generator for local development.

Fires realistic, spec-shaped signals at the running backend so the dashboard
always has live confirmation cards to look at.  Nothing here touches a broker:
the backend must be running with ``ATLAS_MOCK_BROKERS=true``.

Usage
-----
    python scripts/conjure.py                       # one signal every 20s, forever
    python scripts/conjure.py --once                # a single signal
    python scripts/conjure.py --count 3 --interval 5
    python scripts/conjure.py --sign                # authenticate by HMAC header
                                                    # instead of the in-body token

Environment
-----------
    ATLAS_API_BASE            default http://localhost:8000
    ATLAS_WEBHOOK_TOKEN       default "devtoken"
    ATLAS_WEBHOOK_HMAC_SECRET only needed with --sign
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Tuple

API_BASE = os.environ.get("ATLAS_API_BASE", "http://localhost:8000").rstrip("/")
WEBHOOK_URL = f"{API_BASE}/api/v1/webhooks/tradingview"
TOKEN = os.environ.get("ATLAS_WEBHOOK_TOKEN", "devtoken")
HMAC_SECRET = os.environ.get("ATLAS_WEBHOOK_HMAC_SECRET", "")

# (symbol, venue, reference price, tick) — CME futures and Kraken margin pairs.
INSTRUMENTS: Tuple[Tuple[str, str, float, float], ...] = (
    ("NQ1!",   "TRADOVATE", 20_155.25, 0.25),
    ("MNQ1!",  "TRADOVATE", 20_155.25, 0.25),
    ("ES1!",   "TRADOVATE",  5_905.25, 0.25),
    ("MES1!",  "TRADOVATE",  5_905.25, 0.25),
    ("CL1!",   "TRADOVATE",     78.42, 0.01),
    ("GC1!",   "TRADOVATE",  2_655.40, 0.10),
    ("XBTUSD", "KRAKEN",    37_500.00, 0.10),
    ("ETHUSD", "KRAKEN",     2_480.00, 0.01),
)

BULL_REGIMES = ("BULL_TREND_EXPANSION", "RANGE_COMPRESSION")
BEAR_REGIMES = ("BEAR_TREND_EXPANSION", "RANGE_COMPRESSION")


def round_to_tick(value: float, tick: float) -> float:
    return round(round(value / tick) * tick, 8)


def build_signal() -> Dict[str, Any]:
    """A payload matching FROZEN CONTRACT A, with 1.5R / 3.0R targets."""
    symbol, venue, reference, tick = random.choice(INSTRUMENTS)
    action = random.choice(("BUY", "SELL"))

    # Drift the entry a little so successive cards are visibly different.
    entry = round_to_tick(reference * random.uniform(0.997, 1.003), tick)
    # Stop distance ~0.05%-0.25% of price, the usual intraday shape.
    risk_distance = max(tick * 4, round_to_tick(entry * random.uniform(0.0005, 0.0025), tick))

    if action == "BUY":
        stop = round_to_tick(entry - risk_distance, tick)
        tp1 = round_to_tick(entry + risk_distance * 1.5, tick)
        tp2 = round_to_tick(entry + risk_distance * 3.0, tick)
        regime = random.choice(BULL_REGIMES)
    else:
        stop = round_to_tick(entry + risk_distance, tick)
        tp1 = round_to_tick(entry - risk_distance * 1.5, tick)
        tp2 = round_to_tick(entry - risk_distance * 3.0, tick)
        regime = random.choice(BEAR_REGIMES)

    return {
        "token": TOKEN,
        "symbol": symbol,
        "venue": venue,
        "action": action,
        "limit_price": entry,
        "stop_loss": stop,
        "take_profit_1": tp1,
        "take_profit_2": tp2,
        "regime": regime,
        "risk_pct": round(random.choice((0.5, 1.0, 1.0, 1.25, 1.5, 2.0)), 2),
    }


def send(signal: Dict[str, Any], sign: bool = False) -> Tuple[int, str]:
    body = json.dumps(signal).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if sign:
        if not HMAC_SECRET:
            sys.exit("--sign needs ATLAS_WEBHOOK_HMAC_SECRET in the environment")
        digest = hmac.new(HMAC_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-Atlas-Signature"] = f"sha256={digest}"

    request = urllib.request.Request(WEBHOOK_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")
    except urllib.error.URLError as exc:
        return 0, f"unreachable: {exc.reason}"


def describe(signal: Dict[str, Any], status: int, body: str) -> str:
    head = (
        f"{signal['action']:4} {signal['symbol']:<7} {signal['venue']:<9} "
        f"@ {signal['limit_price']:<10} sl {signal['stop_loss']:<10} "
        f"risk {signal['risk_pct']}%"
    )
    if status == 200:
        try:
            return f"  200  {head}  -> {json.loads(body)['signal_id'][:8]}"
        except Exception:
            return f"  200  {head}"
    return f"  {status or 'ERR':<4} {head}  -> {body[:90]}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Conjure synthetic ATLAS trading signals.")
    parser.add_argument("--interval", type=float, default=20.0, help="seconds between signals")
    parser.add_argument("--count", type=int, default=0, help="stop after N signals (0 = forever)")
    parser.add_argument("--once", action="store_true", help="send exactly one signal")
    parser.add_argument("--sign", action="store_true", help="authenticate with the HMAC header")
    parser.add_argument("--seed", type=int, default=None, help="seed the RNG for reproducible runs")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    limit = 1 if args.once else args.count
    auth = "hmac header" if args.sign else "body token"
    print(f"conjure -> {WEBHOOK_URL}  (auth: {auth}, interval: {args.interval}s)", flush=True)

    sent = 0
    try:
        while limit == 0 or sent < limit:
            signal = build_signal()
            status, body = send(signal, sign=args.sign)
            print(describe(signal, status, body), flush=True)
            if status == 0:
                print("  backend unreachable — is uvicorn running?", flush=True)
            sent += 1
            if limit and sent >= limit:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\nstopped after {sent} signal(s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
