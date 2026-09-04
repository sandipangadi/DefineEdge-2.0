# Definedge Momentum Tick Collector V2

This is the recommended version for your momentum trading project.

## Why V2 is safer and more useful

V1 could resolve exact token/tradingsymbol, but if the CSV only had components it could still face ambiguous contracts.

V2:
- NEVER silently chooses an ambiguous contract.
- Supports common AlgoStra columns such as `Entry Time 1`, `Exit Time 1`, `Entry Time 2`, `Exit Time 2`.
- Pulls today's Definedge broker `/trades` data and can reconcile an AlgoStra row to the actual NFO token using underlying + fill time.
- Saves broker orders, trades, positions and limits in the ZIP.
- Downloads exact option tick data.
- Downloads 1-minute option data as backup.
- Downloads underlying tick and 1-minute data.
- Builds entry-time ATM-1 / ATM / ATM+1 for BOTH CE and PE.
- Creates `analysis/trade_metrics.csv` with MFE, MAE, entry/exit tick, peak give-back and post-exit extremes.

## Daily flow

1. Open Render URL.
2. Enter Collector Password (if configured).
3. Click Send Definedge OTP.
4. Enter the OTP.
5. Upload your day's AlgoStra / momentum CSV.
6. Click Collect & Download Analysis ZIP.
7. Upload that ZIP to the momentum trading project in ChatGPT.

## Render environment variables

Required:
- `DEFINEDGE_API_TOKEN`
- `DEFINEDGE_API_SECRET`

Recommended:
- `DEFINEDGE_CLIENT_SECRET`
  - If omitted, the code falls back to `DEFINEDGE_API_SECRET`.
- `COLLECTOR_PASSWORD`
  - Strongly recommended because a Render URL is public and otherwise anyone who finds it could trigger Definedge OTPs.

## Important limitation

Definedge documents tick history as available for the last **2 trading sessions**. Intraday minute history is documented as available for the last **6 months**.

If a trade is virtual and the AlgoStra CSV does not contain an exact token/tradingsymbol/strike-expiry-CE/PE combination, and there is no matching broker execution, V2 deliberately leaves it unresolved rather than guessing the wrong option contract.

## Read-only

No place-order, modify-order or cancel-order endpoint is present in this project.
