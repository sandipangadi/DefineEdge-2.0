# Pre-OAR NIFTY Forward-Test Engine V1

Status: APPROVED FOR BUILD / SHADOW FORWARD TEST

## Objective

Automate the market-analysis steps that precede Opstra OAR so the user only needs to open OAR when the engine has reached a qualified paper-trade selection stage. Existing AlgoStra strategy slots remain untouched. No real orders are placed.

Target flow:

`Definedge live data -> OI/option-chain state -> volatility state -> P&F/D-SMART/Renko regime -> Red-Bar timing shadow gate -> TRADE/WAIT/NO-TRADE -> OAR instruction -> manual Opstra Paper Portfolio entry -> automated post-entry tracking`

OAR is a final validation/structure-selection layer, not the source of market regime. While OAR ranking is unavailable, upstream signals continue in shadow mode and are recorded as `OAR_UNAVAILABLE`.

## V1 scope

- NIFTY only.
- Advisory / paper-forward-test only.
- Do not modify or consume AlgoStra strategy slots.
- Do not place broker orders.
- Do not browser-automate Opstra in V1.
- Preserve the existing V6 reconstruction engine and frozen strategy baselines.

## Decision checkpoints

Initial research checkpoints (IST):

- 09:35
- 10:15
- 12:30
- 13:30

No new paper-trade selection after 14:00 in V1. These times are PROVISIONAL and must be evaluated rather than assumed optimal.

## Gate 1 — OI / option-chain state

Collect current expiry and next relevant expiry around NIFTY ATM, initially a bounded strike window.

Required raw fields where available:

- underlying LTP
- option LTP
- CE/PE OI
- previous OI / change in OI
- volume
- best bid/ask and quantities
- strike and expiry

Derive reproducibly:

- strike-wise OI concentration
- CE vs PE OI change
- PCR and change in PCR
- price + OI classification: long buildup / short buildup / short covering / long unwinding
- important call resistance / put support strikes
- migration or weakening of those strikes

All numerical thresholds start PROVISIONAL unless already supported by independent evidence.

## Gate 2 — Volatility / movement state

Track:

- ATM CE + PE combined premium
- change in ATM straddle premium from prior checkpoint/open
- realised underlying range
- IV/IVP where a supported source is available
- option premium expansion/contraction

Classify initially as `EXPANDING`, `CONTRACTING`, or `MIXED/UNKNOWN` using explicit versioned rules.

Do not infer IV from price alone if an actual IV field is unavailable; record `IV_UNAVAILABLE` and continue with observable premium/range measures.

## Gate 3 — Price structure / regime

Maintain NIFTY price structure from Definedge live/minute data.

Desired confirmations:

- deterministic P&F state
- bullish/bearish breakout/reversal state
- D-SMART/RL equivalent only after mathematical reconstruction is validated
- Renko state where independently validated

Native P&F/Renko reconstruction must remain PROVISIONAL until regression-tested against DefineEdge chart examples.

Regime output:

- `BULLISH_EXPANSION`
- `BEARISH_EXPANSION`
- `NONDIRECTIONAL_EXPANSION`
- `RANGE_COMPRESSION`
- `CONFLICTING`

## Gate 4 — Permitted strategy family

This gate does not select a specific strike. It restricts what OAR is allowed to validate.

- BULLISH_EXPANSION -> Long Call / Bull Call Spread
- BEARISH_EXPANSION -> Long Put / Bear Put Spread
- NONDIRECTIONAL_EXPANSION -> Long Straddle / Long Strangle
- RANGE_COMPRESSION -> defined-risk range structures may be observed, but no production rule until IV/range evidence is validated
- CONFLICTING -> NO TRADE

A high OAR rank cannot override `NO TRADE`.

## Gate 5 — Dr Devendra Red-Bar timing

Purpose: entry timing only, not regime or strategy selection.

Current status: PROVISIONAL / SHADOW ONLY.

Known research hypothesis to preserve:

- choose/estimate a symbol's typical good-day intraday movement range
- divide by 12 for an intraday Renko box-size hypothesis
- for NIFTY, current research assumption is 150 points / 12 = 12.5 points

This is not yet treated as the proven proprietary Red-Bar formula.

Build requirement:

1. reconstruct candidate Red-Bar/positive-negative trigger levels from available evidence/data;
2. timestamp every reconstructed level;
3. compare it with independently observed/published Red-Bar levels when available;
4. store level error, trigger timing error, direction, and subsequent movement;
5. do not allow Red Bar to block or trigger a paper trade until validation promotes it to ACCEPTED.

