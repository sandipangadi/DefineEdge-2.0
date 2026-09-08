# Definedge Momentum Collector V6 Production

This folder is the single production control layer for the Definedge Momentum evidence pipeline.

## Architecture

- `app_v6.py` owns every user-visible Flask route: home, OTP, collect, status and health.
- The proven V5 `app.py` is loaded only as a frozen reconstruction engine for parsing, Definedge market-data retrieval, MFE/MAE, chain reconstruction, duplicate mapping and ZIP construction.
- `drive_bridge.py` owns Google Drive inbox reading and legacy evidence/manifest publishing logic.
- `templates/index_v6.html` and `templates/job_v6.html` are the only production UI templates.
- `requirements.txt` and `render.yaml` are the authoritative production deployment files.

## Important

The old V2/V3/V5 folders are historical/reference code and are not production entrypoints. Do not point Render to `app:app` from the V5 folder.

The production start command is:

`gunicorn --workers 1 --threads 4 --timeout 300 app_v6:app --bind 0.0.0.0:$PORT`

The intended Render root directory is:

`definedge-momentum-collector-v6-production`

## V6 evidence flow

`Drive AlgoStra Inbox -> Definedge OTP -> frozen V5 reconstruction engine -> option/underlying/ATM chain ticks -> MFE/MAE/give-back/duplicates/strategy comparison -> evidence ZIP + manifest`

V6 is now treated primarily as the historical evidence/reconstruction layer. Google Drive write OAuth is not a dependency for the future live Trading Brain.

## Live Trading Brain direction

The approved next architecture is cloud-only and does not require ChatGPT in the live decision loop:

`Definedge live API/WebSocket -> Render decision engine -> Telegram`

Historical V6 evidence ZIPs feed the evidence database used to validate or reject live qualifiers. They do not themselves generate the live trigger.

The live engine will combine three input modes:

1. Historical evidence from V6 ZIPs.
2. Live Definedge market data.
3. Frozen strategy/risk configuration plus only evidence-approved thresholds.

Initial live outputs are advisory only:

- ENTRY CANDIDATE
- REJECT
- HOLD
- TIGHTEN
- EXIT
- NO TRADE

Automatic order execution is explicitly deferred until separate forward validation and risk review.

## Evidence governance

Every theory/rule/threshold must be classified as one of:

- PROVEN / ACCEPTED
- PROVISIONAL / TO BE VALIDATED
- REJECTED / NULLIFIED
- TWEAK REQUIRED

Frozen baselines are preserved. Duplicate strategy variants are not counted as independent evidence. Rules are not promoted from one-day results or because they improve win rate alone; expectancy, drawdown, frequency, tail behavior and opportunity loss must also be considered.

## Official Definedge live-feed feasibility

Definedge Integrate documentation confirms the required building blocks for a native live engine:

- WebSocket live market feed.
- `api_session_key` for REST and `susertoken` for WebSocket after authentication.
- One WebSocket connection with up to 500 subscribed tokens.
- Heartbeat every 50 seconds.
- Touchline fields including LTP, volume, OI and best bid/ask.
- Depth feed including five bid/ask levels, LTQ and total buy/sell quantities.
- Historical tick data for the last two trading sessions and minute history for six months.

This supports a native live P&F/option-state engine without relying on AlgoStra as the only trigger source, subject to validating our own P&F reconstruction against DefineEdge chart behavior.

## Research specification

See:

`research/LIVE_TELEGRAM_TRADING_BRAIN.md`

That document is the current authoritative design and validation roadmap for the live Telegram Trading Brain.
