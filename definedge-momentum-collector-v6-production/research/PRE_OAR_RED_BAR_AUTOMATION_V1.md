# Pre-OAR Red-Bar Automation V1

Status: DRAFT IMPLEMENTATION SPEC

This document freezes the corrected Red-Bar automation workflow agreed during live forward testing on 2026-09-17.

## Source chart and levels

- Derive Red-Bar levels from the qualifying 5-minute candlestick sequence, not from Renko bricks.
- Freeze Fibonacci levels 0.44, 0.50, and 0.56 as absolute price coordinates.
- After calculation, treat these as fixed horizontal prices. Do not re-anchor or recalculate them when switching chart type.

## Renko timing layer

- NIFTY Traditional Renko research box size: 12.5 points.
- Renko is a timing/confirmation layer only.
- Renko must be evaluated against the already-fixed 0.44/0.50/0.56 Red-Bar price levels.
- Do not derive Red-Bar levels from Renko.
- Do not silently promote the 12.5-point setting from research assumption to proven rule; log forward-test evidence first.

## Decision order

`Definedge live data -> OI/options buildup/chain -> volatility and regime -> 5-minute Red-Bar 0.44/0.50/0.56 fixed levels -> 12.5 Traditional Renko timing -> OAR Rank 1 final structure selection -> compact manual Opstra paper-trade ticket -> post-entry tracking`

OAR does not define market direction by itself. It is the final structure-selection/validation layer after upstream evidence.

## Compact output contract

Every actionable ticket must contain:

- TRADE / WAIT / NO TRADE
- trade type
- option-chain expiry
- payoff date
- every BUY/SELL leg
- quantity / ratio
- Red-Bar/Renko state

Example:

```
NIFTY — TRADE
Type: Bear Put Spread
Expiry: 22-Sep-2026
Payoff date: 18-Sep-2026
BUY: 23300 PE x1
SELL: 23200 PE x1
Red-Bar/Renko: CONFIRMED
Status: ENTER PAPER TRADE
```

## Safety and experiment integrity

- Do not modify the existing 12 AlgoStra forward-test strategies.
- No real-money orders.
- User continues to place Opstra paper positions manually in V1.
- Preserve all accepted and rejected setups for forward-test comparison.
- Store raw inputs and derived states with timestamps; never rewrite a historical decision after seeing the outcome.
- WhatsApp/Telegram delivery remains a separate adapter project and must not be mixed into the decision engine.