Until then output `RED_BAR_SHADOW = WAIT/TRIGGERED/UNAVAILABLE` alongside the normal pre-OAR result.

## State machine

- `NO_TRADE`: hard conflict or insufficient evidence.
- `WATCH`: possible setup but key gates not aligned.
- `ARMED`: regime + permitted strategy family established; timing confirmation pending.
- `TRIGGERED`: all currently accepted gates pass. If Red Bar remains provisional, report both the accepted-gate state and shadow Red-Bar state separately.
- `OAR_UNAVAILABLE`: qualified upstream state but OAR cannot currently be checked.
- `CHECK_OAR`: qualified upstream state and user should manually open Opstra OAR.

## OAR handoff

When available, produce a compact instruction containing:

- timestamp
- NIFTY level
- regime
- evidence by gate
- permitted strategy family
- OAR Risk Bias: Risk Defined
- OAR Directional Bias: Bullish / Bearish / None as dictated by the independently established regime
- rejected strategy families

User manually sends/records OAR ranking and places the chosen Opstra Paper Portfolio trade.

Do not silently substitute an internally invented OAR ranking while the Opstra feature is unavailable.

## Post-entry tracking

Once a paper entry is registered, automatically track where data permits:

- underlying entry and subsequent high/low
- each option entry premium and live premium
- combined strategy value
- MFE / MAE
- time to MFE / MAE
- peak-to-current and peak-to-exit give-back
- OI evolution
- ATM straddle evolution
- IV/Greeks where supported
- end-of-day and next-session values where relevant
- exit/final observation P&L

Record whether the original regime thesis occurred separately from whether the option structure made money.

## Comparison experiments

Preserve both accepted and rejected opportunities. For rejected setups, where practical, record what subsequent movement occurred so filters can be evaluated for losses avoided AND opportunity lost.

Existing historical/paper observations (e.g. profitable and losing OAR straddle/strangle cases) may seed descriptive research but must not be used to optimize V1 thresholds retrospectively.

## Engineering integration

Reuse the existing production direction documented in `LIVE_TELEGRAM_TRADING_BRAIN.md`:

`Definedge live API/WebSocket -> Render decision engine -> advisory output`

Implement Pre-OAR as isolated modules. Do not alter the frozen V5 reconstruction engine.

Suggested module boundaries:

- `live_feed.py` — session/WebSocket/token snapshots
- `option_chain_state.py` — OI/PCR/build-up/strike state
- `volatility_state.py` — ATM straddle/range/IV inputs
- `market_structure.py` — P&F/Renko/D-SMART research states
- `red_bar_shadow.py` — Red-Bar reconstruction/validation only
- `pre_oar_engine.py` — state machine and OAR handoff
- `paper_tracker.py` — registered Opstra paper-position tracking

## Audit record

Every checkpoint must save raw inputs plus derived output and rule version. Minimum fields:

`timestamp_ist, engine_version, underlying, spot, expiry, atm, raw_chain_ref, oi_state, volatility_state, pnf_state, renko_state, dsmart_state, red_bar_shadow_state, regime, allowed_strategy_family, decision_state, rejection_reasons, oar_status`

Never overwrite historical decisions after seeing the outcome. Corrections create a new version/record.

## Promotion rules

Follow the repository evidence governance:

- PROVEN / ACCEPTED
- PROVISIONAL / TO BE VALIDATED
- REJECTED / NULLIFIED
- TWEAK REQUIRED

Red-Bar reconstruction, checkpoint timings, volatility thresholds, OI thresholds and regime combinations begin PROVISIONAL unless separately supported by adequate evidence.

## Immediate build sequence

1. Complete the existing Definedge live-feed proof for NIFTY + bounded option strikes.
2. Persist timestamped LTP/OI/volume/bid/ask snapshots.
3. Implement deterministic OI/PCR/build-up calculations with no trade decisions yet.
4. Implement ATM straddle/range state.
5. Add P&F reconstruction and regression validation.
6. Add Red-Bar shadow module and comparison log.
7. Run the Pre-OAR state machine in shadow mode.
8. When OAR is restored, enable `CHECK_OAR` handoff; user retains manual Paper Portfolio placement.
9. Register paper positions for automated observation/tracking.

No real-money execution is part of V1.