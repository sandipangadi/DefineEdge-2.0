# Indian Indices Trading — Work Handoff

Updated: 18 Sep 2026

## Purpose
Build a screenshot-free, advisory/paper forward-test engine for NIFTY using the DefineEdge ecosystem and Opstra OAR. Existing 12 AlgoStra strategies remain untouched.

## Frozen decision flow
Definedge live data → OI / option-chain / IV / volatility regime → Dr Devendra Red-Bar calculation from the correct 5-minute candle → freeze 0.44 / 0.50 / 0.56 as absolute price levels → evaluate those fixed levels using 12.5-point Traditional Renko → P&F / price-structure confirmation → OAR as final structure-selection layer → compact trade ticket → user manually places Opstra paper trade → automated post-entry tracking.

## Red-Bar / Renko rule
- Red-Bar levels are derived from candle data, not from Renko.
- Fib levels frozen for testing: 0.44, 0.50, 0.56.
- After levels are converted to fixed horizontal prices, Renko is used for timing/reversal confirmation.
- Do not recalculate Fib from Renko bricks.
- NIFTY Renko research setting: Traditional 12.5 points, based on 150-point good-day range / 12.
- This remains provisional until forward evidence promotes it.

## OAR role
- OAR is not the first signal and must not define market regime.
- OAR is the final structure-selection/validation layer.
- If unavailable, record OAR_UNAVAILABLE and keep upstream signal in shadow mode.
- Never invent a replacement OAR ranking.
- Payoff Date is mandatory in every trade ticket.
- OAR Rank 1 may be rejected by upstream gates.

## Output format
NIFTY — TRADE / WAIT / NO TRADE
Type:
Expiry / Chain:
Payoff Date:
BUY:
SELL:
Quantity:
Timing/confirmation:
Status:

## Current engineering status
Ready:
- Pre-OAR V1 specification.
- Red-Bar 0.44/0.50/0.56 workflow.
- 12.5 Traditional Renko research rule.
- Pre-OAR state-machine design.
- Render service exists and deploys.
- GitHub research documentation exists.

Still incomplete:
- Correct V6 production startup path on Render.
- Persistent Definedge OTP/session wiring for live collector.
- WebSocket live NIFTY feed proof.
- Automatic bounded option-strike/OI/volume/bid-ask snapshots.
- End-to-end feed from live snapshot into Pre-OAR.
- Screenshot-free live TRADE/WAIT/NO TRADE proof.

Readiness definition: do not call READY until one timestamped live NIFTY decision is produced end-to-end from Definedge data without manual screenshots.

## Definedge live data target
OTP/session → api_session_key + susertoken → WebSocket → heartbeat → NIFTY + bounded option subscriptions → LTP, OI/previous OI, volume, bid/ask → snapshots → Pre-OAR engine.

## Render / GitHub
- GitHub: sandipangadi/DefineEdge-2.0
- Production folder: definedge-momentum-collector-v6-production
- Key docs:
  - research/PRE_OAR_FORWARD_TEST_V1.md
  - research/LIVE_TELEGRAM_TRADING_BRAIN.md
- Render service: definedge-momentum-batch-collector-v6
- Service ID: srv-dafe27e1egvs739p74ng
- Workspace: My Workspace
- Workspace ID: tea-dacr6itsb3ts73aqagq0
- Known issue: Render has historically started via the older nested V5 path; production-path alignment must be verified.

## Paper forward-test principles
- Advisory/paper only; no real orders.
- Existing AlgoStra strategies remain untouched.
- Preserve accepted and rejected setups.
- Track MFE, MAE, give-back, OI evolution, option premium behaviour and whether the regime thesis occurred separately from final P&L.
- Never overwrite historical decisions after seeing outcomes.
- States: PROVEN/ACCEPTED, PROVISIONAL/TO BE VALIDATED, REJECTED/NULLIFIED, TWEAK REQUIRED.
- No new V1 paper entry after 14:00 until the timing rule is revalidated.

## Key learning from 17 Sep 2026
- OI/chain could look directional while the market remained contested.
- 12.5 Renko exposed a tradable CALL leg followed by a later downward reversal more clearly than the earlier wide Red-Bar interpretation.
- Working hypothesis: Red-Bar provides fixed reference levels; Renko helps evaluate intraday momentum/reversal around those levels.
- OAR #47 Long Strangle was rejected at/after 14:00 under V1 despite being an OAR candidate.

## User action when live auth is ready
User should only need to provide/enter one fresh Definedge OTP when requested. Do not request OTP before the live collector is actually ready to consume it.

## Separate alert project
WhatsApp preferred, Telegram fallback. Keep alert transport separate from Pre-OAR decision logic. Do not block engine completion on alerts.

## Evidence to keep accessible
- Friend's Dr Devendra training notes.
- Red-Bar/Fib screenshots proving 0.44 / 0.50 / 0.56 workflow.
- 12.5 Traditional Renko screenshots.
- OAR screenshots/PDFs including Rank 1 examples and OAR #47.
- Options Algorithm.pdf.
- AlgoStra virtual-trade screenshots used for comparison.

## Next engineering action
1. Correct/verify V6 Render startup path.
2. Wire existing Definedge OTP auth into persistent live collector.
3. Retain session key + susertoken.
4. Subscribe to NIFTY + bounded option strikes.
5. Persist live snapshots.
6. Feed snapshots into Pre-OAR state machine.
7. Prove a screenshot-free live decision.
8. Only then proceed to alerts/refinements.
