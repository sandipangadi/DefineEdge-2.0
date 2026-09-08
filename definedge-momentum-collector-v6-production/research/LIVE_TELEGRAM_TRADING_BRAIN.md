# Live Telegram Trading Brain — Architecture and Validation Plan

Status: design approved for research; live execution remains advisory-only until forward validation.

## Objective

Build a cloud-only Trading Brain that can operate without ChatGPT in the live decision loop.

The system has three distinct input modes:

1. Historical evidence input — daily V6 evidence ZIPs generated from AlgoStra logs plus Definedge reconstruction.
2. Live market input — Definedge Integrate API/WebSocket feed.
3. Strategy configuration input — frozen P&F/option/risk rules plus only those thresholds promoted by evidence.

The historical ZIP is not the live trigger. It is the evidence base used to measure which setups historically produced acceptable MFE, MAE, give-back, post-exit behavior, liquidity and strategy outcomes.

## Live data source confirmed from official Definedge documentation

Definedge Integrate supports live market data through WebSocket and historical/tick retrieval through the data API.

Relevant live WebSocket capabilities:

- WebSocket URL: `wss://trade.definedgesecurities.com/NorenWSTRTP/`
- One connection at a time.
- Up to 500 tokens per connection.
- Heartbeat required every 50 seconds.
- Touchline subscription provides LTP, % change, volume, OHLC, average price, OI, previous OI, total OI, best bid/ask price and quantity.
- Depth feed provides LTP, volume, OHLC, last trade time/quantity, total buy/sell quantity and five levels of bid/ask depth.
- Authentication returns both `api_session_key` for REST and `susertoken` for WebSocket.

Relevant historical API capability:

- Tick history is available for the last two trading sessions.
- Tick records contain UTC timestamp, LTP, LTQ and OI for derivatives.
- Intraday minute history is available for the last six months.

Conclusion: a native live signal engine is feasible without waiting for AlgoStra alerts, provided we reconstruct P&F state and option-chain context ourselves from the live stream.

## Proposed live flow

`Definedge OTP -> session_key + susertoken -> WebSocket -> token subscriptions -> market-state engine -> P&F/regime/options qualifiers -> evidence score -> Telegram`

Telegram outputs are restricted initially to:

- `ENTRY CANDIDATE`
- `REJECT`
- `HOLD`
- `TIGHTEN`
- `EXIT`
- `NO TRADE`

No automatic broker order placement is enabled in the first production stage.

## Signal pipeline

### 1. Universe allocator

Start with a deliberately bounded liquid universe rather than all F&O symbols.

Initial target:

- current LIQ20/LIQ50 research universe
- active index contracts where separately enabled
- current expiry and next relevant expiry only
- ATM, ATM-1 and ATM+1 CE/PE around qualified underlyings

This stays comfortably below the 500-token WebSocket connection limit.

### 2. Underlying state engine

For each underlying maintain:

- live LTP/ticks
- P&F box/column state
- current breakout/reversal state
- D-SMART/trend proxy where reproducible mathematically
- regime state
- time-of-day filter
- momentum persistence
- volatility/range state

The P&F implementation must be deterministic and separately regression-tested against known DefineEdge chart examples before it can qualify trades.

### 3. Option state engine

For each candidate maintain:

- exact traded option LTP
- ATM-1 / ATM / ATM+1 CE and PE
- OI and OI change where available
- volume
- bid/ask and spread
- best depth where required
- option momentum from trigger
- premium overextension
- liquidity rejection conditions

### 4. Historical evidence layer

Daily V6 ZIPs feed an evidence database containing:

- strategy identity
- entry/exit timestamp
- entry/exit premium
- MFE
- MAE
- time to MFE
- peak-to-exit give-back
- post-exit movement
- underlying movement
- option-chain context
- duplicate-trade family
- day/time/expiry/regime labels when derivable

Evidence from duplicated variants must not be counted as independent observations.

## Validation gates

Every theory, rule and threshold is stored with one of these states:

### PROVEN / ACCEPTED

Promoted only after sufficient independent observations and forward evidence show robust improvement without unacceptable loss of opportunity.

### PROVISIONAL / TO BE VALIDATED

Plausible rule with insufficient independent evidence. It may be logged and shadow-scored, but it cannot block or trigger a live trade by itself.

### REJECTED / NULLIFIED

Evidence contradicts the rule, or the rule produces no robust improvement after costs/robustness checks.

### TWEAK REQUIRED

Signal has value but needs modification. The original version remains frozen in history; the revised version receives a new version ID and is independently validated.

## Anti-curve-fitting rules

- Do not optimize on one day or one strategy variant.
- Deduplicate same underlying/time signals across strategy variants before measuring evidence strength.
- Keep frozen baselines.
- Evaluate out-of-sample and forward performance before promotion.
- Record both opportunity lost and losses avoided by every qualifier.
- A filter is not automatically good because it improves win rate; expectancy, drawdown, trade frequency and tail-loss behavior must also improve.
- Time-window, SL, TSL and option-selection parameters are treated as hypotheses until validated across enough independent trades.

## Initial decision model

The live engine will use explicit gates first, not an LLM.

A candidate can become `ENTRY CANDIDATE` only if:

1. P&F trigger is valid.
2. directional/regime gate passes.
3. allowed trading window passes.
4. liquidity gate passes.
5. option selection gate passes.
6. no hard risk rejection is active.
7. evidence score reaches the current production threshold.

If any hard gate fails, Telegram sends `REJECT` with the exact failed gate.

## Exit management model

The bot must manage the option after entry, not only the underlying.

Track in real time:

- live MFE from entry
- live MAE
- peak premium
- current give-back from peak
- P&F continuation/reversal state
- option liquidity deterioration
- time/expiry risk

Telegram actions:

- `HOLD` while momentum remains intact.
- `TIGHTEN` when profit protection conditions activate.
- `EXIT` when validated invalidation/give-back/reversal criteria are met.

The current historical values from any one V6 ZIP are descriptive only; they are not automatically production thresholds.

## Cloud-only architecture

Target production stack:

`Definedge -> Render live engine -> Telegram`

Evidence archive:

`V6 evidence ZIP -> private GitHub evidence repository -> evidence database/build artifacts`

Google Drive can remain input-only where useful. Google production OAuth/domain verification is not required for the live Trading Brain design.

ChatGPT is optional for research review, model criticism and documentation; it is not required to run live signals.

## Build order

### Phase 1 — Live-feed proof

- authenticate Definedge and retain `susertoken`
- connect WebSocket
- maintain heartbeat
- subscribe to a small fixed token set
- log LTP/OI/volume/bid/ask stream
- prove reconnection and session-expiry handling

### Phase 2 — Native P&F state

- implement deterministic P&F column/box engine
- compare against known DefineEdge chart states
- freeze validated implementation

### Phase 3 — Shadow signals

- run ENTRY/REJECT/HOLD/TIGHTEN/EXIT decisions in shadow mode
- send Telegram messages
- no broker orders
- compare every shadow decision against AlgoStra/V6 post-trade evidence

### Phase 4 — Evidence promotion

- accumulate independent observations
- promote only robust gates from PROVISIONAL to ACCEPTED
- nullify or version rules that fail

### Phase 5 — Optional execution

Automatic order placement is considered only after sufficient forward validation and a separate explicit risk-control review.

## Immediate engineering task

Build the Phase-1 WebSocket collector as an isolated module without changing the frozen V5 reconstruction engine. The first proof should subscribe only to a small test set and write structured live snapshots suitable for the future Telegram decision engine.
