# ATLAS — Pine Script v6 Indicator Suite (LAYER 6)

Three native **Pine Script v6** scripts (`//@version=6`). They are the signal
producers of the ATLAS stack and the only component that talks to
`POST /api/v1/webhooks/tradingview` (FROZEN CONTRACT B).

| File | Role | Draws | Emits webhooks |
|------|------|-------|----------------|
| `Atlas_SMC_Core.pine` | Smart Money Concepts engine — swings, MSS/BOS, FVG + mitigation, order blocks, liquidity sweeps | yes | no |
| `Atlas_Regime_Filter.pine` | Regime classifier + multi-timeframe radar (5m / 1h / D) | table + EMAs | no |
| `Atlas_Execution_Suite.pine` | **Production script.** Merges the two above and carries the webhook dispatcher | yes | **yes** |

Run the two modular scripts while tuning/inspecting; run **only**
`Atlas_Execution_Suite.pine` on the chart that holds the live alert.

---

## 1. Installing a script on TradingView

1. Open the chart for the instrument you want to trade (e.g. `CME_MINI:NQ1!`
   or `KRAKEN:BTCUSD`) on the timeframe the strategy was validated on (the
   spec's reference DSL uses `5m`).
2. `Pine Editor` (bottom panel) → `Open` → `New indicator…`.
3. Select all the boilerplate and paste the full contents of the `.pine` file.
4. `Save` → give it the same name as the file → `Add to chart`.
5. Repeat per script. TradingView's free tier allows 2 indicators per chart;
   the Execution Suite alone is sufficient for production.

The scripts declare `max_boxes_count = 500, max_lines_count = 500`. All drawing
objects are pooled and stale ones are deleted, so the caps are never hit.

---

## 2. Inputs to set

### `Atlas_Execution_Suite.pine` — **General Setup** (frozen by spec §3.1)

| Input | Default | Set it to |
|-------|---------|-----------|
| `Target Venue` | `TRADOVATE` | `TRADOVATE` for CME futures (ES/NQ/MES/MNQ/CL/GC), `KRAKEN` for margin crypto (BTC/USD, ETH/USD). Becomes the `venue` field. |
| `Risk Per Trade (%)` | `1.0` | 0.1 – 5.0. Becomes the `risk_pct` field; the backend risk engine sizes from it. |
| `Webhook Token` | `SECRET_TOKEN` | **Must equal the backend's `ATLAS_WEBHOOK_TOKEN`** (see `.env.example`). Becomes the `token` field. |

### **Smart Money Concepts** (frozen)

| Input | Default | Notes |
|-------|---------|-------|
| `Enable SMC Engine` | `true` | Master switch for structure/FVG/OB/liquidity drawing. |
| `Swing Pivot Length` | `5` | Bars either side of a pivot. Higher = fewer, more significant swings. |
| `Show Fair Value Gaps` | `true` | **Also gates signal generation** — `bull_fvg` / `bear_fvg` are the entry trigger, so turning this off silences all alerts. |

### **Market Regime Filter** (frozen)

| Input | Default | Notes |
|-------|---------|-------|
| `ADX Length` | `14` | Feeds `ta.dmi(adx_len, adx_len)`. |
| `ADX Trend Threshold` | `22` | Above ⇒ trend expansion, at/below + no ATR expansion ⇒ range compression. |

### Extended inputs (additive; safe to leave at defaults)

`Smart Money Concepts — Extended` (FVG box length, 50% midline, mitigation mode,
order-block lookback/extension, liquidity sweep reference & session window),
`Multi-Timeframe Radar` (5 / 60 / D), `Execution & Display`
(`Enable Webhook Dispatch`, SL/TP level lines, info panel, colours).

Set `Enable Webhook Dispatch (alert())` to `false` while backtesting visually so
a running alert cannot fire.

---

## 3. Creating the alert

These scripts are **indicators**, not strategies, and they call Pine's
`alert()` built-in:

```pine
if enable_alerts and fire_buy
    alert(payload("BUY", sl, tp1, tp2), alert.freq_once_per_bar_close)
```

**Why this matters for the Message box.** There are two different TradingView
mechanisms and they take different message bodies:

| Mechanism | Message box content | Applies here |
|-----------|--------------------|--------------|
| `strategy.entry(..., alert_message = ...)` on a **strategy** | `{{strategy.order.alert_message}}` — a placeholder that TradingView substitutes | ❌ not used — ATLAS ships indicators |
| `alert(msg, freq)` inside an **indicator** | *whatever you type is ignored* — the string passed to `alert()` **is** the delivered message | ✅ this is the path |
| `alertcondition(cond, title, message)` | the `message` string, editable in the dialog, with `{{…}}` placeholders substituted | ✅ optional UI path (§3.2) |

### 3.1 Recommended: the `alert()` path

1. Right-click the chart → `Add alert`, or press `Alt+A`.
2. **Condition**: pick `Atlas SMC & Regime Execution Suite [v6]` and, in the
   second dropdown, choose **`Any alert() function call`**.
3. **Trigger**: `Once Per Bar Close` (the script already passes
   `alert.freq_once_per_bar_close`, but keep the dialog consistent).
4. **Expiration**: set "Open-ended" if your plan allows it.
5. **Notifications → Webhook URL**, tick it and enter:

   ```
   https://<host>/api/v1/webhooks/tradingview
   ```

   (local dev: `http://localhost:8000/api/v1/webhooks/tradingview` — note
   TradingView's cloud alert servers cannot reach `localhost`; use a tunnel.)
6. **Leave the Message box alone.** With `Any alert() function call` the body
   TradingView POSTs is exactly the string built by `payload()`. Do **not** put
   `{{strategy.order.alert_message}}` there — that placeholder belongs to
   strategies and would be sent literally.
7. `Create`.

One alert covers both directions; `payload()` sets `"action"` to `BUY` or
`SELL` per signal.

### 3.2 Optional: the `alertcondition()` path (wired from the UI)

The Suite also exposes six `alertcondition()` entries so the contract can be
assembled by TradingView's own placeholder engine:

* `ATLAS — BUY (BULL_TREND_EXPANSION)`
* `ATLAS — BUY (RANGE_COMPRESSION)`
* `ATLAS — SELL (BEAR_TREND_EXPANSION)`
* `ATLAS — SELL (RANGE_COMPRESSION)`
* `ATLAS — Any BUY Signal` / `ATLAS — Any SELL Signal` (plain text, for phone
  pushes rather than the webhook)

The regime is split across four conditions because `alertcondition()` requires a
**const** message — a runtime string cannot be concatenated into it. Each of the
four default messages is the full frozen JSON with the numeric fields supplied by
`{{plot("limit_price")}}`, `{{plot("stop_loss")}}`, `{{plot("take_profit_1")}}`,
`{{plot("take_profit_2")}}` and `{{plot("risk_pct")}}` (those plot titles exist
in the script's Data Window).

Before saving such an alert you must edit two literals in the Message box:

* replace `__ATLAS_WEBHOOK_TOKEN__` with your real token, and
* change `"venue":"TRADOVATE"` to `"venue":"KRAKEN"` if you are routing crypto.

Because of that manual step, **§3.1 is the supported production path**; §3.2 is a
fallback for users who prefer stock TradingView alert wiring. You need one alert
per condition if you use this path.

`Atlas_SMC_Core.pine` and `Atlas_Regime_Filter.pine` expose plain-text
`alertcondition()` entries only (MSS/BOS/FVG/sweep events, regime transitions).
They never emit the webhook JSON — that is the Suite's job alone.

---

## 4. Worked example of the emitted JSON

Chart `CME_MINI:NQ1!`, 5m. `Target Venue = TRADOVATE`, `Risk Per Trade = 1.0`,
`Webhook Token = SECRET_TOKEN`. Regime is `BULL_TREND_EXPANSION`, a bullish FVG
prints and `close > ema_fast`:

```
close      = 20155.25          -> limit_price
low[1]     = 20147.50
atr_val    = 15.00
sl         = low[1] - atr*0.5  = 20147.50 - 7.50 = 20140.00
risk_dist  = |close - sl|      = 15.25
tp1        = close + 1.5R      = 20155.25 + 22.875 = 20178.125
tp2        = close + 3.0R      = 20155.25 + 45.750 = 20201.00
```

The body POSTed to `/api/v1/webhooks/tradingview` is a single line:

```json
{"token":"SECRET_TOKEN","symbol":"NQ1!","venue":"TRADOVATE","action":"BUY","limit_price":20155.25,"stop_loss":20140,"take_profit_1":20178.12,"take_profit_2":20201,"regime":"BULL_TREND_EXPANSION","risk_pct":1}
```

Pretty-printed, that is FROZEN CONTRACT A verbatim:

```json
{
  "token": "SECRET_TOKEN",
  "symbol": "NQ1!",
  "venue": "TRADOVATE",
  "action": "BUY",
  "limit_price": 20155.25,
  "stop_loss": 20140,
  "take_profit_1": 20178.12,
  "take_profit_2": 20201,
  "regime": "BULL_TREND_EXPANSION",
  "risk_pct": 1
}
```

### Number formatting note for the backend

The spec mandates `str.tostring(x, "#.##")` (and `"#.#"` for `risk_pct`). In that
DecimalFormat pattern `#` is an *optional* digit, so **trailing zeros are
dropped**: `20140.00` serialises as `20140`, `20201.00` as `20201`, and
`risk_pct 1.0` as `1`. These are still bare JSON numbers and parse to the same
floats — a `float` / `Decimal` field accepts them. Do not add a
`str.tostring`-level `0.00` pattern: the format string is contractual.
Rounding is half-even at 2 dp, which is why `20178.125` becomes `20178.12`,
exactly as in the artifact's reference payload.

`symbol` is `syminfo.ticker`, i.e. what TradingView shows without the exchange
prefix (`NQ1!`, `MES1!`, `BTCUSD`). The venue adapter maps it to the broker
symbol.

---

## 5. Token / auth

`webhook_token` in the Pine input **must equal `ATLAS_WEBHOOK_TOKEN`** in the
backend environment (`.env`, see `.env.example`). The backend does a
constant-time comparison of the in-body `token`.

Per FROZEN CONTRACT A, transport auth is either the
`X-Atlas-Signature: sha256=<hex hmac of raw body>` header **or** the in-body
`token`; **body HMAC wins when both are present**. TradingView's alert dialog
cannot add custom headers, so the in-body `token` is the mechanism these scripts
rely on. Treat the token as a secret: anyone who can replay the body can inject a
signal, which is precisely why the HITL 60-second confirmation step exists.

If you rotate `ATLAS_WEBHOOK_TOKEN`, update the `Webhook Token` input on every
chart running the Suite — alerts keep the settings they were created with, so an
alert created before the rotation keeps firing the old token until you recreate
it.

---

## 6. State exports (Data Window)

Every script publishes its state with `plot(..., display = display.data_window)`
so it is readable in the Data Window, usable in `{{plot("…")}}` alert
placeholders, and consumable by any downstream script.

`Atlas_SMC_Core.pine` — contract series: `bull_mss`, `bear_mss`, `bull_fvg`,
`bear_fvg`, `bull_ob`, `bear_ob`, `sweep_high`, `sweep_low`
(plus `bull_bos`, `bear_bos`, `fvg_mitigated`, `trend_dir`, `live_fvg_count`).

`Atlas_Regime_Filter.pine` — `regime_code`, `regime_code_confirmed`,
`regime_code_ltf`, `regime_code_mtf`, `regime_code_htf`, `mtf_alignment`, `adx`,
`di_plus`, `di_minus`, `atr`, `atr_sma50`, `atr_percentile`, `vol_expansion`,
`ema_fast`, `ema_slow`.

`Atlas_Execution_Suite.pine` — all of the above plus `limit_price`, `stop_loss`,
`take_profit_1`, `take_profit_2`, `risk_pct`, `signal_buy`, `signal_sell`.

Regime code mapping: `1 = BULL_TREND_EXPANSION`, `2 = BEAR_TREND_EXPANSION`,
`3 = RANGE_COMPRESSION`, `4 = VOLATILITY_CHOP`, `0 = NEUTRAL / warm-up`.

---

## 7. Signal logic reference

```
regime:      if   adx > adx_threshold and close > ema200 and ema50 > ema200 -> BULL_TREND_EXPANSION
             elif adx > adx_threshold and close < ema200 and ema50 < ema200 -> BEAR_TREND_EXPANSION
             elif adx <= adx_threshold and not (atr > sma(atr,50))          -> RANGE_COMPRESSION
             else                                                           -> VOLATILITY_CHOP

signal_buy  = (BULL_TREND_EXPANSION or RANGE_COMPRESSION) and bull_fvg and close > ema_fast
signal_sell = (BEAR_TREND_EXPANSION or RANGE_COMPRESSION) and bear_fvg and close < ema_fast

bull_fvg    = low  > high[2]
bear_fvg    = high < low[2]
sl          = buy ? low[1] - atr*0.5 : high[1] + atr*0.5
tp1         = entry ± 1.5 * |close - sl|
tp2         = entry ± 3.0 * |close - sl|
```

`BOS` vs `MSS`: a close through the last swing high/low is a **BOS**
(continuation) when the tracked leg already points that way, and an **MSS**
(structure shift) when it breaks against the prior leg.

An FVG is **mitigated** once price trades back through its 50% *consequent
encroachment* level — the box is greyed out (or deleted, per input) and its
dotted midline greys with it.

A **liquidity sweep** is the previous day's (or previous session's) high/low being
taken intrabar and then reclaimed on the close.

Higher-timeframe regimes use `request.security(..., lookahead = barmerge.lookahead_off)`
and return the previous *closed* HTF bar's state, so the radar never repaints.

---

## 8. Non-repainting checklist before going live

* Alert trigger is `Once Per Bar Close`.
* `Enable Webhook Dispatch (alert())` is on.
* `Show Fair Value Gaps` is on (it gates the entry trigger).
* `Webhook Token` matches `ATLAS_WEBHOOK_TOKEN`.
* `Target Venue` matches the instrument class on the chart.
* The webhook URL is https and publicly reachable by TradingView's alert servers.
